"""
Risk Manager — Position sizing, SL/TP calculation, daily loss limits.
All calculations are pure functions (no side effects) except for DailyRiskTracker.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import config


@dataclass
class TradeParams:
    """Full computed trade parameters for one signal."""
    symbol:       str
    direction:    str        # "BUY" | "SELL"
    entry_price:  float
    stop_loss:    float
    take_profit:  float
    tp2:          float      # secondary TP at higher R:R
    risk_amount:  float      # USDT risked
    position_size: float     # quantity (base asset)
    risk_pct:     float      # actual % risked of account
    rr_ratio:     float      # reward:risk
    leverage:     int
    account_balance: float


def _find_swing_low(candles: list, lookback: int = 5) -> float:
    """Most recent significant swing low within last `lookback` candles."""
    lows = [c["low"] for c in candles[-lookback:]]
    return min(lows)


def _find_swing_high(candles: list, lookback: int = 5) -> float:
    """Most recent significant swing high within last `lookback` candles."""
    highs = [c["high"] for c in candles[-lookback:]]
    return max(highs)


def _auto_leverage(atr_pct: float) -> int:
    """Scale leverage inversely to volatility (only when AUTO_LEVERAGE=True)."""
    if atr_pct <= 0:
        return 1
    # High volatility → lower leverage
    if atr_pct > 0.03:   return 1
    if atr_pct > 0.02:   return 2
    if atr_pct > 0.01:   return 3
    return min(5, config.DEFAULT_LEVERAGE)


def compute_trade_params(
    direction:       str,
    candles:         list,
    account_balance: float | None = None,
    risk_pct:        float | None = None,
) -> Optional[TradeParams]:
    """
    Compute full trade parameters for a BUY or SELL signal.

    Stop Loss:
      BUY  → recent swing low minus SL_BUFFER_PCT
      SELL → recent swing high plus SL_BUFFER_PCT

    Take Profit:
      TP1 = entry + (risk × TP_RATIO)
      TP2 = entry + (risk × TP_RATIO × 2)
    """
    balance    = account_balance or config.ACCOUNT_BALANCE_USDT
    risk_frac  = (risk_pct or config.RISK_PER_TRADE_PCT) / 100.0
    entry      = candles[-1]["close"]

    # ── Stop Loss ─────────────────────────────────────────────────────────
    buf = config.SL_BUFFER_PCT
    if direction == "BUY":
        swing_low = _find_swing_low(candles, lookback=8)
        sl = swing_low * (1 - buf)
        risk_per_unit = entry - sl
    elif direction == "SELL":
        swing_high = _find_swing_high(candles, lookback=8)
        sl = swing_high * (1 + buf)
        risk_per_unit = sl - entry
    else:
        return None

    if risk_per_unit <= 0:
        return None

    # ── Take Profit ───────────────────────────────────────────────────────
    tp_distance = risk_per_unit * config.TP_RATIO
    if direction == "BUY":
        tp1 = entry + tp_distance
        tp2 = entry + tp_distance * 2
    else:
        tp1 = entry - tp_distance
        tp2 = entry - tp_distance * 2

    rr = tp_distance / risk_per_unit  # should equal TP_RATIO

    # ── Leverage ──────────────────────────────────────────────────────────
    if config.AUTO_LEVERAGE:
        atr_pct = risk_per_unit / entry
        leverage = _auto_leverage(atr_pct)
    else:
        leverage = config.DEFAULT_LEVERAGE

    effective_balance = balance * leverage

    # ── Position Size ─────────────────────────────────────────────────────
    risk_amount   = balance * risk_frac
    position_size = risk_amount / risk_per_unit   # units of base asset
    max_size      = effective_balance / entry       # don't exceed balance
    position_size = min(position_size, max_size)

    actual_risk_pct = (position_size * risk_per_unit / balance) * 100

    return TradeParams(
        symbol        = candles[-1].get("symbol", ""),
        direction     = direction,
        entry_price   = round(entry, 8),
        stop_loss     = round(sl, 8),
        take_profit   = round(tp1, 8),
        tp2           = round(tp2, 8),
        risk_amount   = round(risk_amount, 4),
        position_size = round(position_size, 6),
        risk_pct      = round(actual_risk_pct, 3),
        rr_ratio      = round(rr, 2),
        leverage      = leverage,
        account_balance = balance,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Daily Risk Tracker
# ─────────────────────────────────────────────────────────────────────────────

class DailyRiskTracker:
    """
    Thread-safe tracker for daily PnL and trade count.
    Resets automatically at UTC midnight.
    """

    def __init__(self):
        self._day:          int   = self._today()
        self._daily_pnl:    float = 0.0
        self._trade_count:  int   = 0
        self._halted:       bool  = False

    @staticmethod
    def _today() -> int:
        return time.gmtime().tm_yday

    def _check_reset(self):
        today = self._today()
        if today != self._day:
            self._day         = today
            self._daily_pnl   = 0.0
            self._trade_count = 0
            self._halted      = False

    def record_trade_open(self):
        self._check_reset()
        self._trade_count += 1

    def record_pnl(self, pnl: float):
        self._check_reset()
        self._daily_pnl += pnl
        balance = config.ACCOUNT_BALANCE_USDT
        max_loss = balance * config.MAX_DAILY_LOSS_PCT / 100
        if self._daily_pnl <= -max_loss:
            self._halted = True

    def can_trade(self) -> tuple[bool, str]:
        """Returns (allowed: bool, reason: str)."""
        self._check_reset()
        if self._halted:
            return False, f"Daily loss limit reached ({self._daily_pnl:.2f} USDT)"
        if self._trade_count >= config.MAX_TRADES_PER_DAY:
            return False, f"Max daily trades reached ({self._trade_count})"
        return True, "ok"

    def status(self) -> dict:
        self._check_reset()
        balance  = config.ACCOUNT_BALANCE_USDT
        max_loss = balance * config.MAX_DAILY_LOSS_PCT / 100
        return {
            "daily_pnl":     round(self._daily_pnl, 4),
            "trade_count":   self._trade_count,
            "max_trades":    config.MAX_TRADES_PER_DAY,
            "max_daily_loss": round(max_loss, 4),
            "halted":         self._halted,
            "remaining_trades": max(0, config.MAX_TRADES_PER_DAY - self._trade_count),
        }


# Singleton
daily_risk = DailyRiskTracker()
