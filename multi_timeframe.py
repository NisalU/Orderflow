"""
Multi-Timeframe (MTF) Analysis
==============================
Implements the top-down trading methodology:

  Daily  → Find Trend            (EMA20/50/200, ADX-like slope)
     ↓
  4H     → Find Support/Resistance (swing highs/lows, key levels)
     ↓
  1H     → Detect Pattern          (engulfing, pin bar, inside bar…)
     ↓
  Footprint → Confirm buyers/sellers (delta, stacked imbalances)
     ↓
  Entry  → Stop at swing high/low, Target 2R–3R

The MTF result is a single structured dict that signal_generator uses
to gate entries. All four layers must align for a valid signal.
"""
from __future__ import annotations

from typing import List, Optional

import config
import data_feed
import pattern_detector as pd
import support_resistance as sr
import trend_detector as td


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ema(values: List[float], period: int) -> List[Optional[float]]:
    if len(values) < period:
        return [None] * len(values)
    k = 2.0 / (period + 1)
    result: List[Optional[float]] = [None] * len(values)
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    first = result[period - 1]
    for i in range(period - 1):
        result[i] = first
    return result


def _slope(series: List[Optional[float]], lookback: int = 5) -> float:
    """Simple linear slope of the last `lookback` non-None values."""
    vals = [v for v in series[-lookback:] if v is not None]
    if len(vals) < 2:
        return 0.0
    return (vals[-1] - vals[0]) / max(abs(vals[0]), 1e-12)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — Daily Trend
# ─────────────────────────────────────────────────────────────────────────────

def analyze_daily_trend(candles_daily: List[dict]) -> dict:
    """
    Uses EMA20, EMA50, EMA200 on Daily candles.
    BULLISH  : EMA20 > EMA50 > EMA200, price > EMA20
    BEARISH  : EMA20 < EMA50 < EMA200, price < EMA20
    NEUTRAL  : anything else
    """
    if not candles_daily or len(candles_daily) < 200:
        # Fall back to EMA50 if not enough daily candles
        result = td.detect_trend(candles_daily) if candles_daily else _neutral_trend()
        result["ema200_last"] = None
        result["timeframe"] = "1d"
        return result

    closes = [c["close"] for c in candles_daily]
    ema20  = _ema(closes, 20)
    ema50  = _ema(closes, 50)
    ema200 = _ema(closes, 200)

    ef  = ema20[-1]
    es  = ema50[-1]
    e2  = ema200[-1]
    price = closes[-1]

    if None in (ef, es, e2):
        return _neutral_trend()

    if ef > es and es > e2 and price > ef:
        trend = "BULLISH"
    elif ef < es and es < e2 and price < ef:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    slope = _slope(ema50)

    return {
        "trend":         trend,
        "ema_fast_last": round(ef,  8),
        "ema_slow_last": round(es,  8),
        "ema200_last":   round(e2,  8),
        "price":         round(price, 8),
        "slope":         round(slope, 6),
        "signal_ok":     trend != "NEUTRAL",
        "timeframe":     "1d",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — 4H Support & Resistance
# ─────────────────────────────────────────────────────────────────────────────

def analyze_4h_sr(candles_4h: List[dict]) -> dict:
    """
    Detect S/R levels from 4H candles.
    Returns S/R data plus a proximity flag (is price near a key level?).
    """
    if not candles_4h or len(candles_4h) < 20:
        return {
            "nearest_support":    None,
            "nearest_resistance": None,
            "all_levels":         [],
            "near_level":         False,
            "level_strength":     0,
            "current_price":      candles_4h[-1]["close"] if candles_4h else 0,
            "timeframe":          "4h",
        }

    sr_data = sr.detect_levels(candles_4h)
    price   = sr_data["current_price"]

    # "Near level" = within 0.3% of a significant S/R
    near_level   = False
    level_strength = 0
    for lv in sr_data["all_levels"][:8]:
        prox = abs(price - lv["price"]) / max(price, 1e-12)
        if prox <= 0.003:
            near_level     = True
            level_strength = max(level_strength, lv["strength"])

    return {
        **sr_data,
        "near_level":     near_level,
        "level_strength": level_strength,
        "timeframe":      "4h",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — 1H Pattern
# ─────────────────────────────────────────────────────────────────────────────

def analyze_1h_pattern(candles_1h: List[dict]) -> dict:
    """Detect entry patterns on the 1H chart."""
    result = pd.detect_patterns(candles_1h)
    result["timeframe"] = "1h"
    return result


# ─────────────────────────────────────────────────────────────────────────────
# MTF Confluence Score
# ─────────────────────────────────────────────────────────────────────────────

def mtf_confluence(
    daily_trend: dict,
    sr_4h: dict,
    pattern_1h: dict,
    fp_snapshot: dict,
    direction: str,          # "BUY" or "SELL"
) -> dict:
    """
    Score how many MTF layers align with the proposed trade direction.
    Returns a confluence dict with individual layer pass/fail and total score.
    """
    checks = {}

    # Layer 1: Daily trend aligns
    checks["daily_trend"] = (
        (direction == "BUY"  and daily_trend.get("trend") == "BULLISH") or
        (direction == "SELL" and daily_trend.get("trend") == "BEARISH")
    )

    # Layer 2: 4H S/R — price near a key level
    checks["4h_near_level"] = sr_4h.get("near_level", False)

    # Layer 3: 1H pattern aligns with direction
    pat_bias = pattern_1h.get("bias", "NEUTRAL")
    checks["1h_pattern"] = (
        (direction == "BUY"  and pat_bias == "BULLISH") or
        (direction == "SELL" and pat_bias == "BEARISH")
    )

    # Layer 4: Footprint confirms direction
    last_closed = fp_snapshot.get("last_closed", {})
    fp_delta = last_closed.get("delta", 0)
    checks["footprint_confirms"] = (
        (direction == "BUY"  and fp_delta >= config.DELTA_THRESHOLD) or
        (direction == "SELL" and fp_delta <= -config.DELTA_THRESHOLD)
    )

    passed = [k for k, v in checks.items() if v]
    failed = [k for k, v in checks.items() if not v]
    score  = len(passed)  # 0-4

    return {
        "checks":  checks,
        "passed":  passed,
        "failed":  failed,
        "score":   score,
        "max":     4,
        "strong":  score >= 3,      # 3+ layers = tradeable confluence
        "perfect": score == 4,      # all 4 = highest conviction
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main MTF Analysis (fetches data internally)
# ─────────────────────────────────────────────────────────────────────────────

def run_mtf_analysis(symbol: str) -> dict:
    """
    Fetch daily, 4H, and 1H candles and run all three analysis layers.
    Returns a unified MTF dict for the signal generator and dashboard.
    """
    results = {"symbol": symbol, "error": None}

    try:
        candles_1d = data_feed.get_klines(symbol, "1d", limit=210)
    except Exception as exc:
        candles_1d = []
        results["error"] = str(exc)

    try:
        candles_4h = data_feed.get_klines(symbol, "4h", limit=200)
    except Exception as exc:
        candles_4h = []

    try:
        candles_1h = data_feed.get_klines(symbol, "1h", limit=100)
    except Exception as exc:
        candles_1h = []

    daily = analyze_daily_trend(candles_1d)
    sr4h  = analyze_4h_sr(candles_4h)
    pat1h = analyze_1h_pattern(candles_1h)

    results["daily"] = daily
    results["4h_sr"] = sr4h
    results["1h_pattern"] = pat1h

    # Overall MTF bias
    bias_votes = {"BULLISH": 0, "BEARISH": 0}
    if daily.get("trend") == "BULLISH":
        bias_votes["BULLISH"] += 2   # daily gets double weight
    elif daily.get("trend") == "BEARISH":
        bias_votes["BEARISH"] += 2

    if pat1h.get("bias") == "BULLISH":
        bias_votes["BULLISH"] += 1
    elif pat1h.get("bias") == "BEARISH":
        bias_votes["BEARISH"] += 1

    if bias_votes["BULLISH"] > bias_votes["BEARISH"]:
        results["bias"] = "BULLISH"
    elif bias_votes["BEARISH"] > bias_votes["BULLISH"]:
        results["bias"] = "BEARISH"
    else:
        results["bias"] = "NEUTRAL"

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _neutral_trend() -> dict:
    return {
        "trend":         "NEUTRAL",
        "ema_fast_last": 0,
        "ema_slow_last": 0,
        "ema200_last":   None,
        "price":         0,
        "slope":         0,
        "signal_ok":     False,
        "timeframe":     "1d",
    }
