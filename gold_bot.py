from market_regime import analyze_regime
#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║           GOLD EMA 15 ENHANCED STRATEGY — FUTURES ALERT BOT        ║
║           XAUUSDT Perpetual | 5-Minute | 1:3 RR | 6 Filters        ║
║           24/7 — Binance Futures Data (same as BTC bot)            ║
╚══════════════════════════════════════════════════════════════════════╝

Data Source: Binance Futures (fapi.binance.com) — XAUUSDT TradFi Perp
  - Launched January 5, 2026 on Binance
  - Free market data, no API key needed
  - 24/7 trading (no weekend shutdown needed)
  - Same API as BTC bot — consistent, reliable

Strategy unchanged:
  - EMA 15 Crossover on 5M candles
  - HTF Trend filter on 15M EMA 50
  - 6 Filters: Session, HTF, Body, ATR, Cooldown, Spread
  - 1:3 RR ratio
  - Slippage guard before every alert
"""

import os, sys, json, time, signal, logging, threading
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
import requests, numpy as np, pandas as pd

BOT_DIR     = Path(__file__).parent.resolve()
CONFIG_PATH = BOT_DIR / "config.json"
STATE_PATH  = BOT_DIR / "bot_state.json"
LOG_PATH    = BOT_DIR / "gold_bot.log"

DEFAULT_CONFIG = {
    "telegram": {"bot_token": "8134933083:AAE7mUZsskolHhwFcUZ2gpUq8_906niWFIs", "chat_id": "7396015146"},
    "data_source": {
        "primary": "binance_futures",
    },
    "strategy": {
        "symbol": "XAUUSDT",
        "timeframe": "5m",
        "htf_timeframe": "15m",
        "ema_period": 15,
        "htf_ema_period": 50,
        "atr_period": 14,
        "rr_ratio": 3.0,
        "risk_per_trade_pct": 2.0,
        "account_balance": 1000.0,
        "min_risk_dollars": 0.50
    },
    "filters": {
        "session_filter_enabled": False,
        "session_start_utc": 7,
        "session_end_utc": 21,
        "min_candle_body": 1.50,
        "min_atr": 2.00,
        "max_spread_pct": 0.05,
        "cooldown_candles": 2
    },
    "risk_management": {
        "max_daily_losses": 3,
        "max_daily_loss_pct": 6.0,
        "max_signals_per_day": 5
    },
    "bot": {
        "check_interval_seconds": 20,
        "candle_history_count": 100,
        "send_rejected_signals": False,
        "health_check_interval_minutes": 60,
        "max_slippage_pct": 0.05,
        "auto_trade": False
    },
    "binance": {
        "api_key": "HqmDjVSziGuHTeuoYSWDWeRtGcow4HOVA3SdtYWmMtHCkFk5RhJeAZG9NwzL6NEc",
        "api_secret": "X1B2HtpC4Yn1VmJXeyYVFBXZSsWPCb6MWuXzKnoC9jh69PSUcuYS9uMEiCki5We7",
        "testnet": False,
        "leverage": 1,
        "margin_type": "ISOLATED"
    }
}

def deep_merge(base, override):
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result

def load_config():
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        print(f"Template created: {CONFIG_PATH}"); sys.exit(1)
    with open(CONFIG_PATH, "r") as f:
        user = json.load(f)
    cfg = deep_merge(DEFAULT_CONFIG, user)
    if not cfg["telegram"]["bot_token"] or str(cfg["telegram"]["bot_token"]).startswith("PASTE"):
        print("ERROR: telegram.bot_token not set"); sys.exit(1)
    if not cfg["telegram"]["chat_id"] or str(cfg["telegram"]["chat_id"]).startswith("PASTE"):
        print("ERROR: telegram.chat_id not set"); sys.exit(1)
    return cfg

CFG = load_config()

def cfg_get(s, k, d=None):
    try: return CFG[s][k]
    except: return d if d is not None else DEFAULT_CONFIG.get(s, {}).get(k)

# ── Logging ──────────────────────────────────────────────────────
log = logging.getLogger("GoldBot")
log.setLevel(logging.INFO)
fh = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(fh)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(ch)

# ── Shutdown ─────────────────────────────────────────────────────
shutdown_event = threading.Event()
def handle_shutdown(signum, frame):
    log.info(f"Signal {signum} — shutting down..."); shutdown_event.set()
signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT,  handle_shutdown)

# ══════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════

class Telegram:
    def __init__(self, token, chat_id):
        self.token    = token
        self.url      = f"https://api.telegram.org/bot{token}/sendMessage"
        # Support single chat_id (str) or multiple (list)
        if isinstance(chat_id, list):
            self.chat_ids = [str(c) for c in chat_id if c]
        else:
            self.chat_ids = [str(chat_id)]
        self.chat_id = self.chat_ids[0]  # backward compat

    def send(self, msg, retries=3):
        results = []
        for cid in self.chat_ids:
            payload = {
                "chat_id": cid, "text": msg[:4000],
                "parse_mode": "HTML", "disable_web_page_preview": True
            }
            sent = False
            for i in range(1, retries+1):
                try:
                    r = requests.post(self.url, json=payload, timeout=15)
                    if r.status_code == 200:
                        sent = True
                        break
                    if r.status_code == 400:
                        payload["parse_mode"] = None
                except: pass
                if i < retries: time.sleep(2*i)
            results.append(sent)
        return all(results)

    def test(self):
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{self.token}/getMe", timeout=10
            )
            if r.status_code == 200:
                log.info(f"Telegram: @{r.json().get('result',{}).get('username','?')}")
                return True
        except: pass
        return False

# Build recipient list — primary + any additional chat IDs
_tg_ids = [CFG["telegram"]["chat_id"]]
if CFG["telegram"].get("chat_id_2"):
    _tg_ids.append(CFG["telegram"]["chat_id_2"])
telegram = Telegram(CFG["telegram"]["bot_token"], _tg_ids)


# ══════════════════════════════════════════════════════════════════
# DATA SOURCE: BINANCE FUTURES
# XAUUSDT TradFi Perpetual — launched Jan 5, 2026
# Same fapi.binance.com endpoint as BTC bot
# Free, no API key, 24/7, most reliable
# ══════════════════════════════════════════════════════════════════

class BinanceFuturesData:
    BASE_URL = "https://fapi.binance.com"

    INTERVAL_MAP = {
        "1min": "1m",  "5min": "5m",   "15min": "15m",
        "30min": "30m", "1h": "1h",    "4h": "4h",
        "1d": "1d",    "5m": "5m",     "15m": "15m"
    }

    def __init__(self):
        self.call_count = 0
        log.info("Data source: Binance Futures XAUUSDT (TradFi Perp)")
        log.info("✅ Free market data — no API key needed — 24/7")

    def fetch_candles(self, symbol: str, interval: str, limit: int = 100) -> pd.DataFrame:
        self.call_count += 1
        tf = self.INTERVAL_MAP.get(interval, interval)
        symbol_clean = symbol.replace("/", "").upper()

        url    = f"{self.BASE_URL}/fapi/v1/klines"
        params = {
            "symbol":   symbol_clean,
            "interval": tf,
            "limit":    min(limit, 1500)
        }

        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            raise RuntimeError(f"Binance Futures: empty data for {symbol}")

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
            "low": "Low",   "close": "Close",
            "volume": "Volume"
        }, inplace=True)

        df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)

        if df.empty:
            raise RuntimeError(f"Binance Futures: no valid candles for {symbol}")

        # Validate gold price range
        price = df["Close"].iloc[-1]
        if price < 500 or price > 10000:
            raise ValueError(f"Suspicious XAUUSDT price: ${price:.2f}")

        return df

    def fetch_live_price(self, symbol: str) -> float:
        """Real-time mark price for slippage check."""
        url = f"{self.BASE_URL}/fapi/v1/ticker/price"
        resp = requests.get(
            url,
            params={"symbol": symbol.replace("/", "").upper()},
            timeout=10
        )
        resp.raise_for_status()
        return float(resp.json()["price"])

    @property
    def name(self): return "Binance Futures"

    @property
    def daily_calls(self): return self.call_count


# ══════════════════════════════════════════════════════════════════
# DATA MANAGER
# ══════════════════════════════════════════════════════════════════

class DataManager:
    def __init__(self):
        self.source   = BinanceFuturesData()
        self.failures = 0

    def fetch(self, symbol: str, interval: str, count: int) -> pd.DataFrame:
        try:
            df = self.source.fetch_candles(symbol, interval, count)
            self._validate(df, symbol)
            self.failures = 0
            return df
        except Exception as e:
            self.failures += 1
            log.warning(f"Data fetch failed ({self.failures}x): {e}")
            if self.failures >= 5:
                telegram.send(
                    f"⚠️ <b>Gold Bot Data Error</b>\n"
                    f"Binance Futures fetch failed {self.failures}x\n"
                    f"Error: {str(e)[:200]}"
                )
            raise

    def fetch_live_price(self, symbol: str) -> float:
        try:
            return self.source.fetch_live_price(symbol)
        except Exception as e:
            log.warning(f"Live price fetch failed: {e}")
            return None

    def _validate(self, df, symbol):
        if df is None or df.empty:
            raise ValueError(f"Empty data for {symbol}")
        if len(df) < 10:
            raise ValueError(f"{symbol}: only {len(df)} rows, need 20+")
        price = df["Close"].iloc[-1]
        if price < 500 or price > 10000:
            raise ValueError(f"Suspicious gold price: ${price:.2f}")

    @property
    def active_name(self): return self.source.name

    @property
    def usage_str(self):
        return f"Binance Futures: {self.source.daily_calls} calls"


# ══════════════════════════════════════════════════════════════════
# BINANCE FUTURES TRADER — EXECUTION ENGINE
# Same as BTC bot — HMAC signed orders
# Supports ISOLATED/CROSSED margin + leverage
# ══════════════════════════════════════════════════════════════════

import hmac
import hashlib
from urllib.parse import urlencode

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
        query     = urlencode(params)
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
        r = self.session.get(
            f"{self.base_url}{endpoint}", params=params, timeout=10
        )
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
        try:
            self._get("/fapi/v2/account")
            return True
        except Exception as e:
            log.error(f"Binance API connection failed: {e}")
            return False

    def get_balance(self) -> float:
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
        try:
            self._post("/fapi/v1/leverage", {
                "symbol": symbol, "leverage": leverage
            })
            log.info(f"Leverage set: {symbol} {leverage}x")
        except Exception as e:
            log.warning(f"Leverage set failed: {e}")

    def set_margin_type(self, symbol: str, margin_type: str):
        try:
            self._post("/fapi/v1/marginType", {
                "symbol": symbol, "marginType": margin_type.upper()
            })
            log.info(f"Margin type set: {symbol} {margin_type}")
        except Exception as e:
            if "No need to change" not in str(e):
                log.warning(f"Margin type set failed: {e}")

    def place_order(self, symbol: str, side: str, quantity: float,
                    sl_price: float, tp_price: float) -> dict:
        """Place market order + SL + TP on Binance Futures."""
        symbol = symbol.replace("/", "").upper()
        result = {}
        try:
            # Main market entry order
            order = self._post("/fapi/v1/order", {
                "symbol":     symbol,
                "side":       side,
                "type":       "MARKET",
                "quantity":   f"{quantity:.2f}",
                "reduceOnly": "false"
            })
            result["entry_order"] = order
            log.info(
                f"✅ GOLD order placed: {order.get('orderId')} "
                f"| {side} {quantity} {symbol}"
            )

            sl_side = "SELL" if side == "BUY" else "BUY"

            # Stop Loss
            sl_order = self._post("/fapi/v1/order", {
                "symbol":        symbol,
                "side":          sl_side,
                "type":          "STOP_MARKET",
                "stopPrice":     f"{sl_price:.2f}",
                "closePosition": "true",
                "timeInForce":   "GTE_GTC"
            })
            result["sl_order"] = sl_order
            log.info(f"✅ GOLD SL placed @ ${sl_price:,.2f}")

            # Take Profit
            tp_order = self._post("/fapi/v1/order", {
                "symbol":        symbol,
                "side":          sl_side,
                "type":          "TAKE_PROFIT_MARKET",
                "stopPrice":     f"{tp_price:.2f}",
                "closePosition": "true",
                "timeInForce":   "GTE_GTC"
            })
            result["tp_order"] = tp_order
            log.info(f"✅ GOLD TP placed @ ${tp_price:,.2f}")
            result["success"] = True

        except Exception as e:
            log.error(f"GOLD order placement failed: {e}")
            result["success"] = False
            result["error"]   = str(e)

        return result


def calc_position_size(balance: float, risk_pct: float,
                        sl_dist: float, price: float,
                        leverage: int = 1) -> dict:
    if sl_dist <= 0 or price <= 0:
        return {"qty": 0, "usd": 0, "risk_usd": 0}
    risk_usd = balance * risk_pct / 100
    # For XAUUSDT: 1 contract = 1 troy oz
    qty      = round(risk_usd / sl_dist, 2)
    usd_val  = round(qty * price, 2)
    return {
        "qty":      qty,
        "usd":      usd_val,
        "risk_usd": round(risk_usd, 2)
    }


# ══════════════════════════════════════════════════════════════════
# SLIPPAGE GUARD
# ══════════════════════════════════════════════════════════════════

def check_slippage(dm, symbol, signal_price):
    max_slip   = cfg_get("bot", "max_slippage_pct", 0.05)
    live_price = dm.fetch_live_price(symbol)
    if live_price is None:
        return True, signal_price, 0.0
    slip_pct = abs(live_price - signal_price) / signal_price * 100
    if slip_pct > max_slip:
        log.warning(
            f"⚠️ Slippage too high: signal=${signal_price:,.2f} "
            f"live=${live_price:,.2f} diff={slip_pct:.3f}%"
        )
        return False, live_price, slip_pct
    log.info(f"✅ Slippage OK: ${live_price:,.2f} diff={slip_pct:.3f}%")
    return True, live_price, slip_pct


# ══════════════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════════════

def calc_ema(s, p): return s.ewm(span=p, adjust=False).mean()

def calc_atr(df, p):
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift(1)).abs(),
        (df["Low"]  - df["Close"].shift(1)).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def est_spread_pct(df):
    if len(df) < 3: return 0.03
    recent = df.tail(3)
    avg_range = (recent["High"] - recent["Low"]).mean()
    price = recent["Close"].iloc[-1]
    if price <= 0: return 0.03
    return round(max((avg_range * 0.12 / price) * 100, 0.01), 4)


# ══════════════════════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════════════════════

class BotState:
    def __init__(self):
        self.data = {
            "last_candle_time": None, "cooldown_remaining": 0,
            "daily_losses": 0, "daily_loss_date": None, "trades_today": [],
            "total_trades": 0, "total_wins": 0, "total_losses": 0,
            "last_health_check": None,
            "bot_start_time": datetime.now(timezone.utc).isoformat(),
            "signals_sent": 0, "signals_rejected": 0,
            "signals_slippage_rejected": 0, "last_signal_hash": None
        }
        if STATE_PATH.exists():
            try:
                saved = json.load(open(STATE_PATH))
                for k in saved:
                    if k in self.data: self.data[k] = saved[k]
                log.info("State restored.")
            except: pass

    def save(self):
        try: json.dump(self.data, open(STATE_PATH, "w"), indent=2, default=str)
        except: pass

    def reset_daily(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.data["daily_loss_date"] != today:
            self.data["daily_losses"]    = 0
            self.data["daily_loss_date"] = today
            self.data["trades_today"]    = []

    def tick_cooldown(self):
        if self.data["cooldown_remaining"] > 0:
            self.data["cooldown_remaining"] -= 1

    def is_duplicate(self, sig_hash):
        if self.data.get("last_signal_hash") == sig_hash: return True
        self.data["last_signal_hash"] = sig_hash
        return False

    @property
    def is_cooling(self): return self.data["cooldown_remaining"] > 0

    @property
    def daily_limit_hit(self):
        return self.data["daily_losses"] >= cfg_get("risk_management", "max_daily_losses", 3)

    @property
    def win_rate(self):
        t = self.data["total_wins"] + self.data["total_losses"]
        return (self.data["total_wins"] / t * 100) if t > 0 else 0.0


# ══════════════════════════════════════════════════════════════════
# SIGNAL DETECTION — EMA 15 Crossover
# ══════════════════════════════════════════════════════════════════

def detect_signal(df):
    ep = cfg_get("strategy", "ema_period", 15)
    if len(df) < ep + 3: return None
    ema  = calc_ema(df["Close"], ep)
    prev, curr = df.iloc[-2], df.iloc[-1]
    pe, ce     = ema.iloc[-2], ema.iloc[-1]
    d = None
    if prev["Close"] <= pe and curr["Close"] > ce: d = "LONG"
    elif prev["Close"] >= pe and curr["Close"] < ce: d = "SHORT"
    if not d: return None
    entry = curr["Close"]
    sl    = curr["Low"] if d == "LONG" else curr["High"]
    risk  = abs(entry - sl)
    min_r = cfg_get("strategy", "min_risk_dollars", 0.50)
    if risk < min_r: return None
    rr = cfg_get("strategy", "rr_ratio", 3.0)
    tp = entry + (risk * rr) if d == "LONG" else entry - (risk * rr)
    return {
        "direction": d, "entry": round(entry, 2),
        "sl": round(sl, 2), "tp": round(tp, 2),
        "risk": round(risk, 2), "reward": round(risk * rr, 2),
        "ema": round(ce, 2), "time": str(curr.name)
    }


# ══════════════════════════════════════════════════════════════════
# FILTER ENGINE
# ══════════════════════════════════════════════════════════════════

class FilterEngine:
    def __init__(self, state): self.state = state

    def run_all(self, sig, candle, ct, df_5m, df_15m):
        atr    = calc_atr(df_5m, cfg_get("strategy", "atr_period", 14)).iloc[-1]
        spread = est_spread_pct(df_5m)
        results = []

        if cfg_get("filters", "session_filter_enabled", False):
            results.append(self._session(ct))
        else:
            results.append((True, "✅ Session: 24/7 (Binance TradFi Perp)"))

        results.append(self._htf(df_15m, sig["direction"]))
        results.append(self._body(candle))
        results.append(self._atr(atr))
        results.append(self._cool())
        results.append(self._spread(spread))
        results.append(self._daily())
        return results

    def _session(self, t):
        h = t.hour
        s = cfg_get("filters", "session_start_utc", 7)
        e = cfg_get("filters", "session_end_utc",   21)
        ok = s <= h < e
        return (ok, f"{'✅' if ok else '❌'} Session: {h:02d}:00 UTC")

    def _htf(self, df, d):
        p = cfg_get("strategy", "htf_ema_period", 50)
        if df is None or len(df) < p + 3:
            return (False, "❌ HTF: no 15M data")
        ema    = calc_ema(df["Close"], p)
        rising = ema.iloc[-1] > ema.iloc[-3]
        ok     = (d == "LONG" and rising) or (d == "SHORT" and not rising)
        trend  = "rising ↑" if rising else "falling ↓"
        return (ok, f"{'✅' if ok else '❌'} HTF EMA{p}: {trend} (${ema.iloc[-1]:,.2f})")

    def _body(self, c):
        b = abs(c["Close"] - c["Open"])
        t = cfg_get("filters", "min_candle_body", 1.50)
        return (b >= t, f"{'✅' if b>=t else '❌'} Body: ${b:.2f}")

    def _atr(self, a):
        t = cfg_get("filters", "min_atr", 2.00)
        return (a >= t, f"{'✅' if a>=t else '❌'} ATR: ${a:.2f}")

    def _cool(self):
        ok = not self.state.is_cooling
        cd = self.state.data["cooldown_remaining"]
        return (ok, f"{'✅' if ok else '❌'} Cooldown: {cd} left")

    def _spread(self, sp):
        t = cfg_get("filters", "max_spread_pct", 0.05)
        return (sp <= t, f"{'✅' if sp<=t else '❌'} Spread: {sp:.3f}%")

    def _daily(self):
        l = self.state.data["daily_losses"]
        m = cfg_get("risk_management", "max_daily_losses", 3)
        return (l < m, f"{'✅' if l<m else '❌'} Daily: {l}/{m} losses")


# ══════════════════════════════════════════════════════════════════
# ALERT FORMATTING
# ══════════════════════════════════════════════════════════════════

def format_alert(sig, filters, atr, state, live_price, slip_pct,
                 order_result=None):
    d     = sig["direction"]
    emoji = "🟡" if d == "LONG" else "🔴"
    arrow = "⬆️" if d == "LONG" else "⬇️"
    bal   = cfg_get("strategy", "account_balance", 1000)
    rp    = cfg_get("strategy", "risk_per_trade_pct", 2.0)
    lev   = cfg_get("binance", "leverage", 1)
    ra    = bal * rp / 100
    pos   = calc_position_size(bal, rp, sig["risk"], live_price, lev)
    rr    = cfg_get("strategy", "rr_ratio", 3.0)
    auto  = cfg_get("bot", "auto_trade", False)
    mode  = "🤖 AUTO-TRADED" if auto else "📋 SIGNAL ONLY"

    msg = (
        f"{emoji} <b>GOLD {d} SIGNAL</b> {arrow}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💛 <b>XAUUSDT Perp</b> | {mode}\n\n"
        f"📍 <b>Entry:</b>  ${live_price:,.2f}\n"
        f"🛑 <b>Stop:</b>   ${sig['sl']:,.2f}\n"
        f"🎯 <b>Target:</b> ${sig['tp']:,.2f}\n\n"
        f"📏 Risk: ${sig['risk']:.2f} → Reward: ${sig['reward']:.2f} (1:{rr:.0f})\n"
        f"📦 Size: {pos['qty']} oz (${pos['risk_usd']:.0f} risk @ {lev}x)\n"
        f"💱 Slippage: {slip_pct:.3f}% ✅\n\n"
        f"━━━━━━ <b>Filters</b> ━━━━━━\n"
    )
    for _, detail in filters:
        msg += f"  {detail}\n"
    msg += (
        f"\n📊 EMA{cfg_get('strategy','ema_period',15)}: ${sig['ema']:,.2f}\n"
        f"📈 ATR(14): ${atr:.2f}\n"
        f"📡 Binance Futures (live)\n"
        f"🕐 {sig['time']}\n"
    )
    if order_result:
        if order_result.get("success"):
            msg += (
                f"\n✅ <b>Order Executed</b>\n"
                f"Entry ID: {order_result.get('entry_order',{}).get('orderId','N/A')}\n"
                f"SL + TP set automatically\n"
            )
        else:
            msg += (
                f"\n❌ <b>Order Failed</b>\n"
                f"Error: {order_result.get('error','Unknown')[:100]}\n"
                f"⚠️ Place manually!\n"
            )
    msg += (
        f"\n📉 {state.data['total_wins']}W/{state.data['total_losses']}L "
        f"({state.win_rate:.1f}%)\n"
        f"📊 {state.data.get('signals_sent',0)} sent / "
        f"{state.data.get('orders_placed',0)} traded\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    return msg


# ══════════════════════════════════════════════════════════════════
# HEALTH CHECK THREAD
# ══════════════════════════════════════════════════════════════════

def health_loop(state, dm, trader=None):
    interval   = cfg_get("bot", "health_check_interval_minutes", 60) * 60
    last_daily = None

    while not shutdown_event.is_set():
        try:
            now  = datetime.now(timezone.utc)
            last = state.data.get("last_health_check")
            send = True
            if last:
                try:
                    elapsed = (now - datetime.fromisoformat(str(last))).total_seconds()
                    send    = elapsed >= interval
                except: pass

            if send:
                try:
                    start = datetime.fromisoformat(str(state.data["bot_start_time"]))
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=timezone.utc)
                    uptime = now - start
                    h = int(uptime.total_seconds() // 3600)
                    m = int((uptime.total_seconds() % 3600) // 60)
                    uptime_str = f"{h}h {m}m"
                except:
                    uptime_str = "unknown"

                live    = dm.fetch_live_price(cfg_get("strategy", "symbol", "XAUUSDT"))
                live_str = f"${live:,.2f}" if live else "N/A"

                bal_str  = f"${trader.get_balance():,.2f}" if trader else "N/A"
                auto     = cfg_get("bot", "auto_trade", False)

                # Build today's signals list
                trades_today = state.data.get("trades_today", [])
                wins  = state.data.get("total_wins",   0)
                losses= state.data.get("total_losses", 0)
                rejected = state.data.get("signals_rejected", 0)

                # Open trades = today's signals (signal-only: all sent signals are "open")
                if trades_today:
                    trade_lines = ""
                    for t in trades_today[-5:]:  # show last 5 max
                        arrow = "🟢" if t.get("dir") == "LONG" else "🔴"
                        entry = t.get("entry", 0)
                        ttime = str(t.get("time", ""))[-8:-3] if t.get("time") else "?"
                        trade_lines += f"\n  {arrow} {t.get('dir','?')} @ ${entry:,.2f} ({ttime})"
                else:
                    trade_lines = "\n  None yet"

                telegram.send(
                    f"💛 <b>Gold Bot Health</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏱ Uptime: {uptime_str}\n"
                    f"💛 XAUUSDT: {live_str}\n"
                    f"🤖 Mode: {'AUTO-TRADE' if auto else 'Signal Only'}\n"
                    f"📡 {dm.usage_str}\n"
                    f"━━━━━━ <b>Today</b> ━━━━━━\n"
                    f"📨 Signals sent: {state.data.get('signals_sent', 0)}\n"
                    f"❌ Rejected: {rejected}\n"
                    f"🏆 Winners: {wins}   💔 Losers: {losses}   "
                    f"WR: {state.win_rate:.1f}%\n"
                    f"━━━━━━ <b>Open Signals</b> ━━━━━━"
                    f"{trade_lines}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🕐 {now.strftime('%Y-%m-%d %H:%M UTC')}"
                )
                state.data["last_health_check"] = now.isoformat()
                state.save()

            # Daily summary at 21:05 UTC
            today = now.strftime("%Y-%m-%d")
            if now.hour == 21 and now.minute >= 5 and last_daily != today:
                trades = state.data.get("trades_today", [])
                telegram.send(
                    f"📋 <b>Gold Daily Summary — {today}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Signals today: {len(trades)}\n"
                    f"Slippage rejected: "
                    f"{state.data.get('signals_slippage_rejected', 0)}\n"
                    f"Daily losses: {state.data['daily_losses']}\n"
                    f"All-time: {state.data['total_wins']}W / "
                    f"{state.data['total_losses']}L ({state.win_rate:.1f}%)\n"
                    f"📡 Binance Futures XAUUSDT\n"
                    f"━━━━━━━━━━━━━━━━━━━━━"
                )
                last_daily = today

        except Exception as e:
            log.error(f"Health check error: {e}")

        shutdown_event.wait(30)


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def run():
    log.info("=" * 55)
    log.info("  GOLD EMA 15 BOT — BINANCE FUTURES (XAUUSDT)")
    log.info("  Data + Execution: Binance Futures API")
    log.info("=" * 55)

    state = BotState()
    dm    = DataManager()
    fe    = FilterEngine(state)

    if not telegram.test():
        log.error("Telegram failed!"); sys.exit(1)

    # ── Binance Trader setup ──────────────────────────────────────
    api_key     = cfg_get("binance", "api_key",     "")
    api_secret  = cfg_get("binance", "api_secret",  "")
    auto_trade  = cfg_get("bot",     "auto_trade",  False)
    testnet     = cfg_get("binance", "testnet",     False)
    leverage    = cfg_get("binance", "leverage",    1)
    margin_type = cfg_get("binance", "margin_type", "ISOLATED")

    trader = None
    if api_key and api_secret and not str(api_key).startswith("PASTE"):
        trader = BinanceFuturesTrader(api_key, api_secret, testnet)
        if trader.is_connected():
            bal = trader.get_balance()
            log.info(f"✅ Binance API connected | Balance: ${bal:,.2f} USDT")
            telegram.send(
                f"✅ <b>Gold Bot — Binance API Connected</b>\n"
                f"Mode: {'TESTNET' if testnet else '🔴 LIVE'}\n"
                f"Balance: ${bal:,.2f} USDT\n"
                f"Auto-trade: {'ON' if auto_trade else 'OFF'}"
            )
        else:
            log.warning("Binance API keys invalid — signal-only mode")
            trader = None
    else:
        log.info("No Binance API keys — signal-only mode")

    sym   = cfg_get("strategy", "symbol",        "XAUUSDT")
    tf5   = cfg_get("strategy", "timeframe",     "5m")
    tf15  = cfg_get("strategy", "htf_timeframe", "15m")
    cnt   = cfg_get("bot", "candle_history_count", 100)
    iv    = cfg_get("bot", "check_interval_seconds", 20)
    ep    = cfg_get("strategy", "ema_period",    15)
    rr    = cfg_get("strategy", "rr_ratio",      3.0)
    mslip = cfg_get("bot", "max_slippage_pct",   0.05)
    bal   = cfg_get("strategy", "account_balance", 1000)
    rp    = cfg_get("strategy", "risk_per_trade_pct", 2.0)

    # Set leverage for gold
    if trader:
        trader.set_margin_type(sym, margin_type)
        trader.set_leverage(sym, leverage)

    # Test data connection
    log.info("Testing Binance Futures XAUUSDT connection...")
    try:
        test_df = dm.fetch(sym, tf5, 10)
        live_px = dm.fetch_live_price(sym)
        log.info(f"✅ Binance Futures XAUUSDT connected — ${live_px:,.2f}")
    except Exception as e:
        log.warning(f"Data test failed: {e} — will retry in loop")

    threading.Thread(
        target=health_loop, args=(state, dm, trader), daemon=True
    ).start()

    telegram.send(
        f"🟡 <b>Gold Bot Started</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💛 XAUUSDT Perpetual (Binance TradFi)\n"
        f"📊 EMA{ep} | 5M/15M | 1:{rr:.0f} RR\n"
        f"📡 Data: Binance Futures (free, live)\n"
        f"🤖 Auto-trade: {'ON — LIVE' if auto_trade and trader else 'OFF (signal only)'}\n"
        f"🔍 Slippage guard: {mslip}%\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Running 24/7 ☁️"
    )

    log.info(f"Symbol: {sym} | TF: {tf5}/{tf15} | EMA: {ep} | RR: 1:{rr}")
    log.info(f"Auto-trade: {auto_trade} | Leverage: {leverage}x")
    log.info("Entering main loop...\n")

    last = state.data.get("last_candle_time")
    errs = 0

    while not shutdown_event.is_set():
        try:
            state.reset_daily()

            df_5m = dm.fetch(sym, tf5, cnt)

            if len(df_5m) < ep + 5:
                log.warning(f"Only {len(df_5m)} bars — waiting")
                shutdown_event.wait(iv)
                continue

            latest = str(df_5m.index[-1])
            if latest == last:
                shutdown_event.wait(iv)
                continue

            last = latest
            state.data["last_candle_time"] = latest
            state.tick_cooldown()

            sig = detect_signal(df_5m)
            if not sig:
                state.save()
                shutdown_event.wait(iv)
                continue

            log.info(f"🔔 GOLD {sig['direction']} @ ${sig['entry']:,.2f}")

            sig_hash = f"{sig['direction']}_{sig['time']}_{sig['entry']:.0f}"
            if state.is_duplicate(sig_hash):
                log.info("↩️ Duplicate signal — skipping")
                continue

            try:
                df_15m = dm.fetch(sym, tf15, cnt)
            except Exception as e:
                log.warning(f"15M fetch failed: {e}")
                df_15m = None

            candle = df_5m.iloc[-1]
            ct     = df_5m.index[-1]
            atr    = calc_atr(df_5m, cfg_get("strategy", "atr_period", 14)).iloc[-1]
            fr     = fe.run_all(sig, candle, ct, df_5m, df_15m)

            if not all(p for p, _ in fr):
                failed = [d for p, d in fr if not p]
                log.info(f"❌ Rejected → {failed}")
                state.data["signals_rejected"] = (
                    state.data.get("signals_rejected", 0) + 1
                )
                if cfg_get("bot", "send_rejected_signals", False):
                    telegram.send(
                        f"⚪ <b>Rejected:</b> GOLD {sig['direction']} "
                        f"@ ${sig['entry']:,.2f}\n"
                        + "\n".join(f"  {f}" for f in failed)
                    )
                state.save()
                shutdown_event.wait(iv)
                continue

            slip_ok, live_price, slip_pct = check_slippage(dm, sym, sig["entry"])

            if not slip_ok:
                state.data["signals_slippage_rejected"] = (
                    state.data.get("signals_slippage_rejected", 0) + 1
                )
                telegram.send(
                    f"⚠️ <b>Gold Signal Cancelled — Slippage</b>\n"
                    f"{sig['direction']} @ ${sig['entry']:,.2f}\n"
                    f"Live: ${live_price:,.2f} | Slip: {slip_pct:.3f}%"
                )
                continue

            # ── ALL FILTERS PASSED ────────────────────────────────
            log.info("✅ ALL FILTERS PASSED!")

            # Auto-trade execution
            order_result = None
            if auto_trade and trader:
                pos  = calc_position_size(bal, rp, sig["risk"], live_price, leverage)
                side = "BUY" if sig["direction"] == "LONG" else "SELL"
                if pos["qty"] >= 0.01:
                    order_result = trader.place_order(
                        sym, side, pos["qty"], sig["sl"], sig["tp"]
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
                    log.warning(f"Position too small: {pos['qty']} oz — skipping")

            alert = format_alert(sig, fr, atr, state, live_price, slip_pct, order_result)
            telegram.send(alert)

            state.data["signals_sent"]  = state.data.get("signals_sent", 0) + 1
            state.data["total_trades"]  = state.data.get("total_trades", 0) + 1
            state.data["trades_today"].append({
                "time":        sig["time"],
                "dir":         sig["direction"],
                "entry":       live_price,
                "auto_traded": bool(order_result and order_result.get("success"))
            })

            state.save()
            errs = 0

        except KeyboardInterrupt:
            break
        except Exception as e:
            errs += 1
            log.error(f"Error ({errs}): {e}", exc_info=True)
            if errs == 5:
                telegram.send(
                    f"⚠️ <b>Gold Bot: 5 errors</b>\n"
                    f"<code>{str(e)[:300]}</code>"
                )
            if errs >= 10:
                telegram.send(
                    f"🔴 <b>Gold Bot Critical</b>\nPausing 5min.\n"
                    f"<code>{str(e)[:200]}</code>"
                )
                shutdown_event.wait(300)
                errs = 0
            else:
                shutdown_event.wait(min(60, iv * 2))
            continue

        shutdown_event.wait(iv)

    telegram.send(
        f"🔴 <b>Gold Bot Stopped</b>\n"
        f"At {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )
    state.save()
    log.info("Gold bot shutdown complete.")

    telegram.send(
        f"🟡 <b>Gold Bot Started</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💛 XAUUSDT Perpetual (Binance TradFi)\n"
        f"📊 EMA{ep} | 5M/15M | 1:{rr:.0f} RR\n"
        f"📡 Data: Binance Futures (free, live)\n"
        f"🔍 Slippage guard: {mslip}%\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Running 24/7 ☁️"
    )

    log.info(f"Symbol: {sym} | TF: {tf5}/{tf15} | EMA: {ep} | RR: 1:{rr}")
    log.info("Entering main loop...\n")

    last = state.data.get("last_candle_time")
    errs = 0

    while not shutdown_event.is_set():
        try:
            state.reset_daily()

            # Fetch XAUUSDT candles from Binance Futures
            df_5m = dm.fetch(sym, tf5, cnt)

            if len(df_5m) < ep + 5:
                log.warning(f"Only {len(df_5m)} bars — waiting")
                shutdown_event.wait(iv)
                continue

            latest = str(df_5m.index[-1])
            if latest == last:
                shutdown_event.wait(iv)
                continue

            last = latest
            state.data["last_candle_time"] = latest
            state.tick_cooldown()

            # Detect EMA crossover
            sig = detect_signal(df_5m)
            if not sig:
                state.save()
                shutdown_event.wait(iv)
                continue

            log.info(f"🔔 GOLD {sig['direction']} @ ${sig['entry']:,.2f}")

            # Duplicate check
            sig_hash = f"{sig['direction']}_{sig['time']}_{sig['entry']:.0f}"
            if state.is_duplicate(sig_hash):
                log.info("↩️ Duplicate signal — skipping")
                continue

            # Fetch 15M for HTF filter
            try:
                df_15m = dm.fetch(sym, tf15, cnt)
            except Exception as e:
                log.warning(f"15M fetch failed: {e}")
                df_15m = None

            candle = df_5m.iloc[-1]
            ct     = df_5m.index[-1]
            atr    = calc_atr(df_5m, cfg_get("strategy", "atr_period", 14)).iloc[-1]
            fr     = fe.run_all(sig, candle, ct, df_5m, df_15m)

            if not all(p for p, _ in fr):
                failed = [d for p, d in fr if not p]
                log.info(f"❌ Rejected → {failed}")
                state.data["signals_rejected"] = (
                    state.data.get("signals_rejected", 0) + 1
                )
                if cfg_get("bot", "send_rejected_signals", False):
                    telegram.send(
                        f"⚪ <b>Rejected:</b> GOLD {sig['direction']} "
                        f"@ ${sig['entry']:,.2f}\n"
                        + "\n".join(f"  {f}" for f in failed)
                    )
                state.save()
                shutdown_event.wait(iv)
                continue

            # Slippage guard
            slip_ok, live_price, slip_pct = check_slippage(dm, sym, sig["entry"])

            if not slip_ok:
                state.data["signals_slippage_rejected"] = (
                    state.data.get("signals_slippage_rejected", 0) + 1
                )
                telegram.send(
                    f"⚠️ <b>Gold Signal Cancelled — Slippage</b>\n"
                    f"{sig['direction']} @ ${sig['entry']:,.2f}\n"
                    f"Live: ${live_price:,.2f} | Slip: {slip_pct:.3f}%"
                )
                continue

            # All filters passed — send alert
            log.info("✅ ALL FILTERS PASSED!")
            alert = format_alert(sig, fr, atr, state, live_price, slip_pct)
            telegram.send(alert)

            state.data["signals_sent"] = state.data.get("signals_sent", 0) + 1
            state.data["total_trades"] = state.data.get("total_trades", 0) + 1
            state.data["trades_today"].append({
                "time":  sig["time"],
                "dir":   sig["direction"],
                "entry": live_price
            })

            state.save()
            errs = 0

        except KeyboardInterrupt:
            break
        except Exception as e:
            errs += 1
            log.error(f"Error ({errs}): {e}", exc_info=True)
            if errs == 5:
                telegram.send(
                    f"⚠️ <b>Gold Bot: 5 errors</b>\n"
                    f"<code>{str(e)[:300]}</code>"
                )
            if errs >= 10:
                telegram.send(
                    f"🔴 <b>Gold Bot Critical</b>\nPausing 5min.\n"
                    f"<code>{str(e)[:200]}</code>"
                )
                shutdown_event.wait(300)
                errs = 0
            else:
                shutdown_event.wait(min(60, iv * 2))
            continue

        shutdown_event.wait(iv)

    telegram.send(
        f"🔴 <b>Gold Bot Stopped</b>\n"
        f"At {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )
    state.save()
    log.info("Gold bot shutdown complete.")


if __name__ == "__main__":
    print("""
    ╔══════════════════════════════════════════════╗
    ║   GOLD EMA 15 — BINANCE FUTURES BOT         ║
    ║   XAUUSDT Perp | 5M | 1:3 RR | 24/7        ║
    ║   Data: Binance Futures (free, live)        ║
    ╚══════════════════════════════════════════════╝
    """)
    run()
