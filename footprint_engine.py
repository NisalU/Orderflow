"""
Footprint Engine — Real-time Order Flow Analysis
================================================
Builds a price-level footprint (bid/ask volume per price tick) from
Binance aggTrade WebSocket data and detects:
  - Buy / sell imbalances at each price level
  - Stacked imbalances (consecutive same-side imbalances)
  - Buy / sell absorption (large opposing volume absorbed without moving price)
  - Exhaustion (delta divergence vs price)

Architecture: FootprintEngine is a singleton updated by the WebSocket
listener. Thread-safe — multiple readers, one writer pattern.
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config


# ─────────────────────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PriceLevelData:
    """Volume at a single price bucket within a candle."""
    price:      float
    buy_vol:    float = 0.0
    sell_vol:   float = 0.0

    @property
    def delta(self) -> float:
        return self.buy_vol - self.sell_vol

    @property
    def total(self) -> float:
        return self.buy_vol + self.sell_vol

    @property
    def imbalance_side(self) -> Optional[str]:
        """Return 'buy', 'sell', or None based on imbalance ratio."""
        ratio = config.IMBALANCE_RATIO
        if self.sell_vol > 0 and self.buy_vol / self.sell_vol >= ratio:
            return "buy"
        if self.buy_vol > 0 and self.sell_vol / self.buy_vol >= ratio:
            return "sell"
        return None


@dataclass
class CandleFootprint:
    """Complete footprint for one candle."""
    symbol:        str
    interval:      str
    open_time:     int              # unix seconds
    close_time:    int
    open:          float = 0.0
    high:          float = 0.0
    low:           float = 0.0
    close:         float = 0.0
    volume:        float = 0.0
    buy_volume:    float = 0.0
    sell_volume:   float = 0.0
    levels:        Dict[float, PriceLevelData] = field(default_factory=dict)
    trade_count:   int   = 0
    is_closed:     bool  = False

    # ── Derived signals (computed once candle closes) ─────────────────────
    delta:                   float = 0.0
    cumulative_delta:        float = 0.0     # filled in by engine
    buy_imbalances:          int   = 0
    sell_imbalances:         int   = 0
    max_stacked_buy:         int   = 0
    max_stacked_sell:        int   = 0
    buy_absorption:          bool  = False
    sell_absorption:         bool  = False
    exhaustion:              bool  = False
    poc:                     float = 0.0     # price of highest volume
    vah:                     float = 0.0     # value area high (70% vol)
    val:                     float = 0.0     # value area low

    def sorted_levels(self) -> List[PriceLevelData]:
        return sorted(self.levels.values(), key=lambda x: x.price)

    def serialize(self) -> dict:
        """Compact dict for JSON serialization to dashboard."""
        lvls = [
            {
                "p":  round(lv.price, 8),
                "b":  round(lv.buy_vol, 4),
                "s":  round(lv.sell_vol, 4),
                "im": lv.imbalance_side,
            }
            for lv in self.sorted_levels()
        ]
        return {
            "ot":     self.open_time,
            "ct":     self.close_time,
            "o":      self.open,
            "h":      self.high,
            "l":      self.low,
            "c":      self.close,
            "vol":    round(self.volume, 4),
            "bvol":   round(self.buy_volume, 4),
            "svol":   round(self.sell_volume, 4),
            "delta":  round(self.delta, 4),
            "cd":     round(self.cumulative_delta, 4),
            "bi":     self.buy_imbalances,
            "si":     self.sell_imbalances,
            "msb":    self.max_stacked_buy,
            "mss":    self.max_stacked_sell,
            "ba":     self.buy_absorption,
            "sa":     self.sell_absorption,
            "ex":     self.exhaustion,
            "poc":    self.poc,
            "vah":    self.vah,
            "val":    self.val,
            "lvls":   lvls[-config.FOOTPRINT_PRICE_LEVELS:],  # limit for bandwidth
            "closed": self.is_closed,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Interval helpers
# ─────────────────────────────────────────────────────────────────────────────

_INTERVAL_SECONDS = {
    "1m":  60,   "3m":  180,  "5m":   300,
    "15m": 900,  "1h":  3600, "4h":  14400,
    "1d":  86400,
}

def _interval_to_seconds(interval: str) -> int:
    return _INTERVAL_SECONDS.get(interval, 300)

def _candle_open_time(ts: int, interval: str) -> int:
    s = _interval_to_seconds(interval)
    return (ts // s) * s


# ─────────────────────────────────────────────────────────────────────────────
# FootprintEngine
# ─────────────────────────────────────────────────────────────────────────────

class FootprintEngine:
    """
    Accumulates live trades into per-candle footprints.
    Thread-safe: trades written by WebSocket thread, read by HTTP handlers.
    """

    def __init__(self):
        self._lock = threading.RLock()
        # (symbol, interval) -> list[CandleFootprint] (history, newest last)
        self._candles:   Dict[tuple, List[CandleFootprint]] = defaultdict(list)
        # (symbol, interval) -> current open candle
        self._current:   Dict[tuple, CandleFootprint]       = {}
        # (symbol,) -> cumulative delta (session running total)
        self._cum_delta: Dict[str, float]                   = defaultdict(float)
        # Price bucket size (fraction of price for grouping trades)
        self._tick_pct   = 0.0002   # 0.02% buckets

    # ── Trade ingestion ───────────────────────────────────────────────────────

    def process_trade(
        self,
        symbol:   str,
        interval: str,
        price:    float,
        qty:      float,
        is_buyer_maker: bool,   # True = aggressor is seller (sell taker)
        ts:       int,          # unix milliseconds
    ) -> None:
        """
        Called for every aggTrade from WebSocket.
        is_buyer_maker=True  → the resting order was a buy → aggressor sold → SELL.
        is_buyer_maker=False → the resting order was a sell → aggressor bought → BUY.
        """
        ts_s      = ts // 1000
        open_time = _candle_open_time(ts_s, interval)
        close_time = open_time + _interval_to_seconds(interval) - 1
        key = (symbol, interval)

        # Volume in quote currency (USDT) for meaningful delta threshold comparison
        quote_qty = price * qty

        with self._lock:
            cur = self._current.get(key)

            # ── New candle? ────────────────────────────────────────────────
            if cur is None or cur.open_time != open_time:
                if cur is not None:
                    self._close_candle(cur, key)
                cur = CandleFootprint(
                    symbol    = symbol,
                    interval  = interval,
                    open_time = open_time,
                    close_time = close_time,
                    open      = price,
                    high      = price,
                    low       = price,
                    close     = price,
                )
                self._current[key] = cur

            # ── Update OHLCV ───────────────────────────────────────────────
            cur.high   = max(cur.high, price)
            cur.low    = min(cur.low,  price)
            cur.close  = price
            cur.volume += qty
            cur.trade_count += 1

            # ── Classify direction ─────────────────────────────────────────
            if is_buyer_maker:
                # Seller is aggressor
                cur.sell_volume += quote_qty
            else:
                # Buyer is aggressor
                cur.buy_volume  += quote_qty

            # ── Accumulate price-level footprint ──────────────────────────
            bucket = self._price_bucket(price)
            if bucket not in cur.levels:
                cur.levels[bucket] = PriceLevelData(price=bucket)
            lv = cur.levels[bucket]
            if is_buyer_maker:
                lv.sell_vol += quote_qty
            else:
                lv.buy_vol  += quote_qty

    # ── Price bucketing ───────────────────────────────────────────────────────

    def _price_bucket(self, price: float) -> float:
        """Round price to nearest tick bucket for footprint grouping."""
        if price <= 0:
            return price
        magnitude = 10 ** math.floor(math.log10(price))
        tick = magnitude * self._tick_pct * 10
        return round(round(price / tick) * tick, 8)

    # ── Candle finalization ───────────────────────────────────────────────────

    def _close_candle(self, candle: CandleFootprint, key: tuple) -> None:
        """Compute all derived metrics and move candle to history."""
        candle.is_closed = True
        candle.delta     = candle.buy_volume - candle.sell_volume

        # Cumulative delta
        self._cum_delta[key[0]] += candle.delta
        candle.cumulative_delta  = self._cum_delta[key[0]]

        # Imbalance analysis
        self._analyze_imbalances(candle)

        # Absorption detection
        self._analyze_absorption(candle)

        # Exhaustion detection
        self._analyze_exhaustion(candle)

        # Point of Control + Value Area
        self._analyze_value_area(candle)

        hist = self._candles[key]
        hist.append(candle)
        # Keep 500 closed candles
        if len(hist) > 500:
            hist.pop(0)

    # ── Imbalance analysis ────────────────────────────────────────────────────

    def _analyze_imbalances(self, candle: CandleFootprint) -> None:
        """
        Walk price levels from low → high.
        An imbalance exists when buy_vol / sell_vol ≥ IMBALANCE_RATIO (or vice-versa).
        Stacked = N consecutive same-side imbalances.
        """
        levels = candle.sorted_levels()
        buy_im  = 0
        sell_im = 0
        cur_buy_stack  = 0
        cur_sell_stack = 0
        max_buy_stack  = 0
        max_sell_stack = 0

        for lv in levels:
            side = lv.imbalance_side
            if side == "buy":
                buy_im += 1
                cur_buy_stack  += 1
                cur_sell_stack  = 0
                max_buy_stack   = max(max_buy_stack, cur_buy_stack)
            elif side == "sell":
                sell_im += 1
                cur_sell_stack += 1
                cur_buy_stack   = 0
                max_sell_stack  = max(max_sell_stack, cur_sell_stack)
            else:
                cur_buy_stack  = 0
                cur_sell_stack = 0

        candle.buy_imbalances   = buy_im
        candle.sell_imbalances  = sell_im
        candle.max_stacked_buy  = max_buy_stack
        candle.max_stacked_sell = max_sell_stack

    # ── Absorption analysis ───────────────────────────────────────────────────

    def _analyze_absorption(self, candle: CandleFootprint) -> None:
        """
        Sell absorption: large sell volume appears at a level but price did NOT
        fall — buyers absorbed the selling.  Detected when:
          - delta > 0 (buyers won overall)
          - Top sell-volume level has sell_vol > ABSORPTION_MIN_VOL
          - But candle closes near high (bullish candle body)

        Buy absorption: symmetric, candle closes near low.
        """
        if candle.volume < 1:
            return
        levels = candle.sorted_levels()
        if not levels:
            return

        body_pct = (candle.close - candle.open) / max(candle.open, 1e-12)
        rng = candle.high - candle.low or 1e-12

        max_sell_lv = max(levels, key=lambda x: x.sell_vol)
        max_buy_lv  = max(levels, key=lambda x: x.buy_vol)
        thresh = config.ABSORPTION_MIN_VOL

        # Sell absorption: big sell vol but price went up
        if (max_sell_lv.sell_vol > thresh
                and candle.delta > 0
                and body_pct > 0.001):
            candle.sell_absorption = True

        # Buy absorption: big buy vol but price went down
        if (max_buy_lv.buy_vol > thresh
                and candle.delta < 0
                and body_pct < -0.001):
            candle.buy_absorption = True

    # ── Exhaustion analysis ───────────────────────────────────────────────────

    def _analyze_exhaustion(self, candle: CandleFootprint) -> None:
        """
        Exhaustion: price makes new high/low but delta diverges.
        E.g., candle is highest high, but delta is negative → selling pressure
        building at highs despite price rising → exhaustion.
        """
        key = (candle.symbol, candle.interval)
        hist = self._candles.get(key, [])
        if len(hist) < 2:
            return
        prev = hist[-1]

        # Price new high but delta declining → buy exhaustion
        if candle.high > prev.high and candle.delta < prev.delta * 0.5:
            candle.exhaustion = True
        # Price new low but delta inclining → sell exhaustion
        elif candle.low < prev.low and candle.delta > prev.delta * 0.5:
            candle.exhaustion = True

    # ── Value Area ────────────────────────────────────────────────────────────

    def _analyze_value_area(self, candle: CandleFootprint) -> None:
        """Compute POC, VAH, VAL (70% of total volume)."""
        levels = candle.sorted_levels()
        if not levels:
            return
        # POC = price level with most total volume
        poc_lv = max(levels, key=lambda x: x.total)
        candle.poc = poc_lv.price

        # Value Area: 70% of volume around POC
        target = candle.volume * 0.70
        va_levels = [poc_lv]
        va_vol = poc_lv.total
        sorted_by_vol = sorted(levels, key=lambda x: x.total, reverse=True)
        for lv in sorted_by_vol[1:]:
            if va_vol >= target:
                break
            va_levels.append(lv)
            va_vol += lv.total
        prices = [lv.price for lv in va_levels]
        candle.vah = max(prices)
        candle.val = min(prices)

    # ── Public read API ───────────────────────────────────────────────────────

    def get_history(
        self,
        symbol:   str,
        interval: str,
        limit:    int = 100,
    ) -> List[dict]:
        """Return serialized closed candles + open candle (newest last)."""
        key = (symbol, interval)
        with self._lock:
            hist = list(self._candles.get(key, []))[-limit:]
            cur  = self._current.get(key)
        result = [c.serialize() for c in hist]
        if cur is not None:
            result.append(cur.serialize())
        return result

    def get_current(self, symbol: str, interval: str) -> Optional[dict]:
        key = (symbol, interval)
        with self._lock:
            cur = self._current.get(key)
        return cur.serialize() if cur else None

    def get_delta_series(self, symbol: str, interval: str, limit: int = 50) -> List[dict]:
        """Delta + cumulative delta series for charts."""
        key = (symbol, interval)
        with self._lock:
            hist = list(self._candles.get(key, []))[-limit:]
        return [
            {
                "time":   c.open_time,
                "delta":  round(c.delta, 2),
                "cd":     round(c.cumulative_delta, 2),
                "closed": c.is_closed,
            }
            for c in hist
        ]

    def get_signal_snapshot(self, symbol: str, interval: str) -> dict:
        """Compact signal summary for the strategy engine."""
        key = (symbol, interval)
        with self._lock:
            cur  = self._current.get(key)
            hist = list(self._candles.get(key, []))
        last_closed = hist[-1] if hist else None

        def _s(c):
            if c is None:
                return {}
            return {
                "delta":        c.delta,
                "buy_vol":      c.buy_volume,
                "sell_vol":     c.sell_volume,
                "max_stacked_buy":  c.max_stacked_buy,
                "max_stacked_sell": c.max_stacked_sell,
                "buy_absorption":   c.buy_absorption,
                "sell_absorption":  c.sell_absorption,
                "exhaustion":       c.exhaustion,
                "volume":           c.volume,
                "poc":              c.poc,
            }

        return {
            "current":     _s(cur),
            "last_closed": _s(last_closed),
        }


# ── Module-level singleton ────────────────────────────────────────────────────
footprint_engine = FootprintEngine()
