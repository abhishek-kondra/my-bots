#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║         BTC EMA 15 ENHANCED STRATEGY — FUTURES ALERT BOT           ║
║         BTC/USDT Perpetual | 5-Min | 1:3 RR | 6 Filters            ║
║         24/7 Crypto — Binance Futures Data + Execution             ║
╚══════════════════════════════════════════════════════════════════════╝

Strategy:
  - EMA 15 Crossover on 5M candles
  - HTF Trend filter on 15M EMA 50
  - 6 filters for signal quality
  - Slippage guard before every order
  - Auto order placement on Binance Futures

Data Source: Binance Futures (fapi.binance.com) — most stable, free
Execution:   Binance Futures API (HMAC signed)

Deploy:
  cd /root/new_bots
  pm2 start btc_bot.py --name btc-bot --interpreter python3
  pm2 save
"""

import os
import sys
import json
import time
import hmac
import hashlib
import signal
import logging
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlencode

import requests
import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════════════════════
# PATHS
# ══════════════════════════════════════════════════════════════════════

BOT_DIR    = Path(__file__).parent.resolve()
CONFIG_PATH = BOT_DIR / "config.json"
STATE_PATH  = BOT_DIR / "bot_state.json"
LOG_PATH    = BOT_DIR / "btc_bot.log"


# ══════════════════════════════════════════════════════════════════════
# DEFAULT CONFIG
# ══════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "telegram": {
        "bot_token": "8134933083:AAE7mUZsskolHhwFcUZ2gpUq8_906niWFIs",
        "chat_id": "7396015146"
    },
    "binance": {
        "api_key": "HqmDjVSziGuHTeuoYSWDWeRtGcow4HOVA3SdtYWmMtHCkFk5RhJeAZG9NwzL6NEc",
        "api_secret": "X1B2HtpC4Yn1VmJXeyYVFBXZSsWPCb6MWuXzKnoC9jh69PSUcuYS9uMEiCki5We7",
        "testnet": True,
        "leverage": 1,
        "margin_type": "ISOLATED"
    },
    "symbols": ["BTCUSDT"],
    "strategy": {
        "timeframe": "5m",
        "htf_timeframe": "15m",
        "ema_period": 15,
        "htf_ema_period": 50,
        "atr_period": 14,
        "rr_ratio": 3.0,
        "risk_per_trade_pct": 1.0,
        "account_balance": 1000.0,
        "min_risk_dollars": 50
    },
    "filters": {
        "session_filter_enabled": False,
        "session_start_utc": 7,
        "session_end_utc": 21,
        "min_candle_body": 100,
        "min_atr": 200,
        "max_spread_pct": 0.05,
        "cooldown_candles": 2
    },
    "risk_management": {
        "max_daily_losses": 3,
        "max_daily_loss_pct": 6.0,
        "trailing_stop_at_r": 2.0,
        "max_signals_per_symbol_per_day": 5
    },
    "bot": {
        "check_interval_seconds": 20,
        "candle_history_count": 100,
        "send_rejected_signals": False,
        "health_check_interval_minutes": 60,
        "max_slippage_pct": 0.1,
        "auto_trade": False
    }
}


def deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found. Creating template...")
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        sys.exit(1)

    with open(CONFIG_PATH, "r") as f:
        user_config = json.load(f)

    cfg = deep_merge(DEFAULT_CONFIG, user_config)

    errors = []
    if not cfg["telegram"]["bot_token"] or str(cfg["telegram"]["bot_token"]).startswith("PASTE"):
        errors.append("telegram.bot_token not set")
    if not cfg["telegram"]["chat_id"] or str(cfg["telegram"]["chat_id"]).startswith("PASTE"):
        errors.append("telegram.chat_id not set")

    if errors:
        print("CONFIG ERRORS:")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)

    return cfg


CFG = load_config()


def cfg_get(section: str, key: str, default=None):
    try:
        return CFG[section][key]
    except (KeyError, TypeError):
        if default is not None:
            return default
        try:
            return DEFAULT_CONFIG[section][key]
        except (KeyError, TypeError):
            return None


# ══════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════

log = logging.getLogger("BTCBot")
log.setLevel(logging.INFO)

fh = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(fh)

ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(ch)


# ══════════════════════════════════════════════════════════════════════
# GRACEFUL SHUTDOWN
# ══════════════════════════════════════════════════════════════════════

shutdown_event = threading.Event()

def handle_shutdown(signum, frame):
    log.info(f"Signal {signum} — shutting down gracefully...")
    shutdown_event.set()

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT,  handle_shutdown)


# ══════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════

class Telegram:
    def __init__(self, token: str, chat_id):
        self.token = token
        self.url   = f"https://api.telegram.org/bot{token}/sendMessage"
        # Support single chat_id (str) or multiple (list)
        if isinstance(chat_id, list):
            self.chat_ids = [str(c) for c in chat_id if c]
        else:
            self.chat_ids = [str(chat_id)]
        self.chat_id = self.chat_ids[0]  # backward compat

    def send(self, message: str, retries: int = 3) -> bool:
        results = []
        for cid in self.chat_ids:
            if len(message) > 4000:
                for chunk in [message[i:i+4000] for i in range(0, len(message), 4000)]:
                    self._post(chunk, retries, cid)
                results.append(True)
            else:
                results.append(self._post(message, retries, cid))
        return all(results)

    def _post(self, message: str, retries: int, chat_id: str = None) -> bool:
        cid = chat_id or self.chat_id
        payload = {
            "chat_id": cid,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(self.url, json=payload, timeout=15)
                if r.status_code == 200:
                    return True
                if r.status_code == 400 and "parse" in r.text.lower():
                    payload["parse_mode"] = None
            except Exception as e:
                log.warning(f"Telegram attempt {attempt}: {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
        return False

    def test_connection(self) -> bool:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{self.token}/getMe", timeout=10
            )
            if r.status_code == 200:
                name = r.json().get("result", {}).get("username", "unknown")
                log.info(f"Telegram connected: @{name}")
                return True
        except Exception as e:
            log.error(f"Telegram test failed: {e}")
        return False


# Build recipient list — primary + any additional chat IDs
_tg_ids = [CFG["telegram"]["chat_id"]]
if CFG["telegram"].get("chat_id_2"):
    _tg_ids.append(CFG["telegram"]["chat_id_2"])
telegram = Telegram(CFG["telegram"]["bot_token"], _tg_ids)


# ══════════════════════════════════════════════════════════════════════
# BINANCE FUTURES DATA SOURCE
# Endpoint: fapi.binance.com — most stable crypto API available
# Free, no key needed for market data
# Returns exactly 12 columns, always reliable
# ══════════════════════════════════════════════════════════════════════

class BinanceFuturesData:
    BASE_URL = "https://fapi.binance.com"

    def __init__(self):
        self.call_count = 0
        log.info("Data source: Binance Futures (fapi.binance.com)")
        log.info("✅ Free market data — no API key needed")

    def fetch_candles(self, symbol: str, interval: str, limit: int = 100) -> pd.DataFrame:
        """
        Fetch OHLCV from Binance Futures.
        symbol:   'BTCUSDT'
        interval: '5m', '15m', '1h' etc
        """
        self.call_count += 1
        # Convert config format if needed
        interval_map = {
            "5min": "5m", "15min": "15m", "1min": "1m",
            "30min": "30m", "1h": "1h", "4h": "4h", "1d": "1d"
        }
        tf = interval_map.get(interval, interval)

        url = f"{self.BASE_URL}/fapi/v1/klines"
        params = {
            "symbol": symbol.replace("/", "").upper(),
            "interval": tf,
            "limit": min(limit, 1500)
        }

        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            raise RuntimeError(f"Binance Futures: empty data for {symbol}")

        # Binance always returns exactly 12 columns
        df = pd.DataFrame(data, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])

        df["open_time"] = pd.to_datetime(
            df["open_time"].astype(float), unit="ms", utc=True
        )
        df.set_index("open_time", inplace=True)
        df = df.sort_index()

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df.rename(columns={
            "open": "Open", "high": "High",
            "low": "Low", "close": "Close",
            "volume": "Volume"
        }, inplace=True)

        df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)

        if df.empty:
            raise RuntimeError(f"Binance Futures: no valid data for {symbol}")

        return df

    def fetch_live_price(self, symbol: str) -> float:
        """Get real-time mark price for slippage check."""
        url = f"{self.BASE_URL}/fapi/v1/ticker/price"
        resp = requests.get(
            url, params={"symbol": symbol.replace("/", "").upper()}, timeout=10
        )
        resp.raise_for_status()
        return float(resp.json()["price"])

    @property
    def name(self):
        return "Binance Futures"

    @property
    def daily_calls(self):
        return self.call_count


# ══════════════════════════════════════════════════════════════════════
# DATA MANAGER
# ══════════════════════════════════════════════════════════════════════

class DataManager:
    def __init__(self):
        self.source = BinanceFuturesData()
        self.consecutive_failures = 0

    def fetch(self, symbol: str, interval: str, count: int) -> pd.DataFrame:
        try:
            df = self.source.fetch_candles(symbol, interval, count)
            self._validate(df, symbol)
            self.consecutive_failures = 0
            return df
        except Exception as e:
            self.consecutive_failures += 1
            log.warning(f"Data fetch failed ({self.consecutive_failures}x): {e}")
            if self.consecutive_failures >= 5:
                telegram.send(
                    f"⚠️ <b>BTC Bot Data Error</b>\n"
                    f"Binance Futures fetch failed {self.consecutive_failures}x\n"
                    f"Error: {str(e)[:200]}"
                )
            raise

    def fetch_live_price(self, symbol: str) -> float:
        try:
            return self.source.fetch_live_price(symbol)
        except Exception as e:
            log.warning(f"Live price fetch failed: {e}")
            return None

    def _validate(self, df: pd.DataFrame, symbol: str):
        if df is None or df.empty:
            raise ValueError(f"Empty data for {symbol}")
        if len(df) < 10:
            raise ValueError(f"{symbol}: only {len(df)} rows, need 20+")
        last_close = df["Close"].iloc[-1]
        if last_close < 1000 or last_close > 500000:
            raise ValueError(f"Suspicious BTC price: ${last_close:,.0f}")


# ══════════════════════════════════════════════════════════════════════
# BINANCE FUTURES EXECUTION ENGINE
# Handles signed API calls for placing real futures orders
# ══════════════════════════════════════════════════════════════════════

class BinanceFuturesTrader:
    LIVE_URL    = "https://fapi.binance.com"
    TESTNET_URL = "https://testnet.binancefuture.com"

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.testnet    = testnet
        self.base_url   = self.TESTNET_URL if testnet else self.LIVE_URL
        self.session    = requests.Session()
        self.session.headers.update({
            "X-MBX-APIKEY": api_key,
            "Content-Type": "application/x-www-form-urlencoded"
        })

        mode = "TESTNET" if testnet else "LIVE"
        log.info(f"Binance Futures Trader: {mode} mode")

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _get(self, endpoint: str, params: dict = None) -> dict:
        params = params or {}
        params = self._sign(params)
        r = self.session.get(f"{self.base_url}{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, endpoint: str, params: dict) -> dict:
        params = self._sign(params)
        r = self.session.post(
            f"{self.base_url}{endpoint}",
            data=urlencode(params),
            timeout=10
        )
        if r.status_code != 200:
            raise RuntimeError(f"Binance API error {r.status_code}: {r.text}")
        return r.json()

    def is_connected(self) -> bool:
        """Test API key validity."""
        try:
            self._get("/fapi/v2/account")
            return True
        except Exception as e:
            log.error(f"Binance API connection failed: {e}")
            return False

    def get_balance(self) -> float:
        """Get available USDT balance."""
        try:
            data = self._get("/fapi/v2/account")
            for asset in data.get("assets", []):
                if asset["asset"] == "USDT":
                    return float(asset["availableBalance"])
            return 0.0
        except Exception as e:
            log.error(f"Balance fetch failed: {e}")
            return 0.0

    def set_leverage(self, symbol: str, leverage: int):
        """Set leverage for symbol."""
        try:
            self._post("/fapi/v1/leverage", {
                "symbol": symbol,
                "leverage": leverage
            })
            log.info(f"Leverage set: {symbol} {leverage}x")
        except Exception as e:
            log.warning(f"Leverage set failed: {e}")

    def set_margin_type(self, symbol: str, margin_type: str):
        """Set ISOLATED or CROSSED margin."""
        try:
            self._post("/fapi/v1/marginType", {
                "symbol": symbol,
                "marginType": margin_type.upper()
            })
            log.info(f"Margin type set: {symbol} {margin_type}")
        except Exception as e:
            # Already set error is OK to ignore
            if "No need to change" not in str(e):
                log.warning(f"Margin type set failed: {e}")

    def place_order(self, symbol: str, side: str, quantity: float,
                    sl_price: float, tp_price: float) -> dict:
        """
        Place market order with SL and TP on Binance Futures.
        side: 'BUY' for LONG, 'SELL' for SHORT
        """
        symbol = symbol.replace("/", "").upper()
        result = {}

        try:
            # Main market order
            order = self._post("/fapi/v1/order", {
                "symbol":       symbol,
                "side":         side,
                "type":         "MARKET",
                "quantity":     f"{quantity:.3f}",
                "reduceOnly":   "false"
            })
            result["entry_order"] = order
            order_id = order.get("orderId")
            log.info(f"✅ Entry order placed: {order_id} | {side} {quantity} {symbol}")

            # Stop Loss order
            sl_side = "SELL" if side == "BUY" else "BUY"
            sl_order = self._post("/fapi/v1/order", {
                "symbol":          symbol,
                "side":            sl_side,
                "type":            "STOP_MARKET",
                "stopPrice":       f"{sl_price:.2f}",
                "closePosition":   "true",
                "timeInForce":     "GTE_GTC"
            })
            result["sl_order"] = sl_order
            log.info(f"✅ SL order placed @ ${sl_price:,.2f}")

            # Take Profit order
            tp_order = self._post("/fapi/v1/order", {
                "symbol":          symbol,
                "side":            sl_side,
                "type":            "TAKE_PROFIT_MARKET",
                "stopPrice":       f"{tp_price:.2f}",
                "closePosition":   "true",
                "timeInForce":     "GTE_GTC"
            })
            result["tp_order"] = tp_order
            log.info(f"✅ TP order placed @ ${tp_price:,.2f}")

            result["success"] = True

        except Exception as e:
            log.error(f"Order placement failed: {e}")
            result["success"] = False
            result["error"] = str(e)

        return result

    def get_position(self, symbol: str) -> dict:
        """Get current open position for symbol."""
        try:
            data = self._get("/fapi/v2/positionRisk",
                             {"symbol": symbol.replace("/", "").upper()})
            for pos in data:
                if float(pos.get("positionAmt", 0)) != 0:
                    return pos
            return {}
        except Exception as e:
            log.error(f"Position fetch failed: {e}")
            return {}


# ══════════════════════════════════════════════════════════════════════
# SLIPPAGE GUARD
# ══════════════════════════════════════════════════════════════════════

def check_slippage(data_mgr: DataManager, symbol: str,
                   signal_price: float) -> tuple:
    max_slip = cfg_get("bot", "max_slippage_pct", 0.1)
    live_price = data_mgr.fetch_live_price(symbol)

    if live_price is None:
        log.warning("Slippage check skipped — no live price")
        return True, signal_price, 0.0

    slip_pct = abs(live_price - signal_price) / signal_price * 100

    if slip_pct > max_slip:
        log.warning(
            f"⚠️ Slippage too high: signal=${signal_price:,.2f} "
            f"live=${live_price:,.2f} diff={slip_pct:.3f}%"
        )
        return False, live_price, slip_pct

    log.info(
        f"✅ Slippage OK: ${live_price:,.2f} diff={slip_pct:.3f}%"
    )
    return True, live_price, slip_pct


# ══════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low = df["High"], df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def estimate_spread_pct(df: pd.DataFrame) -> float:
    if len(df) < 3:
        return 0.03
    recent = df.tail(3)
    avg_range = (recent["High"] - recent["Low"]).mean()
    price = recent["Close"].iloc[-1]
    if price <= 0:
        return 0.03
    return round(max((avg_range * 0.12 / price) * 100, 0.01), 4)


# ══════════════════════════════════════════════════════════════════════
# BOT STATE
# ══════════════════════════════════════════════════════════════════════

class BotState:
    def __init__(self):
        self.data = {
            "last_candle_time": {},
            "cooldown_remaining": {},
            "daily_losses": 0,
            "daily_loss_date": None,
            "trades_today": [],
            "signals_per_symbol_today": {},
            "total_trades": 0,
            "total_wins": 0,
            "total_losses": 0,
            "last_health_check": None,
            "bot_start_time": datetime.now(timezone.utc).isoformat(),
            "signals_sent": 0,
            "signals_rejected": 0,
            "signals_slippage_rejected": 0,
            "last_signal_hash": None,
            "orders_placed": 0,
            "orders_failed": 0
        }
        self._load()

    def _load(self):
        if STATE_PATH.exists():
            try:
                with open(STATE_PATH, "r") as f:
                    saved = json.load(f)
                for k in saved:
                    if k in self.data:
                        self.data[k] = saved[k]
                log.info("State restored from disk.")
            except Exception as e:
                log.warning(f"State load error: {e}")

    def save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump(self.data, f, indent=2, default=str)
        except Exception as e:
            log.error(f"State save error: {e}")

    def reset_daily(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.data["daily_loss_date"] != today:
            self.data["daily_losses"] = 0
            self.data["daily_loss_date"] = today
            self.data["trades_today"] = []
            self.data["signals_per_symbol_today"] = {}
            log.info(f"Daily counters reset — {today}")

    def tick_cooldown(self, symbol):
        cd = self.data["cooldown_remaining"]
        if not isinstance(cd, dict):
            self.data["cooldown_remaining"] = {}
        cur = self.data["cooldown_remaining"].get(symbol, 0)
        if cur > 0:
            self.data["cooldown_remaining"][symbol] = cur - 1

    def get_cooldown(self, symbol):
        cd = self.data["cooldown_remaining"]
        return cd.get(symbol, 0) if isinstance(cd, dict) else 0

    def get_last_candle_time(self, symbol):
        lct = self.data["last_candle_time"]
        return lct.get(symbol) if isinstance(lct, dict) else None

    def set_last_candle_time(self, symbol, t):
        if not isinstance(self.data["last_candle_time"], dict):
            self.data["last_candle_time"] = {}
        self.data["last_candle_time"][symbol] = t

    def get_symbol_signals_today(self, symbol):
        sst = self.data.get("signals_per_symbol_today", {})
        return sst.get(symbol, 0) if isinstance(sst, dict) else 0

    def increment_symbol_signals(self, symbol):
        if not isinstance(self.data.get("signals_per_symbol_today"), dict):
            self.data["signals_per_symbol_today"] = {}
        cur = self.data["signals_per_symbol_today"].get(symbol, 0)
        self.data["signals_per_symbol_today"][symbol] = cur + 1

    def is_duplicate_signal(self, sig_hash):
        if self.data.get("last_signal_hash") == sig_hash:
            return True
        self.data["last_signal_hash"] = sig_hash
        return False

    @property
    def daily_limit_hit(self):
        return self.data["daily_losses"] >= cfg_get("risk_management", "max_daily_losses", 3)

    @property
    def win_rate(self):
        total = self.data["total_wins"] + self.data["total_losses"]
        return (self.data["total_wins"] / total * 100) if total > 0 else 0.0


# ══════════════════════════════════════════════════════════════════════
# SIGNAL DETECTION — EMA 15 Crossover
# ══════════════════════════════════════════════════════════════════════

def detect_signal(df: pd.DataFrame, symbol: str) -> dict | None:
    ema_period = cfg_get("strategy", "ema_period", 15)
    if len(df) < ema_period + 3:
        return None

    ema  = calc_ema(df["Close"], ema_period)
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    prev_ema = ema.iloc[-2]
    curr_ema = ema.iloc[-1]

    direction = None
    if prev["Close"] <= prev_ema and curr["Close"] > curr_ema:
        direction = "LONG"
    elif prev["Close"] >= prev_ema and curr["Close"] < curr_ema:
        direction = "SHORT"

    if direction is None:
        return None

    entry = curr["Close"]
    sl    = curr["Low"]  if direction == "LONG" else curr["High"]
    risk  = entry - sl   if direction == "LONG" else sl - entry

    if risk < cfg_get("strategy", "min_risk_dollars", 50):
        return None

    rr = cfg_get("strategy", "rr_ratio", 3.0)
    tp = entry + (risk * rr) if direction == "LONG" else entry - (risk * rr)

    return {
        "symbol":    symbol,
        "direction": direction,
        "entry":     round(entry, 2),
        "sl":        round(sl, 2),
        "tp":        round(tp, 2),
        "risk":      round(risk, 2),
        "reward":    round(risk * rr, 2),
        "ema":       round(curr_ema, 2),
        "time":      str(curr.name),
    }


# ══════════════════════════════════════════════════════════════════════
# FILTER ENGINE
# ══════════════════════════════════════════════════════════════════════

class FilterEngine:
    def __init__(self, state: BotState):
        self.state = state

    def run_all(self, signal, candle, candle_time, df_5m, df_15m):
        symbol   = signal["symbol"]
        atr      = calc_atr(df_5m, cfg_get("strategy", "atr_period", 14)).iloc[-1]
        spread   = estimate_spread_pct(df_5m)
        results  = []

        if cfg_get("filters", "session_filter_enabled", False):
            results.append(self._f1_session(candle_time))
        else:
            results.append((True, "✅ Session: 24/7 crypto"))

        results.append(self._f2_htf(df_15m, signal["direction"]))
        results.append(self._f3_body(candle))
        results.append(self._f4_atr(atr))
        results.append(self._f5_cooldown(symbol))
        results.append(self._f6_spread(spread))
        results.append(self._risk_daily())
        results.append(self._risk_symbol(symbol))
        return results

    def _f1_session(self, t):
        h = t.hour
        s = cfg_get("filters", "session_start_utc", 7)
        e = cfg_get("filters", "session_end_utc", 21)
        if s <= h < e:
            return (True,  f"✅ Session: {h:02d}:00 UTC")
        return (False, f"❌ Session: {h:02d}:00 UTC outside {s:02d}-{e:02d}")

    def _f2_htf(self, df_15m, direction):
        period = cfg_get("strategy", "htf_ema_period", 50)
        if df_15m is None or len(df_15m) < period + 3:
            return (False, "❌ HTF: insufficient 15M data")
        ema    = calc_ema(df_15m["Close"], period)
        rising = ema.iloc[-1] > ema.iloc[-3]
        trend  = "rising ↑" if rising else "falling ↓"
        if (direction == "LONG" and rising) or (direction == "SHORT" and not rising):
            return (True,  f"✅ HTF: 15M EMA{period} {trend} (${ema.iloc[-1]:,.0f})")
        return (False, f"❌ HTF: 15M EMA{period} {trend} conflicts {direction}")

    def _f3_body(self, candle):
        body = abs(candle["Close"] - candle["Open"])
        thr  = cfg_get("filters", "min_candle_body", 100)
        if body >= thr:
            return (True,  f"✅ Body: ${body:,.0f} ≥ ${thr:,.0f}")
        return (False, f"❌ Body: ${body:,.0f} < ${thr:,.0f}")

    def _f4_atr(self, atr):
        thr = cfg_get("filters", "min_atr", 200)
        if atr >= thr:
            return (True,  f"✅ ATR: ${atr:,.0f} ≥ ${thr:,.0f}")
        return (False, f"❌ ATR: ${atr:,.0f} < ${thr:,.0f}")

    def _f5_cooldown(self, symbol):
        cd = self.state.get_cooldown(symbol)
        if cd <= 0:
            return (True,  "✅ Cooldown: clear")
        return (False, f"❌ Cooldown: {cd} candle(s) left")

    def _f6_spread(self, spread):
        thr = cfg_get("filters", "max_spread_pct", 0.05)
        if spread <= thr:
            return (True,  f"✅ Spread: {spread:.3f}%")
        return (False, f"❌ Spread: {spread:.3f}% > {thr}%")

    def _risk_daily(self):
        losses = self.state.data["daily_losses"]
        limit  = cfg_get("risk_management", "max_daily_losses", 3)
        if losses < limit:
            return (True,  f"✅ Daily: {losses}/{limit} losses")
        return (False, f"❌ Daily: {losses}/{limit} — LIMIT HIT")

    def _risk_symbol(self, symbol):
        count = self.state.get_symbol_signals_today(symbol)
        limit = cfg_get("risk_management", "max_signals_per_symbol_per_day", 5)
        if count < limit:
            return (True,  f"✅ {symbol}: {count}/{limit} today")
        return (False, f"❌ {symbol}: {count}/{limit} — limit hit")


# ══════════════════════════════════════════════════════════════════════
# POSITION SIZING
# ══════════════════════════════════════════════════════════════════════

def calc_position_size(balance: float, risk_pct: float,
                        sl_dist: float, price: float,
                        leverage: int = 1) -> dict:
    if sl_dist <= 0 or price <= 0:
        return {"btc": 0, "usd": 0}
    risk_usd = balance * risk_pct / 100
    btc_qty  = risk_usd / sl_dist
    usd_val  = btc_qty * price
    return {
        "btc": round(btc_qty, 3),
        "usd": round(usd_val, 2),
        "risk_usd": round(risk_usd, 2)
    }


# ══════════════════════════════════════════════════════════════════════
# ALERT FORMATTING
# ══════════════════════════════════════════════════════════════════════

def format_alert(signal: dict, filters: list, atr: float,
                 state: BotState, live_price: float,
                 slip_pct: float, order_result: dict = None) -> str:

    d       = signal["direction"]
    sym     = signal["symbol"]
    emoji   = "🟢" if d == "LONG" else "🔴"
    arrow   = "⬆️" if d == "LONG" else "⬇️"
    balance = cfg_get("strategy", "account_balance", 1000)
    risk_pct = cfg_get("strategy", "risk_per_trade_pct", 1.0)
    leverage = cfg_get("binance", "leverage", 1)
    rr       = cfg_get("strategy", "rr_ratio", 3.0)
    pos      = calc_position_size(
        balance, risk_pct, signal["risk"], live_price, leverage
    )

    auto_trade = cfg_get("bot", "auto_trade", False)
    trade_mode = "🤖 AUTO-TRADED" if auto_trade else "📋 SIGNAL ONLY"

    msg = (
        f"{emoji} <b>BTC FUTURES {d}</b> {arrow}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>{sym} Perp</b> | {trade_mode}\n"
        f"\n"
        f"📍 <b>Entry:</b>  ${live_price:,.2f}\n"
        f"🛑 <b>Stop:</b>   ${signal['sl']:,.2f}\n"
        f"🎯 <b>Target:</b> ${signal['tp']:,.2f}\n"
        f"\n"
        f"📏 Risk: ${signal['risk']:,.0f} → Reward: ${signal['reward']:,.0f} (1:{rr:.0f})\n"
        f"📦 Size: {pos['btc']} BTC (${pos['risk_usd']:.0f} risk @ {leverage}x)\n"
        f"💱 Slippage: {slip_pct:.3f}% ✅\n"
        f"\n"
        f"━━━━━━ <b>Filters</b> ━━━━━━\n"
    )
    for passed, detail in filters:
        msg += f"  {detail}\n"

    msg += (
        f"\n"
        f"━━━━━━ <b>Context</b> ━━━━━━\n"
        f"📊 EMA{cfg_get('strategy','ema_period',15)}: ${signal['ema']:,.2f}\n"
        f"📈 ATR(14): ${atr:,.0f}\n"
        f"📡 Binance Futures (live)\n"
        f"🕐 {signal['time']}\n"
    )

    if order_result:
        if order_result.get("success"):
            msg += (
                f"\n✅ <b>Order Executed</b>\n"
                f"Entry ID: {order_result.get('entry_order', {}).get('orderId', 'N/A')}\n"
                f"SL + TP set automatically\n"
            )
        else:
            msg += (
                f"\n❌ <b>Order Failed</b>\n"
                f"Error: {order_result.get('error', 'Unknown')[:100]}\n"
                f"⚠️ Place manually!\n"
            )

    msg += (
        f"\n📉 Record: {state.data['total_wins']}W / "
        f"{state.data['total_losses']}L ({state.win_rate:.1f}%)\n"
        f"📊 Signals: {state.data.get('signals_sent',0)} sent / "
        f"{state.data.get('orders_placed',0)} traded\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    return msg


# ══════════════════════════════════════════════════════════════════════
# HEALTH CHECK THREAD
# ══════════════════════════════════════════════════════════════════════

def health_check_loop(state: BotState, data_mgr: DataManager,
                      trader, symbols: list):
    interval         = cfg_get("bot", "health_check_interval_minutes", 60) * 60
    last_daily       = None

    while not shutdown_event.is_set():
        try:
            now = datetime.now(timezone.utc)

            # Hourly health ping
            last = state.data.get("last_health_check")
            do_ping = True
            if last:
                try:
                    elapsed = (now - datetime.fromisoformat(str(last))).total_seconds()
                    do_ping = elapsed >= interval
                except Exception:
                    pass

            if do_ping:
                try:
                    start   = datetime.fromisoformat(str(state.data["bot_start_time"]))
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=timezone.utc)
                    uptime  = now - start
                    h       = int(uptime.total_seconds() // 3600)
                    m       = int((uptime.total_seconds() % 3600) // 60)
                    uptime_str = f"{h}h {m}m"
                except Exception:
                    uptime_str = "unknown"

                auto = cfg_get("bot", "auto_trade", False)
                bal  = trader.get_balance() if trader else None
                bal_str = f"${bal:,.2f}" if bal is not None else "N/A"

                msg = (
                    f"💚 <b>BTC Futures Bot — Health</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏱ Uptime: {uptime_str}\n"
                    f"💎 Watching: {', '.join(symbols)}\n"
                    f"📡 Data: Binance Futures\n"
                    f"🤖 Auto-trade: {'ON' if auto else 'OFF'}\n"
                    f"💰 Balance: {bal_str}\n"
                    f"📊 Signals: {state.data.get('signals_sent',0)} sent / "
                    f"{state.data.get('orders_placed',0)} traded\n"
                    f"📈 Record: {state.data['total_wins']}W / "
                    f"{state.data['total_losses']}L ({state.win_rate:.1f}%)\n"
                    f"🕐 {now.strftime('%Y-%m-%d %H:%M UTC')}"
                )
                telegram.send(msg)
                state.data["last_health_check"] = now.isoformat()
                state.save()

            # Daily summary
            today = now.strftime("%Y-%m-%d")
            if now.hour == 0 and now.minute >= 5 and last_daily != today:
                trades = state.data.get("trades_today", [])
                msg = (
                    f"📋 <b>BTC Daily Summary — {today}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Signals sent: {len(trades)}\n"
                    f"Orders placed: {state.data.get('orders_placed', 0)}\n"
                    f"Daily losses: {state.data['daily_losses']}\n"
                    f"All-time: {state.data['total_wins']}W / "
                    f"{state.data['total_losses']}L ({state.win_rate:.1f}%)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"24/7 monitoring continues."
                )
                telegram.send(msg)
                last_daily = today

        except Exception as e:
            log.error(f"Health check error: {e}")

        shutdown_event.wait(30)


# ══════════════════════════════════════════════════════════════════════
# MAIN BOT ENGINE
# ══════════════════════════════════════════════════════════════════════

def run():
    log.info("=" * 60)
    log.info("    BTC EMA 15 BOT — BINANCE FUTURES")
    log.info("    Data + Execution: Binance Futures API")
    log.info("=" * 60)

    state        = BotState()
    data_mgr     = DataManager()
    filter_engine = FilterEngine(state)

    # ── Telegram ──────────────────────────────────────────────────────
    if not telegram.test_connection():
        log.error("Telegram connection failed!")
        sys.exit(1)

    # ── Binance Trader (optional — only if keys configured) ──────────
    api_key    = cfg_get("binance", "api_key",    "")
    api_secret = cfg_get("binance", "api_secret", "")
    auto_trade = cfg_get("bot",     "auto_trade", False)
    testnet    = cfg_get("binance", "testnet",    True)
    leverage   = cfg_get("binance", "leverage",   1)
    margin_type = cfg_get("binance", "margin_type", "ISOLATED")

    trader = None
    if api_key and api_secret and not str(api_key).startswith("PASTE"):
        trader = BinanceFuturesTrader(api_key, api_secret, testnet)
        if trader.is_connected():
            bal = trader.get_balance()
            log.info(f"✅ Binance API connected | Balance: ${bal:,.2f} USDT")
            telegram.send(
                f"✅ <b>Binance API Connected</b>\n"
                f"Mode: {'TESTNET' if testnet else '🔴 LIVE'}\n"
                f"Balance: ${bal:,.2f} USDT\n"
                f"Auto-trade: {'ON' if auto_trade else 'OFF'}"
            )
        else:
            log.warning("Binance API keys invalid — running in signal-only mode")
            trader = None
    else:
        log.info("No Binance API keys — running in signal-only mode")

    # ── Config ────────────────────────────────────────────────────────
    symbols   = CFG.get("symbols", ["BTCUSDT"])
    ema_period = cfg_get("strategy", "ema_period",   15)
    rr        = cfg_get("strategy", "rr_ratio",      3.0)
    balance   = cfg_get("strategy", "account_balance", 1000)
    tf_5m     = cfg_get("strategy", "timeframe",     "5m")
    tf_15m    = cfg_get("strategy", "htf_timeframe", "15m")
    count     = cfg_get("bot", "candle_history_count", 100)
    interval  = cfg_get("bot", "check_interval_seconds", 20)
    max_slip  = cfg_get("bot", "max_slippage_pct",   0.1)

    # ── Test data connection ──────────────────────────────────────────
    log.info("Testing Binance Futures data connection...")
    try:
        test_df = data_mgr.fetch(symbols[0], tf_5m, 10)
        live_px = data_mgr.fetch_live_price(symbols[0])
        log.info(f"✅ Binance Futures connected — {symbols[0]}: ${live_px:,.2f}")
    except Exception as e:
        log.warning(f"Data test failed: {e} — will retry in main loop")

    # Set leverage for all symbols
    if trader:
        for sym in symbols:
            trader.set_margin_type(sym, margin_type)
            trader.set_leverage(sym, leverage)

    # ── Start health check thread ─────────────────────────────────────
    threading.Thread(
        target=health_check_loop,
        args=(state, data_mgr, trader, symbols),
        daemon=True, name="HealthCheck"
    ).start()

    # ── Startup Telegram alert ────────────────────────────────────────
    sym_str = " + ".join(symbols)
    telegram.send(
        f"🟢 <b>BTC Futures Bot Started</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 Symbols: {sym_str} (Perp)\n"
        f"📊 EMA {ema_period} | 5M/15M | 1:{rr:.0f} RR\n"
        f"📡 Data: Binance Futures (live)\n"
        f"🤖 Auto-trade: {'ON — ' + ('TESTNET' if testnet else '🔴 LIVE') if auto_trade and trader else 'OFF (signal only)'}\n"
        f"🔍 Slippage guard: {max_slip}%\n"
        f"💰 Account: ${balance:,.0f}\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Running 24/7 ☁️"
    )

    log.info(f"Symbols: {symbols}")
    log.info(f"TF: {tf_5m}/{tf_15m} | EMA: {ema_period} | RR: 1:{rr}")
    log.info(f"Auto-trade: {auto_trade} | Leverage: {leverage}x")
    log.info("Entering main loop...\n")

    error_streak = 0

    # ══════════════════════════════════════════════════════════════════
    # MAIN LOOP
    # ══════════════════════════════════════════════════════════════════

    while not shutdown_event.is_set():
        try:
            state.reset_daily()

            for symbol in symbols:
                if shutdown_event.is_set():
                    break
                try:
                    # Fetch candles from Binance Futures
                    df_5m  = data_mgr.fetch(symbol, tf_5m,  count)
                    df_15m = data_mgr.fetch(symbol, tf_15m, count)

                    if len(df_5m) < ema_period + 5:
                        log.warning(f"{symbol}: only {len(df_5m)} bars")
                        continue

                    # New candle check
                    latest = str(df_5m.index[-1])
                    if latest == state.get_last_candle_time(symbol):
                        continue
                    state.set_last_candle_time(symbol, latest)
                    state.tick_cooldown(symbol)

                    # EMA crossover detection
                    sig = detect_signal(df_5m, symbol)
                    if sig is None:
                        continue

                    log.info(
                        f"🔔 {symbol} {sig['direction']} @ "
                        f"${sig['entry']:,.2f} risk=${sig['risk']:,.0f}"
                    )

                    # Duplicate check
                    sig_hash = f"{sig['direction']}_{sig['time']}_{sig['entry']:.0f}"
                    if state.is_duplicate_signal(sig_hash):
                        log.info(f"↩️ Duplicate — skipping {symbol}")
                        continue

                    # Run filters
                    candle      = df_5m.iloc[-1]
                    candle_time = df_5m.index[-1]
                    atr         = calc_atr(df_5m, cfg_get("strategy","atr_period",14)).iloc[-1]
                    spread      = estimate_spread_pct(df_5m)
                    filters     = filter_engine.run_all(sig, candle, candle_time, df_5m, df_15m)
                    all_passed  = all(p for p, _ in filters)

                    if not all_passed:
                        failed = [d for p, d in filters if not p]
                        log.info(f"❌ {symbol} Rejected → {failed}")
                        state.data["signals_rejected"] = state.data.get("signals_rejected", 0) + 1
                        if cfg_get("bot", "send_rejected_signals", False):
                            telegram.send(
                                f"⚪ <b>Rejected:</b> {symbol} {sig['direction']} "
                                f"@ ${sig['entry']:,.2f}\n"
                                + "\n".join(f"  {f}" for f in failed)
                            )
                        continue

                    # Slippage guard
                    slip_ok, live_price, slip_pct = check_slippage(
                        data_mgr, symbol, sig["entry"]
                    )

                    if not slip_ok:
                        state.data["signals_slippage_rejected"] = (
                            state.data.get("signals_slippage_rejected", 0) + 1
                        )
                        telegram.send(
                            f"⚠️ <b>Slippage Cancelled</b>\n"
                            f"{symbol} {sig['direction']}\n"
                            f"Signal: ${sig['entry']:,.2f} → Live: ${live_price:,.2f}\n"
                            f"Slippage: {slip_pct:.3f}% > {max_slip}% limit"
                        )
                        continue

                    # ── ALL FILTERS PASSED ────────────────────────
                    log.info(f"✅ {symbol} ALL FILTERS PASSED!")

                    # Auto-trade execution
                    order_result = None
                    if auto_trade and trader:
                        risk_pct = cfg_get("strategy", "risk_per_trade_pct", 1.0)
                        pos      = calc_position_size(
                            balance, risk_pct, sig["risk"], live_price, leverage
                        )
                        side = "BUY" if sig["direction"] == "LONG" else "SELL"

                        if pos["btc"] >= 0.001:
                            order_result = trader.place_order(
                                symbol, side, pos["btc"],
                                sig["sl"], sig["tp"]
                            )
                            if order_result.get("success"):
                                state.data["orders_placed"] = (
                                    state.data.get("orders_placed", 0) + 1
                                )
                            else:
                                state.data["orders_failed"] = (
                                    state.data.get("orders_failed", 0) + 1
                                )
                        else:
                            log.warning(f"Position too small: {pos['btc']} BTC — skipping order")

                    # Send Telegram alert
                    alert = format_alert(
                        sig, filters, atr, state,
                        live_price, slip_pct, order_result
                    )
                    telegram.send(alert)

                    # Update state
                    state.data["signals_sent"] = state.data.get("signals_sent", 0) + 1
                    state.data["total_trades"] = state.data.get("total_trades", 0) + 1
                    state.increment_symbol_signals(symbol)
                    state.data["trades_today"].append({
                        "time":        sig["time"],
                        "symbol":      symbol,
                        "direction":   sig["direction"],
                        "entry":       live_price,
                        "sl":          sig["sl"],
                        "tp":          sig["tp"],
                        "slip_pct":    slip_pct,
                        "auto_traded": bool(order_result and order_result.get("success"))
                    })

                except Exception as e:
                    log.error(f"Error processing {symbol}: {e}", exc_info=True)

            state.save()
            error_streak = 0

        except KeyboardInterrupt:
            break

        except Exception as e:
            error_streak += 1
            log.error(f"Main loop error ({error_streak}x): {e}", exc_info=True)
            if error_streak == 5:
                telegram.send(
                    f"⚠️ <b>BTC Bot Error</b>\n5 errors.\n"
                    f"<code>{str(e)[:300]}</code>"
                )
            if error_streak >= 10:
                telegram.send(
                    f"🔴 <b>BTC Bot Critical</b>\n10 errors. Pausing 5min.\n"
                    f"<code>{str(e)[:300]}</code>"
                )
                shutdown_event.wait(300)
                error_streak = 0
            else:
                shutdown_event.wait(min(60, interval * (1 + error_streak)))
            continue

        shutdown_event.wait(interval)

    # Shutdown
    log.info("BTC Bot shutting down...")
    telegram.send(
        f"🔴 <b>BTC Futures Bot Stopped</b>\n"
        f"At {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )
    state.save()
    log.info("Done.")


if __name__ == "__main__":
    print("""
    ╔══════════════════════════════════════════════════╗
    ║   BTC EMA 15 — BINANCE FUTURES BOT              ║
    ║   BTCUSDT Perp | 5M | 1:3 RR | 24/7            ║
    ║   Data + Execution: Binance Futures API         ║
    ╚══════════════════════════════════════════════════╝
    """)
    run()
