"""
Stream Manager — Manages WebSocket clients and Binance aggTrade feed.

Architecture:
  - BinanceTradeStream: connects to Binance aggTrade WebSocket, feeds trades
    into FootprintEngine. Reconnects automatically on disconnect.
  - ClientManager: tracks connected dashboard clients, routes broadcasts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import traceback
from collections import defaultdict
from typing import Callable, Dict, Optional, Set

import websockets

import config
from footprint_engine import footprint_engine

log = logging.getLogger("stream")


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard WebSocket Client
# ─────────────────────────────────────────────────────────────────────────────

class Client:
    def __init__(self, ws, symbol: str = None, interval: str = None):
        self.ws       = ws
        self.symbol   = symbol   or config.DEFAULT_SYMBOL
        self.interval = interval or config.DEFAULT_INTERVAL
        self.queue:   asyncio.Queue = asyncio.Queue(maxsize=200)

    def send(self, msg: dict):
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass

    def market(self):
        return (self.symbol, self.interval)


# ─────────────────────────────────────────────────────────────────────────────
# Client Manager
# ─────────────────────────────────────────────────────────────────────────────

class ClientManager:
    def __init__(self):
        self._lock    = threading.Lock()
        self.clients: Set[Client] = set()

    def add_client(self, c: Client):
        with self._lock:
            self.clients.add(c)

    def remove_client(self, c: Client):
        with self._lock:
            self.clients.discard(c)

    def broadcast(self, msg: dict, symbol: str | None = None):
        """Send to all clients, optionally filtered by symbol."""
        with self._lock:
            targets = list(self.clients)
        for c in targets:
            if symbol is None or c.symbol == symbol:
                c.send(msg)

    def retarget(self, client: Client, symbol: str, interval: str):
        client.symbol   = symbol
        client.interval = interval

    async def start(self):
        pass  # placeholder — BinanceTradeStream handles background tasks

    async def stop(self):
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Binance aggTrade WebSocket Stream
# ─────────────────────────────────────────────────────────────────────────────

class BinanceTradeStream:
    """
    Maintains persistent Binance aggTrade WebSocket connection(s).
    Subscriptions are updated dynamically as clients subscribe to symbols.
    Feeds trades into FootprintEngine.
    """
    _WS_BASE = "wss://stream.binance.com:9443/ws"

    def __init__(
        self,
        on_trade: Callable | None = None,
        manager:  ClientManager | None = None,
    ):
        self._on_trade  = on_trade
        self._manager   = manager
        self._subscribed: Set[str] = set()
        self._lock       = asyncio.Lock()
        self._ws         = None
        self._running    = False
        self._task       = None

    def start(self, loop: asyncio.AbstractEventLoop):
        self._running = True
        self._task    = loop.create_task(self._run())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def subscribe(self, symbols: list[str], interval: str):
        """Add symbols to the active stream."""
        async with self._lock:
            for sym in symbols:
                key = f"{sym.lower()}@aggTrade"
                if key not in self._subscribed:
                    self._subscribed.add(key)
            if self._ws:
                streams = list(self._subscribed)
                await self._send_subscribe(streams)

    async def _send_subscribe(self, streams: list):
        try:
            await self._ws.send(json.dumps({
                "method": "SUBSCRIBE",
                "params": streams,
                "id":     int(time.time()),
            }))
        except Exception:
            pass

    async def _run(self):
        """Auto-reconnecting WebSocket loop."""
        retry_delay = 1
        while self._running:
            try:
                uri = f"{self._WS_BASE}/stream"
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    retry_delay = 1

                    # Subscribe to initial symbols
                    async with self._lock:
                        initial = list(self._subscribed)
                    if initial:
                        await self._send_subscribe(initial)

                    log.info(f"[stream] Connected to Binance — {len(initial)} streams")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self._dispatch(msg)
                        except Exception:
                            pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning(f"[stream] WS error: {exc} — reconnecting in {retry_delay}s")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)

    async def _dispatch(self, msg: dict):
        """Route incoming Binance message to FootprintEngine."""
        # Combined stream wrapper
        data = msg.get("data", msg)
        e    = data.get("e", "")
        if e != "aggTrade":
            return

        symbol = data.get("s", "")
        price  = float(data.get("p", 0))
        qty    = float(data.get("q", 0))
        ts     = int(data.get("T", 0))          # trade time ms
        buyer_maker = data.get("m", False)       # True = sell aggressor

        # Feed into footprint for ALL active intervals clients are watching
        if self._manager:
            with self._manager._lock:
                intervals = {c.interval for c in self._manager.clients
                             if c.symbol == symbol}
            for ivl in intervals:
                footprint_engine.process_trade(symbol, ivl, price, qty, buyer_maker, ts)

            # Broadcast live price tick to subscribed clients
            tick = {
                "type":    "tick",
                "symbol":  symbol,
                "price":   price,
                "qty":     qty,
                "side":    "sell" if buyer_maker else "buy",
                "ts":      ts,
            }
            self._manager.broadcast(tick, symbol=symbol)

        if self._on_trade:
            await self._on_trade(symbol, price, qty, buyer_maker, ts)


# ─────────────────────────────────────────────────────────────────────────────
# Singletons
# ─────────────────────────────────────────────────────────────────────────────

manager = ClientManager()
