"""
Trade Logger — Persistent JSON log of every trade and signal.

Logs every trade with:
  trend, S/R level, delta, imbalances, volume,
  entry reason, exit reason, PnL, duration.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import List

import config


class TradeLogger:

    def __init__(
        self,
        trade_file:  str = config.TRADE_LOG_FILE,
        signal_file: str = config.SIGNAL_LOG_FILE,
    ):
        self._lock        = threading.RLock()
        self._trade_file  = trade_file
        self._signal_file = signal_file
        self._trades:  List[dict] = self._load(self._trade_file)
        self._signals: List[dict] = self._load(self._signal_file)

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self, path: str) -> List[dict]:
        if not os.path.exists(path):
            return []
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return []

    def _save(self, path: str, data: List[dict]):
        try:
            with open(path, "w") as f:
                json.dump(data[-config.MAX_LOG_ENTRIES:], f, indent=2)
        except Exception as e:
            print(f"[trade_logger] save error: {e}")

    # ── Public API ─────────────────────────────────────────────────────────────

    def log_signal(self, signal: dict):
        """Record every evaluated signal (BUY / SELL / NONE)."""
        entry = {
            "time":       signal.get("time", int(time.time())),
            "symbol":     signal.get("symbol", ""),
            "interval":   signal.get("interval", ""),
            "signal":     signal.get("signal", "NONE"),
            "trend":      signal.get("trend", {}).get("direction", ""),
            "ema_fast":   signal.get("trend", {}).get("ema_fast", 0),
            "ema_slow":   signal.get("trend", {}).get("ema_slow", 0),
            "sr_resist":  signal.get("sr", {}).get("nearest_resistance", 0),
            "sr_support": signal.get("sr", {}).get("nearest_support", 0),
            "delta":      signal.get("analytics", {}).get("delta", 0),
            "stacked_buy":  signal.get("analytics", {}).get("stacked_buy", 0),
            "stacked_sell": signal.get("analytics", {}).get("stacked_sell", 0),
            "volume":       signal.get("analytics", {}).get("candle_volume", 0),
            "avg_volume":   signal.get("analytics", {}).get("avg_volume", 0),
            "sell_absorption": signal.get("analytics", {}).get("sell_absorption", False),
            "buy_absorption":  signal.get("analytics", {}).get("buy_absorption", False),
            "passed":  signal.get("passed", []),
            "failed":  signal.get("failed", []),
        }
        with self._lock:
            self._signals.append(entry)
            self._save(self._signal_file, self._signals)

    def log_trade_open(self, position: dict, signal: dict):
        """Record when a trade is opened."""
        entry = {
            "id":           position.get("id", ""),
            "status":       "OPEN",
            "symbol":       position.get("symbol", ""),
            "direction":    position.get("direction", ""),
            "entry_price":  position.get("entry_price", 0),
            "stop_loss":    position.get("stop_loss", 0),
            "take_profit":  position.get("take_profit", 0),
            "tp2":          position.get("tp2", 0),
            "position_size": position.get("position_size", 0),
            "risk_amount":  position.get("risk_amount", 0),
            "leverage":     position.get("leverage", 1),
            "open_time":    position.get("open_time", int(time.time())),
            # Signal context
            "trend":        signal.get("trend", {}).get("direction", ""),
            "sr_level":     position.get("sr_level", 0),
            "delta":        signal.get("analytics", {}).get("delta", 0),
            "stacked_imb":  max(
                signal.get("analytics", {}).get("stacked_buy", 0),
                signal.get("analytics", {}).get("stacked_sell", 0),
            ),
            "volume":       signal.get("analytics", {}).get("candle_volume", 0),
            "entry_reasons": signal.get("reasons", []),
        }
        with self._lock:
            self._trades.append(entry)
            self._save(self._trade_file, self._trades)

    def log_trade_close(self, position: dict):
        """Update existing trade record with close details."""
        pid       = position.get("id", "")
        close_ts  = position.get("close_time", int(time.time()))
        open_ts   = position.get("open_time", close_ts)
        duration_s = close_ts - open_ts

        with self._lock:
            # Find and update in-place
            for trade in reversed(self._trades):
                if trade.get("id") == pid:
                    trade.update({
                        "status":      "CLOSED",
                        "exit_price":  position.get("exit_price", 0),
                        "exit_reason": position.get("exit_reason", ""),
                        "realized_pnl": position.get("realized_pnl", 0),
                        "close_time":  close_ts,
                        "duration_s":  duration_s,
                        "duration_human": _fmt_duration(duration_s),
                    })
                    break
            self._save(self._trade_file, self._trades)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_trades(self, limit: int = 100, symbol: str | None = None) -> List[dict]:
        with self._lock:
            data = list(reversed(self._trades))
        if symbol:
            data = [t for t in data if t.get("symbol") == symbol]
        return data[:limit]

    def get_signals(self, limit: int = 200, symbol: str | None = None) -> List[dict]:
        with self._lock:
            data = list(reversed(self._signals))
        if symbol:
            data = [d for d in data if d.get("symbol") == symbol]
        return data[:limit]

    def get_summary(self) -> dict:
        with self._lock:
            closed = [t for t in self._trades if t.get("status") == "CLOSED"]
        if not closed:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                    "total_pnl": 0, "avg_pnl": 0, "best_trade": 0, "worst_trade": 0,
                    "avg_duration_s": 0}
        pnls  = [t.get("realized_pnl", 0) for t in closed]
        wins  = [p for p in pnls if p > 0]
        durs  = [t.get("duration_s", 0) for t in closed if t.get("duration_s")]
        return {
            "total":         len(closed),
            "wins":          len(wins),
            "losses":        len(closed) - len(wins),
            "win_rate":      round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "total_pnl":     round(sum(pnls), 4),
            "avg_pnl":       round(sum(pnls) / len(pnls), 4),
            "best_trade":    round(max(pnls), 4),
            "worst_trade":   round(min(pnls), 4),
            "avg_duration_s": round(sum(durs) / len(durs), 0) if durs else 0,
        }


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


# Singleton
trade_logger = TradeLogger()
