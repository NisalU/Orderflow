"""
Trade Manager — Manages open positions with:
  - Break-even stop movement
  - Trailing stop
  - Partial profit taking
  - Early exit on opposite footprint signal
  - Position state persistence (JSON)
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import config
from risk_manager import daily_risk, TradeParams


# ─────────────────────────────────────────────────────────────────────────────
# Position State
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Position:
    id:              str
    symbol:          str
    direction:       str        # "BUY" | "SELL"
    entry_price:     float
    stop_loss:       float
    take_profit:     float
    tp2:             float
    position_size:   float      # total (base asset)
    remaining_size:  float      # after partial closes
    risk_amount:     float
    leverage:        int
    open_time:       int        # unix seconds
    close_time:      Optional[int] = None
    status:          str = "OPEN"   # OPEN | PARTIAL | CLOSED
    realized_pnl:    float = 0.0
    partial_taken:   bool  = False
    breakeven_moved: bool  = False
    trailing_active: bool  = False
    trailing_sl:     float = 0.0
    exit_price:      Optional[float] = None
    exit_reason:     str = ""
    trend:           str = ""
    sr_level:        float = 0.0
    delta:           float = 0.0
    stacked_imb:     int   = 0
    volume:          float = 0.0

    def pnl_at(self, price: float) -> float:
        """Unrealized PnL in USDT at given price."""
        if self.direction == "BUY":
            return (price - self.entry_price) * self.remaining_size
        else:
            return (self.entry_price - price) * self.remaining_size

    def r_multiple(self, price: float) -> float:
        """Current P&L in multiples of initial risk."""
        risk = abs(self.entry_price - self.stop_loss) * self.position_size
        if risk == 0:
            return 0.0
        return self.pnl_at(price) / risk


# ─────────────────────────────────────────────────────────────────────────────
# Trade Manager
# ─────────────────────────────────────────────────────────────────────────────

class TradeManager:
    _POSITIONS_FILE = "positions.json"

    def __init__(self):
        self._lock      = threading.RLock()
        self._positions: Dict[str, Position] = {}
        self._counter   = 0
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self):
        try:
            data = {pid: asdict(p) for pid, p in self._positions.items()}
            with open(self._POSITIONS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[trade_manager] save error: {e}")

    def _load(self):
        if not os.path.exists(self._POSITIONS_FILE):
            return
        try:
            with open(self._POSITIONS_FILE) as f:
                data = json.load(f)
            for pid, d in data.items():
                self._positions[pid] = Position(**d)
        except Exception:
            pass

    # ── Open Position ─────────────────────────────────────────────────────────

    def open_position(self, params: TradeParams, signal: dict) -> Optional[Position]:
        with self._lock:
            allowed, reason = daily_risk.can_trade()
            if not allowed:
                print(f"[trade_manager] Trade blocked: {reason}")
                return None

            self._counter += 1
            pid = f"T{int(time.time())}_{self._counter}"
            pos = Position(
                id             = pid,
                symbol         = params.symbol,
                direction      = params.direction,
                entry_price    = params.entry_price,
                stop_loss      = params.stop_loss,
                take_profit    = params.take_profit,
                tp2            = params.tp2,
                position_size  = params.position_size,
                remaining_size = params.position_size,
                risk_amount    = params.risk_amount,
                leverage       = params.leverage,
                open_time      = int(time.time()),
                trend          = signal.get("trend", {}).get("direction", ""),
                sr_level       = (signal.get("sr", {}).get("nearest_resistance") or
                                  signal.get("sr", {}).get("nearest_support") or 0),
                delta          = signal.get("analytics", {}).get("delta", 0),
                stacked_imb    = max(
                    signal.get("analytics", {}).get("stacked_buy", 0),
                    signal.get("analytics", {}).get("stacked_sell", 0),
                ),
                volume         = signal.get("analytics", {}).get("candle_volume", 0),
            )
            self._positions[pid] = pos
            daily_risk.record_trade_open()
            self._save()
            print(f"[trade_manager] Opened {pos.direction} {pos.symbol} @ {pos.entry_price} SL={pos.stop_loss} TP={pos.take_profit}")
            return pos

    # ── Update / Manage Open Positions ────────────────────────────────────────

    def update(self, symbol: str, current_price: float, fp_signal: dict | None = None) -> List[dict]:
        """
        Call on every price tick / candle close.
        Returns list of events (partial_close, sl_hit, tp_hit, etc.)
        """
        events = []
        with self._lock:
            for pos in list(self._positions.values()):
                if pos.symbol != symbol or pos.status == "CLOSED":
                    continue
                ev = self._manage_position(pos, current_price, fp_signal)
                events.extend(ev)
            self._save()
        return events

    def _manage_position(
        self,
        pos:           Position,
        price:         float,
        fp_signal:     dict | None,
    ) -> List[dict]:
        events = []
        direction = pos.direction

        # ── Stop Loss Hit ─────────────────────────────────────────────────
        effective_sl = pos.trailing_sl if pos.trailing_active else pos.stop_loss
        sl_hit = (direction == "BUY"  and price <= effective_sl) or \
                 (direction == "SELL" and price >= effective_sl)
        if sl_hit:
            return self._close_position(pos, price, "stop_loss")

        # ── TP1 → partial profit + move to break-even ─────────────────────
        tp1_hit = (direction == "BUY"  and price >= pos.take_profit) or \
                  (direction == "SELL" and price <= pos.take_profit)
        if tp1_hit and not pos.partial_taken:
            close_qty   = pos.remaining_size * config.PARTIAL_PROFIT_PCT
            pnl         = self._calc_pnl(pos, price, close_qty)
            pos.remaining_size -= close_qty
            pos.realized_pnl   += pnl
            pos.partial_taken   = True
            pos.status          = "PARTIAL"
            daily_risk.record_pnl(pnl)
            events.append({"type": "partial_close", "id": pos.id, "price": price, "pnl": pnl})

        # ── Break-even ────────────────────────────────────────────────────
        if pos.partial_taken and not pos.breakeven_moved:
            be_trigger_r = config.BREAKEVEN_TRIGGER_R
            risk = abs(pos.entry_price - pos.stop_loss) * pos.position_size
            if risk > 0 and pos.pnl_at(price) >= risk * be_trigger_r:
                pos.stop_loss      = pos.entry_price
                pos.breakeven_moved = True
                events.append({"type": "breakeven", "id": pos.id, "new_sl": pos.entry_price})

        # ── Trailing Stop ─────────────────────────────────────────────────
        trail_r = config.TRAILING_STOP_R
        risk = abs(pos.entry_price - pos.stop_loss) * pos.position_size
        if risk > 0 and pos.pnl_at(price) >= risk * trail_r and not pos.trailing_active:
            pos.trailing_active = True
            pos.trailing_sl     = self._calc_trailing_sl(pos, price)
            events.append({"type": "trailing_activated", "id": pos.id, "trail_sl": pos.trailing_sl})
        elif pos.trailing_active:
            new_sl = self._calc_trailing_sl(pos, price)
            if direction == "BUY"  and new_sl > pos.trailing_sl:
                pos.trailing_sl = new_sl
            elif direction == "SELL" and new_sl < pos.trailing_sl:
                pos.trailing_sl = new_sl

        # ── TP2 Hit → full close ──────────────────────────────────────────
        tp2_hit = (direction == "BUY"  and price >= pos.tp2) or \
                  (direction == "SELL" and price <= pos.tp2)
        if tp2_hit:
            return self._close_position(pos, price, "take_profit_2")

        # ── Early Exit — opposite footprint ───────────────────────────────
        if fp_signal:
            opposite = self._detect_opposite_signal(pos, fp_signal)
            if opposite:
                return self._close_position(pos, price, "opposite_footprint")

        return events

    def _calc_trailing_sl(self, pos: Position, price: float) -> float:
        trail_dist = price * config.TRAILING_STOP_PCT
        if pos.direction == "BUY":
            return price - trail_dist
        else:
            return price + trail_dist

    def _calc_pnl(self, pos: Position, exit_price: float, qty: float) -> float:
        if pos.direction == "BUY":
            return (exit_price - pos.entry_price) * qty
        else:
            return (pos.entry_price - exit_price) * qty

    def _detect_opposite_signal(self, pos: Position, fp_signal: dict) -> bool:
        """Trigger early exit if footprint flips strongly against open position."""
        stacked_buy  = fp_signal.get("last_closed", {}).get("max_stacked_sell", 0)
        stacked_sell = fp_signal.get("last_closed", {}).get("max_stacked_sell", 0)
        thresh = config.MIN_STACKED_IMBALANCES
        if pos.direction == "BUY"  and stacked_sell >= thresh:
            return True
        if pos.direction == "SELL" and stacked_buy  >= thresh:
            return True
        return False

    def _close_position(
        self, pos: Position, price: float, reason: str
    ) -> List[dict]:
        pnl = self._calc_pnl(pos, price, pos.remaining_size) + pos.realized_pnl
        pos.exit_price   = price
        pos.exit_reason  = reason
        pos.close_time   = int(time.time())
        pos.status       = "CLOSED"
        pos.realized_pnl = pnl
        daily_risk.record_pnl(pnl)
        print(f"[trade_manager] Closed {pos.id} @ {price} reason={reason} PnL={pnl:.4f}")
        return [{"type": "closed", "id": pos.id, "price": price,
                 "reason": reason, "pnl": round(pnl, 4)}]

    # ── Public Read API ───────────────────────────────────────────────────────

    def get_open_positions(self) -> List[dict]:
        with self._lock:
            return [asdict(p) for p in self._positions.values()
                    if p.status in ("OPEN", "PARTIAL")]

    def get_all_positions(self, limit: int = 100) -> List[dict]:
        with self._lock:
            all_p = sorted(self._positions.values(), key=lambda x: -x.open_time)
            return [asdict(p) for p in all_p[:limit]]

    def get_stats(self) -> dict:
        with self._lock:
            closed = [p for p in self._positions.values() if p.status == "CLOSED"]
            if not closed:
                return {"trades": 0, "win_rate": 0, "total_pnl": 0,
                        "avg_pnl": 0, "best": 0, "worst": 0}
            pnls     = [p.realized_pnl for p in closed]
            wins     = [p for p in pnls if p > 0]
            return {
                "trades":    len(closed),
                "win_rate":  round(len(wins) / len(closed) * 100, 1),
                "total_pnl": round(sum(pnls), 4),
                "avg_pnl":   round(sum(pnls) / len(pnls), 4),
                "best":      round(max(pnls), 4),
                "worst":     round(min(pnls), 4),
            }


# Singleton
trade_manager = TradeManager()
