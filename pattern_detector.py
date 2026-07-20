"""
Pattern Detector — 1H Candle Pattern Recognition
=================================================
Detects high-probability entry patterns on the 1H chart that confirm
the multi-timeframe bias (Daily trend + 4H S/R + 1H pattern + Footprint).

Patterns detected:
  - Bullish/Bearish Engulfing
  - Hammer / Shooting Star (pin bars)
  - Bullish/Bearish Inside Bar breakout
  - Morning/Evening Doji Star
  - Bullish/Bearish Marubozu (strong momentum candles)
"""
from __future__ import annotations

from typing import List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _body(c: dict) -> float:
    return abs(c["close"] - c["open"])

def _upper_wick(c: dict) -> float:
    return c["high"] - max(c["open"], c["close"])

def _lower_wick(c: dict) -> float:
    return min(c["open"], c["close"]) - c["low"]

def _range(c: dict) -> float:
    return c["high"] - c["low"] or 1e-12

def _is_bullish(c: dict) -> bool:
    return c["close"] > c["open"]

def _is_bearish(c: dict) -> bool:
    return c["close"] < c["open"]

def _is_doji(c: dict, threshold: float = 0.1) -> bool:
    return _body(c) / _range(c) < threshold


# ─────────────────────────────────────────────────────────────────────────────
# Individual Pattern Functions
# ─────────────────────────────────────────────────────────────────────────────

def _bullish_engulfing(prev: dict, curr: dict) -> bool:
    """
    Current bullish candle body completely engulfs the previous bearish body.
    Strong reversal at support.
    """
    return (
        _is_bearish(prev)
        and _is_bullish(curr)
        and curr["open"] <= prev["close"]
        and curr["close"] >= prev["open"]
        and _body(curr) > _body(prev) * 0.9
    )


def _bearish_engulfing(prev: dict, curr: dict) -> bool:
    """
    Current bearish candle body completely engulfs the previous bullish body.
    Strong reversal at resistance.
    """
    return (
        _is_bullish(prev)
        and _is_bearish(curr)
        and curr["open"] >= prev["close"]
        and curr["close"] <= prev["open"]
        and _body(curr) > _body(prev) * 0.9
    )


def _hammer(c: dict) -> bool:
    """
    Small body at the top, long lower wick (≥ 2× body), minimal upper wick.
    Bullish reversal at support.
    """
    body = _body(c)
    lo_w = _lower_wick(c)
    hi_w = _upper_wick(c)
    rng  = _range(c)
    return (
        body > 0
        and lo_w >= body * 2.0
        and hi_w <= body * 0.5
        and body / rng <= 0.35
    )


def _shooting_star(c: dict) -> bool:
    """
    Small body at the bottom, long upper wick (≥ 2× body), minimal lower wick.
    Bearish reversal at resistance.
    """
    body = _body(c)
    hi_w = _upper_wick(c)
    lo_w = _lower_wick(c)
    rng  = _range(c)
    return (
        body > 0
        and hi_w >= body * 2.0
        and lo_w <= body * 0.5
        and body / rng <= 0.35
    )


def _bullish_inside_bar(prev: dict, curr: dict) -> bool:
    """
    Current candle range is entirely within the previous candle range.
    Bullish bias: current close is in the upper half of the inside bar range.
    """
    inside = (curr["high"] < prev["high"] and curr["low"] > prev["low"])
    if not inside:
        return False
    midpoint = (curr["high"] + curr["low"]) / 2
    return curr["close"] > midpoint


def _bearish_inside_bar(prev: dict, curr: dict) -> bool:
    """Bearish inside bar — close in the lower half."""
    inside = (curr["high"] < prev["high"] and curr["low"] > prev["low"])
    if not inside:
        return False
    midpoint = (curr["high"] + curr["low"]) / 2
    return curr["close"] < midpoint


def _bullish_marubozu(c: dict, threshold: float = 0.85) -> bool:
    """
    Strong bullish candle: body ≥ 85% of total range, closing near high.
    Momentum continuation signal.
    """
    body = _body(c)
    rng  = _range(c)
    return (
        _is_bullish(c)
        and body / rng >= threshold
        and _upper_wick(c) / rng <= 0.08
    )


def _bearish_marubozu(c: dict, threshold: float = 0.85) -> bool:
    """Strong bearish candle: body ≥ 85% of range, closing near low."""
    body = _body(c)
    rng  = _range(c)
    return (
        _is_bearish(c)
        and body / rng >= threshold
        and _lower_wick(c) / rng <= 0.08
    )


def _doji_star_bullish(c1: dict, c2: dict, c3: dict) -> bool:
    """
    Morning Doji Star: bearish candle, doji gap (or small gap), bullish candle.
    Three-candle reversal pattern.
    """
    return (
        _is_bearish(c1)
        and _is_doji(c2)
        and _is_bullish(c3)
        and c3["close"] > (c1["open"] + c1["close"]) / 2
    )


def _doji_star_bearish(c1: dict, c2: dict, c3: dict) -> bool:
    """Evening Doji Star: bullish candle, doji, bearish candle."""
    return (
        _is_bullish(c1)
        and _is_doji(c2)
        and _is_bearish(c3)
        and c3["close"] < (c1["open"] + c1["close"]) / 2
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def detect_patterns(candles: List[dict]) -> dict:
    """
    Scan the last few candles for known patterns.

    Parameters
    ----------
    candles : list of OHLCV dicts, newest last. Needs at least 3 candles.

    Returns
    -------
    {
        "bullish_patterns" : list of pattern names detected
        "bearish_patterns" : list of pattern names detected
        "bias"             : "BULLISH" | "BEARISH" | "NEUTRAL"
        "confidence"       : 0-100 (number of confirming patterns × weight)
        "last_candle"      : dict of last candle pattern flags
    }
    """
    if len(candles) < 3:
        return _empty()

    c3 = candles[-3]    # 3 bars ago
    c2 = candles[-2]    # previous bar
    c1 = candles[-1]    # latest (most recent closed) bar

    bullish: List[str] = []
    bearish: List[str] = []

    # Two-candle patterns
    if _bullish_engulfing(c2, c1):
        bullish.append("bullish_engulfing")
    if _bearish_engulfing(c2, c1):
        bearish.append("bearish_engulfing")
    if _bullish_inside_bar(c2, c1):
        bullish.append("bullish_inside_bar")
    if _bearish_inside_bar(c2, c1):
        bearish.append("bearish_inside_bar")

    # Single-candle patterns
    if _hammer(c1):
        bullish.append("hammer")
    if _shooting_star(c1):
        bearish.append("shooting_star")
    if _bullish_marubozu(c1):
        bullish.append("bullish_marubozu")
    if _bearish_marubozu(c1):
        bearish.append("bearish_marubozu")

    # Three-candle patterns
    if _doji_star_bullish(c3, c2, c1):
        bullish.append("morning_doji_star")
    if _doji_star_bearish(c3, c2, c1):
        bearish.append("evening_doji_star")

    # Weights: strong reversal patterns get higher weight
    weights = {
        "bullish_engulfing":  30,
        "bearish_engulfing":  30,
        "morning_doji_star":  25,
        "evening_doji_star":  25,
        "hammer":             20,
        "shooting_star":      20,
        "bullish_marubozu":   20,
        "bearish_marubozu":   20,
        "bullish_inside_bar": 10,
        "bearish_inside_bar": 10,
    }

    bull_score = sum(weights.get(p, 10) for p in bullish)
    bear_score = sum(weights.get(p, 10) for p in bearish)

    if bull_score > bear_score and bull_score > 0:
        bias = "BULLISH"
        confidence = min(100, bull_score)
    elif bear_score > bull_score and bear_score > 0:
        bias = "BEARISH"
        confidence = min(100, bear_score)
    else:
        bias = "NEUTRAL"
        confidence = 0

    return {
        "bullish_patterns": bullish,
        "bearish_patterns": bearish,
        "bias":             bias,
        "confidence":       confidence,
        "has_pattern":      bool(bullish or bearish),
        "last_candle": {
            "is_bullish":        _is_bullish(c1),
            "is_bearish":        _is_bearish(c1),
            "is_doji":           _is_doji(c1),
            "body_pct":          round(_body(c1) / _range(c1) * 100, 1),
            "upper_wick_pct":    round(_upper_wick(c1) / _range(c1) * 100, 1),
            "lower_wick_pct":    round(_lower_wick(c1) / _range(c1) * 100, 1),
        },
    }


def _empty() -> dict:
    return {
        "bullish_patterns": [],
        "bearish_patterns": [],
        "bias":             "NEUTRAL",
        "confidence":       0,
        "has_pattern":      False,
        "last_candle":      {},
    }


def pattern_aligns_with_trend(pattern_result: dict, trend_direction: str) -> bool:
    """
    Returns True if the detected pattern bias matches the overall trend.
    Prevents counter-trend entries.
    """
    bias = pattern_result.get("bias", "NEUTRAL")
    if trend_direction == "BULLISH" and bias == "BULLISH":
        return True
    if trend_direction == "BEARISH" and bias == "BEARISH":
        return True
    return False
