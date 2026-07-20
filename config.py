"""
Configuration — Professional Order Flow + Footprint Trading Bot
All parameters are configurable here. No magic numbers in strategy code.
"""
import os

# ─────────────────────────── Server ──────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 8000

# ─────────────────────────── Market Data ─────────────────────────────────────
DEFAULT_SYMBOL   = "BTCUSDT"
DEFAULT_INTERVAL = "5m"
INTERVALS        = ["1m", "3m", "5m", "15m", "1h", "4h", "1d"]
KLINE_LIMIT      = 300          # candles to fetch per request
ACTIVE_EXCHANGE  = "spot"       # "spot" | "futures"

# Pinned symbols always in the watchlist (scanner adds more dynamically)
PINNED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "APTUSDT", "INJUSDT", "SUIUSDT",
    "WIFUSDT", "BONKUSDT", "PEPEUSDT", "FETUSDT", "TIAUSDT",
]
SYMBOLS = list(PINNED_SYMBOLS)

# Binance REST endpoints — tried in order on failure
SPOT_ENDPOINTS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://data-api.binance.vision",
]
FUTURES_ENDPOINTS = [
    "https://fapi.binance.com",
]

# WebSocket stream base
WS_STREAM_BASE = "wss://stream.binance.com:9443/stream"

# ─────────────────────────── Credentials ─────────────────────────────────────
# Loaded from env or keys.json at runtime (see server.py load_keys())
BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY",    "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
KEYS_FILE          = os.path.join(os.path.dirname(__file__), "keys.json")

# ─────────────────────────── Trading Mode ────────────────────────────────────
# "signal_only" — show signals only, never place orders
# "live"        — place real orders via Binance API (requires API keys)
TRADING_MODE       = os.environ.get("TRADING_MODE", "signal_only")

# Order placement options (only active when TRADING_MODE == "live")
ORDER_TYPE         = "MARKET"   # MARKET | LIMIT
AUTO_SL_TP         = True       # automatically set SL/TP on entry
USE_REDUCE_ONLY    = True       # close orders use reduceOnly (futures)

# ─────────────────────────── Trend Filter ────────────────────────────────────
EMA_FAST_PERIOD = 20          # EMA20 — short-term trend
EMA_SLOW_PERIOD = 50          # EMA50 — medium-term trend

# ─────────────────────────── Footprint Engine ────────────────────────────────
IMBALANCE_RATIO        = 3.0  # buy_vol / sell_vol >= 3 → buy imbalance (and vice-versa)
MIN_STACKED_IMBALANCES = 3    # minimum consecutive imbalances required for signal
DELTA_THRESHOLD        = 500  # minimum |delta| to consider order flow significant (USDT)
ABSORPTION_MIN_VOL     = 200  # minimum volume at a level to consider absorption
FOOTPRINT_PRICE_LEVELS = 50   # max price buckets per candle for footprint display

# ─────────────────────────── Support & Resistance ────────────────────────────
SR_SWING_LOOKBACK  = 10       # bars each side to confirm swing point
SR_PROXIMITY_PCT   = 0.001    # 0.1% — merge S/R levels this close together
SR_MAX_LEVELS      = 12       # keep only the strongest N levels

# ─────────────────────────── Signal / Entry ──────────────────────────────────
VOLUME_MA_PERIOD   = 20       # candles for average volume comparison
VOLUME_MULTIPLIER  = 1.0      # volume must exceed average × this
SR_BREAKOUT_BUFFER = 0.0005   # 0.05% above/below level to confirm breakout

# ─────────────────────────── Risk Management ─────────────────────────────────
RISK_PER_TRADE_PCT   = 1.0    # % of account risked per trade
ACCOUNT_BALANCE_USDT = 10000  # default paper balance
TP_RATIO             = 2.0    # take-profit at 2× the risk (1:2 R:R minimum)
SL_BUFFER_PCT        = 0.002  # extra buffer below swing low / above swing high for SL
MAX_DAILY_LOSS_PCT   = 3.0    # halt trading if day loss exceeds this %
MAX_TRADES_PER_DAY   = 5      # hard cap on daily trades
DEFAULT_LEVERAGE     = 1      # leverage (1 = spot/no leverage)
AUTO_LEVERAGE        = False  # auto-scale leverage based on volatility

# ─────────────────────────── Position Management ─────────────────────────────
BREAKEVEN_TRIGGER_R  = 1.0    # move SL to entry after 1R profit
TRAILING_STOP_R      = 1.5    # activate trailing stop after 1.5R
TRAILING_STOP_PCT    = 0.005  # 0.5% trail distance
PARTIAL_PROFIT_R     = 1.0    # take 50% position off at 1R
PARTIAL_PROFIT_PCT   = 0.5    # fraction of position closed at partial profit

# ─────────────────────────── Trade Filters ───────────────────────────────────
# Session windows (UTC hours): [open_hour, close_hour]
SESSION_LONDON   = [7,  16]   # 07:00–16:00 UTC
SESSION_NEW_YORK = [13, 22]   # 13:00–22:00 UTC
TRADE_SESSIONS   = ["london", "new_york"]   # active sessions to trade

MAX_SPREAD_PCT   = 0.001      # 0.1% max spread to allow entry

# ─────────────────────────── Scanner ─────────────────────────────────────────
SCANNER_ENABLED            = True
SCANNER_MIN_VOLUME_USDT    = 5_000_000    # minimum 24h volume
SCANNER_VOLATILITY_MIN_PCT = 3.0
SCANNER_TOP_N              = 20
SCANNER_EXCLUDE_SLOW_CAPS  = {
    "ETH", "BNB", "SOL", "LTC", "ADA", "DOT", "AVAX",
    "LINK", "UNI", "AAVE", "MATIC", "OP", "ARB",
    "XRP", "ATOM", "ALGO", "FTM", "SAND", "MANA", "ICP",
}

# ─────────────────────────── Backtesting ─────────────────────────────────────
BACKTEST_INITIAL_BALANCE = 10_000
BACKTEST_COMMISSION_PCT  = 0.001   # 0.1% taker fee
BACKTEST_SLIPPAGE_PCT    = 0.0005  # 0.05% slippage assumption

# ─────────────────────────── Logging ─────────────────────────────────────────
TRADE_LOG_FILE    = "trades.json"
SIGNAL_LOG_FILE   = "signals.json"
LOG_LEVEL         = "INFO"
MAX_SIGNAL_HISTORY = 500
MAX_LOG_ENTRIES    = 1000

# ─────────────────────────── Dashboard ───────────────────────────────────────
REFRESH_SECONDS   = 10         # background analysis loop interval
