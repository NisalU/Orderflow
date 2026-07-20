"""
Server — aiohttp + WebSocket dashboard for the Order Flow + Footprint Bot.

REST API:
  GET  /api/config           → current configuration
  GET  /api/state            → full analysis snapshot
  GET  /api/footprint        → footprint candles
  GET  /api/delta            → delta series
  GET  /api/sr               → S/R levels
  GET  /api/ema              → EMA overlay series
  GET  /api/candles          → raw OHLCV for chart
  GET  /api/positions        → open positions
  GET  /api/trades           → closed trades
  GET  /api/stats            → trade statistics
  GET  /api/risk-status      → daily risk tracker
  GET  /api/scanner          → hot coins
  POST /api/scanner/scan     → trigger manual scan
  POST /api/backtest         → start backtest (SSE stream)
  GET  /api/signals          → recent signal log
  GET  /api/keys/status      → API key status (masked)
  POST /api/keys             → save API key + secret
  GET  /api/settings         → get trading mode + params
  POST /api/settings         → update trading mode + params

WebSocket:
  /ws  → subscribe, ping, scan_now, set_exchange
         server pushes: snapshot, footprint_update, delta_update,
                        risk_status, tick, exchange_changed, pong,
                        scanner_update, scanner_scanning, config, keys_status
"""
import asyncio
import contextlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

from aiohttp import WSMsgType, web

BASE_DIR = Path(__file__).parent

import config
import data_feed
import multi_timeframe as mtf_mod
import signal_generator
import support_resistance as sr
import trend_detector as td
from backtester import run_backtest
from footprint_engine import footprint_engine
from risk_manager import daily_risk
from scanner import scanner
from stream import BinanceTradeStream, Client, manager
from trade_logger import trade_logger
from trade_manager import trade_manager


# ─────────────────────────────────────────────────────────────────────────────
# API Key persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_keys():
    """Load API keys from keys.json into config, if file exists."""
    kf = Path(config.KEYS_FILE)
    if not kf.exists():
        return
    try:
        data = json.loads(kf.read_text())
        if data.get("api_key"):
            config.BINANCE_API_KEY = data["api_key"]
        if data.get("api_secret"):
            config.BINANCE_API_SECRET = data["api_secret"]
        if data.get("trading_mode"):
            config.TRADING_MODE = data["trading_mode"]
        print(f"[server] Loaded API keys from {kf}")
    except Exception as exc:
        print(f"[server] Failed to load keys.json: {exc}")


def save_keys(api_key: str, api_secret: str, trading_mode: str = None):
    """Persist API keys to keys.json."""
    kf = Path(config.KEYS_FILE)
    existing = {}
    if kf.exists():
        try:
            existing = json.loads(kf.read_text())
        except Exception:
            pass
    existing["api_key"]    = api_key
    existing["api_secret"] = api_secret
    if trading_mode:
        existing["trading_mode"] = trading_mode
    kf.write_text(json.dumps(existing, indent=2))


def keys_configured() -> bool:
    return bool(config.BINANCE_API_KEY and config.BINANCE_API_SECRET)


def keys_status_payload() -> dict:
    has_key    = bool(config.BINANCE_API_KEY)
    has_secret = bool(config.BINANCE_API_SECRET)
    key_masked = (config.BINANCE_API_KEY[:4] + "…" + config.BINANCE_API_KEY[-4:]
                  if len(config.BINANCE_API_KEY) > 8 else ("***" if has_key else ""))
    return {
        "has_key":      has_key,
        "has_secret":   has_secret,
        "key_masked":   key_masked,
        "configured":   keys_configured(),
        "trading_mode": config.TRADING_MODE,
    }


# ─────────────────────────────────────────────────────────────────────────────
# State cache
# ─────────────────────────────────────────────────────────────────────────────

_state_cache: dict = {}
_state_lock = asyncio.Lock()
_CACHE_TTL  = 8    # seconds

# MTF cache — updated by background loop (one entry per symbol, refreshes every ~60s)
_mtf_cache: dict = {}
_mtf_lock  = asyncio.Lock()
_MTF_TTL   = 60   # seconds — daily/4h data changes slowly


async def _get_state(symbol: str, interval: str) -> dict:
    key = f"{symbol}:{interval}"
    now = time.time()
    async with _state_lock:
        cached = _state_cache.get(key)
        if cached and now - cached["_ts"] < _CACHE_TTL:
            return cached

    candles = await asyncio.to_thread(data_feed.get_klines, symbol, interval)

    ticker = None
    try:
        ticker = await asyncio.to_thread(data_feed.get_ticker, symbol)
    except Exception:
        pass

    trend_data  = td.detect_trend(candles)
    sr_data     = sr.detect_levels(candles)
    fp_history  = footprint_engine.get_history(symbol, interval, limit=100)
    ema_overlay = td.get_ema_overlay(candles)

    # signal is built later (after MTF cache pull)

    # Pull MTF data from cache (refreshed separately by background loop)
    async with _mtf_lock:
        mtf_cached = _mtf_cache.get(symbol)
    mtf_data = mtf_cached.get("data") if mtf_cached else None

    # Re-evaluate signal with MTF data attached
    signal = signal_generator.evaluate(symbol, interval, candles, ticker, mtf_data=mtf_data)

    state = {
        "_ts":       now,
        "symbol":    symbol,
        "interval":  interval,
        "price":     candles[-1]["close"] if candles else 0,
        "ticker":    ticker,
        "trend":     trend_data,
        "sr":        sr_data,
        "signal":    signal,
        "footprint_history": fp_history,
        "ema":       ema_overlay,
        "mtf":       mtf_data or {},
        "candles":   [
            {"time": c["time"], "open": c["open"], "high": c["high"],
             "low": c["low"],  "close": c["close"], "volume": c["volume"]}
            for c in candles
        ],
    }
    async with _state_lock:
        _state_cache[key] = state
    return state


def _invalidate_cache(symbol: str, interval: str):
    _state_cache.pop(f"{symbol}:{interval}", None)


# ─────────────────────────────────────────────────────────────────────────────
# Static files
# ─────────────────────────────────────────────────────────────────────────────

async def index(_req: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "static" / "index.html")


# ─────────────────────────────────────────────────────────────────────────────
# REST Handlers
# ─────────────────────────────────────────────────────────────────────────────

async def api_config(_req: web.Request) -> web.Response:
    return web.json_response({
        "symbols":            config.SYMBOLS,
        "intervals":          config.INTERVALS,
        "default_symbol":     config.DEFAULT_SYMBOL,
        "default_interval":   config.DEFAULT_INTERVAL,
        "exchange":           config.ACTIVE_EXCHANGE,
        "ema_fast":           config.EMA_FAST_PERIOD,
        "ema_slow":           config.EMA_SLOW_PERIOD,
        "delta_threshold":    config.DELTA_THRESHOLD,
        "imbalance_ratio":    config.IMBALANCE_RATIO,
        "min_stacked":        config.MIN_STACKED_IMBALANCES,
        "risk_pct":           config.RISK_PER_TRADE_PCT,
        "tp_ratio":           config.TP_RATIO,
        "max_trades_day":     config.MAX_TRADES_PER_DAY,
        "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
        "sessions":           config.TRADE_SESSIONS,
        "leverage":           config.DEFAULT_LEVERAGE,
        "trading_mode":       config.TRADING_MODE,
        "keys_configured":    keys_configured(),
    })


async def api_keys_status(_req: web.Request) -> web.Response:
    return web.json_response(keys_status_payload())


async def api_keys(req: web.Request) -> web.Response:
    """POST /api/keys  {api_key, api_secret, trading_mode?}"""
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    api_key    = str(body.get("api_key",    "")).strip()
    api_secret = str(body.get("api_secret", "")).strip()
    mode       = str(body.get("trading_mode", config.TRADING_MODE)).strip()

    if not api_key or not api_secret:
        return web.json_response({"error": "api_key and api_secret required"}, status=400)
    if mode not in ("signal_only", "live"):
        mode = "signal_only"

    config.BINANCE_API_KEY    = api_key
    config.BINANCE_API_SECRET = api_secret
    config.TRADING_MODE       = mode

    # Refresh data_feed sessions so new key is picked up immediately
    data_feed._tls.__dict__.clear()

    try:
        save_keys(api_key, api_secret, mode)
    except Exception as exc:
        return web.json_response({"error": f"Could not save keys: {exc}"}, status=500)

    payload = keys_status_payload()
    _ws_broadcast({"type": "keys_status", "data": payload})
    return web.json_response({"ok": True, **payload})


async def api_settings_get(_req: web.Request) -> web.Response:
    return web.json_response({
        "trading_mode":        config.TRADING_MODE,
        "order_type":          config.ORDER_TYPE,
        "auto_sl_tp":          config.AUTO_SL_TP,
        "risk_pct":            config.RISK_PER_TRADE_PCT,
        "tp_ratio":            config.TP_RATIO,
        "max_daily_loss_pct":  config.MAX_DAILY_LOSS_PCT,
        "max_trades_day":      config.MAX_TRADES_PER_DAY,
        "delta_threshold":     config.DELTA_THRESHOLD,
        "min_stacked":         config.MIN_STACKED_IMBALANCES,
        "sessions":            config.TRADE_SESSIONS,
        "keys_configured":     keys_configured(),
        "trading_mode":        config.TRADING_MODE,
    })


async def api_settings_post(req: web.Request) -> web.Response:
    """POST /api/settings — update trading mode and risk parameters at runtime."""
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    if "trading_mode" in body:
        mode = body["trading_mode"]
        if mode in ("signal_only", "live"):
            config.TRADING_MODE = mode
    if "order_type" in body:
        ot = body["order_type"]
        if ot in ("MARKET", "LIMIT"):
            config.ORDER_TYPE = ot
    if "auto_sl_tp" in body:
        config.AUTO_SL_TP = bool(body["auto_sl_tp"])
    if "risk_pct" in body:
        v = float(body["risk_pct"])
        if 0.1 <= v <= 10:
            config.RISK_PER_TRADE_PCT = v
    if "tp_ratio" in body:
        v = float(body["tp_ratio"])
        if 0.5 <= v <= 10:
            config.TP_RATIO = v
    if "max_daily_loss_pct" in body:
        v = float(body["max_daily_loss_pct"])
        if 0.5 <= v <= 20:
            config.MAX_DAILY_LOSS_PCT = v
    if "max_trades_day" in body:
        v = int(body["max_trades_day"])
        if 1 <= v <= 50:
            config.MAX_TRADES_PER_DAY = v
    if "delta_threshold" in body:
        v = float(body["delta_threshold"])
        if v >= 0:
            config.DELTA_THRESHOLD = v
    if "min_stacked" in body:
        v = int(body["min_stacked"])
        if 1 <= v <= 10:
            config.MIN_STACKED_IMBALANCES = v
    if "sessions" in body:
        sess = body["sessions"]
        if isinstance(sess, list):
            valid = [s for s in sess if s in ("london", "new_york", "asia")]
            config.TRADE_SESSIONS = valid

    # Persist to keys.json
    try:
        save_keys(config.BINANCE_API_KEY, config.BINANCE_API_SECRET, config.TRADING_MODE)
    except Exception:
        pass

    # Broadcast updated config so all clients reflect the change immediately
    _ws_broadcast({
        "type":         "settings_updated",
        "trading_mode": config.TRADING_MODE,
        "order_type":   config.ORDER_TYPE,
        "auto_sl_tp":   config.AUTO_SL_TP,
        "risk_pct":     config.RISK_PER_TRADE_PCT,
        "tp_ratio":     config.TP_RATIO,
        "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
        "max_trades_day":     config.MAX_TRADES_PER_DAY,
        "delta_threshold":    config.DELTA_THRESHOLD,
        "min_stacked":        config.MIN_STACKED_IMBALANCES,
    })
    return web.json_response({"ok": True})


async def api_state(req: web.Request) -> web.Response:
    symbol   = req.query.get("symbol",   config.DEFAULT_SYMBOL)
    interval = req.query.get("interval", config.DEFAULT_INTERVAL)
    if symbol not in config.SYMBOLS:
        return web.json_response({"error": "invalid symbol"}, status=400)
    try:
        state = await _get_state(symbol, interval)
        return web.json_response(state)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)


async def api_candles(req: web.Request) -> web.Response:
    symbol   = req.query.get("symbol",   config.DEFAULT_SYMBOL)
    interval = req.query.get("interval", config.DEFAULT_INTERVAL)
    limit    = int(req.query.get("limit", 300))
    try:
        candles = await asyncio.to_thread(data_feed.get_klines, symbol, interval, limit)
        return web.json_response({"candles": candles})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)


async def api_footprint(req: web.Request) -> web.Response:
    symbol   = req.query.get("symbol",   config.DEFAULT_SYMBOL)
    interval = req.query.get("interval", config.DEFAULT_INTERVAL)
    limit    = int(req.query.get("limit", 50))
    data     = footprint_engine.get_history(symbol, interval, limit)
    return web.json_response({"footprint": data})


async def api_delta(req: web.Request) -> web.Response:
    symbol   = req.query.get("symbol",   config.DEFAULT_SYMBOL)
    interval = req.query.get("interval", config.DEFAULT_INTERVAL)
    limit    = int(req.query.get("limit", 100))
    data     = footprint_engine.get_delta_series(symbol, interval, limit)
    return web.json_response({"delta": data})


async def api_sr(req: web.Request) -> web.Response:
    symbol   = req.query.get("symbol",   config.DEFAULT_SYMBOL)
    interval = req.query.get("interval", config.DEFAULT_INTERVAL)
    try:
        candles  = await asyncio.to_thread(data_feed.get_klines, symbol, interval)
        sr_data  = sr.detect_levels(candles)
        return web.json_response(sr_data)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)


async def api_ema(req: web.Request) -> web.Response:
    symbol   = req.query.get("symbol",   config.DEFAULT_SYMBOL)
    interval = req.query.get("interval", config.DEFAULT_INTERVAL)
    try:
        candles = await asyncio.to_thread(data_feed.get_klines, symbol, interval)
        ema     = td.get_ema_overlay(candles)
        return web.json_response(ema)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)


async def api_positions(_req: web.Request) -> web.Response:
    return web.json_response({"positions": trade_manager.get_open_positions()})


async def api_trades(req: web.Request) -> web.Response:
    symbol = req.query.get("symbol")
    limit  = int(req.query.get("limit", 50))
    return web.json_response({"trades": trade_logger.get_trades(limit=limit, symbol=symbol)})


async def api_stats(_req: web.Request) -> web.Response:
    return web.json_response({
        "manager": trade_manager.get_stats(),
        "logger":  trade_logger.get_summary(),
        "daily":   daily_risk.status(),
    })


async def api_risk_status(_req: web.Request) -> web.Response:
    return web.json_response(daily_risk.status())


async def api_signals(req: web.Request) -> web.Response:
    symbol = req.query.get("symbol")
    limit  = int(req.query.get("limit", 100))
    return web.json_response({"signals": trade_logger.get_signals(limit=limit, symbol=symbol)})


async def api_mtf(req: web.Request) -> web.Response:
    """GET /api/mtf?symbol=BTCUSDT — Multi-timeframe analysis snapshot."""
    symbol = req.query.get("symbol", config.DEFAULT_SYMBOL)
    async with _mtf_lock:
        cached = _mtf_cache.get(symbol)
    if cached and time.time() - cached.get("_ts", 0) < _MTF_TTL:
        return web.json_response(cached["data"])
    try:
        data = await asyncio.to_thread(mtf_mod.run_mtf_analysis, symbol)
        async with _mtf_lock:
            _mtf_cache[symbol] = {"data": data, "_ts": time.time()}
        return web.json_response(data)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)


async def api_scanner_get(_req: web.Request) -> web.Response:
    return web.json_response({
        "coins":     scanner.get_hot_coins(),
        "last_scan": scanner.get_last_scan(),
        "scanning":  scanner.is_scanning(),
        "count":     len(scanner.get_hot_coins()),
    })


async def api_scanner_trigger(_req: web.Request) -> web.Response:
    if scanner.is_scanning():
        return web.json_response({"ok": False, "already_scanning": True})
    return web.json_response({"ok": scanner.trigger_scan()})


async def api_exchange(req: web.Request) -> web.Response:
    if req.method == "GET":
        return web.json_response({"exchange": config.ACTIVE_EXCHANGE})
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    ex = body.get("exchange", "spot")
    if ex not in ("spot", "futures"):
        return web.json_response({"error": "use spot or futures"}, status=400)
    config.ACTIVE_EXCHANGE = ex
    async with _state_lock:
        _state_cache.clear()
    _ws_broadcast({"type": "exchange_changed", "exchange": ex})
    if scanner:
        scanner.trigger_scan()
    return web.json_response({"ok": True, "exchange": ex})


# ─────────────────────────────────────────────────────────────────────────────
# Backtest SSE stream
# ─────────────────────────────────────────────────────────────────────────────

async def api_backtest(req: web.Request) -> web.StreamResponse:
    try:
        body = await req.json()
    except Exception:
        body = {}

    symbol   = body.get("symbol",   config.DEFAULT_SYMBOL)
    interval = body.get("interval", config.DEFAULT_INTERVAL)
    balance  = float(body.get("balance",    config.BACKTEST_INITIAL_BALANCE))
    comm     = float(body.get("commission", config.BACKTEST_COMMISSION_PCT))
    slip     = float(body.get("slippage",   config.BACKTEST_SLIPPAGE_PCT))

    resp = web.StreamResponse(headers={
        "Content-Type":  "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(req)

    async def _send(obj):
        line = f"data: {json.dumps(obj, default=str)}\n\n"
        await resp.write(line.encode())

    try:
        candles = await asyncio.to_thread(
            data_feed.get_klines, symbol, interval, config.KLINE_LIMIT
        )
        await _send({"type": "started", "candles": len(candles), "symbol": symbol})

        def _run():
            return list(run_backtest(candles, balance, comm, slip, progress_every=10))

        events = await asyncio.to_thread(_run)
        for ev in events:
            await _send(ev)
            if ev.get("type") in ("trade", "progress"):
                await asyncio.sleep(0.01)

    except Exception as exc:
        await _send({"type": "error", "message": str(exc)})
    finally:
        await resp.write(b"data: {\"type\": \"end\"}\n\n")
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket endpoint
# ─────────────────────────────────────────────────────────────────────────────

def _ws_broadcast(msg: dict, symbol: str | None = None):
    manager.broadcast(msg, symbol=symbol)


async def ws_endpoint(req: web.Request) -> web.WebSocketResponse:
    ws     = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    client = Client(ws)

    async def sender():
        try:
            while True:
                msg = await client.queue.get()
                try:
                    await ws.send_str(json.dumps(msg, default=str))
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    send_task = asyncio.create_task(sender())

    try:
        # Hello burst
        client.send({
            "type":           "config",
            "symbols":        config.SYMBOLS,
            "intervals":      config.INTERVALS,
            "default_symbol": config.DEFAULT_SYMBOL,
            "default_interval": config.DEFAULT_INTERVAL,
            "exchange":       config.ACTIVE_EXCHANGE,
            "trading_mode":   config.TRADING_MODE,
            "keys_configured": keys_configured(),
        })
        client.send({"type": "risk_status",  "data": daily_risk.status()})
        client.send({"type": "keys_status",  "data": keys_status_payload()})

        # Immediately push snapshot for default symbol
        asyncio.create_task(_push_snapshot(client, client.symbol, client.interval))
        manager.add_client(client)

        await _trade_stream.subscribe([client.symbol], client.interval)

        async for frame in ws:
            if frame.type != WSMsgType.TEXT:
                if frame.type == WSMsgType.ERROR:
                    break
                continue
            try:
                msg = json.loads(frame.data)
            except Exception:
                continue
            kind = msg.get("type")

            if kind == "subscribe":
                sym = msg.get("symbol",   config.DEFAULT_SYMBOL)
                ivl = msg.get("interval", config.DEFAULT_INTERVAL)
                if sym in config.SYMBOLS and ivl in config.INTERVALS:
                    manager.retarget(client, sym, ivl)
                    await _trade_stream.subscribe([sym], ivl)
                    # Invalidate cache so user gets fresh data immediately
                    _invalidate_cache(sym, ivl)
                    # Push snapshot immediately — this is the "auto-update" on user change
                    asyncio.create_task(_push_snapshot(client, sym, ivl))
                    # Also push footprint + delta immediately
                    asyncio.create_task(_push_fp_delta(client, sym, ivl))

            elif kind == "ping":
                client.send({"type": "pong", "t": msg.get("t", 0)})

            elif kind == "scan_now":
                scanner.trigger_scan()

            elif kind == "set_exchange":
                ex = msg.get("exchange", "spot")
                if ex in ("spot", "futures"):
                    config.ACTIVE_EXCHANGE = ex
                    async with _state_lock:
                        _state_cache.clear()
                    # Notify all clients
                    _ws_broadcast({"type": "exchange_changed", "exchange": ex})
                    # Push fresh snapshot to this client immediately
                    asyncio.create_task(_push_snapshot(client, client.symbol, client.interval))
                    asyncio.create_task(_push_fp_delta(client, client.symbol, client.interval))
                    scanner.trigger_scan()

    finally:
        send_task.cancel()
        manager.remove_client(client)
        with contextlib.suppress(asyncio.CancelledError):
            await send_task
        with contextlib.suppress(Exception):
            await ws.close()

    return ws


async def _push_snapshot(client: Client, symbol: str, interval: str):
    try:
        state = await _get_state(symbol, interval)
        client.send({"type": "snapshot", "data": state})
    except Exception as exc:
        client.send({"type": "error", "message": str(exc)})


async def _push_fp_delta(client: Client, symbol: str, interval: str):
    try:
        fp = footprint_engine.get_history(symbol, interval, limit=10)
        client.send({"type": "footprint_update", "symbol": symbol,
                     "interval": interval, "data": fp})
        delta = footprint_engine.get_delta_series(symbol, interval, 50)
        client.send({"type": "delta_update", "symbol": symbol,
                     "interval": interval, "data": delta})
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Background analysis loop
# ─────────────────────────────────────────────────────────────────────────────

_trade_stream: BinanceTradeStream = None   # type: ignore


async def _refresh_mtf_cache():
    """Background task: refresh MTF data for all subscribed symbols every 60s."""
    while True:
        try:
            with manager._lock:
                symbols = list({c.symbol for c in manager.clients})
            for sym in symbols:
                try:
                    data = await asyncio.to_thread(mtf_mod.run_mtf_analysis, sym)
                    async with _mtf_lock:
                        _mtf_cache[sym] = {"data": data, "_ts": time.time()}
                    manager.broadcast({"type": "mtf_update", "symbol": sym, "data": data}, symbol=sym)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(60)   # daily + 4H data doesn't change fast


async def _analysis_loop():
    """Periodic full-state push to all connected clients."""
    while True:
        try:
            with manager._lock:
                pairs = list({c.market() for c in manager.clients})

            for sym, ivl in pairs:
                try:
                    _invalidate_cache(sym, ivl)
                    state = await _get_state(sym, ivl)
                    manager.broadcast({"type": "snapshot",        "data": state}, symbol=sym)
                    manager.broadcast({
                        "type": "footprint_update", "symbol": sym, "interval": ivl,
                        "data": footprint_engine.get_history(sym, ivl, limit=10),
                    }, symbol=sym)
                    manager.broadcast({
                        "type": "delta_update", "symbol": sym, "interval": ivl,
                        "data": footprint_engine.get_delta_series(sym, ivl, 50),
                    }, symbol=sym)

                    sig = state.get("signal", {})
                    if sig.get("signal") in ("BUY", "SELL"):
                        trade_logger.log_signal(sig)

                except Exception:
                    traceback.print_exc()

            manager.broadcast({"type": "risk_status", "data": daily_risk.status()})

        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()

        await asyncio.sleep(config.REFRESH_SECONDS)


# ─────────────────────────────────────────────────────────────────────────────
# App lifecycle
# ─────────────────────────────────────────────────────────────────────────────

async def on_startup(app: web.Application):
    global _trade_stream
    loop = asyncio.get_running_loop()

    # Load persisted API keys
    load_keys()

    _trade_stream = BinanceTradeStream(manager=manager)
    _trade_stream.start(loop)

    await _trade_stream.subscribe(config.PINNED_SYMBOLS[:5], config.DEFAULT_INTERVAL)

    app["analysis_task"] = loop.create_task(_analysis_loop())
    app["mtf_task"] = loop.create_task(_refresh_mtf_cache())

    if config.SCANNER_ENABLED:
        scanner.set_broadcaster(_ws_broadcast)
        import threading as _t
        _t.Thread(target=scanner.initial_scan, daemon=True, name="scanner-init").start()

    print(f"[server] Order Flow Bot started on http://{config.HOST}:{config.PORT}")
    print(f"[server] Trading mode: {config.TRADING_MODE}")
    print(f"[server] API keys configured: {keys_configured()}")


async def on_cleanup(app: web.Application):
    if _trade_stream:
        _trade_stream.stop()
    for task_key in ("analysis_task", "mtf_task"):
        task = app.get(task_key)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


# ─────────────────────────────────────────────────────────────────────────────
# Router + main
# ─────────────────────────────────────────────────────────────────────────────

def make_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/",                       index)
    app.router.add_get("/api/config",             api_config)
    app.router.add_get("/api/state",              api_state)
    app.router.add_get("/api/candles",            api_candles)
    app.router.add_get("/api/footprint",          api_footprint)
    app.router.add_get("/api/delta",              api_delta)
    app.router.add_get("/api/sr",                 api_sr)
    app.router.add_get("/api/ema",                api_ema)
    app.router.add_get("/api/positions",          api_positions)
    app.router.add_get("/api/trades",             api_trades)
    app.router.add_get("/api/stats",              api_stats)
    app.router.add_get("/api/risk-status",        api_risk_status)
    app.router.add_get("/api/signals",            api_signals)
    app.router.add_get("/api/mtf",               api_mtf)
    app.router.add_get("/api/scanner",            api_scanner_get)
    app.router.add_post("/api/scanner/scan",      api_scanner_trigger)
    app.router.add_get("/api/exchange",           api_exchange)
    app.router.add_post("/api/exchange",          api_exchange)
    app.router.add_post("/api/backtest",          api_backtest)
    app.router.add_get("/api/keys/status",        api_keys_status)
    app.router.add_post("/api/keys",              api_keys)
    app.router.add_get("/api/settings",           api_settings_get)
    app.router.add_post("/api/settings",          api_settings_post)
    app.router.add_get("/ws",                     ws_endpoint)
    app.router.add_static("/static", BASE_DIR / "static")

    return app


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    web.run_app(make_app(), host=config.HOST, port=config.PORT)
