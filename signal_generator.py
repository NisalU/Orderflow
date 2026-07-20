"""
Signal Generator — Combines trend, S/R, and footprint into actionable entry signals.

BUY when ALL of:
  ✓ Bullish trend (EMA20 > EMA50, price > EMA20)
  ✓ Price breaks above nearest resistance
  ✓ Positive delta exceeds DELTA_THRESHOLD
  ✓ At least MIN_STACKED_IMBALANCES stacked BUY imbalances
  ✓ No sell absorption on last candle
  ✓ Candle closes bullish
  ✓ Volume above 20-candle average × VOLUME_MULTIPLIER

SELL when ALL of:
  ✓ Bearish trend
  ✓ Price breaks below nearest support
  ✓ Negative delta magnitude exceeds DELTA_THRESHOLD
  ✓ At least MIN_STACKED_IMBALANCES stacked SELL imbalances
  ✓ No buy absorption on last candle
  ✓ Candle closes bearish
  ✓ Volume above average
"""
from __future__ import annotations

import time
from typing import List, Optional

import config
import support_resistance as sr
import trend_detector as td
from footprint_engine import footprint_engine


# ─────────────────────────────────────────────────────────────────────────────
# Volume helper
# ─────────────────────────────────────────────────────────────────────────────

def _avg_volume(candles: List[dict], period: int = None) -> float:
    period = period or config.VOLUME_MA_PERIOD
    if len(candles) < period:
        return 0.0
    return sum(c["volume"] for c in candles[-period:]) / period


# ─────────────────────────────────────────────────────────────────────────────
# Session filter
# ─────────────────────────────────────────────────────────────────────────────

def _in_trading_session() -> bool:
    """Return True if current UTC time is inside any configured trading session."""
    hour = time.gmtime().tm_hour
    sessions = getattr(config, "TRADE_SESSIONS", ["london", "new_york"])
    if "london" in sessions:
        s, e = config.SESSION_LONDON
        if s <= hour < e:
            return True
    if "new_york" in sessions:
        s, e = config.SESSION_NEW_YORK
        if s <= hour < e:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Spread check
# ─────────────────────────────────────────────────────────────────────────────

def _spread_ok(ticker: Optional[dict]) -> bool:
    if not ticker:
        return True
    bid = ticker.get("bid", 0)
    ask = ticker.get("ask", 0)
    if bid <= 0 or ask <= 0:
        return True
    spread_pct = (ask - bid) / bid
    return spread_pct <= config.MAX_SPREAD_PCT


# ─────────────────────────────────────────────────────────────────────────────
# Core signal evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate(
    symbol:   str,
    interval: str,
    candles:  List[dict],
    ticker:   Optional[dict] = None,
) -> dict:
    """
    Run all signal filters and return a signal dict.

    Returns
    -------
    {
      "signal":    "BUY" | "SELL" | "NONE"
      "trend":     <trend dict>
      "sr":        <S/R dict>
      "footprint": <snapshot dict>
      "reasons":   list of strings (all conditions checked)
      "passed":    list of passed conditions
      "failed":    list of failed conditions
      "time":      unix timestamp
    }
    """
    now = int(time.time())
    result = {
        "signal":  "NONE",
        "symbol":  symbol,
        "interval": interval,
        "time":    now,
        "trend":   {},
        "sr":      {},
        "footprint": {},
        "reasons": [],
        "passed":  [],
        "failed":  [],
    }

    if len(candles) < config.EMA_SLOW_PERIOD:
        result["reasons"].append("Insufficient candle history")
        return result

    # ── 1. Trend Filter ────────────────────────────────────────────────────
    trend = td.detect_trend(candles)
    result["trend"] = {
        "direction":  trend["trend"],
        "ema_fast":   trend["ema_fast_last"],
        "ema_slow":   trend["ema_slow_last"],
        "price":      trend["price"],
    }

    # ── 2. S/R Levels ─────────────────────────────────────────────────────
    sr_data = sr.detect_levels(candles)
    result["sr"] = {
        "nearest_support":    sr_data["nearest_support"],
        "nearest_resistance": sr_data["nearest_resistance"],
        "levels":             sr_data["all_levels"],
    }

    # ── 3. Footprint snapshot ──────────────────────────────────────────────
    fp = footprint_engine.get_signal_snapshot(symbol, interval)
    result["footprint"] = fp

    # Use last closed candle's footprint for signal decisions
    fp_closed = fp.get("last_closed", {})
    fp_cur    = fp.get("current", {})

    last_candle = candles[-1]
    avg_vol     = _avg_volume(candles, config.VOLUME_MA_PERIOD)
    candle_vol  = last_candle["volume"]
    candle_bullish = last_candle["close"] > last_candle["open"]
    candle_bearish = last_candle["close"] < last_candle["open"]

    # Delta from footprint; fall back to taker imbalance from klines
    fp_delta  = fp_closed.get("delta", 0) if fp_closed else (
        last_candle.get("taker_buy_vol", 0) - last_candle.get("taker_sell_vol", 0)
    ) * last_candle["close"]

    stacked_buy  = fp_closed.get("max_stacked_buy",  0)
    stacked_sell = fp_closed.get("max_stacked_sell", 0)
    sell_absorb  = fp_closed.get("sell_absorption", False)
    buy_absorb   = fp_closed.get("buy_absorption",  False)

    # ── 4. Session and spread checks ──────────────────────────────────────
    session_ok = _in_trading_session()
    spread_ok  = _spread_ok(ticker)

    # ── 5. Evaluate BUY conditions ────────────────────────────────────────
    buy_checks = {
        "session_ok":          session_ok,
        "spread_ok":           spread_ok,
        "trend_bullish":       trend["trend"] == "BULLISH",
        "breaks_resistance":   (sr_data["nearest_resistance"] is not None
                                and sr.is_breakout_above(last_candle["close"],
                                                         sr_data["nearest_resistance"])),
        "delta_positive":      fp_delta >= config.DELTA_THRESHOLD,
        "stacked_buy_imb":     stacked_buy >= config.MIN_STACKED_IMBALANCES,
        "no_sell_absorption":  not sell_absorb,
        "candle_bullish":      candle_bullish,
        "volume_above_avg":    avg_vol > 0 and candle_vol >= avg_vol * config.VOLUME_MULTIPLIER,
    }

    # ── 6. Evaluate SELL conditions ───────────────────────────────────────
    sell_checks = {
        "session_ok":          session_ok,
        "spread_ok":           spread_ok,
        "trend_bearish":       trend["trend"] == "BEARISH",
        "breaks_support":      (sr_data["nearest_support"] is not None
                                and sr.is_breakout_below(last_candle["close"],
                                                         sr_data["nearest_support"])),
        "delta_negative":      fp_delta <= -config.DELTA_THRESHOLD,
        "stacked_sell_imb":    stacked_sell >= config.MIN_STACKED_IMBALANCES,
        "no_buy_absorption":   not buy_absorb,
        "candle_bearish":      candle_bearish,
        "volume_above_avg":    avg_vol > 0 and candle_vol >= avg_vol * config.VOLUME_MULTIPLIER,
    }

    buy_passed  = [k for k, v in buy_checks.items()  if v]
    buy_failed  = [k for k, v in buy_checks.items()  if not v]
    sell_passed = [k for k, v in sell_checks.items() if v]
    sell_failed = [k for k, v in sell_checks.items() if not v]

    # ── 7. Signal decision ────────────────────────────────────────────────
    if len(buy_failed) == 0:
        result["signal"]  = "BUY"
        result["passed"]  = buy_passed
        result["failed"]  = []
        result["reasons"] = [f"✓ {k}" for k in buy_passed]

    elif len(sell_failed) == 0:
        result["signal"]  = "SELL"
        result["passed"]  = sell_passed
        result["failed"]  = []
        result["reasons"] = [f"✓ {k}" for k in sell_passed]

    else:
        # Show the most-complete case (fewest failing)
        if len(buy_passed) >= len(sell_passed):
            result["passed"]  = buy_passed
            result["failed"]  = buy_failed
            result["reasons"] = (
                [f"✓ {k}" for k in buy_passed] +
                [f"✗ {k}" for k in buy_failed]
            )
        else:
            result["passed"]  = sell_passed
            result["failed"]  = sell_failed
            result["reasons"] = (
                [f"✓ {k}" for k in sell_passed] +
                [f"✗ {k}" for k in sell_failed]
            )

    # ── 8. Attach analytics ───────────────────────────────────────────────
    result["analytics"] = {
        "delta":        round(fp_delta, 2),
        "stacked_buy":  stacked_buy,
        "stacked_sell": stacked_sell,
        "avg_volume":   round(avg_vol, 4),
        "candle_volume": round(candle_vol, 4),
        "sell_absorption": sell_absorb,
        "buy_absorption":  buy_absorb,
    }

    return result
