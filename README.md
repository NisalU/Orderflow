# Order Flow + Footprint Professional Trading Bot

## Quick Start

```bash
cd trading-bot
pip install -r requirements.txt
python server.py
```

Open **http://localhost:8000** in your browser.

---

## Architecture

```
trading-bot/
├── server.py              ← aiohttp web server + REST + WebSocket
├── config.py              ← ALL configurable parameters
├── data_feed.py           ← Binance REST (klines, tickers, trades)
├── stream.py              ← Binance aggTrade WebSocket + client manager
├── footprint_engine.py    ← Real-time footprint from live trades
├── trend_detector.py      ← EMA20/50 trend filter
├── support_resistance.py  ← Swing highs/lows, session H/L, prev day H/L
├── signal_generator.py    ← Combines all signals → BUY/SELL/NONE
├── risk_manager.py        ← Position sizing, SL/TP, daily limits
├── trade_manager.py       ← Open positions, BE, trailing, partials
├── trade_logger.py        ← JSON log of every trade + signal
├── backtester.py          ← Historical backtest with SSE streaming
├── scanner.py             ← Hot-coin scanner (manual trigger)
└── static/
    ├── index.html         ← Hacker terminal dashboard
    ├── style.css          ← Dark/green terminal theme
    └── app.js             ← Charts, footprint canvas, backtest UI
```

---

## Entry Rules

### BUY — ALL must be true
| Condition | Check |
|---|---|
| Bullish trend | EMA20 > EMA50 AND price > EMA20 |
| Breakout above resistance | Price closes above nearest resistance + 0.05% buffer |
| Positive delta | Delta ≥ DELTA_THRESHOLD (default 500 USDT) |
| Stacked buy imbalances | ≥ 3 consecutive buy-side imbalances |
| No sell absorption | No large sell volume absorbed at highs |
| Bullish candle | Close > Open |
| Volume surge | Volume ≥ 20-candle average |
| Session filter | London (07-16 UTC) or New York (13-22 UTC) |
| Spread check | Spread ≤ 0.1% |

### SELL — ALL must be true (symmetric)

---

## Configuration (`config.py`)

| Parameter | Default | Description |
|---|---|---|
| `EMA_FAST_PERIOD` | 20 | Fast EMA period |
| `EMA_SLOW_PERIOD` | 50 | Slow EMA period |
| `DELTA_THRESHOLD` | 500 | Min delta (USDT) |
| `IMBALANCE_RATIO` | 3.0 | Bid:ask ratio for imbalance |
| `MIN_STACKED_IMBALANCES` | 3 | Min stacked imbalances |
| `RISK_PER_TRADE_PCT` | 1.0 | Risk % per trade |
| `TP_RATIO` | 2.0 | Take-profit R:R ratio |
| `MAX_DAILY_LOSS_PCT` | 3.0 | Daily drawdown halt |
| `MAX_TRADES_PER_DAY` | 5 | Max trades per day |
| `BREAKEVEN_TRIGGER_R` | 1.0 | Move to BE at 1R profit |
| `TRAILING_STOP_R` | 1.5 | Activate trailing at 1.5R |
| `PARTIAL_PROFIT_PCT` | 0.5 | Close 50% at TP1 |
| `DEFAULT_LEVERAGE` | 1 | Leverage (1 = spot) |
| `AUTO_LEVERAGE` | False | Auto-scale by volatility |

---

## Dashboard Tabs

| Tab | What you see |
|---|---|
| **Dashboard** | Live signal, candlestick chart, EMA lines, S/R levels, delta bar, order flow metrics, open positions |
| **Footprint** | Real-time footprint canvas with price-level bid/ask volumes, imbalance highlighting, POC/VAH/VAL |
| **Scanner** | Hot-coin grid sorted by volatility score, click to subscribe |
| **Positions** | Trade history table with full stats, win rate, P&L, profit factor |
| **Backtest** | Configure + run backtest with live streaming equity curve and trade log |

---

## Optional: Binance API Keys

Set as environment variables to access private endpoints and higher rate limits:

```bash
export BINANCE_API_KEY=your_key
export BINANCE_API_SECRET=your_secret
python server.py
```

Without keys, all public market data endpoints work fine (read-only mode).

---

## Footprint Engine

The footprint engine subscribes to Binance's **aggTrade** WebSocket stream
and classifies every trade as buy or sell:

- `is_buyer_maker = False` → buy aggressor (price lifted) → **BUY volume**
- `is_buyer_maker = True`  → sell aggressor (price hit)   → **SELL volume**

Trades are bucketed by price level (0.02% increments) to build the footprint.
On every candle close, the engine computes:

- **Delta** = buy volume − sell volume
- **Stacked imbalances** = consecutive price levels where one side ≥ 3× the other
- **Absorption** = large opposing volume that fails to move price
- **Exhaustion** = price new high/low with diverging delta
- **POC / VAH / VAL** = Point of Control and 70% Value Area

---

## Backtester

Since real historical tick data is not available, the backtester reconstructs
synthetic footprint signals from kline taker-buy/sell volumes. This provides
a realistic approximation for strategy validation.

Results stream via SSE (Server-Sent Events) so the equity curve and trade log
animate in real-time as the backtest processes each candle.
