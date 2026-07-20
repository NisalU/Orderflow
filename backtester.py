"""
Backtester — Simulates the Order Flow + Footprint strategy on historical OHLCV data.

Since real tick-level trade data isn't available for history, the backtester
reconstructs synthetic footprint signals from kline taker buy/sell volumes,
then applies the full signal generation logic to each candle sequentially.

Results are emitted in real-time via a generator so the dashboard can
stream progress as a live animation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Generator, List, Optional

import config
import support_resistance as sr
import trend_detector as td


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic Footprint from OHLCV
# ─────────────────────────────────────────────────────────────────────────────

def _synthetic_fp(candle: dict) -> dict:
    """
    Reconstruct approximate footprint signals from taker buy/sell volume.
    Not as precise as real trade-level data, but gives useful signal approximations.
    """
    buy_vol  = candle.get("taker_buy_vol", 0) * candle["close"]
    sell_vol = candle.get("taker_sell_vol", 0) * candle["close"]
    total    = buy_vol + sell_vol

    delta    = buy_vol - sell_vol
    body_pct = (candle["close"] - candle["open"]) / max(candle["open"], 1e-12)

    # Imbalance estimation from volume ratio
    ratio = config.IMBALANCE_RATIO
    stacked_buy  = 0
    stacked_sell = 0
    if total > 0:
        buy_frac = buy_vol / total
        if buy_frac >= ratio / (ratio + 1):
            stacked_buy  = config.MIN_STACKED_IMBALANCES + (1 if buy_frac > 0.80 else 0)
        elif (1 - buy_frac) >= ratio / (ratio + 1):
            stacked_sell = config.MIN_STACKED_IMBALANCES + (1 if buy_frac < 0.20 else 0)

    # Absorption: large opposing vol but price didn't move much relative to body
    sell_absorption = sell_vol > config.ABSORPTION_MIN_VOL and delta > 0 and body_pct > 0.001
    buy_absorption  = buy_vol  > config.ABSORPTION_MIN_VOL and delta < 0 and body_pct < -0.001

    return {
        "delta":            delta,
        "buy_vol":          buy_vol,
        "sell_vol":         sell_vol,
        "max_stacked_buy":  stacked_buy,
        "max_stacked_sell": stacked_sell,
        "sell_absorption":  sell_absorption,
        "buy_absorption":   buy_absorption,
        "volume":           candle["volume"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Signal Check (self-contained for backtester)
# ─────────────────────────────────────────────────────────────────────────────

def _avg_volume(candles: List[dict], i: int, period: int = 20) -> float:
    start = max(0, i - period)
    vols  = [c["volume"] for c in candles[start:i]]
    return sum(vols) / len(vols) if vols else 0.0


def _check_signal(
    candles: List[dict],
    idx:     int,
) -> dict:
    """Evaluate entry signal at candle index `idx`."""
    candle = candles[idx]
    history = candles[:idx + 1]

    # Trend
    if len(history) < config.EMA_SLOW_PERIOD:
        return {"signal": "NONE", "fp": {}}
    trend = td.detect_trend(history)
    if not trend["signal_ok"]:
        return {"signal": "NONE", "trend": trend, "fp": {}}

    # S/R
    sr_data  = sr.detect_levels(history)
    fp       = _synthetic_fp(candle)
    avg_vol  = _avg_volume(candles, idx)
    bullish  = candle["close"] > candle["open"]
    bearish  = candle["close"] < candle["open"]

    sig = "NONE"
    if (
        trend["trend"] == "BULLISH"
        and sr_data["nearest_resistance"] is not None
        and sr.is_breakout_above(candle["close"], sr_data["nearest_resistance"])
        and fp["delta"]            >= config.DELTA_THRESHOLD
        and fp["max_stacked_buy"]  >= config.MIN_STACKED_IMBALANCES
        and not fp["sell_absorption"]
        and bullish
        and avg_vol > 0 and candle["volume"] >= avg_vol * config.VOLUME_MULTIPLIER
    ):
        sig = "BUY"
    elif (
        trend["trend"] == "BEARISH"
        and sr_data["nearest_support"] is not None
        and sr.is_breakout_below(candle["close"], sr_data["nearest_support"])
        and fp["delta"]             <= -config.DELTA_THRESHOLD
        and fp["max_stacked_sell"]  >= config.MIN_STACKED_IMBALANCES
        and not fp["buy_absorption"]
        and bearish
        and avg_vol > 0 and candle["volume"] >= avg_vol * config.VOLUME_MULTIPLIER
    ):
        sig = "SELL"

    return {
        "signal":  sig,
        "trend":   trend,
        "sr":      sr_data,
        "fp":      fp,
        "avg_vol": avg_vol,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Position Simulation
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BtPosition:
    direction:   str
    entry:       float
    sl:          float
    tp:          float
    tp2:         float
    size:        float         # units
    open_idx:    int
    partial_taken: bool = False
    breakeven_moved: bool = False
    trailing_sl: Optional[float] = None
    realized_pnl: float = 0.0
    remaining_size: float = 0.0

    def __post_init__(self):
        self.remaining_size = self.size

    def pnl_at(self, price: float, qty: float | None = None) -> float:
        q = qty if qty is not None else self.remaining_size
        if self.direction == "BUY":
            return (price - self.entry) * q
        return (self.entry - price) * q


def _open_position(
    candle: dict,
    signal: str,
    balance: float,
) -> Optional[BtPosition]:
    """Compute SL/TP and position size for a backtested entry."""
    entry = candle["close"]
    buf   = config.SL_BUFFER_PCT

    if signal == "BUY":
        sl = candle["low"] * (1 - buf)
    else:
        sl = candle["high"] * (1 + buf)

    risk = abs(entry - sl)
    if risk <= 0:
        return None

    tp  = entry + risk * config.TP_RATIO     if signal == "BUY" else entry - risk * config.TP_RATIO
    tp2 = entry + risk * config.TP_RATIO * 2 if signal == "BUY" else entry - risk * config.TP_RATIO * 2

    risk_amount = balance * config.RISK_PER_TRADE_PCT / 100
    size        = risk_amount / risk
    max_size    = balance / entry
    size        = min(size, max_size)

    return BtPosition(
        direction = signal,
        entry     = entry,
        sl        = sl,
        tp        = tp,
        tp2       = tp2,
        size      = round(size, 6),
        open_idx  = 0,
    )


@dataclass
class TradeRecord:
    idx_open:   int
    idx_close:  int
    time_open:  int
    time_close: int
    direction:  str
    entry:      float
    exit:       float
    sl:         float
    tp:         float
    pnl:        float
    exit_reason: str
    balance_after: float


# ─────────────────────────────────────────────────────────────────────────────
# Main Backtester
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(
    candles:          List[dict],
    initial_balance:  float = config.BACKTEST_INITIAL_BALANCE,
    commission_pct:   float = config.BACKTEST_COMMISSION_PCT,
    slippage_pct:     float = config.BACKTEST_SLIPPAGE_PCT,
    progress_every:   int   = 5,
) -> Generator[dict, None, None]:
    """
    Run backtest and yield progress events for live streaming to dashboard.

    Yields dicts of type:
      { "type": "progress", "pct": float, "candle_idx": int, "event": str | None }
      { "type": "trade",    ...trade fields... }
      { "type": "done",     ...summary... }
    """
    n       = len(candles)
    balance = initial_balance
    equity_curve: List[dict] = [{"time": candles[0]["time"], "equity": balance}]
    trades:  List[TradeRecord] = []
    pos:     Optional[BtPosition] = None
    max_trades_day = config.MAX_TRADES_PER_DAY
    day_trades = 0
    day_loss   = 0.0
    cur_day    = None

    commission  = commission_pct
    slippage    = slippage_pct

    for i in range(config.EMA_SLOW_PERIOD, n):
        candle = candles[i]

        # ── Day reset ──────────────────────────────────────────────────────
        from datetime import datetime, timezone
        dt  = datetime.fromtimestamp(candle["time"], tz=timezone.utc).date()
        if dt != cur_day:
            cur_day    = dt
            day_trades = 0
            day_loss   = 0.0

        hi = candle["high"]
        lo = candle["low"]

        # ── Manage open position ───────────────────────────────────────────
        if pos is not None:
            eff_sl = pos.trailing_sl if pos.trailing_sl else pos.sl

            # SL hit
            sl_hit = (pos.direction == "BUY"  and lo <= eff_sl) or \
                     (pos.direction == "SELL" and hi >= eff_sl)
            # TP1 hit
            tp_hit = (pos.direction == "BUY"  and hi >= pos.tp) or \
                     (pos.direction == "SELL" and lo <= pos.tp)
            # TP2 hit
            tp2_hit = (pos.direction == "BUY"  and hi >= pos.tp2) or \
                      (pos.direction == "SELL" and lo <= pos.tp2)

            exit_price  = None
            exit_reason = None

            if sl_hit:
                exit_price  = eff_sl * (1 - slippage) if pos.direction == "BUY" else eff_sl * (1 + slippage)
                exit_reason = "stop_loss"

            elif tp2_hit:
                exit_price  = pos.tp2
                exit_reason = "tp2"

            elif tp_hit and not pos.partial_taken:
                # Partial close at TP1
                qty  = pos.remaining_size * config.PARTIAL_PROFIT_PCT
                pnl  = pos.pnl_at(pos.tp, qty) * (1 - commission)
                balance += pnl
                day_loss = min(day_loss, -pnl)
                pos.realized_pnl    += pnl
                pos.remaining_size  -= qty
                pos.partial_taken    = True
                pos.sl               = pos.entry  # move to BE
                yield {"type": "partial", "idx": i, "pnl": round(pnl, 4),
                       "balance": round(balance, 4)}

                # Activate trailing
                pos.trailing_sl = pos.entry

            if exit_price is not None:
                raw_pnl  = pos.pnl_at(exit_price)
                fees     = abs(raw_pnl) * commission + exit_price * pos.remaining_size * commission
                net_pnl  = raw_pnl - fees + pos.realized_pnl
                balance  = max(0.01, balance + net_pnl)
                day_loss = min(day_loss, day_loss - net_pnl)

                rec = TradeRecord(
                    idx_open   = pos.open_idx,
                    idx_close  = i,
                    time_open  = candles[pos.open_idx]["time"],
                    time_close = candle["time"],
                    direction  = pos.direction,
                    entry      = pos.entry,
                    exit       = round(exit_price, 8),
                    sl         = pos.sl,
                    tp         = pos.tp,
                    pnl        = round(net_pnl, 4),
                    exit_reason = exit_reason,
                    balance_after = round(balance, 4),
                )
                trades.append(rec)
                pos = None
                equity_curve.append({"time": candle["time"], "equity": round(balance, 4)})

                yield {
                    "type":       "trade",
                    "idx":        i,
                    "direction":  rec.direction,
                    "entry":      rec.entry,
                    "exit":       rec.exit,
                    "pnl":        rec.pnl,
                    "reason":     rec.exit_reason,
                    "balance":    rec.balance_after,
                    "time_open":  rec.time_open,
                    "time_close": rec.time_close,
                }

        # ── Check for new signal (only when flat) ──────────────────────────
        if pos is None:
            # Daily guards
            max_loss_usdt = initial_balance * config.MAX_DAILY_LOSS_PCT / 100
            if day_trades >= max_trades_day or day_loss <= -max_loss_usdt:
                pass
            else:
                ev = _check_signal(candles, i)
                sig = ev.get("signal", "NONE")
                if sig in ("BUY", "SELL"):
                    new_pos = _open_position(candle, sig, balance)
                    if new_pos:
                        new_pos.open_idx = i
                        # Apply entry slippage
                        if sig == "BUY":
                            new_pos.entry *= (1 + slippage)
                        else:
                            new_pos.entry *= (1 - slippage)
                        pos = new_pos
                        day_trades += 1
                        yield {
                            "type":    "entry",
                            "idx":     i,
                            "signal":  sig,
                            "price":   round(new_pos.entry, 8),
                            "sl":      round(new_pos.sl, 8),
                            "tp":      round(new_pos.tp, 8),
                            "balance": round(balance, 4),
                        }

        # ── Progress heartbeat ─────────────────────────────────────────────
        if i % progress_every == 0:
            pct = round((i - config.EMA_SLOW_PERIOD) / max(n - config.EMA_SLOW_PERIOD, 1) * 100, 1)
            yield {"type": "progress", "pct": pct, "idx": i, "balance": round(balance, 4)}

    # ── Summary ────────────────────────────────────────────────────────────
    closed = [t for t in trades]
    pnls   = [t.pnl for t in closed]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    dd     = _max_drawdown(equity_curve)

    yield {
        "type":          "done",
        "total_trades":  len(closed),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl":     round(sum(pnls), 4),
        "total_return_pct": round((balance - initial_balance) / initial_balance * 100, 2),
        "max_drawdown_pct": round(dd, 2),
        "avg_win":       round(sum(wins) / len(wins), 4) if wins else 0,
        "avg_loss":      round(sum(losses) / len(losses), 4) if losses else 0,
        "profit_factor": round(-sum(wins) / sum(losses), 3) if losses and sum(losses) != 0 else 0,
        "final_balance": round(balance, 4),
        "equity_curve":  equity_curve,
        "trades":        [
            {
                "i_open":   t.idx_open,
                "i_close":  t.idx_close,
                "t_open":   t.time_open,
                "t_close":  t.time_close,
                "dir":      t.direction,
                "entry":    t.entry,
                "exit":     t.exit,
                "pnl":      t.pnl,
                "reason":   t.exit_reason,
                "bal":      t.balance_after,
            }
            for t in closed
        ],
    }


def _max_drawdown(equity_curve: List[dict]) -> float:
    """Maximum drawdown as % of peak equity."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]["equity"]
    max_dd = 0.0
    for pt in equity_curve:
        e = pt["equity"]
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd
