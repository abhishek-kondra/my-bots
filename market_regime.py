"""
market_regime.py
────────────────────────────────────────────────────────
Drop this file into BOTH bot directories:
  /root/gold_bot/market_regime.py
  /root/new_bots/market_regime.py

Then call analyze_regime() once per cycle in each bot.
────────────────────────────────────────────────────────
"""

import math
import json
import os
import requests
from datetime import datetime

# ─── Thresholds ───────────────────────────────────────
ADX_PERIOD    = 14
CHOP_PERIOD   = 14
ADX_TREND     = 25      # ADX above this → trend present
ADX_WEAK      = 20      # ADX below this → no trend / choppy
CHOP_CHOPPY   = 61.8    # CHOP above this → definitely choppy
CHOP_TRENDING = 61.8    # CHOP below this + ADX > 25 → trending


# ─── ADX (Wilder's) ───────────────────────────────────
def calculate_adx(candles, period=ADX_PERIOD):
    """
    candles = Binance klines list
    Each candle: [open_time, open, high, low, close, ...]
    Returns ADX float, or None if not enough data.
    """
    if len(candles) < period * 2 + 5:
        return None

    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]

    tr_list, plus_dm_list, minus_dm_list = [], [], []

    for i in range(1, len(candles)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        ph, pl   = highs[i - 1], lows[i - 1]

        tr       = max(h - l, abs(h - pc), abs(l - pc))
        up_move  = h - ph
        dn_move  = pl - l

        tr_list.append(tr)
        plus_dm_list.append(up_move  if (up_move  > dn_move and up_move  > 0) else 0)
        minus_dm_list.append(dn_move if (dn_move  > up_move and dn_move  > 0) else 0)

    def wilder(data, p):
        s = [sum(data[:p])]
        for v in data[p:]:
            s.append(s[-1] - s[-1] / p + v)
        return s

    s_tr  = wilder(tr_list,       period)
    s_pdm = wilder(plus_dm_list,  period)
    s_mdm = wilder(minus_dm_list, period)

    dx_list = []
    for i in range(len(s_tr)):
        if s_tr[i] == 0:
            continue
        pdi  = 100 * s_pdm[i] / s_tr[i]
        mdi  = 100 * s_mdm[i] / s_tr[i]
        dsum = pdi + mdi
        dx_list.append(100 * abs(pdi - mdi) / dsum if dsum != 0 else 0)

    if len(dx_list) < period:
        return None

    return round(wilder(dx_list, period)[-1], 2)


# ─── Choppiness Index ─────────────────────────────────
def calculate_choppiness(candles, period=CHOP_PERIOD):
    """
    Returns CHOP float (0-100), or None if not enough data.
    Below 38.2 → trending  |  Above 61.8 → choppy
    """
    if len(candles) < period + 1:
        return None

    recent = candles[-(period + 1):]
    highs  = [float(c[2]) for c in recent]
    lows   = [float(c[3]) for c in recent]
    closes = [float(c[4]) for c in recent]

    atr_sum = 0
    for i in range(1, len(recent)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        atr_sum += max(h - l, abs(h - pc), abs(l - pc))

    hh       = max(highs[1:])
    ll       = min(lows[1:])
    hl_range = hh - ll

    if hl_range == 0 or atr_sum == 0:
        return None

    chop = 100 * math.log10(atr_sum / hl_range) / math.log10(period)
    return round(chop, 2)


# ─── Regime Classification ────────────────────────────
def get_regime(adx, chop):
    if adx is None or chop is None:
        return "UNKNOWN"
    if adx > ADX_TREND and chop < CHOP_CHOPPY:
        return "TRENDING"
    elif adx < ADX_WEAK or chop > CHOP_CHOPPY:
        return "CHOPPY"
    else:
        return "NEUTRAL"


# ─── State (tracks last regime per bot) ───────────────
def _state_path(bot_name):
    return f"regime_state_{bot_name.lower().replace(' ', '_')}.json"

def _load_state(bot_name):
    p = _state_path(bot_name)
    if os.path.exists(p):
        with open(p, "r") as f:
            return json.load(f)
    return {"last_regime": None}

def _save_state(bot_name, state):
    with open(_state_path(bot_name), "w") as f:
        json.dump(state, f)


# ─── Telegram ─────────────────────────────────────────
def _send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        print(f"[REGIME] Telegram error: {e}")


# ─── Main Entry Point ─────────────────────────────────
EMOJI = {
    "TRENDING": "✅",
    "CHOPPY":   "⚠️",
    "NEUTRAL":  "⏸️",
    "UNKNOWN":  "❓"
}

def analyze_regime(candles, bot_name, telegram_token, telegram_chat_id):
    """
    Call once per cycle after fetching candles.

    Parameters
    ----------
    candles          : list  — same klines list the bot already fetches
    bot_name         : str   — "Gold Bot" or "BTC Bot"
    telegram_token   : str   — your bot token
    telegram_chat_id : str   — your chat/channel ID

    Returns
    -------
    (regime: str, adx: float, chop: float)
    """
    adx    = calculate_adx(candles)
    chop   = calculate_choppiness(candles)
    regime = get_regime(adx, chop)
    ts     = datetime.now().strftime("%H:%M:%S")
    emoji  = EMOJI.get(regime, "❓")

    # ── Console log (PM2 captures this) ───────────────
    print(f"[MARKET REGIME | {bot_name}] {ts} | ADX: {adx} | CHOP: {chop} | {emoji} {regime}")

    # ── Telegram only on regime change ─────────────────
    state = _load_state(bot_name)
    last  = state.get("last_regime")

    if regime != "UNKNOWN" and regime != last:
        msg = (
            f"🔔 <b>Market Regime Change</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Bot  :</b> {bot_name}\n"
            f"<b>From :</b> {EMOJI.get(last, '❓')} {last or 'N/A'}\n"
            f"<b>To   :</b> {emoji} {regime}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>ADX  :</b> {adx}\n"
            f"<b>CHOP :</b> {chop}\n"
            f"🕐 {ts}"
        )
        _send_telegram(telegram_token, telegram_chat_id, msg)
        state["last_regime"] = regime
        _save_state(bot_name, state)

    return regime, adx, chop
