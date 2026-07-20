"""
Server — aiohttp + WebSocket dashboard for the Order Flow + Footprint Bot.

REST API:
  GET  /api/config           → current configuration
  GET  /api/state            → full analysis snapshot (trend, SR, footprint, signal)
  GET  /api/footprint        → footprint candles for selected symbol/interval
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

WebSocket:
  /ws  → bidirectional: subscribe, ping, scan_now, set_exchange

SSE:
  /api/backtest/stream → streamed backtest results
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

# ── Module imports ────────────────────────────────────────────────────────────
import config
import data_feed
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
# State cache (avoids hitting Binance on every WS push)
# ─────────────────────────────────────────────────────────────────────────────

_state_cache: dict = {}
_state_lock = asyncio.Lock()
_CACHE_TTL  = 8    # seconds


async def _get_state(symbol: str, interval: str) -> dict:
    key = f"{symbol}:{interval}"
    now = time.time()
    async with _state_lock:
        cached = _state_cache.get(key)
        if cached and now - cached["_ts"] < _CACHE_TTL:
            return cached

    # Fetch candles in thread pool
    candles = await asyncio.to_thread(data_feed.get_klines, symbol, interval)

    ticker = None
    try:
        ticker = await asyncio.to_thread(data_feed.get_ticker, symbol)
    except Exception:
        pass

    # Compute all strategy components
    trend_data  = td.detect_trend(candles)
    sr_data     = sr.detect_levels(candles)
    signal      = signal_generator.evaluate(symbol, interval, candles, ticker)
    fp_history  = footprint_engine.get_history(symbol, interval, limit=100)
    ema_overlay = td.get_ema_overlay(candles)

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
    key = f"{symbol}:{interval}"
    _state_cache.pop(key, None)


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
        "symbols":           config.SYMBOLS,
        "intervals":         config.INTERVALS,
        "default_symbol":    config.DEFAULT_SYMBOL,
        "default_interval":  config.DEFAULT_INTERVAL,
        "exchange":          config.ACTIVE_EXCHANGE,
        "ema_fast":          config.EMA_FAST_PERIOD,
        "ema_slow":          config.EMA_SLOW_PERIOD,
        "delta_threshold":   config.DELTA_THRESHOLD,
        "imbalance_ratio":   config.IMBALANCE_RATIO,
        "min_stacked":       config.MIN_STACKED_IMBALANCES,
        "risk_pct":          config.RISK_PER_TRADE_PCT,
        "tp_ratio":          config.TP_RATIO,
        "max_trades_day":    config.MAX_TRADES_PER_DAY,
        "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
        "sessions":          config.TRADE_SESSIONS,
        "leverage":          config.DEFAULT_LEVERAGE,
    })


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
        "manager":  trade_manager.get_stats(),
        "logger":   trade_logger.get_summary(),
        "daily":    daily_risk.status(),
    })


async def api_risk_status(_req: web.Request) -> web.Response:
    return web.json_response(daily_risk.status())


async def api_signals(req: web.Request) -> web.Response:
    symbol = req.query.get("symbol")
    limit  = int(req.query.get("limit", 100))
    return web.json_response({"signals": trade_logger.get_signals(limit=limit, symbol=symbol)})


async def api_scanner_get(_req: web.Request) -> web.Response:
    coins     = scanner.get_hot_coins()
    last_scan = scanner.get_last_scan()
    return web.json_response({
        "coins":     coins,
        "last_scan": last_scan,
        "scanning":  scanner.is_scanning(),
        "count":     len(coins),
    })


async def api_scanner_trigger(_req: web.Request) -> web.Response:
    if scanner.is_scanning():
        return web.json_response({"ok": False, "already_scanning": True})
    ok = scanner.trigger_scan()
    return web.json_response({"ok": ok})


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
    """POST /api/backtest  body: {symbol, interval, balance?, commission?}
    Returns SSE stream of backtest events."""
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

    async def _send(data: dict):
        line = f"data: {json.dumps(data)}\n\n"
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
            # Tiny yield so client can render frame-by-frame
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
    ws   = web.WebSocketResponse(heartbeat=20)
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
        # ── Hello burst ───────────────────────────────────────────────────
        client.send({
            "type":     "config",
            "symbols":  config.SYMBOLS,
            "intervals": config.INTERVALS,
            "default_symbol":   config.DEFAULT_SYMBOL,
            "default_interval": config.DEFAULT_INTERVAL,
            "exchange":  config.ACTIVE_EXCHANGE,
        })
        client.send({"type": "risk_status", "data": daily_risk.status()})

        # Immediately push snapshot
        asyncio.create_task(_push_snapshot(client, client.symbol, client.interval))
        manager.add_client(client)

        # Subscribe Binance stream for this symbol
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
                    asyncio.create_task(_push_snapshot(client, sym, ivl))

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
                    _ws_broadcast({"type": "exchange_changed", "exchange": ex})
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


# ─────────────────────────────────────────────────────────────────────────────
# Background tasks
# ─────────────────────────────────────────────────────────────────────────────

_trade_stream: BinanceTradeStream = None   # type: ignore


async def _analysis_loop():
    """Periodic full-state push to all connected clients."""
    while True:
        try:
            # Collect unique (symbol, interval) pairs being watched
            with manager._lock:
                pairs = list({c.market() for c in manager.clients})

            for sym, ivl in pairs:
                try:
                    _invalidate_cache(sym, ivl)
                    state = await _get_state(sym, ivl)
                    snap  = {"type": "snapshot", "data": state}
                    fp    = footprint_engine.get_history(sym, ivl, limit=10)
                    fp_msg = {"type": "footprint_update", "symbol": sym,
                              "interval": ivl, "data": fp}
                    delta_msg = {"type": "delta_update", "symbol": sym,
                                 "interval": ivl,
                                 "data": footprint_engine.get_delta_series(sym, ivl, 50)}

                    manager.broadcast(snap,     symbol=sym)
                    manager.broadcast(fp_msg,   symbol=sym)
                    manager.broadcast(delta_msg, symbol=sym)

                    # Log signal
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

    # Start Binance aggTrade WebSocket
    _trade_stream = BinanceTradeStream(manager=manager)
    _trade_stream.start(loop)

    # Subscribe to default symbol
    await _trade_stream.subscribe(config.PINNED_SYMBOLS[:5], config.DEFAULT_INTERVAL)

    # Start analysis loop
    app["analysis_task"] = loop.create_task(_analysis_loop())

    # Coin scanner initial scan
    if config.SCANNER_ENABLED:
        scanner.set_broadcaster(_ws_broadcast)
        import threading as _t
        _t.Thread(target=scanner.initial_scan, daemon=True, name="scanner-init").start()

    print(f"[server] Order Flow Bot started on http://{config.HOST}:{config.PORT}")
    print(f"[server] Binance aggTrade stream active for {config.DEFAULT_SYMBOL}")


async def on_cleanup(app: web.Application):
    if _trade_stream:
        _trade_stream.stop()
    task = app.get("analysis_task")
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
    app.router.add_get("/api/scanner",            api_scanner_get)
    app.router.add_post("/api/scanner/scan",      api_scanner_trigger)
    app.router.add_get("/api/exchange",           api_exchange)
    app.router.add_post("/api/exchange",          api_exchange)
    app.router.add_post("/api/backtest",          api_backtest)
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
