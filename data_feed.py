"""
Data Feed — Binance REST + WebSocket market data.
Thread-safe with endpoint fallback, TTL caches, and optional API key signing.
"""
import hashlib
import hmac
import threading
import time
import urllib.parse

import requests

import config

_tls = threading.local()        # one requests.Session per thread
_spot_base   = None
_fut_base    = None
_cache_lock  = threading.Lock()
_ticker_cache  = {}             # symbol -> (expires_at, data)
_futures_cache = {}
TICKER_TTL  = 10
FUTURES_TTL = 120


class DataError(Exception):
    pass


def _session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        headers = {"User-Agent": "orderflow-bot/2.0"}
        if config.BINANCE_API_KEY:
            headers["X-MBX-APIKEY"] = config.BINANCE_API_KEY
        s.headers.update(headers)
        _tls.session = s
    return s


def _sign(params: dict) -> str:
    if not config.BINANCE_API_SECRET:
        raise DataError("BINANCE_API_SECRET not set")
    query = urllib.parse.urlencode(params)
    return hmac.new(
        config.BINANCE_API_SECRET.encode(),
        query.encode(),
        hashlib.sha256,
    ).hexdigest()


def _signed_params(params: dict) -> dict:
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    return p


def _get(base_candidates, cached_base, path, params, signed=False):
    global _spot_base, _fut_base
    if signed:
        params = _signed_params(params)
    bases = ([cached_base] if cached_base else []) + [
        b for b in base_candidates if b != cached_base
    ]
    last_err = None
    for base in bases:
        try:
            r = _session().get(base + path, params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "msg" in data and "code" in data:
                    last_err = data.get("msg")
                    continue
                return data, base
            last_err = f"HTTP {r.status_code}"
        except Exception as exc:
            last_err = str(exc)
    raise DataError(f"All endpoints failed for {path}: {last_err}")


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def get_klines(symbol: str, interval: str, limit: int | None = None) -> list[dict]:
    """Fetch OHLCV candles. Returns newest-last list."""
    global _spot_base, _fut_base
    limit = limit or config.KLINE_LIMIT
    exchange = getattr(config, "ACTIVE_EXCHANGE", "spot")
    if exchange == "futures":
        raw, _fut_base = _get(
            config.FUTURES_ENDPOINTS, _fut_base,
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
    else:
        raw, _spot_base = _get(
            config.SPOT_ENDPOINTS, _spot_base,
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
    candles = []
    for k in raw:
        candles.append({
            "time":          k[0] // 1000,
            "open":          float(k[1]),
            "high":          float(k[2]),
            "low":           float(k[3]),
            "close":         float(k[4]),
            "volume":        float(k[5]),
            "close_time":    k[6] // 1000,
            "quote_volume":  float(k[7]),
            "trades":        int(k[8]),
            "taker_buy_vol": float(k[9]),
            "taker_sell_vol": float(k[5]) - float(k[9]),
        })
    return candles


def get_ticker(symbol: str) -> dict:
    """24h ticker (cached)."""
    global _spot_base
    now = time.time()
    with _cache_lock:
        cached = _ticker_cache.get(symbol)
        if cached and now < cached[0]:
            return cached[1]
    exchange = getattr(config, "ACTIVE_EXCHANGE", "spot")
    if exchange == "futures":
        raw, _ = _get(config.FUTURES_ENDPOINTS, _fut_base, "/fapi/v1/ticker/24hr",
                      {"symbol": symbol})
    else:
        raw, _spot_base = _get(config.SPOT_ENDPOINTS, _spot_base, "/api/v3/ticker/24hr",
                               {"symbol": symbol})
    result = {
        "price":          float(raw.get("lastPrice", 0)),
        "change_pct":     float(raw.get("priceChangePercent", 0)),
        "high":           float(raw.get("highPrice", 0)),
        "low":            float(raw.get("lowPrice", 0)),
        "volume":         float(raw.get("volume", 0)),
        "quote_volume":   float(raw.get("quoteVolume", 0)),
        "bid":            float(raw.get("bidPrice", 0)),
        "ask":            float(raw.get("askPrice", 0)),
    }
    with _cache_lock:
        _ticker_cache[symbol] = (now + TICKER_TTL, result)
    return result


def get_recent_trades(symbol: str, limit: int = 500) -> list[dict]:
    """Recent trades (up to 1000) for footprint reconstruction."""
    global _spot_base
    exchange = getattr(config, "ACTIVE_EXCHANGE", "spot")
    if exchange == "futures":
        raw, _ = _get(config.FUTURES_ENDPOINTS, _fut_base, "/fapi/v1/aggTrades",
                      {"symbol": symbol, "limit": limit})
    else:
        raw, _spot_base = _get(config.SPOT_ENDPOINTS, _spot_base, "/api/v3/aggTrades",
                               {"symbol": symbol, "limit": limit})
    trades = []
    for t in raw:
        trades.append({
            "time":     t["T"] // 1000,
            "price":    float(t["p"]),
            "qty":      float(t["q"]),
            "is_buyer_maker": t["m"],   # True = sell aggressor (taker sells)
        })
    return trades


def get_order_book(symbol: str, depth: int = 20) -> dict:
    """Snapshot of order book bids/asks."""
    global _spot_base
    raw, _spot_base = _get(
        config.SPOT_ENDPOINTS, _spot_base, "/api/v3/depth",
        {"symbol": symbol, "limit": depth},
    )
    return {
        "bids": [[float(p), float(q)] for p, q in raw["bids"]],
        "asks": [[float(p), float(q)] for p, q in raw["asks"]],
    }


def get_all_tickers_24hr(futures: bool = False) -> list[dict]:
    """All 24h tickers (for scanner)."""
    global _spot_base, _fut_base
    if futures:
        raw, _fut_base = _get(config.FUTURES_ENDPOINTS, _fut_base,
                              "/fapi/v1/ticker/24hr", {})
    else:
        raw, _spot_base = _get(config.SPOT_ENDPOINTS, _spot_base,
                               "/api/v3/ticker/24hr", {})
    if not isinstance(raw, list):
        raw = [raw]
    return raw
