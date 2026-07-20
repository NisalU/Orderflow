"""
Coin Scanner — Finds high-volatility coins on Binance.
Manual-trigger mode: no background loop. Call trigger_scan() to fire once.
Results broadcast via registered broadcaster callback.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback

import config
import data_feed

log = logging.getLogger("scanner")

_EXCLUDE_BASES = {
    "USDC", "BUSD", "TUSD", "USDT", "DAI", "FDUSD", "USDP", "EUR", "GBP", "BIFI",
}
_EXCLUDE_FRAGMENTS = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "2L", "2S")


def _vol_score(t: dict) -> float:
    pct    = abs(float(t.get("priceChangePercent", 0)))
    vol    = float(t.get("quoteVolume", 0))
    high   = float(t.get("highPrice",  1))
    low    = max(float(t.get("lowPrice", 1e-12)), 1e-12)
    amp    = (high / low - 1.0) * 100.0
    trades = float(t.get("count", 0))
    return pct * 6.0 + amp * 2.5 + min(vol / 1e9, 3.0) * 0.5 + min(trades / 2e5, 2.0) * 0.2


def _valid_symbol(sym: str) -> bool:
    if not sym.endswith("USDT"):
        return False
    base = sym[:-4]
    if base in _EXCLUDE_BASES:
        return False
    if base in config.SCANNER_EXCLUDE_SLOW_CAPS:
        return False
    if any(f in base for f in _EXCLUDE_FRAGMENTS):
        return False
    return True


class CoinScanner:
    def __init__(self):
        self._lock        = threading.Lock()
        self._hot_coins:  list = []
        self._last_scan:  float = 0
        self._scanning:   bool  = False
        self._broadcaster = None

    def set_broadcaster(self, fn):
        self._broadcaster = fn

    def get_hot_coins(self) -> list:
        with self._lock:
            return list(self._hot_coins)

    def get_last_scan(self) -> float:
        with self._lock:
            return self._last_scan

    def is_scanning(self) -> bool:
        with self._lock:
            return self._scanning

    def trigger_scan(self) -> bool:
        with self._lock:
            if self._scanning:
                return False
            self._scanning = True
        t = threading.Thread(target=self._do_scan, daemon=True, name="scanner")
        t.start()
        return True

    def initial_scan(self):
        """Called once at startup."""
        self._do_scan()

    def _do_scan(self):
        futures = getattr(config, "ACTIVE_EXCHANGE", "spot") == "futures"
        try:
            if self._broadcaster:
                self._broadcaster({"type": "scanner_scanning", "scanning": True})

            raw = data_feed.get_all_tickers_24hr(futures=futures)
            candidates = []
            for t in raw:
                sym = t.get("symbol", "")
                if not _valid_symbol(sym):
                    continue
                vol = float(t.get("quoteVolume", 0))
                pct = abs(float(t.get("priceChangePercent", 0)))
                if vol < config.SCANNER_MIN_VOLUME_USDT:
                    continue
                if pct < config.SCANNER_VOLATILITY_MIN_PCT:
                    continue
                candidates.append({
                    "symbol":      sym,
                    "price":       float(t.get("lastPrice", 0)),
                    "change_pct":  round(float(t.get("priceChangePercent", 0)), 2),
                    "volume":      round(vol, 0),
                    "high":        float(t.get("highPrice", 0)),
                    "low":         float(t.get("lowPrice",  0)),
                    "score":       round(_vol_score(t), 3),
                })

            candidates.sort(key=lambda x: -x["score"])
            top = candidates[:config.SCANNER_TOP_N]

            # Update global symbol list
            new_syms = [c["symbol"] for c in top]
            merged   = list(dict.fromkeys(config.PINNED_SYMBOLS + new_syms))
            config.SYMBOLS = merged

            with self._lock:
                self._hot_coins = top
                self._last_scan = time.time()
                self._scanning  = False

            if self._broadcaster:
                self._broadcaster({"type": "scanner_update",   "data": top})
                self._broadcaster({"type": "scanner_scanning", "scanning": False})
                self._broadcaster({"type": "config", "symbols": config.SYMBOLS})

        except Exception:
            traceback.print_exc()
            with self._lock:
                self._scanning = False
            if self._broadcaster:
                self._broadcaster({"type": "scanner_scanning", "scanning": False})


scanner = CoinScanner()
