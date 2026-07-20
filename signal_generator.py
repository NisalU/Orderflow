"""
Signal Generator — Multi-Timeframe Order Flow Strategy
=======================================================
Implements the top-down trading methodology:

  Daily  → Find Trend            (Layer 1)
  4H     → Find Support/Resistance (Layer 2)
  1H     → Detect Pattern          (Layer 3)
  Footprint → Confirm buyers/sellers (Layer 4)
  Enter trade
  Stop: Swing high/low
  Target: 2R (TP1) — 3R (TP2)

When MTF_ENABLED=True, all four layers must score ≥ MTF_MIN_CONFLUENCE
for a valid signal. When False, falls back to the legacy single-timeframe
logic (trend + S/R + footprint on the active interval).
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
# MTF-aware signal evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate(
    symbol:   str,
    interval: str,
    candles:  List[dict],
    ticker:   Optional[dict] = None,
    mtf_data: Optional[dict] = None,   # pre-fetched MTF result (avoids re-fetch)
) -> dict:
    """
    Run all signal filters and return a signal dict.

    When mtf_data is provided (fetched by server's background loop),
    the MTF confluence score is incorporated. Otherwise, falls back
    to the single-timeframe evaluation.

    Returns
    -------
    {
      "signal":    "BUY" | "SELL" | "NONE"
      "trend":     <trend dict>
      "sr":        <S/R dict>
      "footprint": <snapshot dict>
      "mtf":       <MTF confluence dict>  (new)
      "reasons":   list of strings (all conditions checked)
      "passed":    list of passed conditions
      "failed":    list of failed conditions
      "time":      unix timestamp
    }
    """
    now = int(time.time())
    result = {
        "signal":   "NONE",
        "symbol":   symbol,
        "interval": interval,
        "time":     now,
        "trend":    {},
        "sr":       {},
        "footprint": {},
        "mtf":      {},
        "reasons":  [],
        "passed":   [],
        "failed":   [],
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

    # ── 2. S/R Levels (on active interval) ────────────────────────────────
    sr_data = sr.detect_levels(candles)
    result["sr"] = {
        "nearest_support":    sr_data["nearest_support"],
        "nearest_resistance": sr_data["nearest_resistance"],
        "levels":             sr_data["all_levels"],
    }

    # ── 3. Footprint snapshot ──────────────────────────────────────────────
    fp = footprint_engine.get_signal_snapshot(symbol, interval)
    result["footprint"] = fp

    fp_closed = fp.get("last_closed", {})
    last_candle    = candles[-1]
    avg_vol        = _avg_volume(candles, config.VOLUME_MA_PERIOD)
    candle_vol     = last_candle["volume"]
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

    # ── 5. MTF Confluence ─────────────────────────────────────────────────
    mtf_buy_ok  = True
    mtf_sell_ok = True
    mtf_confluence = {}

    if config.MTF_ENABLED and mtf_data:
        from multi_timeframe import mtf_confluence as _conf
        daily   = mtf_data.get("daily", {})
        sr_4h   = mtf_data.get("4h_sr", {})
        pat_1h  = mtf_data.get("1h_pattern", {})

        conf_buy  = _conf(daily, sr_4h, pat_1h, fp, "BUY")
        conf_sell = _conf(daily, sr_4h, pat_1h, fp, "SELL")

        mtf_confluence = {
            "buy":  conf_buy,
            "sell": conf_sell,
            "bias": mtf_data.get("bias", "NEUTRAL"),
        }

        mtf_buy_ok  = conf_buy["score"]  >= config.MTF_MIN_CONFLUENCE
        mtf_sell_ok = conf_sell["score"] >= config.MTF_MIN_CONFLUENCE

    result["mtf"] = mtf_confluence

    # ── 6. Evaluate BUY conditions ────────────────────────────────────────
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
    if config.MTF_ENABLED and mtf_data:
        buy_checks["mtf_confluence"] = mtf_buy_ok

    # ── 7. Evaluate SELL conditions ───────────────────────────────────────
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
    if config.MTF_ENABLED and mtf_data:
        sell_checks["mtf_confluence"] = mtf_sell_ok

    buy_passed  = [k for k, v in buy_checks.items()  if v]
    buy_failed  = [k for k, v in buy_checks.items()  if not v]
    sell_passed = [k for k, v in sell_checks.items() if v]
    sell_failed = [k for k, v in sell_checks.items() if not v]

    # ── 8. Signal decision ────────────────────────────────────────────────
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

    # ── 9. Attach analytics ───────────────────────────────────────────────
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
