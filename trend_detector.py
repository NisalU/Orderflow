"""
Trend Detector — EMA-based directional bias filter.
Only allows trades in the direction of the trend; NEUTRAL blocks all entries.

Trend rules:
  BULLISH : EMA20 > EMA50  AND  price > EMA20
  BEARISH : EMA20 < EMA50  AND  price < EMA20
  NEUTRAL : anything else  → no trade
"""
from __future__ import annotations

from typing import List, Optional

import config


# ─────────────────────────────────────────────────────────────────────────────
# EMA calculation (no external libs — pure Python)
# ─────────────────────────────────────────────────────────────────────────────

def _ema(values: List[float], period: int) -> List[float]:
    """
    Exponential Moving Average via SMA seed → EMA recursion.
    Returns same-length list; first (period-1) values are NaN-filled with
    the first valid EMA value for simplicity.
    """
    if len(values) < period:
        return values[:]
    k      = 2.0 / (period + 1)
    result = [None] * len(values)
    # Seed with SMA
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    # Back-fill with first valid value
    first = result[period - 1]
    for i in range(period - 1):
        result[i] = first
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def detect_trend(candles: List[dict]) -> dict:
    """
    Parameters
    ----------
    candles : list of OHLCV dicts (oldest → newest), min length = EMA_SLOW_PERIOD

    Returns
    -------
    dict with keys:
      trend       : "BULLISH" | "BEARISH" | "NEUTRAL"
      ema_fast    : list of EMA-fast values
      ema_slow    : list of EMA-slow values
      ema_fast_last : float
      ema_slow_last : float
      price       : float (last close)
      signal_ok   : bool (True = trend is clear, entry filtering allowed)
    """
    if len(candles) < config.EMA_SLOW_PERIOD:
        return _neutral_result(0, [], [])

    closes = [c["close"] for c in candles]
    ema_fast = _ema(closes, config.EMA_FAST_PERIOD)
    ema_slow = _ema(closes, config.EMA_SLOW_PERIOD)

    ef = ema_fast[-1]
    es = ema_slow[-1]
    price = closes[-1]

    if ef is None or es is None:
        return _neutral_result(price, ema_fast, ema_slow)

    if ef > es and price > ef:
        trend = "BULLISH"
    elif ef < es and price < ef:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    return {
        "trend":        trend,
        "ema_fast":     ema_fast,
        "ema_slow":     ema_slow,
        "ema_fast_last": round(ef, 8),
        "ema_slow_last": round(es, 8),
        "price":        round(price, 8),
        "signal_ok":    trend != "NEUTRAL",
    }


def _neutral_result(price, ef_series, es_series) -> dict:
    ef = ef_series[-1] if ef_series else 0
    es = es_series[-1] if es_series else 0
    return {
        "trend":        "NEUTRAL",
        "ema_fast":     ef_series,
        "ema_slow":     es_series,
        "ema_fast_last": ef or 0,
        "ema_slow_last": es or 0,
        "price":        price,
        "signal_ok":    False,
    }


def get_ema_overlay(candles: List[dict]) -> dict:
    """Return EMA series formatted for lightweight-charts line overlay."""
    closes = [c["close"] for c in candles]
    times  = [c["time"]  for c in candles]
    ema_fast = _ema(closes, config.EMA_FAST_PERIOD)
    ema_slow = _ema(closes, config.EMA_SLOW_PERIOD)
    return {
        "ema_fast": [{"time": t, "value": round(v, 8)}
                     for t, v in zip(times, ema_fast) if v is not None],
        "ema_slow": [{"time": t, "value": round(v, 8)}
                     for t, v in zip(times, ema_slow) if v is not None],
        "ema_fast_period": config.EMA_FAST_PERIOD,
        "ema_slow_period": config.EMA_SLOW_PERIOD,
    }
