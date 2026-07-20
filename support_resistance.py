"""
Support & Resistance Detector
==============================
Detects key price levels from:
  1. Swing highs / swing lows
  2. Previous day high / low
  3. Session high / low (London / New York)

Levels are deduplicated (merged within SR_PROXIMITY_PCT) and strength-ranked.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import config


# ─────────────────────────────────────────────────────────────────────────────
# Swing point detection
# ─────────────────────────────────────────────────────────────────────────────

def _swing_highs(candles: List[dict], lookback: int) -> List[Tuple[int, float]]:
    """Return (index, price) for confirmed swing highs."""
    highs = []
    for i in range(lookback, len(candles) - lookback):
        val = candles[i]["high"]
        if all(val >= candles[i - j]["high"] for j in range(1, lookback + 1)) and \
           all(val >= candles[i + j]["high"] for j in range(1, lookback + 1)):
            highs.append((i, val))
    return highs


def _swing_lows(candles: List[dict], lookback: int) -> List[Tuple[int, float]]:
    """Return (index, price) for confirmed swing lows."""
    lows = []
    for i in range(lookback, len(candles) - lookback):
        val = candles[i]["low"]
        if all(val <= candles[i - j]["low"] for j in range(1, lookback + 1)) and \
           all(val <= candles[i + j]["low"] for j in range(1, lookback + 1)):
            lows.append((i, val))
    return lows


# ─────────────────────────────────────────────────────────────────────────────
# Session levels
# ─────────────────────────────────────────────────────────────────────────────

def _session_range(candles: List[dict], open_hour_utc: int, close_hour_utc: int):
    """High / low of candles within the UTC session window."""
    session_highs, session_lows = [], []
    for c in candles:
        dt = datetime.fromtimestamp(c["time"], tz=timezone.utc)
        if open_hour_utc <= dt.hour < close_hour_utc:
            session_highs.append(c["high"])
            session_lows.append(c["low"])
    if session_highs:
        return max(session_highs), min(session_lows)
    return None, None


def _prev_day_range(candles: List[dict]):
    """Previous UTC-day high / low extracted from 5m candles."""
    now_day = datetime.fromtimestamp(time.time(), tz=timezone.utc).date()
    prev_high, prev_low = [], []
    for c in candles:
        dt   = datetime.fromtimestamp(c["time"], tz=timezone.utc)
        diff = (now_day - dt.date()).days
        if diff == 1:
            prev_high.append(c["high"])
            prev_low.append(c["low"])
    if prev_high:
        return max(prev_high), min(prev_low)
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# Level merging & ranking
# ─────────────────────────────────────────────────────────────────────────────

def _merge_levels(
    levels: List[dict],
    proximity_pct: float,
    max_levels: int,
) -> List[dict]:
    """
    Merge price levels within proximity_pct of each other.
    Strength = number of source levels merged together.
    """
    if not levels:
        return []
    levels = sorted(levels, key=lambda x: x["price"])
    merged = []
    for lv in levels:
        p = lv["price"]
        absorbed = False
        for m in merged:
            if abs(p - m["price"]) / max(m["price"], 1e-12) <= proximity_pct:
                # Merge into existing level — take weighted average
                m["price"] = (m["price"] * m["strength"] + p) / (m["strength"] + 1)
                m["strength"] += 1
                m["tags"] = list(set(m["tags"] + lv.get("tags", [])))
                absorbed = True
                break
        if not absorbed:
            merged.append({
                "price":    p,
                "kind":     lv["kind"],
                "tags":     lv.get("tags", [lv["kind"]]),
                "strength": 1,
            })

    # Sort by strength (most touches first), then keep top N
    merged.sort(key=lambda x: -x["strength"])
    return merged[:max_levels]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def detect_levels(candles: List[dict]) -> dict:
    """
    Detect all S/R levels and return a structured dict with:
      supports   : list of support levels (below current price)
      resistances: list of resistance levels (above current price)
      all_levels : merged and ranked list
      nearest_support    : closest support price
      nearest_resistance : closest resistance price
    """
    lb = config.SR_SWING_LOOKBACK
    raw: List[dict] = []

    # 1. Swing highs → resistance
    for _, price in _swing_highs(candles, lb):
        raw.append({"price": price, "kind": "resistance", "tags": ["swing_high"]})

    # 2. Swing lows → support
    for _, price in _swing_lows(candles, lb):
        raw.append({"price": price, "kind": "support",    "tags": ["swing_low"]})

    # 3. Previous day
    pd_high, pd_low = _prev_day_range(candles)
    if pd_high:
        raw.append({"price": pd_high, "kind": "resistance", "tags": ["prev_day_high"]})
    if pd_low:
        raw.append({"price": pd_low,  "kind": "support",    "tags": ["prev_day_low"]})

    # 4. Session: London 07-16 UTC
    sh, sl = _session_range(candles, *config.SESSION_LONDON)
    if sh:
        raw.append({"price": sh, "kind": "resistance", "tags": ["london_high"]})
    if sl:
        raw.append({"price": sl, "kind": "support",    "tags": ["london_low"]})

    # 5. Session: New York 13-22 UTC
    sh, sl = _session_range(candles, *config.SESSION_NEW_YORK)
    if sh:
        raw.append({"price": sh, "kind": "resistance", "tags": ["ny_high"]})
    if sl:
        raw.append({"price": sl, "kind": "support",    "tags": ["ny_low"]})

    # Merge
    all_levels = _merge_levels(raw, config.SR_PROXIMITY_PCT, config.SR_MAX_LEVELS)

    current_price = candles[-1]["close"] if candles else 0
    supports     = [lv for lv in all_levels if lv["price"] <= current_price]
    resistances  = [lv for lv in all_levels if lv["price"] >  current_price]

    nearest_support    = max(supports,    key=lambda x: x["price"])["price"] if supports    else None
    nearest_resistance = min(resistances, key=lambda x: x["price"])["price"] if resistances else None

    return {
        "supports":          sorted(supports,    key=lambda x: x["price"], reverse=True),
        "resistances":       sorted(resistances, key=lambda x: x["price"]),
        "all_levels":        all_levels,
        "nearest_support":   nearest_support,
        "nearest_resistance": nearest_resistance,
        "current_price":     current_price,
    }


def is_breakout_above(price: float, resistance: float, buffer_pct: float | None = None) -> bool:
    """Price has broken AND closed above resistance level."""
    buf = buffer_pct if buffer_pct is not None else config.SR_BREAKOUT_BUFFER
    return price >= resistance * (1 + buf)


def is_breakout_below(price: float, support: float, buffer_pct: float | None = None) -> bool:
    """Price has broken AND closed below support level."""
    buf = buffer_pct if buffer_pct is not None else config.SR_BREAKOUT_BUFFER
    return price <= support * (1 - buf)
