# -*- coding: utf-8 -*-
"""
Tactical Neural Matrix - voice-first command center (Kivy / Android).

  * PAPER_MODE = True      -> the app can never place a real order.
  * KILL SWITCH            -> button or voice: closes every position, halts trading
                              (survives restarts, cleared only by RE-ARM in CONFIG).
  * POSITION CALCULATOR    -> every trade is sized from capital, risk % (max 2),
                              stop loss and target. CALC button for manual what-if.
  * SMART TREND & MOMENTUM SHIELD
                           -> trend, momentum, volatility spikes, traps, over-trading.
  * MULTI-TIMEFRAME CONFLUENCE
                           -> a trade needs the traded timeframe (5 min) AND the
                              higher one (15 min) to point the same way.
  * AUTO JOURNAL + EMOTION TRACKER
                           -> every trade is recorded; mistakes are detected from
                              behaviour and the mood you tell the app; evening feedback.
  * LEARN (sibling tool)   -> separate Rs 1,00,000 virtual account. Never sends orders.
  * Auto scan every 60 s, broker connection + token expiry alerts, crash guards.
"""
import functools
import json
import os
import re
import tempfile
import threading
import time
import traceback
from collections import deque
from datetime import datetime

from kivy.app import App
from kivy.base import ExceptionHandler, ExceptionManager
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.graphics import Color, Line, RoundedRectangle
from kivy.logger import Logger
from kivy.metrics import dp, sp
from kivy.properties import ListProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.utils import platform

from agents_module import (BUY, SELL, MAX_RISK_PCT, MIN_AGREE_FLOOR, MIN_CANDLES,
                           MIN_RR_FLOOR, Candle, ConsensusEngine, RiskManager,
                           build_context, ema, rsi)
from broker_connector import BrokerError, KiteBroker

try:
    from voice_alerts import DEFAULT_VOICE_ID, VoiceAlerts
except Exception:  # voice must never stop the app from starting
    DEFAULT_VOICE_ID = "mAcTWQg9kibLro6Z2ouY"
    VoiceAlerts = None

# ======================================================================= #
#  MASTER SAFETY SWITCH
#  True  = practice only. The app cannot place a real order. (recommended)
#  False = LIVE mode becomes selectable (still needs login + confirmation).
# ======================================================================= #
PAPER_MODE = True

# ----------------------------- palette ------------------------------------ #
BG = (0.016, 0.016, 0.024, 1)
PANEL = (0.047, 0.055, 0.075, 1)
PANEL_HI = (0.07, 0.09, 0.12, 1)
CYAN = (0.0, 0.94, 1.0, 1)
GREEN = (0.1, 1.0, 0.55, 1)
RED = (1.0, 0.22, 0.36, 1)
AMBER = (1.0, 0.75, 0.1, 1)
TEXT = (0.86, 0.93, 0.96, 1)
DIM = (0.45, 0.53, 0.60, 1)

DEFAULT_CFG = {
    "api_key": "", "api_secret": "", "mode": "PAPER", "exchange": "NSE", "symbol": "RELIANCE",
    "interval": "5minute", "capital": 100000.0, "risk_pct": 1.0, "min_rr": 2.0, "min_agree": 6,
    "product": "MIS", "max_qty": 500,
    "eleven_key": "", "voice_id": DEFAULT_VOICE_ID, "voice_alerts": True,
    "auto_scan": True, "scan_secs": 60, "auto_trade": True,
}

HTF_MAP = {"minute": "15minute", "3minute": "15minute", "5minute": "15minute",
           "10minute": "30minute", "15minute": "60minute", "30minute": "60minute",
           "60minute": "day", "day": None}
MINUTES = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15,
           "30minute": 30, "60minute": 60}

CONFIRM_WORDS = {"confirm", "yes", "go", "proceed", "okay"}
CANCEL_WORDS = {"cancel", "abort", "no", "stop"}
CONFIRM_HI = ("हाँ", "हां", "कन्फर्म", "ठीक है")
CANCEL_HI = ("नहीं", "रद्द", "कैंसल")
KILL_WORDS = ("kill", "emergency", "panic", "sab band", "band karo",
              "सब बंद", "बंद करो", "किल", "इमरजेंसी", "इमरजेन्सी", "आपातकाल")
LEARN_WORDS = ("learn", "practice", "practise", "सीख", "प्रैक्टिस", "लर्न")

MOODS = ("CALM", "EXCITED", "ANXIOUS", "ANGRY")
BAD_MOODS = ("EXCITED", "ANXIOUS", "ANGRY")
MOOD_WORDS = (("calm", "CALM"), ("relaxed", "CALM"), ("शांत", "CALM"),
              ("excited", "EXCITED"), ("उत्साह", "EXCITED"),
              ("anxious", "ANXIOUS"), ("nervous", "ANXIOUS"), ("scared", "ANXIOUS"),
              ("worried", "ANXIOUS"), ("डर", "ANXIOUS"),
              ("angry", "ANGRY"), ("upset", "ANGRY"), ("गुस्सा", "ANGRY"), ("ग़ुस्सा", "ANGRY"))
MOOD_TRIGGERS = ("mood", "feel", "मूड", "महसूस")

MISTAKE_FLAGS = ("OVERTRADING", "REVENGE", "IGNORED_SHIELD", "BAD_MOOD", "EARLY_EXIT", "PANIC_EXIT")
MISTAKE_NAMES = {"OVERTRADING": "over trading", "REVENGE": "trading right after a loss",
                 "IGNORED_SHIELD": "ignoring a shield caution", "BAD_MOOD": "trading while not calm",
                 "EARLY_EXIT": "closing a winner by hand too early",
                 "PANIC_EXIT": "closing a losing trade by hand"}
MISTAKE_TIPS = {
    "OVERTRADING": "Tip for tomorrow. More trades do not mean more profit. Stop after two good trades.",
    "REVENGE": "Tip for tomorrow. After a loss, wait at least thirty minutes before the next trade.",
    "IGNORED_SHIELD": "Tip for tomorrow. When the shield says caution, wait for it to turn clear.",
    "BAD_MOOD": "Tip for tomorrow. If you feel angry, scared or too excited, skip trading. The market will be there tomorrow.",
    "EARLY_EXIT": "Tip for tomorrow. You closed a winner early. Let the target do its job, because that is how the one to two rule pays.",
    "PANIC_EXIT": "Tip for tomorrow. You closed a losing trade by hand. Trust the stop loss you planned before you entered.",
}


# ----------------------------- crash protection --------------------------- #
class CrashGuard(ExceptionHandler):
    """Last line of defence: unhandled Kivy exceptions are logged, not fatal."""

    def handle_exception(self, inst):
        try:
            Logger.error("TNM: unhandled exception: %s" % inst)
            Logger.error(traceback.format_exc())
        except Exception:
            pass
        return ExceptionManager.PASS


ExceptionManager.add_handler(CrashGuard())


def safe(default=None):
    """Decorator: any exception is logged and shown in the app log instead of crashing."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                try:
                    Logger.error("TNM: %s failed: %s" % (fn.__name__, exc))
                    Logger.error(traceback.format_exc())
                    app = App.get_running_app()
                    if app is not None:
                        app.log("Recovered from an error in %s: %s" % (fn.__name__, exc))
                except Exception:
                    pass
                return default
        return wrapper
    return deco


# ----------------------------- helpers ------------------------------------ #
def market_open(now=None):
    """NSE/BSE cash hours, Mon-Fri 09:15-15:30, using the phone's local time (assumes IST)."""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


def chunk_text(text, limit=230):
    """Split speech into pieces that fit the per-call length cap of the voice service."""
    pieces = []
    for p in re.split(r"(?<=[.!?])\s+", str(text)):
        p = p.strip()
        while len(p) > limit:
            cut = p.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            pieces.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            pieces.append(p)
    out, cur = [], ""
    for p in pieces:
        if cur and len(cur) + 1 + len(p) > limit:
            out.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        out.append(cur)
    return out


def speakable(text):
    return str(text).replace("%", " percent").replace(":", " to ").replace("/", " of ")


def side_word(side):
    return "Buy" if side == BUY else "Sell"


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def tf_name(itv):
    if itv == "day":
        return "daily"
    if itv == "minute":
        return "1 minute"
    return str(itv).replace("minute", " minute")


def tf_short(itv):
    if itv is None:
        return "-"
    if itv == "day":
        return "1d"
    return str(itv).replace("minute", "m") if itv != "minute" else "1m"


TREND_WORD = {"UP": "up", "DOWN": "down", "SIDEWAYS": "sideways", None: "unknown"}


# ----------------------------- POSITION SIZING & RISK-REWARD -------------- #
def plan_position(capital, risk_pct, entry, sl, tp, max_qty):
    """
    Risk-to-reward and position sizing calculator.
    Quantity = (capital x risk%) / stop distance, then capped by the money you
    actually have (no leverage assumed) and by the max quantity setting.
    Risk % is hard-capped at MAX_RISK_PCT. Returns a dict with ok / reason.
    """
    out = {"ok": False, "reason": "", "side": "", "qty": 0, "per_unit": 0.0, "risk_amt": 0.0,
           "reward_amt": 0.0, "rr": 0.0, "value": 0.0, "risk_pct": 0.0, "capital_pct": 0.0,
           "sl_pct": 0.0, "limit": "risk", "capped": False}
    try:
        capital = float(capital)
        risk_pct = float(risk_pct)
        entry = float(entry)
        sl = float(sl)
        tp = float(tp)
        max_qty = int(max_qty)
    except (TypeError, ValueError):
        out["reason"] = "some numbers are missing"
        return out
    if capital <= 0 or entry <= 0 or sl <= 0 or tp <= 0:
        out["reason"] = "capital, entry, stop loss and target must all be above zero"
        return out
    if sl < entry < tp:
        side = BUY
    elif tp < entry < sl:
        side = SELL
    else:
        out["reason"] = "the stop loss and the target must be on opposite sides of the entry price"
        return out
    out["capped"] = risk_pct > MAX_RISK_PCT
    risk_pct = max(0.0, min(risk_pct, MAX_RISK_PCT))
    if risk_pct <= 0:
        out["reason"] = "risk percent must be above zero"
        return out
    per_unit = abs(entry - sl)
    reward_unit = abs(tp - entry)
    by_risk = int((capital * risk_pct / 100.0) // per_unit)
    by_cap = int(capital // entry)
    qty, limit = min([(by_risk, "risk"), (by_cap, "capital"), (max_qty, "max quantity")],
                     key=lambda x: x[0])
    out.update({"side": side, "per_unit": per_unit, "risk_pct": risk_pct, "limit": limit,
                "sl_pct": per_unit / entry * 100.0, "rr": reward_unit / per_unit})
    if qty < 1:
        out["reason"] = ("the quantity would be zero. The stop is too wide for this capital and risk "
                         "percent, or the price is higher than your capital")
        return out
    out["qty"] = qty
    out["risk_amt"] = qty * per_unit
    out["reward_amt"] = qty * reward_unit
    out["value"] = qty * entry
    out["capital_pct"] = out["risk_amt"] / capital * 100.0
    out["ok"] = True
    return out


# ----------------------------- MULTI-TIMEFRAME CONFLUENCE ----------------- #
def ema_trend(candles):
    """UP / DOWN / SIDEWAYS from EMA9 vs EMA21 and price; None if too few candles."""
    if not candles or len(candles) < 30:
        return None
    closes = [c.c for c in candles]
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    slope = e21[-1] - e21[-4]
    if e9[-1] > e21[-1] and closes[-1] > e21[-1] and slope > 0:
        return "UP"
    if e9[-1] < e21[-1] and closes[-1] < e21[-1] and slope < 0:
        return "DOWN"
    return "SIDEWAYS"


def resample(candles, factor):
    """Merge `factor` candles into one (aligned from the newest candle, so the last bucket is complete)."""
    if factor <= 1 or not candles:
        return list(candles or [])
    n = len(candles)
    out = []
    for i in range(n % factor, n, factor):
        grp = candles[i:i + factor]
        if len(grp) < factor:
            break
        out.append(Candle(grp[0].t, grp[0].o, max(c.h for c in grp), min(c.l for c in grp),
                          grp[-1].c, sum(c.v for c in grp)))
    return out


def fetch_count(itv):
    m = MINUTES.get(itv)
    hm = MINUTES.get(HTF_MAP.get(itv))
    if m and hm and hm > m and hm % m == 0:
        return max(200, 35 * (hm // m))
    return 200


# ----------------------------- SMART TREND & MOMENTUM SHIELD -------------- #
def compute_shield(candles, direction=None):
    """
    Judges the market behind the scenes. Returns a dict:
      level   CLEAR / CAUTION / BLOCK / UNKNOWN
      trend   UP / DOWN / SIDEWAYS        (EMA9 vs EMA21 and price)
      momentum UP / DOWN / FLAT           (EMA9 slope + RSI)
      vol_ratio  recent range vs normal range
      blocks / cautions   plain-English reasons
    """
    out = {"level": "UNKNOWN", "trend": "SIDEWAYS", "momentum": "FLAT", "vol_ratio": 1.0,
           "blocks": [], "cautions": []}
    if not candles or len(candles) < 40:
        out["cautions"].append("not enough candles to judge momentum yet")
        return out

    closes = [c.c for c in candles]
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    rs = rsi(closes)
    last = candles[-1]
    slope = e21[-1] - e21[-4]
    if e9[-1] > e21[-1] and last.c > e21[-1] and slope > 0:
        out["trend"] = "UP"
    elif e9[-1] < e21[-1] and last.c < e21[-1] and slope < 0:
        out["trend"] = "DOWN"
    mom9 = e9[-1] - e9[-4]
    if rs is not None:
        if mom9 > 0 and rs > 52:
            out["momentum"] = "UP"
        elif mom9 < 0 and rs < 48:
            out["momentum"] = "DOWN"

    trs = [max(c.h - c.l, abs(c.h - p.c), abs(c.l - p.c)) for p, c in zip(candles[:-1], candles[1:])]
    base = trs[:-5][-50:]
    long_avg = sum(base) / len(base) if base else 0.0
    short_avg = sum(trs[-5:]) / 5.0
    ratio = (short_avg / long_avg) if long_avg > 0 else 1.0
    spike = (trs[-1] / long_avg) if long_avg > 0 else 1.0
    out["vol_ratio"] = round(ratio, 2)
    if ratio >= 2.0 or spike >= 3.5:
        out["blocks"].append("volatility is extreme, about %.1f times normal" % max(ratio, spike))
    elif ratio >= 1.5:
        out["cautions"].append("volatility is rising")

    window = candles[-12:-2]
    hi = max(c.h for c in window)
    lo = min(c.l for c in window)
    bull_trap = any(c.h > hi for c in candles[-2:]) and last.c < hi
    bear_trap = any(c.l < lo for c in candles[-2:]) and last.c > lo

    rng = last.h - last.l
    body = abs(last.c - last.o)
    upper = last.h - max(last.o, last.c)
    lower = min(last.o, last.c) - last.l
    floor = max(body, rng * 0.05)

    if direction in (BUY, SELL):
        opp_trend = (direction == BUY and out["trend"] == "DOWN") or (direction == SELL and out["trend"] == "UP")
        opp_mom = (direction == BUY and out["momentum"] == "DOWN") or (direction == SELL and out["momentum"] == "UP")
        if opp_trend:
            out["blocks"].append("the short term trend is against this %s" % direction.lower())
        elif opp_mom:
            out["cautions"].append("momentum is fading against this %s" % direction.lower())
        elif out["trend"] == "SIDEWAYS":
            out["cautions"].append("the market is choppy and sideways")
        if rs is not None:
            if direction == BUY and rs > 78:
                out["blocks"].append("price is overextended, so buying now means chasing")
            if direction == SELL and rs < 22:
                out["blocks"].append("price is overextended, so selling now means chasing")
        if direction == BUY and bull_trap:
            out["blocks"].append("a possible bull trap: price broke up but fell back")
        if direction == SELL and bear_trap:
            out["blocks"].append("a possible bear trap: price broke down but bounced back")
        if direction == BUY and rng > 0 and upper > 2 * floor and upper > 0.5 * rng:
            out["cautions"].append("the last candle shows sellers rejecting higher prices")
        if direction == SELL and rng > 0 and lower > 2 * floor and lower > 0.5 * rng:
            out["cautions"].append("the last candle shows buyers rejecting lower prices")
    else:
        if bull_trap or bear_trap:
            out["cautions"].append("a possible trap just formed")

    out["level"] = "BLOCK" if out["blocks"] else ("CAUTION" if out["cautions"] else "CLEAR")
    return out


def shield_level(sh):
    if sh["blocks"]:
        return "BLOCK"
    if sh["level"] == "UNKNOWN":
        return "UNKNOWN"
    if sh["cautions"]:
        return "CAUTION"
    return "CLEAR"


def shield_sentence(sh):
    lv = sh["level"]
    if lv == "BLOCK":
        return "Shield alert. Do not enter now. %s." % "; ".join(sh["blocks"])
    if lv == "CAUTION":
        return "Shield caution. %s." % "; ".join(sh["cautions"])
    if lv == "CLEAR":
        return "Shield is clear. Trend is %s and momentum is %s." % (
            sh["trend"].lower(), sh["momentum"].lower())
    return "Shield cannot judge yet. %s." % "; ".join(sh["cautions"] or ["no data"])


class TradeGovernor:
    """Stops over-trading: limits per hour/day and a cooling-off period after a loss."""
    MAX_PER_HOUR = 2
    MAX_PER_DAY = 4
    COOLDOWN_SECS = 900

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self.state = {"date": datetime.now().date().isoformat(), "times": [], "last_loss": 0.0}
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.state.update(json.load(f))
        except Exception:
            pass
        self._roll()

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def _roll(self):
        today = datetime.now().date().isoformat()
        if self.state.get("date") != today:
            self.state = {"date": today, "times": [], "last_loss": 0.0}
            self._save()

    def record_open(self):
        with self._lock:
            self._roll()
            self.state["times"].append(time.time())
            self._save()

    def record_loss(self):
        with self._lock:
            self._roll()
            self.state["last_loss"] = time.time()
            self._save()

    def minutes_since_loss(self):
        with self._lock:
            last = float(self.state.get("last_loss") or 0.0)
            if last <= 0:
                return None
            return (time.time() - last) / 60.0

    def block_reasons(self):
        with self._lock:
            self._roll()
            now = time.time()
            r = []
            times = self.state["times"]
            if len(times) >= self.MAX_PER_DAY:
                r.append("the daily limit of %d trades is reached" % self.MAX_PER_DAY)
            recent = [t for t in times if now - t < 3600]
            if len(recent) >= self.MAX_PER_HOUR:
                r.append("already %d trades in the last hour, so stop over-trading" % len(recent))
            left = self.COOLDOWN_SECS - (now - float(self.state.get("last_loss") or 0.0))
            if left > 0:
                r.append("cooling off after a loss, %d more minutes" % max(1, int(left // 60) + 1))
            return r


# ----------------------------- AUTO JOURNAL & EMOTION TRACKER ------------- #
class Journal:
    """Digital trade record. Mistakes come from behaviour and the mood the user reports."""
    MAX = 500

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self.state = {"entries": [], "last_report": ""}
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.state.update(json.load(f))
        except Exception:
            pass
        if not isinstance(self.state.get("entries"), list):
            self.state["entries"] = []

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)
        except Exception:
            pass

    @staticmethod
    def _today():
        return datetime.now().date().isoformat()

    @staticmethod
    def _flag(entry, name):
        fl = entry.setdefault("flags", [])
        if name not in fl:
            fl.append(name)

    def add_open(self, entry):
        with self._lock:
            self.state["entries"].append(entry)
            self.state["entries"] = self.state["entries"][-self.MAX:]
            self._save()

    def close_trade(self, t):
        with self._lock:
            e = next((x for x in reversed(self.state["entries"]) if x.get("id") == t.get("id")), None)
            if e is None:
                e = {"id": t.get("id"), "date": self._today(), "opened_at": t.get("opened_at", ""),
                     "symbol": t.get("symbol", ""), "side": t.get("side", ""), "qty": t.get("qty", 0),
                     "entry": t.get("entry", 0), "mode": t.get("mode", ""), "mood": "UNSET",
                     "shield": "UNKNOWN", "flags": [], "auto": False}
                self.state["entries"].append(e)
            e["exit"] = t.get("exit")
            e["pnl"] = t.get("pnl")
            e["reason"] = t.get("reason")
            e["closed_at"] = t.get("closed_at", "")
            pnl = float(t.get("pnl") or 0.0)
            if t.get("reason") == "MANUAL":
                if pnl > 0:
                    self._flag(e, "EARLY_EXIT")
                elif pnl < 0:
                    self._flag(e, "PANIC_EXIT")
            elif t.get("reason") == "KILL":
                self._flag(e, "KILL_USED")
            self._save()
            return dict(e)

    def entries_on(self, day):
        with self._lock:
            return [dict(e) for e in self.state["entries"] if e.get("date") == day]

    def count_today(self):
        return len(self.entries_on(self._today()))

    def recent(self, n):
        with self._lock:
            return [dict(e) for e in self.state["entries"][-n:]][::-1]

    def mark_reported(self, day):
        with self._lock:
            self.state["last_report"] = day
            self._save()

    def summary(self, day):
        ents = self.entries_on(day)
        closed = [e for e in ents if e.get("pnl") is not None]
        counts = {}
        clean = 0
        for e in ents:
            fl = [f for f in e.get("flags", []) if f in MISTAKE_FLAGS]
            if not fl:
                clean += 1
            for f in fl:
                counts[f] = counts.get(f, 0) + 1
        return {"day": day, "trades": len(ents), "closed": len(closed),
                "wins": sum(1 for e in closed if e["pnl"] > 0),
                "losses": sum(1 for e in closed if e["pnl"] < 0),
                "net": float(sum(e["pnl"] for e in closed)), "open": len(ents) - len(closed),
                "mistakes": counts,
                "score": int(round(100.0 * clean / len(ents))) if ents else 100}

    def mood_stats(self, day):
        n, pnl = 0, 0.0
        for e in self.entries_on(day):
            if e.get("mood") in BAD_MOODS:
                n += 1
                pnl += float(e.get("pnl") or 0.0)
        return n, pnl

    def csv_text(self):
        with self._lock:
            rows = ["date,opened,symbol,side,qty,entry,exit,pnl,reason,mode,mood,shield,flags"]
            for e in self.state["entries"]:
                rows.append(",".join(str(x) for x in (
                    e.get("date", ""), e.get("opened_at", ""), e.get("symbol", ""), e.get("side", ""),
                    e.get("qty", ""), e.get("entry", ""), e.get("exit", ""), e.get("pnl", ""),
                    e.get("reason", ""), e.get("mode", ""), e.get("mood", ""), e.get("shield", ""),
                    "|".join(e.get("flags", [])))))
            return "\n".join(rows)


def build_report(sm, bad_n=0, bad_pnl=0.0):
    if sm["trades"] == 0:
        return ("Evening journal. You took no trades today. Staying out when there is no clear setup "
                "is good discipline.")
    parts = ["Evening journal. %s today. %d won and %d lost. Net result %s %.0f rupees." % (
        plural(sm["trades"], "trade"), sm["wins"], sm["losses"],
        "plus" if sm["net"] >= 0 else "minus", abs(sm["net"]))]
    if sm["open"]:
        parts.append("%s still open." % plural(sm["open"], "trade"))
    parts.append("Discipline score %d percent." % sm["score"])
    counts = sm["mistakes"]
    if counts:
        names = ", ".join("%s %d time%s" % (MISTAKE_NAMES[k], v, "" if v == 1 else "s")
                          for k, v in counts.items())
        parts.append("Mistakes noticed: %s." % names)
        parts.append(MISTAKE_TIPS[max(counts, key=counts.get)])
    else:
        parts.append("No mistakes noticed. Well done. Keep following the plan.")
    if bad_n:
        parts.append("You said you were not calm on %s, and those trades made %s %.0f rupees." % (
            plural(bad_n, "trade"), "plus" if bad_pnl >= 0 else "minus", abs(bad_pnl)))
    return " ".join(parts)


# ----------------------------- widgets ------------------------------------ #
def tlabel(text="", color=TEXT, font=13, bold=False, halign="left", valign="middle", **kw):
    lb = Label(text=text, color=color, font_size=sp(font), bold=bold, halign=halign, valign=valign, **kw)
    lb.bind(size=lambda inst, val: setattr(inst, "text_size", val))
    return lb


class NeonCard(BoxLayout):
    accent = ListProperty([0.0, 0.94, 1.0, 1.0])

    def __init__(self, accent=CYAN, **kw):
        super().__init__(**kw)
        self.accent = list(accent)
        with self.canvas.before:
            Color(*PANEL)
            self._bg = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(12)])
            self._lc = Color(*self.accent)
            self._ln = Line(rounded_rectangle=(self.x, self.y, self.width, self.height, dp(12)),
                            width=dp(1.2))
        self.bind(pos=self._redraw, size=self._redraw, accent=self._redraw)

    def _redraw(self, *a):
        self._bg.pos = self.pos
        self._bg.size = self.size
        self._lc.rgba = self.accent
        self._ln.rounded_rectangle = (self.x, self.y, self.width, self.height, dp(12))


class NeonButton(Button):
    accent = ListProperty([0.0, 0.94, 1.0, 1.0])

    def __init__(self, accent=CYAN, **kw):
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("background_color", (0, 0, 0, 0))
        kw.setdefault("bold", True)
        kw.setdefault("font_size", sp(13))
        super().__init__(**kw)
        self.accent = list(accent)
        with self.canvas.before:
            self._fc = Color(*PANEL_HI)
            self._bg = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(10)])
            self._lc = Color(*self.accent)
            self._ln = Line(rounded_rectangle=(self.x, self.y, self.width, self.height, dp(10)),
                            width=dp(1.3))
        self.bind(pos=self._redraw, size=self._redraw, accent=self._redraw, state=self._redraw)
        self._redraw()

    def _redraw(self, *a):
        acc = self.accent
        self.color = acc
        self._fc.rgba = (acc[0] * 0.35, acc[1] * 0.35, acc[2] * 0.35, 1) if self.state == "down" else PANEL_HI
        self._bg.pos = self.pos
        self._bg.size = self.size
        self._lc.rgba = acc
        self._ln.rounded_rectangle = (self.x, self.y, self.width, self.height, dp(10))


class AgentCard(NeonCard):
    def __init__(self, index, name, **kw):
        super().__init__(accent=DIM, orientation="vertical", padding=dp(8), spacing=dp(2),
                         size_hint_y=None, height=dp(122), **kw)
        self.title = tlabel("%02d  %s" % (index, name), color=DIM, font=10, bold=True,
                            size_hint_y=None, height=dp(16))
        self.signal = tlabel("STANDBY", color=DIM, font=18, bold=True, size_hint_y=None, height=dp(28))
        self.conf = tlabel("", color=DIM, font=10, size_hint_y=None, height=dp(14))
        self.reason = tlabel("awaiting scan", color=DIM, font=10, valign="top")
        for w in (self.title, self.signal, self.conf, self.reason):
            self.add_widget(w)

    def set_vote(self, vote):
        if vote.veto:
            col, txt = AMBER, "LOCKED"
        elif vote.signal == BUY:
            col, txt = GREEN, "BUY"
        elif vote.signal == SELL:
            col, txt = RED, "SELL"
        else:
            col, txt = DIM, "NEUTRAL"
        self.accent = list(col)
        self.signal.text = txt
        self.signal.color = col
        self.title.color = col
        self.conf.text = "CONF %d%%" % int(vote.confidence * 100)
        self.reason.text = vote.reason
        self.reason.color = TEXT


# ----------------------------- speech ------------------------------------- #
class NativeTTS:
    """Android's built-in text-to-speech. Used when no ElevenLabs key is set."""

    def __init__(self, log):
        self._log = log
        self.tts = None
        self.failed = False
        self.ready_at = 0.0

    def speak(self, text, priority=False):
        if platform != "android" or self.failed:
            return
        Clock.schedule_once(lambda dt: self._speak_main(text, priority, 0))

    def _speak_main(self, text, priority, attempt):
        try:
            if self.tts is None:
                from jnius import autoclass
                TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                self.tts = TextToSpeech(PythonActivity.mActivity, None)
                self.ready_at = time.time() + 2.0
            if time.time() < self.ready_at and attempt < 4:
                Clock.schedule_once(lambda dt: self._speak_main(text, priority, attempt + 1), 1.0)
                return
            self.tts.speak(text, 0 if priority else 1, None, "tnm")
        except Exception as exc:
            self.failed = True
            self._log("Built-in voice unavailable: %s" % exc)


class Speaker:
    """Routes speech to ElevenLabs or the built-in voice. Never raises."""

    def __init__(self, cache_dir, cfg, log):
        self._log = log
        self.enabled = bool(cfg.get("voice_alerts", True))
        self.native = NativeTTS(log)
        self.eleven = None
        try:
            if VoiceAlerts is not None:
                self.eleven = VoiceAlerts(cache_dir, cfg.get("eleven_key", ""),
                                          cfg.get("voice_id", DEFAULT_VOICE_ID), True, log=log)
        except Exception as exc:
            log("ElevenLabs voice disabled: %s" % exc)

    def configure(self, api_key, voice_id, enabled):
        self.enabled = bool(enabled)
        try:
            if self.eleven is not None:
                self.eleven.configure(api_key, voice_id, True)
        except Exception as exc:
            self._log("Voice configure failed: %s" % exc)

    def say(self, text, priority=False):
        if not self.enabled:
            return
        try:
            el = self.eleven
            if el is not None and el.api_key and not getattr(el, "_auth_failed", False):
                el.speak(text, priority)
            else:
                self.native.speak(text, priority)
        except Exception as exc:
            self._log("Speech error: %s" % exc)


class VoiceInput:
    """Android speech recognition via the system RecognizerIntent dialog."""
    REQUEST_CODE = 7331
    RESULT_OK = -1

    def __init__(self):
        self._bound = False
        self._on_text = None
        self._on_error = None

    def listen(self, on_text, on_error):
        if platform != "android":
            on_error("Voice input works on Android only - use the command box")
            return
        try:
            from android import activity
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            RecognizerIntent = autoclass("android.speech.RecognizerIntent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            self._on_text, self._on_error = on_text, on_error
            if not self._bound:
                activity.bind(on_activity_result=self._on_result)
                self._bound = True
            intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                            RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            intent.putExtra(RecognizerIntent.EXTRA_PROMPT, "Say: scan, execute, status, shield, kill")
            PythonActivity.mActivity.startActivityForResult(intent, self.REQUEST_CODE)
        except Exception as exc:
            on_error("Voice unavailable: %s" % exc)

    def _on_result(self, request_code, result_code, intent):
        try:
            if request_code != self.REQUEST_CODE:
                return
            if result_code != self.RESULT_OK or intent is None:
                Clock.schedule_once(lambda dt: self._on_error("I did not catch that."))
                return
            from jnius import autoclass
            RecognizerIntent = autoclass("android.speech.RecognizerIntent")
            results = intent.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            text = str(results.get(0)) if results is not None and results.size() > 0 else ""
            if text:
                Clock.schedule_once(lambda dt: self._on_text(text))
            else:
                Clock.schedule_once(lambda dt: self._on_error("I did not hear anything."))
        except Exception as exc:
            msg = "Voice decode failed: %s" % exc
            Clock.schedule_once(lambda dt: self._on_error(msg))


def open_url(url):
    try:
        if platform == "android":
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            PythonActivity.mActivity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        else:
            import webbrowser
            webbrowser.open(url)
        return True
    except Exception:
        return False


# ----------------------------- safety state ------------------------------- #
class NullRisk:
    """Used only if risk storage cannot be opened anywhere: trading stays locked."""
    def status(self):
        return {"locked": True, "consecutive_losses": 0, "limit": 2, "wins": 0, "losses": 0,
                "daily_pnl": 0.0, "reason": "Risk storage unavailable - trading disabled"}

    def is_locked(self):
        return True

    def record_trade_result(self, *a, **k):
        pass


class KillSwitch:
    """Persistent emergency halt. Survives app restarts; cleared only by re-arming."""

    def __init__(self, path):
        self.path = path
        self.active = False
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.active = bool(json.load(f).get("active"))
        except Exception:
            pass

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"active": self.active, "ts": datetime.now().isoformat(timespec="seconds")}, f)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def trigger(self):
        self.active = True
        self._save()

    def reset(self):
        self.active = False
        self._save()


class LearnerLedger:
    """Virtual Rs 1,00,000 practice account. Own file. NEVER sends orders."""
    CAPITAL = 100000.0
    RISK_PCT = 1.0

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self.state = {"positions": [], "trades": [], "pnl": 0.0, "seq": 0}
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.state.update(json.load(f))
        except Exception:
            pass

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def positions(self):
        with self._lock:
            return [dict(p) for p in self.state["positions"]]

    def open(self, exchange, symbol, side, entry, sl, tp, grade=""):
        plan = plan_position(self.CAPITAL, self.RISK_PCT, entry, sl, tp, 10000)
        if not plan["ok"]:
            return None
        with self._lock:
            if any(p["symbol"] == symbol for p in self.state["positions"]):
                return None
            self.state["seq"] += 1
            pos = {"id": "L%d" % self.state["seq"], "exchange": exchange, "symbol": symbol,
                   "side": side, "qty": plan["qty"], "entry": round(entry, 2), "sl": round(sl, 2),
                   "tp": round(tp, 2), "grade": grade,
                   "opened_at": datetime.now().isoformat(timespec="seconds")}
            self.state["positions"].append(pos)
            self._save()
            return dict(pos)

    def close(self, pos_id, price, reason):
        with self._lock:
            pos = next((p for p in self.state["positions"] if p["id"] == pos_id), None)
            if pos is None:
                return None
            if pos["side"] == BUY:
                pnl = (price - pos["entry"]) * pos["qty"]
            else:
                pnl = (pos["entry"] - price) * pos["qty"]
            trade = {"id": pos["id"], "symbol": pos["symbol"], "side": pos["side"], "qty": pos["qty"],
                     "entry": pos["entry"], "exit": round(price, 2), "pnl": round(pnl, 2),
                     "reason": reason, "grade": pos.get("grade", ""),
                     "closed_at": datetime.now().isoformat(timespec="seconds")}
            self.state["positions"] = [p for p in self.state["positions"] if p["id"] != pos_id]
            self.state["trades"] = (self.state["trades"] + [trade])[-200:]
            self.state["pnl"] = round(self.state["pnl"] + pnl, 2)
            self._save()
            return trade

    def check(self, symbol, ltp):
        closed = []
        for p in self.positions():
            if p["symbol"] != symbol:
                continue
            buy = p["side"] == BUY
            if (ltp <= p["sl"]) if buy else (ltp >= p["sl"]):
                closed.append(self.close(p["id"], ltp, "STOP"))
            elif (ltp >= p["tp"]) if buy else (ltp <= p["tp"]):
                closed.append(self.close(p["id"], p["tp"], "TARGET"))
        return [c for c in closed if c]

    def stats(self):
        with self._lock:
            trades = self.state["trades"]
            wins = sum(1 for t in trades if t["pnl"] > 0)
            losses = sum(1 for t in trades if t["pnl"] < 0)
            return {"trades": len(trades), "wins": wins, "losses": losses,
                    "pnl": float(self.state["pnl"]), "open": len(self.state["positions"])}


def grade_entry(side, d, shield, min_agree):
    """Teaches the learner how good an entry was: returns (grade letter, plain reason)."""
    if shield["blocks"]:
        return "C", "the shield says avoid: %s" % shield["blocks"][0]
    if d.direction == side:
        if d.rr >= MIN_RR_FLOOR and d.agree >= min_agree:
            if shield["cautions"]:
                return "B", "a good setup, but be careful: %s" % shield["cautions"][0]
            return "A", "the agents agree, the reward is at least twice the risk, and the shield is clear"
        return "B", "the agents lean the same way, but the setup is not strong enough yet"
    if d.direction in (BUY, SELL):
        return "D", "most agents lean the opposite way"
    return "C", "there is no clear signal, so this entry is a guess"


# ----------------------------- application -------------------------------- #
class TacticalApp(App):
    title = "Tactical Neural Matrix"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.log_lines = deque(maxlen=40)
        self.cfg = dict(DEFAULT_CFG)
        self.data_dir = ""
        self.risk = NullRisk()
        self.kill = KillSwitch(os.path.join(tempfile.gettempdir(), "tnm_kill.json"))
        self.governor = None
        self.journal = None
        self.ledger = None
        self.engine = None
        self.broker = None
        self.speaker = None
        self.voice = VoiceInput()
        self.last_decision = None
        self.last_shield = None
        self.last_conf = None
        self.mood = "UNSET"
        self.mood_ts = 0.0
        self.busy = False
        self.monitor_busy = False
        self.health_busy = False
        self.learn_busy = False
        self.pending = None
        self._pending_ev = None
        self._live_pop = None
        self._settings_pop = None
        self._learn_pop = None
        self._journal_pop = None
        self._calc_pop = None
        self._auto_ev = None
        self._exec_after = False
        self._last_key = None
        self._last_shield_key = None
        self._last_spoken = 0.0
        self._closed_said = False
        self.conn_fail = 0
        self.conn_down = False
        self.had_session = False
        self.token_expired = False
        self.learn_prices = {}
        self.lbl_say = self.lbl_log = self.lbl_header = self.lbl_learn = self.lbl_shield = None
        self.lbl_journal = self.lbl_calc = None
        self.lbl_dir = self.lbl_agree = self.lbl_levels = self.lbl_reason = None
        self.lbl_risk = self.lbl_pos = None
        self.card_cons = self.card_risk = self.card_shield = None
        self.btn_mode = None
        self.cards = []
        self.txt_cmd = None
        self._fields = {}
        self._calc = {}

    # ---- startup (every step isolated) ----
    def build(self):
        try:
            Window.clearcolor = BG
            Window.softinput_mode = "below_target"
            Window.bind(on_keyboard=self._on_key)
        except Exception as exc:
            Logger.error("TNM: window setup failed: %s" % exc)

        self.data_dir = self._make_data_dir()
        self.cfg = self._load_cfg()
        self.kill = KillSwitch(os.path.join(self.data_dir, "kill_switch.json"))
        self.governor = TradeGovernor(os.path.join(self.data_dir, "shield_state.json"))
        self.journal = Journal(os.path.join(self.data_dir, "journal.json"))
        self.ledger = LearnerLedger(os.path.join(self.data_dir, "learner_ledger.json"))
        self.risk = self._make_risk()
        self.engine = self._make_engine()
        self.broker = self._make_broker()
        self.speaker = self._make_speaker()
        try:
            self.had_session = bool(self.broker and self.broker.has_session())
        except Exception:
            self.had_session = False
        try:
            root = self._build_ui()
        except Exception as exc:
            Logger.error("TNM: UI build failed: %s" % exc)
            Logger.error(traceback.format_exc())
            root = self._fatal_ui("Screen could not be built: %s" % exc)
        Clock.schedule_once(self._post_start, 0.8)
        return root

    def _make_data_dir(self):
        for cand in (getattr(self, "user_data_dir", ""), os.path.join(tempfile.gettempdir(), "tnm")):
            try:
                if cand:
                    os.makedirs(cand, exist_ok=True)
                    return cand
            except Exception:
                continue
        return "."

    def _make_risk(self):
        for base in (self.data_dir, os.path.join(tempfile.gettempdir(), "tnm")):
            try:
                os.makedirs(base, exist_ok=True)
                return RiskManager(os.path.join(base, "risk_data.json"))
            except Exception as exc:
                Logger.error("TNM: risk store failed at %s: %s" % (base, exc))
        return NullRisk()

    def _make_engine(self):
        try:
            return ConsensusEngine(self.risk, int(self.cfg["min_agree"]), float(self.cfg["min_rr"]))
        except Exception as exc:
            Logger.error("TNM: engine failed with config, using defaults: %s" % exc)
            try:
                return ConsensusEngine(self.risk)
            except Exception as exc2:
                Logger.error("TNM: engine unavailable: %s" % exc2)
                return None

    def _make_broker(self):
        c = self.cfg
        mode = "PAPER" if PAPER_MODE else c["mode"]
        try:
            return KiteBroker(self.data_dir, mode=mode, api_key=c["api_key"],
                              api_secret=c["api_secret"], product=c["product"],
                              paper_capital=float(c["capital"]), max_order_qty=int(c["max_qty"]))
        except Exception as exc:
            Logger.error("TNM: broker failed with config, trying clean paper mode: %s" % exc)
        try:
            return KiteBroker(self.data_dir, mode="PAPER")
        except Exception as exc:
            Logger.error("TNM: broker unavailable: %s" % exc)
            return None

    def _make_speaker(self):
        try:
            return Speaker(os.path.join(self.data_dir, "tts_cache"), self.cfg, self.log)
        except Exception as exc:
            Logger.error("TNM: speaker failed: %s" % exc)
            return None

    def _fatal_ui(self, msg):
        box = BoxLayout(orientation="vertical", padding=dp(20))
        box.add_widget(tlabel("TACTICAL NEURAL MATRIX\n\nStartup problem. Trading is disabled.\n\n%s\n\n"
                              "Close and reopen the app. If this repeats, reinstall." % msg,
                              color=AMBER, font=16))
        return box

    @safe()
    def _post_start(self, dt):
        self._request_permissions()
        Clock.schedule_interval(self._monitor_tick, 5)
        Clock.schedule_interval(self._health_tick, 20)
        Clock.schedule_interval(self._learn_tick, 8)
        Clock.schedule_interval(self._evening_tick, 60)
        self._schedule_auto()
        self._refresh_all()
        st = self.risk.status()
        msg = "Tactical Neural Matrix is online. %s mode. The trend and momentum shield is watching." % \
              self._mode().title()
        if self.kill.active:
            msg += " The emergency kill switch is active, so all trading is stopped until you re-arm it in config."
        elif st["locked"]:
            msg += " Safety lockout is active, so trading is blocked for today."
        else:
            msg += " Safety lockout is clear. I will scan the market every minute."
        try:
            if self.cfg.get("api_key") and self.broker is not None and not self.broker.has_session():
                msg += " Zerodha is not logged in today, so I am using demo prices. Log in from config for live prices."
        except Exception:
            pass
        if not self.cfg.get("eleven_key"):
            msg += " Add your ElevenLabs key in config to use the Riya voice."
        if self.broker is None:
            msg += " The broker connection failed to start, so trading is disabled."
        self.say(msg, priority=True)
        self.log("System online. Mode %s, data source %s." % (self._mode(), self._source()))

    @safe()
    def _request_permissions(self):
        if platform != "android":
            return
        from android.permissions import Permission, request_permissions
        request_permissions([Permission.RECORD_AUDIO])

    def _on_key(self, window, key, *args):
        if key == 27:  # Android back button: never exit by accident
            self.say("Use the home button to leave. I keep watching your trades while the app stays open.")
            return True
        return False

    def on_pause(self):
        return True

    def on_resume(self):
        try:
            self._refresh_all()
        except Exception:
            pass

    # ---- small accessors ----
    def _mode(self):
        return self.broker.mode if self.broker else "OFFLINE"

    def _source(self):
        try:
            return self.broker.data_source if self.broker else "NONE"
        except Exception:
            return "NONE"

    def _positions(self):
        try:
            return self.broker.open_positions() if self.broker else []
        except Exception:
            return []

    def _cur_mood(self):
        if self.mood != "UNSET" and time.time() - self.mood_ts < 7200:
            return self.mood
        return "UNSET"

    # ---- config ----
    def _cfg_path(self):
        return os.path.join(self.data_dir, "config.json")

    def _load_cfg(self):
        cfg = dict(DEFAULT_CFG)
        try:
            with open(self._cfg_path(), "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
        try:  # enforce hard limits even if the file was edited by hand
            cfg["risk_pct"] = max(0.1, min(float(cfg["risk_pct"]), MAX_RISK_PCT))
            cfg["min_rr"] = max(float(cfg["min_rr"]), MIN_RR_FLOOR)
            cfg["min_agree"] = max(MIN_AGREE_FLOOR, min(11, int(cfg["min_agree"])))
            cfg["capital"] = max(1.0, float(cfg["capital"]))
            cfg["max_qty"] = max(1, int(cfg["max_qty"]))
            cfg["scan_secs"] = max(30, min(3600, int(cfg["scan_secs"])))
            cfg["eleven_key"] = str(cfg.get("eleven_key") or "").strip()
            cfg["voice_id"] = str(cfg.get("voice_id") or DEFAULT_VOICE_ID).strip()
            cfg["voice_alerts"] = bool(cfg.get("voice_alerts", True))
            cfg["auto_scan"] = bool(cfg.get("auto_scan", True))
            cfg["auto_trade"] = bool(cfg.get("auto_trade", True))
        except Exception:
            cfg = dict(DEFAULT_CFG)
        if PAPER_MODE:
            cfg["mode"] = "PAPER"
        return cfg

    @safe()
    def _save_cfg(self):
        tmp = self._cfg_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, indent=2)
        os.replace(tmp, self._cfg_path())

    # ---- UI construction ----
    def _build_ui(self):
        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        header = NeonCard(accent=CYAN, orientation="vertical", padding=dp(10), spacing=dp(2),
                          size_hint_y=None, height=dp(72))
        header.add_widget(tlabel("TACTICAL NEURAL MATRIX", color=CYAN, font=19, bold=True,
                                 size_hint_y=None, height=dp(30)))
        self.lbl_header = tlabel("", color=DIM, font=11)
        header.add_widget(self.lbl_header)
        root.add_widget(header)

        say_card = NeonCard(accent=GREEN, orientation="vertical", padding=dp(12),
                            size_hint_y=None, height=dp(104))
        self.lbl_say = tlabel("Tap the big MIC button and say: scan.", color=TEXT, font=17, bold=True,
                              valign="top")
        say_card.add_widget(self.lbl_say)
        root.add_widget(say_card)

        row = BoxLayout(size_hint_y=None, height=dp(54), spacing=dp(8))
        btn_mic = NeonButton(text="MIC", accent=AMBER, font_size=sp(20), size_hint_x=1.5)
        btn_scan = NeonButton(text="SCAN", accent=CYAN, font_size=sp(16))
        btn_exec = NeonButton(text="EXECUTE", accent=GREEN, font_size=sp(16))
        btn_cfg = NeonButton(text="CONFIG", accent=DIM, font_size=sp(14))
        btn_mic.bind(on_release=lambda *a: self.start_voice())
        btn_scan.bind(on_release=lambda *a: self.scan(manual=True))
        btn_exec.bind(on_release=lambda *a: self.execute())
        btn_cfg.bind(on_release=lambda *a: self.open_settings())
        for b in (btn_mic, btn_scan, btn_exec, btn_cfg):
            row.add_widget(b)
        root.add_widget(row)

        row2 = BoxLayout(size_hint_y=None, height=dp(54), spacing=dp(8))
        btn_kill = NeonButton(text="KILL SWITCH", accent=RED, font_size=sp(16), size_hint_x=1.5)
        self.btn_mode = NeonButton(text="MODE", accent=CYAN, font_size=sp(13))
        btn_learn = NeonButton(text="LEARN", accent=GREEN, font_size=sp(14))
        btn_kill.bind(on_release=lambda *a: self.kill_switch("button"))
        self.btn_mode.bind(on_release=lambda *a: self.toggle_mode())
        btn_learn.bind(on_release=lambda *a: self.open_learn())
        for b in (btn_kill, self.btn_mode, btn_learn):
            row2.add_widget(b)
        root.add_widget(row2)

        row3 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        btn_calc = NeonButton(text="CALC", accent=CYAN, font_size=sp(14))
        btn_journal = NeonButton(text="JOURNAL", accent=AMBER, font_size=sp(14))
        btn_calc.bind(on_release=lambda *a: self.open_calc())
        btn_journal.bind(on_release=lambda *a: self.open_journal())
        row3.add_widget(btn_calc)
        row3.add_widget(btn_journal)
        root.add_widget(row3)

        scroll = ScrollView(do_scroll_x=False, bar_width=dp(3), bar_color=CYAN)
        content = GridLayout(cols=1, spacing=dp(8), size_hint_y=None)
        content.bind(minimum_height=content.setter("height"))

        self.card_cons = NeonCard(accent=DIM, orientation="vertical", padding=dp(12), spacing=dp(2),
                                  size_hint_y=None, height=dp(150))
        self.lbl_dir = tlabel("NO SIGNAL", color=DIM, font=24, bold=True, size_hint_y=None, height=dp(36))
        self.lbl_agree = tlabel("0 / 11 AGENTS AGREE", color=DIM, font=12, bold=True,
                                size_hint_y=None, height=dp(18))
        self.lbl_levels = tlabel("ENTRY -   SL -   TP -   RR -", color=TEXT, font=12,
                                 size_hint_y=None, height=dp(20))
        self.lbl_reason = tlabel("Run a scan to evaluate the matrix.", color=DIM, font=11, valign="top")
        for w in (self.lbl_dir, self.lbl_agree, self.lbl_levels, self.lbl_reason):
            self.card_cons.add_widget(w)
        content.add_widget(self.card_cons)

        self.card_shield = NeonCard(accent=DIM, orientation="vertical", padding=dp(12),
                                    size_hint_y=None, height=dp(160))
        self.lbl_shield = tlabel("SMART TREND & MOMENTUM SHIELD\nWaiting for the first scan.",
                                 color=TEXT, font=12, valign="top")
        self.card_shield.add_widget(self.lbl_shield)
        content.add_widget(self.card_shield)

        self.card_risk = NeonCard(accent=GREEN, orientation="vertical", padding=dp(12), spacing=dp(2),
                                  size_hint_y=None, height=dp(160))
        self.lbl_risk = tlabel("", color=TEXT, font=12, valign="top", size_hint_y=None, height=dp(72))
        self.lbl_pos = tlabel("", color=TEXT, font=11, valign="top")
        self.card_risk.add_widget(self.lbl_risk)
        self.card_risk.add_widget(self.lbl_pos)
        content.add_widget(self.card_risk)

        grid = GridLayout(cols=2, spacing=dp(8), size_hint_y=None)
        grid.bind(minimum_height=grid.setter("height"))
        self.cards = []
        names = self.engine.agent_names if self.engine else ["AGENT %d" % i for i in range(1, 12)]
        for i, name in enumerate(names, start=1):
            card = AgentCard(i, name)
            self.cards.append(card)
            grid.add_widget(card)
        content.add_widget(grid)

        card_log = NeonCard(accent=DIM, orientation="vertical", padding=dp(10),
                            size_hint_y=None, height=dp(190))
        self.lbl_log = tlabel("", color=DIM, font=10, valign="top")
        card_log.add_widget(self.lbl_log)
        content.add_widget(card_log)

        scroll.add_widget(content)
        root.add_widget(scroll)

        bar = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.txt_cmd = TextInput(hint_text="type: scan / execute / status / shield / kill / learn",
                                 multiline=False, background_color=PANEL_HI, foreground_color=TEXT,
                                 hint_text_color=DIM, cursor_color=CYAN, padding=[dp(10), dp(12)],
                                 font_size=sp(13))
        self.txt_cmd.bind(on_text_validate=lambda *a: self._send_cmd())
        btn_send = NeonButton(text="SEND", accent=CYAN, size_hint_x=None, width=dp(80))
        btn_send.bind(on_release=lambda *a: self._send_cmd())
        bar.add_widget(self.txt_cmd)
        bar.add_widget(btn_send)
        root.add_widget(bar)
        return root

    # ---- speech + log ----
    def say(self, text, priority=False):
        """Show the sentence in the big banner and speak it. Never raises."""
        try:
            def _show(dt):
                if self.lbl_say is not None:
                    self.lbl_say.text = text
            Clock.schedule_once(_show)
            self.log("SAY> %s" % text)
            if self.speaker is None:
                return
            chunks = chunk_text(speakable(text))
            for i, chunk in enumerate(chunks):
                self.speaker.say(chunk, priority=(priority and i == 0))
        except Exception as exc:
            Logger.error("TNM: say failed: %s" % exc)

    def log(self, msg):
        try:
            line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
            Clock.schedule_once(lambda dt: self._log_main(line))
        except Exception:
            pass

    def _log_main(self, line):
        try:
            self.log_lines.append(line)
            if self.lbl_log is not None:
                self.lbl_log.text = "\n".join(list(self.log_lines)[-12:])
        except Exception:
            pass

    # ---- UI refreshers ----
    @safe()
    def _refresh_all(self):
        self._refresh_header()
        self._refresh_risk()
        self._refresh_positions()

    @safe()
    def _refresh_header(self):
        if self.lbl_header is None:
            return
        extra = ""
        if self.kill.active:
            extra = " | KILLED"
        elif self.token_expired:
            extra = " | LOGIN EXPIRED"
        elif self.conn_down:
            extra = " | BROKER OFFLINE"
        self.lbl_header.text = "MODE %s | DATA %s | %s:%s %s | AUTO %s%s" % (
            self._mode(), self._source(), self.cfg["exchange"], self.cfg["symbol"],
            self.cfg["interval"], "ON" if self.cfg.get("auto_scan") else "OFF", extra)
        if self.kill.active or self.conn_down or self.token_expired:
            self.lbl_header.color = AMBER
        else:
            self.lbl_header.color = RED if self._mode() == "LIVE" else GREEN
        if self.btn_mode is not None:
            self.btn_mode.text = "MODE: %s" % self._mode()
            self.btn_mode.accent = list(RED if self._mode() == "LIVE" else CYAN)

    @safe()
    def _refresh_risk(self):
        if self.lbl_risk is None:
            return
        st = self.risk.status()
        locked = st["locked"] or self.kill.active
        self.card_risk.accent = list(RED if locked else GREEN)
        head = "KILL SWITCH ACTIVE - ALL TRADING STOPPED" if self.kill.active else (
            "ACTIVE - TRADING BLOCKED TODAY" if st["locked"] else "CLEAR")
        self.lbl_risk.text = (
            "HARD DAILY LOCKOUT: %s\nCONSECUTIVE LOSSES  %d / %d\nW/L  %d / %d    DAY P&L  %.2f%s" % (
                head, st["consecutive_losses"], st["limit"], st["wins"], st["losses"], st["daily_pnl"],
                ("\n" + st["reason"]) if st["locked"] and st["reason"] else ""))
        self.lbl_risk.color = RED if locked else TEXT

    @safe()
    def _refresh_positions(self):
        if self.lbl_pos is None:
            return
        pos = self._positions()
        if not pos:
            self.lbl_pos.text = "OPEN POSITIONS: none"
            return
        lines = ["OPEN POSITIONS:"]
        for p in pos:
            lines.append("%s %s x%d @ %.2f  SL %.2f  TP %.2f  [%s]" % (
                p["side"], p["symbol"], p["qty"], p["entry"], p["sl"], p["tp"], p["mode"]))
        self.lbl_pos.text = "\n".join(lines)

    @safe()
    def _refresh_shield(self, sh, conf=None):
        if self.lbl_shield is None or sh is None:
            return
        lv = sh["level"]
        col = {"CLEAR": GREEN, "CAUTION": AMBER, "BLOCK": RED}.get(lv, DIM)
        self.card_shield.accent = list(col)
        lines = ["SMART TREND & MOMENTUM SHIELD: %s" % lv,
                 "TREND %s   MOMENTUM %s   VOLATILITY x%.1f" % (sh["trend"], sh["momentum"], sh["vol_ratio"])]
        if conf is not None:
            b = conf["base"] or "?"
            h = "-" if conf["single"] else (conf["htf"] or "?")
            ok = conf.get("ok")
            state = "MATCH" if ok else ("NO MATCH" if ok is False else "n/a")
            lines.append("CONFLUENCE %s %s | %s %s : %s" % (
                tf_short(conf["itv"]), b, tf_short(conf["htf_itv"]), h, state))
        for b in sh["blocks"]:
            lines.append("BLOCK: %s" % b)
        for c in sh["cautions"]:
            lines.append("CAUTION: %s" % c)
        self.lbl_shield.text = "\n".join(lines)
        self.lbl_shield.color = col if lv in ("BLOCK", "CAUTION") else TEXT

    # ---- commands ----
    @safe()
    def _send_cmd(self):
        text = self.txt_cmd.text.strip()
        self.txt_cmd.text = ""
        if text:
            self.handle_command(text)

    @safe()
    def handle_command(self, text):
        t = (text or "").lower().strip()
        words = set(re.findall(r"[a-z]+", t))
        self.log("CMD> %s" % t)

        # "kill switch off" / "re-arm" must never trigger a kill: it only opens the re-arm dialog
        if "re-arm" in t or "re arm" in t or "rearm" in words or (
                ("kill" in t or "किल" in t) and (words & {"off", "reset", "disable", "clear"})):
            self._ask_rearm()
            return

        # safety first: the kill switch is checked before anything else
        if any(k in t for k in KILL_WORDS):
            self.kill_switch("voice")
            return

        if self.pending:
            if (words & CONFIRM_WORDS) or any(h in t for h in CONFIRM_HI):
                self._confirm_yes()
                return
            if (words & CANCEL_WORDS) or any(h in t for h in CANCEL_HI):
                self._confirm_no()
                return

        if any(k in t for k in MOOD_TRIGGERS):
            for word, mood in MOOD_WORDS:
                if word in t:
                    self.set_mood(mood)
                    return
            self.say("Tell me your mood. Say mood calm, mood excited, mood anxious, or mood angry.")
            return

        if any(k in t for k in ("close all", "exit all", "square off", "squareoff", "close everything")):
            self.close_all()
        elif any(k in t for k in LEARN_WORDS):
            self.open_learn()
        elif "journal" in words or "feedback" in words or "जर्नल" in t:
            self._say_day_report()
        elif any(k in t for k in ("calculator", "position size", "calc", "कैलकुलेटर")):
            self.open_calc()
        elif "shield" in words or "शील्ड" in t:
            self._say_shield()
        elif any(k in t for k in ("execute", "place trade", "take trade", "enter trade",
                                  "एक्जीक्यूट", "एक्ज़ीक्यूट", "ट्रेड लो", "एंट्री", "खरीद", "बेच")) \
                or (words & {"buy", "sell", "entry", "enter"}):
            # natural-language entry: the engine decides the side; only an approved setup is ever placed
            self.execute()
        elif "auto" in words:
            on = not (words & {"off", "stop", "disable"})
            self.cfg["auto_scan"] = on
            self._save_cfg()
            self._schedule_auto()
            self._refresh_header()
            self.say("Automatic scanning is %s." % ("on" if on else "off"), priority=True)
        elif any(k in t for k in ("scan", "analyze", "analyse", "evaluate", "check market", "स्कैन")):
            self.scan(manual=True)
        elif "status" in words or "report" in words or "स्टेटस" in t:
            self._status_report()
        elif "mute" in words:
            if self.speaker is not None:
                self.speaker.enabled = False
            self.log("Voice muted. Say or type unmute to turn it back on.")
        elif "unmute" in words:
            if self.speaker is not None:
                self.speaker.enabled = True
            self.say("Voice is back on.", priority=True)
        elif "help" in words:
            self.say("You can say: scan, execute, status, shield, journal, calculator, close all, kill, learn, "
                     "mood calm, auto on, auto off, mute or unmute. When I ask for confirmation, say confirm or cancel.")
        else:
            self.say("Sorry, I did not understand. Say scan, execute, status, shield, journal, kill or learn.")

    @safe()
    def start_voice(self):
        if platform != "android":
            self.log("Voice input works on Android only - type in the command box.")
            if self.txt_cmd is not None:
                self.txt_cmd.focus = True
            return
        self.voice.listen(lambda text: self.handle_command(text), lambda err: self.say(err))

    @safe()
    def _say_shield(self):
        if self.last_shield is None:
            self.say("The shield has not scanned yet. Say scan first.", priority=True)
            return
        msg = shield_sentence(self.last_shield)
        c = self.last_conf
        if c is not None and c.get("ok") is not None:
            msg += " " + c.get("phrase", "")
        self.say(msg, priority=True)

    # ---- mode toggle (PAPER / LIVE) ----
    @safe()
    def toggle_mode(self):
        if self.broker is None:
            self.say("The broker is not available.", priority=True)
            return
        if self._positions():
            self.say("Close open positions before switching mode.", priority=True)
            return
        target = "PAPER" if self._mode() == "LIVE" else "LIVE"
        if target == "LIVE" and PAPER_MODE:
            self.say("Live mode is locked. To unlock it, set PAPER_MODE to False at the top of main dot py, "
                     "and only do that after weeks of practice.", priority=True)
            return
        try:
            self.broker.set_mode(target)
        except BrokerError as exc:
            self.say("Mode not changed. %s" % exc, priority=True)
            return
        self.cfg["mode"] = self.broker.mode
        self._save_cfg()
        self._refresh_all()
        if self.broker.mode == "LIVE":
            self.say("Live mode is on. Orders use real money and always need your confirmation.",
                     priority=True)
        else:
            self.say("Practice mode is on. No real money is used.", priority=True)

    # ---- KILL SWITCH ----
    @safe()
    def kill_switch(self, source="command"):
        already = self.kill.active
        self.kill.trigger()
        self._close_pending()
        self.last_decision = None
        self._refresh_all()
        self.log("KILL SWITCH (%s)" % source)
        self.say("Emergency kill switch %s. Closing every position and stopping all trading now." % (
            "pressed again" if already else "activated"), priority=True)
        threading.Thread(target=self._kill_worker, daemon=True).start()

    @safe()
    def _kill_worker(self):
        closed_all = []
        try:
            for attempt in range(3):
                for pos in self._positions():
                    try:
                        closed_all.append(self.broker.close_position(pos["id"], "KILL"))
                    except Exception as exc:
                        self.log("Kill close failed for %s: %s" % (pos.get("symbol"), exc))
                if not self._positions():
                    break
                time.sleep(1.5)
        except Exception as exc:
            self.log("Kill error: %s" % exc)
        self._register_closed(closed_all, quiet=True)
        remaining = self._positions()
        total = sum(t["pnl"] for t in closed_all)
        parts = []
        if closed_all:
            parts.append("%s closed. Net result %s %.0f rupees." % (
                plural(len(closed_all), "position"), "plus" if total >= 0 else "minus", abs(total)))
        elif not remaining:
            parts.append("There were no open positions.")
        if remaining:
            parts.append("Warning. %s could not be closed. Open the Zerodha app and close %s manually right now." % (
                plural(len(remaining), "position"), "it" if len(remaining) == 1 else "them"))
        parts.append("All trading is stopped, and it stays stopped even after a restart, until you re-arm it in config.")
        self.say(" ".join(parts), priority=True)
        Clock.schedule_once(lambda dt: self._refresh_all())

    @safe()
    def _ask_rearm(self):
        if not self.kill.active:
            self.say("The kill switch is not active.")
            return
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        box.add_widget(tlabel("Re-arm trading after the kill switch?\nOnly do this when you are calm "
                              "and have checked your positions.", color=TEXT, font=14))
        row = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(10))
        yes = NeonButton(text="RE-ARM", accent=AMBER)
        no = NeonButton(text="STAY STOPPED", accent=GREEN)
        row.add_widget(yes)
        row.add_widget(no)
        box.add_widget(row)
        pop = Popup(title="RE-ARM TRADING", content=box, size_hint=(0.92, 0.4), auto_dismiss=False,
                    background="", background_color=BG, title_color=AMBER, separator_color=AMBER)
        yes.bind(on_release=lambda *a: (pop.dismiss(), self._rearm()))
        no.bind(on_release=lambda *a: pop.dismiss())
        pop.open()

    @safe()
    def _rearm(self):
        self.kill.reset()
        self._refresh_all()
        self.say("Kill switch cleared. Trading is allowed again, but the daily safety lockout still applies.",
                 priority=True)

    # ---- scan (agents + shield + multi-timeframe confluence) ----
    @safe()
    def scan(self, manual=True, then_execute=False):
        if self.broker is None or self.engine is None:
            self.say("The trading engine is not available, so I cannot scan.", priority=True)
            return
        if self.token_expired:
            if manual:
                self.say("Your Zerodha login has expired, so I cannot read live prices. "
                         "Log in again in config, or tap use demo data.", priority=True)
            return
        if self.busy:
            if manual:
                self.say("I am still working on the last request. Please wait a moment.")
            return
        self.busy = True
        self._exec_after = bool(then_execute)
        exch, sym, itv = self.cfg["exchange"], self.cfg["symbol"], self.cfg["interval"]
        if manual:
            self.say("Scanning %s now." % sym)
        threading.Thread(target=self._scan_worker, args=(exch, sym, itv, manual), daemon=True).start()

    def _build_conf(self, exch, sym, itv, candles):
        """Trend of the traded timeframe and of the next higher one (fetched, or built from the candles)."""
        htf_itv = HTF_MAP.get(itv)
        conf = {"itv": itv, "base": ema_trend(candles), "htf_itv": htf_itv, "htf": None,
                "single": htf_itv is None, "ok": None, "phrase": ""}
        if htf_itv is None:
            return conf
        hc = None
        if self._source() == "KITE":
            try:
                hc = self.broker.get_candles(exch, sym, htf_itv, 120)
            except Exception as exc:
                self.log("Higher timeframe fetch failed: %s" % exc)
                hc = None
        if hc is None or len(hc) < 30:
            m, hm = MINUTES.get(itv), MINUTES.get(htf_itv)
            if m and hm and hm > m and hm % m == 0:
                hc = resample(candles, hm // m)
        conf["htf"] = ema_trend(hc) if hc else None
        return conf

    @safe()
    def _scan_worker(self, exch, sym, itv, manual):
        decision = shield = conf = None
        try:
            candles = self.broker.get_candles(exch, sym, itv, fetch_count(itv))
            recent = candles[-200:]
            decision = self.engine.evaluate(sym, recent)
            d_dir = decision.direction if decision.direction in (BUY, SELL) else None
            shield = compute_shield(recent, d_dir)
            conf = self._build_conf(exch, sym, itv, candles)
        except Exception as exc:
            self.log("Scan failed: %s" % exc)
            self._exec_after = False
            if manual:
                self.say("The scan failed. %s" % exc, priority=True)
            return
        finally:
            self.busy = False
        Clock.schedule_once(lambda dt: self._apply_decision(decision, conf, shield, manual))

    @safe()
    def _apply_decision(self, d, conf, shield, manual):
        go = self._exec_after
        self._exec_after = False
        directional = d.direction in (BUY, SELL)

        # --- 1) shield: over-trading limits join the trade-specific blocks ---
        if directional:
            for g in (self.governor.block_reasons() if self.governor else []):
                if g not in shield["blocks"]:
                    shield["blocks"].append(g)
        shield["level"] = shield_level(shield)
        if d.approved and shield["blocks"]:
            d.approved = False
            d.reason = "Shield blocked it: %s" % "; ".join(shield["blocks"])

        # --- 2) multi-timeframe confluence: traded timeframe AND higher timeframe must match ---
        conf_phrase = ""
        if directional:
            want = "UP" if d.direction == BUY else "DOWN"
            b, h = conf["base"], conf["htf"]
            if conf["single"]:
                conf["ok"] = (b == want)
                conf_phrase = ("Confluence %s. The %s trend is %s, and there is no higher timeframe to compare." % (
                    "confirmed" if conf["ok"] else "failed", tf_name(conf["itv"]), TREND_WORD.get(b, "unknown")))
            else:
                conf["ok"] = (b == want and h == want)
                if conf["ok"]:
                    conf_phrase = "Confluence confirmed. The %s and %s trends both point %s." % (
                        tf_name(conf["itv"]), tf_name(conf["htf_itv"]), TREND_WORD[want])
                else:
                    conf_phrase = "Confluence failed. The %s trend is %s and the %s trend is %s." % (
                        tf_name(conf["itv"]), TREND_WORD.get(b, "unknown"),
                        tf_name(conf["htf_itv"]), TREND_WORD.get(h, "unknown"))
            if d.approved and not conf["ok"]:
                d.approved = False
                d.reason = "Confluence failed: %s" % conf_phrase
        conf["phrase"] = conf_phrase
        self.last_shield = shield
        self.last_conf = conf
        self._refresh_shield(shield, conf)

        self.last_decision = d
        for card, vote in zip(self.cards, d.votes):
            card.set_vote(vote)
        col = GREEN if d.direction == BUY else RED if d.direction == SELL else DIM
        shade = col if d.approved else (AMBER if directional else DIM)
        self.card_cons.accent = list(shade)
        self.lbl_dir.text = ("%s  APPROVED" % d.direction) if d.approved else (
            "%s  REJECTED" % d.direction if directional else "NO CONSENSUS")
        self.lbl_dir.color = shade
        need = self.engine.min_agree if self.engine else MIN_AGREE_FLOOR
        self.lbl_agree.text = "%d / %d AGENTS AGREE   (NEED %d)" % (d.agree, d.total, need)
        self.lbl_levels.text = "ENTRY %.2f   SL %.2f   TP %.2f   RR 1:%.2f" % (d.entry, d.sl, d.tp, d.rr)
        self.lbl_reason.text = d.reason
        self.lbl_reason.color = TEXT
        self._refresh_header()
        self._refresh_risk()
        self.log("Scan %s: %s | shield %s" % (d.symbol, d.reason, shield["level"]))

        key = (d.direction, bool(d.approved))
        changed = key != self._last_key
        self._last_key = key
        skey = (shield["level"], tuple(shield["blocks"]), tuple(shield["cautions"]))
        shield_changed = skey != self._last_shield_key
        self._last_shield_key = skey
        stale = time.time() - self._last_spoken > 600

        if manual or go or changed or stale:
            self._narrate(d, conf_phrase, shield)
        elif shield_changed and shield["level"] in ("BLOCK", "CAUTION", "CLEAR"):
            self.say(shield_sentence(shield), priority=(shield["level"] == "BLOCK"))

        if d.approved:
            if go:
                self.execute()
            elif not manual and self._mode() == "PAPER" and self.cfg.get("auto_trade") \
                    and shield["level"] == "CLEAR":
                self.execute(auto=True)
        elif go:
            self.say("There is no approved setup right now, so I did not place a trade.")

    def _edge_text(self, rr):
        st = self.risk.status()
        n = st["wins"] + st["losses"]
        if n < 10:
            return "There are not enough recorded trades yet to measure your statistical edge."
        wr = st["wins"] / float(n)
        expectancy = wr * rr - (1.0 - wr)
        return "Your recorded win rate is %.0f percent. At this reward ratio, the average result is %s%.2f rupees per rupee risked." % (
            wr * 100, "plus " if expectancy >= 0 else "minus ", abs(expectancy))

    def _narrate(self, d, conf_phrase, shield):
        self._last_spoken = time.time()
        sym = d.symbol
        sh_phrase = shield_sentence(shield) if shield and shield["level"] != "UNKNOWN" else ""
        if d.reason == "Insufficient market data":
            self.say("I do not have enough price history for %s yet. Staying out." % sym, priority=True)
            return
        if d.approved:
            plan = plan_position(float(self.cfg["capital"]), float(self.cfg["risk_pct"]), d.entry, d.sl,
                                 d.tp, int(self.cfg["max_qty"]))
            parts = [
                "%s setup approved on %s. %d of %d agents agree." % (side_word(d.direction), sym, d.agree, d.total),
                "Entry about %.2f. Stop loss %.2f. Target %.2f. Risk reward is 1 to %.1f, which is favorable." % (
                    d.entry, d.sl, d.tp, d.rr),
            ]
            if plan["ok"]:
                parts.append("Position size: quantity %d, worth about %.0f rupees. If the stop loss hits, you lose about "
                             "%.0f rupees, which is %.1f percent of your capital. If the target hits, you gain about "
                             "%.0f rupees." % (plan["qty"], plan["value"], plan["risk_amt"], plan["capital_pct"],
                                               plan["reward_amt"]))
            else:
                parts.append("But I cannot size this trade, because %s." % plan["reason"])
            parts.append(self._edge_text(d.rr))
            if conf_phrase:
                parts.append(conf_phrase)
            if sh_phrase:
                parts.append(sh_phrase)
            if self.kill.active:
                parts.append("The kill switch is active, so no trade will be placed.")
            elif self._mode() == "LIVE":
                parts.append("This is live mode. Say execute, then confirm, to place it.")
            elif self.cfg.get("auto_trade") and shield["level"] == "CLEAR":
                parts.append("Paper mode, so I will place this practice trade automatically.")
            elif self.cfg.get("auto_trade"):
                parts.append("Because the shield is not fully clear, I will not auto place it. Say execute if you still want it.")
            else:
                parts.append("Say execute to place this practice trade.")
            self.say(" ".join(parts), priority=True)
        elif d.direction in (BUY, SELL):
            msg = "%s bias on %s, but I am not taking it. %s." % (side_word(d.direction), sym, d.reason)
            if conf_phrase and "Confluence failed" not in d.reason:
                msg += " " + conf_phrase
            if sh_phrase and shield["level"] != "BLOCK":
                msg += " " + sh_phrase
            self.say(msg, priority=True)
        else:
            msg = "No clear trade on %s. The agents disagree, so I am staying out." % sym
            if sh_phrase and shield["level"] in ("BLOCK", "CAUTION"):
                msg += " " + sh_phrase
            self.say(msg, priority=True)

    # ---- execute ----
    @safe()
    def execute(self, auto=False):
        if self.broker is None:
            self.say("The broker is not available, so I cannot trade.", priority=True)
            return
        if self.kill.active:
            self.say("Trade blocked. The kill switch is active. Re-arm it in config first.", priority=True)
            return
        if self.risk.is_locked():
            self._refresh_risk()
            self.say("Trade blocked. The daily safety lockout is active. Trading resumes tomorrow.",
                     priority=True)
            return
        gov = self.governor.block_reasons() if self.governor else []
        if gov:
            self.say("Shield blocked this trade. %s." % "; ".join(gov), priority=True)
            return
        if self.token_expired:
            self.say("Your Zerodha login has expired. Log in again in config first.", priority=True)
            return
        if self._mode() == "LIVE" and PAPER_MODE:
            self.say("Live orders are locked by the paper mode switch.", priority=True)
            return
        if self._mode() == "LIVE" and not market_open():
            self.say("The market is closed, so live orders are blocked.", priority=True)
            return
        d = self.last_decision
        if d is None or not d.approved or time.time() - d.ts > 120:
            if auto:
                return
            self.scan(manual=True, then_execute=True)
            return
        capital = float(self.cfg["capital"])
        plan = plan_position(capital, float(self.cfg["risk_pct"]), d.entry, d.sl, d.tp,
                             int(self.cfg["max_qty"]))
        if not plan["ok"]:
            self.say("I cannot size this trade, because %s." % plan["reason"], priority=True)
            return
        if plan["risk_amt"] > capital * MAX_RISK_PCT / 100.0 + 1e-6:
            self.say("Blocked. The risk is above the hard limit of %.0f percent of capital." % MAX_RISK_PCT,
                     priority=True)
            return
        mood = self._cur_mood()
        sh_level = self.last_shield["level"] if self.last_shield else "UNKNOWN"
        ctx = {"auto": bool(auto), "mood": mood, "shield": sh_level,
               "override": (not auto) and sh_level == "CAUTION"}
        if not auto and mood in BAD_MOODS:
            self.say("You told me you feel %s. When emotions are high, waiting is often the best trade. "
                     "I will mark this trade in your journal." % mood.lower())
        if self._mode() == "LIVE":
            self._ask_live_confirm(d, plan, ctx)
        else:
            self._do_execute(d, plan, ctx)

    @safe()
    def _ask_live_confirm(self, d, plan, ctx):
        self.pending = {"d": d, "plan": plan, "ctx": ctx, "ts": time.time()}
        self.say("Live order, real money. %s %s, quantity %d. Entry about %.2f, stop loss %.2f, target %.2f. "
                 "If the stop loss hits you lose about %.0f rupees, which is %.1f percent of your capital. "
                 "Tap the big MIC button and say confirm to place it, or say cancel." % (
                     side_word(d.direction), d.symbol, plan["qty"], d.entry, d.sl, d.tp, plan["risk_amt"],
                     plan["capital_pct"]), priority=True)
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        box.add_widget(tlabel(
            "LIVE ORDER - REAL MONEY\n\n%s %s x%d\nEntry ~%.2f   SL %.2f   TP %.2f\nRR 1:%.2f\n"
            "Max loss ~%.0f (%.1f%% of capital)" % (
                d.direction, d.symbol, plan["qty"], d.entry, d.sl, d.tp, plan["rr"], plan["risk_amt"],
                plan["capital_pct"]), color=TEXT, font=16))
        row = BoxLayout(size_hint_y=None, height=dp(64), spacing=dp(10))
        yes = NeonButton(text="CONFIRM", accent=RED, font_size=sp(16))
        no = NeonButton(text="CANCEL", accent=DIM, font_size=sp(16))
        row.add_widget(yes)
        row.add_widget(no)
        box.add_widget(row)
        self._live_pop = Popup(title="CONFIRM LIVE TRADE", content=box, size_hint=(0.92, 0.55),
                               auto_dismiss=False, background="", background_color=BG,
                               title_color=RED, separator_color=RED)
        yes.bind(on_release=lambda *a: self._confirm_yes())
        no.bind(on_release=lambda *a: self._confirm_no())
        self._live_pop.open()
        if self._pending_ev is not None:
            self._pending_ev.cancel()
        self._pending_ev = Clock.schedule_once(self._expire_pending, 90)

    def _close_pending(self):
        self.pending = None
        try:
            if self._pending_ev is not None:
                self._pending_ev.cancel()
        except Exception:
            pass
        self._pending_ev = None
        try:
            if self._live_pop is not None:
                self._live_pop.dismiss()
        except Exception:
            pass
        self._live_pop = None

    @safe()
    def _expire_pending(self, dt):
        if self.pending:
            self._close_pending()
            self.say("The live order confirmation timed out. Nothing was placed.", priority=True)

    @safe()
    def _confirm_yes(self):
        p = self.pending
        self._close_pending()
        if not p:
            return
        if time.time() - p["ts"] > 90:
            self.say("That confirmation was too old. Nothing was placed.", priority=True)
            return
        if self.kill.active or self.risk.is_locked():
            self.say("Blocked. The kill switch or safety lockout is active.", priority=True)
            return
        gov = self.governor.block_reasons() if self.governor else []
        if gov:
            self.say("Shield blocked this trade. %s." % "; ".join(gov), priority=True)
            return
        self._do_execute(p["d"], p["plan"], p["ctx"])

    @safe()
    def _confirm_no(self):
        self._close_pending()
        self.say("Cancelled. No order was placed.", priority=True)

    @safe()
    def _do_execute(self, d, plan, ctx):
        if self.kill.active:
            self.say("Blocked. The kill switch is active.", priority=True)
            return
        if self.busy:
            self.say("I am busy with another request. Please try again in a moment.")
            return
        self.busy = True
        self.last_decision = None  # a signal can only be used once
        self.say("Placing the %s order on %s." % (d.direction.lower(), d.symbol))
        threading.Thread(target=self._execute_worker, args=(d, plan, ctx), daemon=True).start()

    @safe()
    def _execute_worker(self, d, plan, ctx):
        try:
            pos = self.broker.open_trade(self.cfg["exchange"], d.symbol, d.direction, plan["qty"],
                                         d.sl, d.tp)
            self._journal_open(pos, ctx)
            if self.governor is not None:
                self.governor.record_open()
            self.log("OPENED %s %s x%d @ %.2f SL %.2f TP %.2f [%s]" % (
                pos["side"], pos["symbol"], pos["qty"], pos["entry"], pos["sl"], pos["tp"], pos["mode"]))
            if self.kill.active:  # kill switch pressed while the order was in flight
                try:
                    t = self.broker.close_position(pos["id"], "KILL")
                    self._register_closed([t], quiet=True)
                    self.say("The kill switch was pressed during the order, so I closed it immediately.",
                             priority=True)
                except Exception as exc:
                    self.say("The kill switch was pressed but the new position could not be closed. "
                             "Close it manually in the Zerodha app now. %s" % exc, priority=True)
                return
            risk_amt = pos["qty"] * abs(pos["entry"] - pos["sl"])
            self.say("%s order filled on %s, quantity %d, at %.2f. Stop loss %.2f. Target %.2f. "
                     "Maximum risk about %.0f rupees, which is %.1f percent of your capital. "
                     "I am watching it for you." % (
                         side_word(pos["side"]), pos["symbol"], pos["qty"], pos["entry"], pos["sl"],
                         pos["tp"], risk_amt, risk_amt / max(1.0, float(self.cfg["capital"])) * 100.0),
                     priority=True)
        except BrokerError as exc:
            self.log("Order failed: %s" % exc)
            self.say("The order failed. %s" % exc, priority=True)
        except Exception as exc:
            self.log("Unexpected order error: %s" % exc)
            self.say("Something unexpected went wrong placing the order. Please check your positions.",
                     priority=True)
        finally:
            self.busy = False
            Clock.schedule_once(lambda dt: self._refresh_all())

    def _journal_open(self, pos, ctx):
        """Record a new trade and detect behaviour mistakes. Never raises."""
        try:
            if self.journal is None:
                return
            since = self.governor.minutes_since_loss() if self.governor else None
            flags = []
            if self.journal.count_today() >= 2:
                flags.append("OVERTRADING")
            if since is not None and since < 30:
                flags.append("REVENGE")
            if ctx.get("override"):
                flags.append("IGNORED_SHIELD")
            if ctx.get("mood") in BAD_MOODS:
                flags.append("BAD_MOOD")
            self.journal.add_open({
                "id": pos["id"], "date": datetime.now().date().isoformat(),
                "opened_at": pos.get("opened_at", datetime.now().isoformat(timespec="seconds")),
                "symbol": pos["symbol"], "side": pos["side"], "qty": pos["qty"], "entry": pos["entry"],
                "sl": pos["sl"], "tp": pos["tp"], "mode": pos["mode"], "mood": ctx.get("mood", "UNSET"),
                "shield": ctx.get("shield", "UNKNOWN"), "auto": bool(ctx.get("auto")), "flags": flags})
        except Exception as exc:
            self.log("Journal error: %s" % exc)

    # ---- monitoring / closing ----
    @safe()
    def _monitor_tick(self, dt):
        if self.broker is None or self.monitor_busy or self.token_expired or not self._positions():
            return
        self.monitor_busy = True
        threading.Thread(target=self._monitor_worker, daemon=True).start()

    @safe()
    def _monitor_worker(self):
        try:
            closed = self.broker.check_exits()
            self._register_closed(closed)
        except Exception as exc:
            self.log("Monitor error: %s" % exc)
        finally:
            self.monitor_busy = False

    @safe()
    def _register_closed(self, closed, quiet=False):
        for t in closed:
            self.risk.record_trade_result(t["pnl"], t["symbol"])
            if t["pnl"] < 0 and self.governor is not None:
                self.governor.record_loss()
            try:
                if self.journal is not None:
                    self.journal.close_trade(t)
            except Exception as exc:
                self.log("Journal error: %s" % exc)
            self.log("CLOSED %s %s %s: exit %.2f P&L %.2f" % (
                t["reason"], t["side"], t["symbol"], t["exit"], t["pnl"]))
            if not quiet:
                how = {"TARGET": "hit its target", "STOP": "hit its stop loss",
                       "MANUAL": "was closed", "KILL": "was closed by the kill switch"}.get(
                           t["reason"], "was closed")
                result = ("Profit of %.0f rupees." % t["pnl"]) if t["pnl"] >= 0 else (
                    "Loss of %.0f rupees." % abs(t["pnl"]))
                self.say("%s %s. %s" % (t["symbol"], how, result), priority=True)
        if not closed:
            return
        Clock.schedule_once(lambda dt: self._refresh_all())
        st = self.risk.status()
        if st["locked"]:
            self.say("Safety lockout triggered. You have had %d losses in a row. Trading is blocked for the "
                     "rest of today to protect your capital." % st["consecutive_losses"], priority=True)
        elif st["consecutive_losses"] == st["limit"] - 1 and st["consecutive_losses"] > 0:
            self.say("Warning. That was %d loss in a row. One more and trading locks for the day." %
                     st["consecutive_losses"])
        elif any(t["pnl"] < 0 for t in closed) and not quiet:
            self.say("The shield will keep you out of new trades for 15 minutes so you can cool off.")

    @safe()
    def close_all(self):
        if self.broker is None:
            self.say("The broker is not available.", priority=True)
            return
        if not self._positions():
            self.say("You have no open positions.")
            return
        if self.busy:
            self.say("I am busy with another request. Please try again in a moment.")
            return
        self.busy = True
        self.say("Closing all positions now.", priority=True)
        threading.Thread(target=self._close_all_worker, daemon=True).start()

    @safe()
    def _close_all_worker(self):
        try:
            closed = []
            errors = []
            for pos in self._positions():
                try:
                    closed.append(self.broker.close_position(pos["id"], "MANUAL"))
                except Exception as exc:
                    errors.append("%s: %s" % (pos.get("symbol"), exc))
            self._register_closed(closed)
            if errors:
                self.say("Some positions could not be closed. %s" % "; ".join(errors), priority=True)
        except Exception as exc:
            self.log("Close all error: %s" % exc)
            self.say("Closing positions hit an error. Please check your positions.", priority=True)
        finally:
            self.busy = False
            Clock.schedule_once(lambda dt: self._refresh_all())

    # ---- broker connection + token expiry watchdog ----
    @safe()
    def _health_tick(self, dt):
        if self.broker is None or self.health_busy:
            return
        try:
            has = self.broker.has_session()
        except Exception:
            has = False
        if has:
            self.had_session = True
        elif self.had_session:
            # we had a Zerodha session and it is gone: the token expired or was rejected
            if not self.token_expired:
                self.token_expired = True
                self.conn_down = True
                live_open = any(p.get("mode") == "LIVE" for p in self._positions())
                msg = ("Alert. Your Zerodha login has expired, so live prices have stopped. "
                       "Please log in again in config.")
                if live_open:
                    msg += (" You have a live position. Its stop loss order stays active on Zerodha's side, "
                            "but open the Zerodha app and check it now.")
                elif self._positions():
                    msg += " Your practice positions are paused until you log in again."
                self.say(msg, priority=True)
                Clock.schedule_once(lambda dt2: self._refresh_header())
            return
        else:
            return  # demo data: nothing to watch
        self.health_busy = True
        threading.Thread(target=self._health_worker, daemon=True).start()

    @safe()
    def _health_worker(self):
        try:
            ok, err = True, ""
            try:
                self.broker.get_ltp(self.cfg["exchange"], self.cfg["symbol"])
            except Exception as exc:
                ok, err = False, str(exc)
            if ok:
                if self.conn_down and not self.token_expired:
                    self.conn_down = False
                    self.say("Broker connection restored.", priority=True)
                    Clock.schedule_once(lambda dt: self._refresh_header())
                self.conn_fail = 0
                return
            self.conn_fail += 1
            self.log("Connection check failed (%d): %s" % (self.conn_fail, err))
            live_open = any(p.get("mode") == "LIVE" for p in self._positions())
            if self.conn_fail >= 2 and not self.conn_down:
                self.conn_down = True
                msg = "Alert. Broker connection lost. I cannot reach Zerodha right now. "
                if live_open:
                    msg += ("You have a live position. Its stop loss order stays active on Zerodha's side, "
                            "but open the Zerodha app and check it now.")
                self.say(msg, priority=True)
                Clock.schedule_once(lambda dt: self._refresh_header())
            elif self.conn_down and live_open and self.conn_fail % 6 == 0:
                self.say("Reminder. The broker connection is still down and you have a live position. "
                         "Check the Zerodha app.", priority=True)
        finally:
            self.health_busy = False

    # ---- status ----
    @safe()
    def _status_report(self):
        if self.broker is None:
            self.say("The broker is not available.", priority=True)
            return
        threading.Thread(target=self._status_worker, daemon=True).start()

    @safe()
    def _status_worker(self):
        try:
            st = self.risk.status()
            acct = self.broker.account_summary()
            msg = ["Status. %s mode." % self._mode().title()]
            if self.kill.active:
                msg.append("The kill switch is active and trading is stopped.")
            if self.token_expired:
                msg.append("Your Zerodha login has expired.")
            elif self.conn_down:
                msg.append("The broker connection is down.")
            if acct.get("equity") is not None:
                msg.append("Account value %.0f rupees." % acct["equity"])
            pos = self._positions()
            if not pos:
                msg.append("No open positions.")
            else:
                msg.append("%s." % plural(len(pos), "open position"))
                for p in pos[:3]:
                    try:
                        ltp = self.broker.get_ltp(p["exchange"], p["symbol"])
                        pnl = (ltp - p["entry"]) * p["qty"] if p["side"] == BUY else (p["entry"] - ltp) * p["qty"]
                        msg.append("%s %s is %s %.0f rupees." % (
                            side_word(p["side"]), p["symbol"], "up" if pnl >= 0 else "down", abs(pnl)))
                    except Exception:
                        msg.append("%s %s price is unavailable." % (side_word(p["side"]), p["symbol"]))
            msg.append("Today's profit and loss is %s %.0f rupees." % (
                "plus" if st["daily_pnl"] >= 0 else "minus", abs(st["daily_pnl"])))
            msg.append("%d of %d losses in a row. Safety lockout is %s." % (
                st["consecutive_losses"], st["limit"], "active" if st["locked"] else "clear"))
            if self.last_shield is not None:
                msg.append(shield_sentence(self.last_shield))
            self.say(" ".join(msg), priority=True)
        except Exception as exc:
            self.log("Status error: %s" % exc)
            self.say("I could not read the status right now.", priority=True)

    # ---- automatic scanning (default every 60 seconds) ----
    @safe()
    def _schedule_auto(self):
        if self._auto_ev is not None:
            self._auto_ev.cancel()
            self._auto_ev = None
        if self.cfg.get("auto_scan"):
            self._auto_ev = Clock.schedule_interval(self._auto_tick, max(30, int(self.cfg.get("scan_secs", 60))))

    @safe()
    def _auto_tick(self, dt):
        if self.kill.active or not self.cfg.get("auto_scan") or self.broker is None \
                or self.busy or self.pending or self.token_expired:
            return
        if self._mode() == "LIVE" and not market_open():
            if not self._closed_said:
                self._closed_said = True
                self.say("The market is closed. I will resume automatic scanning when it opens.")
            return
        self._closed_said = False
        if self.risk.is_locked():
            return
        sym = self.cfg["symbol"]
        if any(p["symbol"] == sym for p in self._positions()):
            return
        self.scan(manual=False)

    # ---- POSITION CALCULATOR ----
    @safe()
    def open_calc(self):
        c = self.cfg
        d = self.last_decision
        self._calc = {}
        grid = GridLayout(cols=2, spacing=dp(6), padding=dp(6), size_hint_y=None,
                          row_default_height=dp(42), row_force_default=True)
        grid.bind(minimum_height=grid.setter("height"))

        def add(key, label, val):
            grid.add_widget(tlabel(label, color=DIM, font=11))
            ti = TextInput(text=str(val), multiline=False, input_filter="float",
                           background_color=PANEL_HI, foreground_color=TEXT, cursor_color=CYAN,
                           padding=[dp(8), dp(10)], font_size=sp(14))
            grid.add_widget(ti)
            self._calc[key] = ti

        has = d is not None and d.entry and d.sl and d.tp
        add("capital", "CAPITAL", c["capital"])
        add("risk_pct", "RISK % (MAX 2)", c["risk_pct"])
        add("entry", "ENTRY PRICE", d.entry if has else "")
        add("sl", "STOP LOSS", d.sl if has else "")
        add("tp", "TARGET", d.tp if has else "")
        sv = ScrollView(do_scroll_x=False)
        sv.add_widget(grid)

        self.lbl_calc = tlabel("Type the entry, stop loss and target, then tap CALCULATE.\n"
                               "Rule: the target should be at least twice as far as the stop.",
                               color=TEXT, font=13, valign="top", size_hint_y=None, height=dp(170))

        def mk(text, accent, fn):
            b = NeonButton(text=text, accent=accent)
            b.bind(on_release=fn)
            return b

        r1 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        r1.add_widget(mk("CALCULATE", GREEN, lambda *a: self._calc_run()))
        r1.add_widget(mk("USE LAST SETUP", CYAN, lambda *a: self._calc_fill()))
        r2 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        r2.add_widget(mk("CLOSE", DIM, lambda *a: self._calc_pop.dismiss()))

        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(6))
        for w in (sv, self.lbl_calc, r1, r2):
            box.add_widget(w)
        self._calc_pop = Popup(title="RISK-REWARD & POSITION SIZE", content=box, size_hint=(0.96, 0.92),
                               auto_dismiss=False, background="", background_color=BG,
                               title_color=CYAN, separator_color=CYAN)
        self._calc_pop.open()

    @safe()
    def _calc_fill(self):
        d = self.last_decision
        if d is None or not (d.entry and d.sl and d.tp):
            self.say("There is no setup yet. Run a scan first.", priority=True)
            return
        self._calc["entry"].text = str(d.entry)
        self._calc["sl"].text = str(d.sl)
        self._calc["tp"].text = str(d.tp)
        self._calc["capital"].text = str(self.cfg["capital"])
        self._calc["risk_pct"].text = str(self.cfg["risk_pct"])

    @safe()
    def _calc_run(self):
        f = self._calc
        try:
            cap = float(f["capital"].text)
            rp = float(f["risk_pct"].text)
            entry = float(f["entry"].text)
            sl = float(f["sl"].text)
            tp = float(f["tp"].text)
        except ValueError:
            self.say("Please type a number in every box.", priority=True)
            return
        plan = plan_position(cap, rp, entry, sl, tp, int(self.cfg["max_qty"]))
        if not plan["ok"]:
            self.lbl_calc.text = "Cannot calculate: %s." % plan["reason"]
            self.lbl_calc.color = AMBER
            self.say("I cannot calculate this, because %s." % plan["reason"], priority=True)
            return
        lines = ["%s TRADE" % plan["side"],
                 "Quantity: %d   Value: %.0f" % (plan["qty"], plan["value"]),
                 "Max loss at stop: %.0f  (%.2f%% of capital)" % (plan["risk_amt"], plan["capital_pct"]),
                 "Gain at target: %.0f" % plan["reward_amt"],
                 "Stop distance: %.2f%%   Risk:Reward = 1:%.2f" % (plan["sl_pct"], plan["rr"])]
        spoken = ("%s trade. Quantity %d, worth about %.0f rupees. If the stop loss hits, you lose about %.0f rupees, "
                  "which is %.1f percent of your capital. If the target hits, you gain about %.0f rupees. "
                  "Risk reward is 1 to %.1f." % (plan["side"].title(), plan["qty"], plan["value"],
                                                  plan["risk_amt"], plan["capital_pct"],
                                                  plan["reward_amt"], plan["rr"]))
        if plan["capped"]:
            lines.append("Risk was capped at %.0f%%, the hard limit." % MAX_RISK_PCT)
            spoken += " Your risk percent was reduced to the hard limit of %.0f percent." % MAX_RISK_PCT
        if plan["limit"] != "risk":
            lines.append("Quantity limited by: %s" % plan["limit"])
        if plan["rr"] < MIN_RR_FLOOR:
            lines.append("WARNING: below 1:2. The app will not take this trade.")
            spoken += " Warning. The reward is less than twice the risk, so this is a poor trade."
        self.lbl_calc.text = "\n".join(lines)
        self.lbl_calc.color = AMBER if plan["rr"] < MIN_RR_FLOOR else TEXT
        self.say(spoken, priority=True)

    # ---- JOURNAL & EMOTION TRACKER ----
    @safe()
    def set_mood(self, mood):
        mood = mood if mood in MOODS else "CALM"
        self.mood = mood
        self.mood_ts = time.time()
        if mood == "CALM":
            msg = "Noted. You feel calm. That is the best state for trading."
        else:
            msg = ("Noted. You feel %s. When emotions are high, the best trade is often no trade. "
                   "I will mark any trade you take now in your journal." % mood.lower())
        self.say(msg, priority=True)
        self._journal_refresh()

    @safe()
    def open_journal(self):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        self.lbl_journal = tlabel("", color=TEXT, font=12, valign="top")
        box.add_widget(self.lbl_journal)

        def mk(text, accent, fn):
            b = NeonButton(text=text, accent=accent, font_size=sp(12))
            b.bind(on_release=fn)
            return b

        box.add_widget(tlabel("HOW DO YOU FEEL RIGHT NOW?", color=DIM, font=11,
                              size_hint_y=None, height=dp(22)))
        r1 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        r1.add_widget(mk("CALM", GREEN, lambda *a: self.set_mood("CALM")))
        r1.add_widget(mk("EXCITED", AMBER, lambda *a: self.set_mood("EXCITED")))
        r1.add_widget(mk("ANXIOUS", AMBER, lambda *a: self.set_mood("ANXIOUS")))
        r1.add_widget(mk("ANGRY", RED, lambda *a: self.set_mood("ANGRY")))
        r2 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        r2.add_widget(mk("SPEAK TODAY'S FEEDBACK", CYAN, lambda *a: self._say_day_report()))
        r2.add_widget(mk("COPY CSV", CYAN, lambda *a: self._copy_csv()))
        r3 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        r3.add_widget(mk("CLOSE", DIM, lambda *a: self._journal_pop.dismiss()))
        for w in (r1, r2, r3):
            box.add_widget(w)
        self._journal_pop = Popup(title="AUTO JOURNAL & EMOTION TRACKER", content=box,
                                  size_hint=(0.96, 0.9), auto_dismiss=False, background="",
                                  background_color=BG, title_color=AMBER, separator_color=AMBER)
        self._journal_refresh()
        self._journal_pop.open()

    @safe()
    def _journal_refresh(self):
        if self.lbl_journal is None or self.journal is None:
            return
        today = datetime.now().date().isoformat()
        sm = self.journal.summary(today)
        lines = ["TODAY %s" % today,
                 "Trades %d   Wins %d   Losses %d   Open %d" % (sm["trades"], sm["wins"], sm["losses"], sm["open"]),
                 "Net P&L %.2f   Discipline score %d%%" % (sm["net"], sm["score"]),
                 "Mood now: %s (lasts 2 hours)" % self._cur_mood()]
        if sm["mistakes"]:
            lines.append("Mistakes: " + ", ".join("%s x%d" % (MISTAKE_NAMES[k], v)
                                                  for k, v in sm["mistakes"].items()))
        lines.append("")
        lines.append("LAST TRADES")
        recent = self.journal.recent(6)
        if not recent:
            lines.append("none yet")
        for e in recent:
            t = str(e.get("opened_at", ""))[11:16]
            pnl = e.get("pnl")
            ptxt = "open" if pnl is None else "%+.0f" % pnl
            fl = ",".join(e.get("flags", []))
            lines.append("%s %s %s x%s %s %s" % (t, e.get("side", ""), e.get("symbol", ""),
                                                   e.get("qty", ""), ptxt, ("[%s]" % fl) if fl else ""))
        self.lbl_journal.text = "\n".join(lines)

    @safe()
    def _copy_csv(self):
        if self.journal is None:
            return
        Clipboard.copy(self.journal.csv_text())
        self.say("The journal was copied as a spreadsheet file text. Paste it into any notes or sheet app.",
                 priority=True)

    @safe()
    def _say_day_report(self, auto=False):
        if self.journal is None:
            self.say("The journal is not available.", priority=True)
            return
        now = datetime.now()
        sm = self.journal.summary(now.date().isoformat())
        if auto and sm["trades"] == 0 and now.weekday() >= 5:
            return
        bad_n, bad_pnl = self.journal.mood_stats(now.date().isoformat())
        self.say(build_report(sm, bad_n, bad_pnl), priority=True)

    @safe()
    def _evening_tick(self, dt):
        if self.journal is None:
            return
        now = datetime.now()
        today = now.date().isoformat()
        if self.journal.state.get("last_report") == today:
            return
        if now.hour * 60 + now.minute < 15 * 60 + 45:
            return
        self.journal.mark_reported(today)
        self._say_day_report(auto=True)

    # ---- SIBLING MASTER LEARNING & PRACTICE TOOL (virtual only) ----
    @safe()
    def open_learn(self):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        self.lbl_learn = tlabel("", color=TEXT, font=13, valign="top")
        box.add_widget(self.lbl_learn)

        def mk(text, accent, fn):
            b = NeonButton(text=text, accent=accent, font_size=sp(13))
            b.bind(on_release=fn)
            return b

        r1 = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        r1.add_widget(mk("EXPLAIN MARKET", CYAN, lambda *a: self._learn_explain()))
        r1.add_widget(mk("WHERE TO ENTER?", CYAN, lambda *a: self._learn_coach()))
        r2 = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        r2.add_widget(mk("PRACTICE BUY", GREEN, lambda *a: self._learn_practice(BUY)))
        r2.add_widget(mk("PRACTICE SELL", RED, lambda *a: self._learn_practice(SELL)))
        r3 = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        r3.add_widget(mk("EXIT PRACTICE", AMBER, lambda *a: self._learn_close_all()))
        r3.add_widget(mk("MY SCORE", GREEN, lambda *a: self._learn_score()))
        r4 = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        r4.add_widget(mk("CLOSE", DIM, lambda *a: self._learn_pop.dismiss()))
        for w in (r1, r2, r3, r4):
            box.add_widget(w)
        self._learn_pop = Popup(title="SIBLING MASTER LEARNING & PRACTICE", content=box,
                                size_hint=(0.96, 0.86), auto_dismiss=False, background="",
                                background_color=BG, title_color=GREEN, separator_color=GREEN)
        self._learn_refresh()
        self._learn_pop.open()
        self.say("Learning mode with one lakh virtual rupees. Nothing here uses real money or sends orders "
                 "to Zerodha. Tap explain market to hear what the market is doing, or where to enter to "
                 "learn the best entry and exit points.", priority=True)

    @safe()
    def _learn_refresh(self):
        if self.lbl_learn is None or self.ledger is None:
            return
        s = self.ledger.stats()
        lines = ["VIRTUAL PRACTICE ACCOUNT (no real money)",
                 "Virtual capital: %.0f" % LearnerLedger.CAPITAL,
                 "Closed trades: %d   Wins: %d   Losses: %d" % (s["trades"], s["wins"], s["losses"]),
                 "Practice profit / loss: %.2f" % s["pnl"],
                 "Open practice trades: %d" % s["open"]]
        live = 0.0
        for p in self.ledger.positions():
            price = self.learn_prices.get(p["symbol"])
            line = "%s %s x%d @ %.2f  SL %.2f  TP %.2f  grade %s" % (
                p["side"], p["symbol"], p["qty"], p["entry"], p["sl"], p["tp"], p.get("grade", "-"))
            if price:
                pnl = (price - p["entry"]) * p["qty"] if p["side"] == BUY else (p["entry"] - price) * p["qty"]
                live += pnl
                line += "\n   now %.2f  open P&L %.0f" % (price, pnl)
            lines.append(line)
        if s["open"]:
            lines.append("Open P&L (live): %.0f" % live)
        self.lbl_learn.text = "\n".join(lines)

    @safe()
    def _learn_score(self):
        s = self.ledger.stats()
        if s["trades"] == 0:
            self.say("You have no finished practice trades yet. Try a practice buy or sell.", priority=True)
        else:
            self.say("Practice score. %s. %s won and %s lost. Virtual %s of %.0f rupees." % (
                plural(s["trades"], "finished trade"), plural(s["wins"], "trade"), plural(s["losses"], "trade"),
                "profit" if s["pnl"] >= 0 else "loss", abs(s["pnl"])), priority=True)
        self._learn_refresh()

    @safe()
    def _learn_explain(self):
        if self.broker is None or self.engine is None:
            self.say("The market engine is not available.", priority=True)
            return
        self.say("Let me look at the market for you.")
        threading.Thread(target=self._explain_worker, daemon=True).start()

    @safe()
    def _explain_worker(self):
        try:
            exch, sym, itv = self.cfg["exchange"], self.cfg["symbol"], self.cfg["interval"]
            candles = self.broker.get_candles(exch, sym, itv, 200)
            if len(candles) < MIN_CANDLES:
                self.say("There is not enough price history for %s yet." % sym, priority=True)
                return
            ctx = build_context(sym, candles)
            d = self.engine.evaluate(sym, candles)
            sh = compute_shield(candles, None)
            if ctx.trend == "BULL":
                tr = ("Price is making higher highs and higher lows. In simple words, buyers are winning, "
                      "so the trend is up.")
            elif ctx.trend == "BEAR":
                tr = ("Price is making lower highs and lower lows. In simple words, sellers are winning, "
                      "so the trend is down.")
            else:
                tr = ("Price is moving sideways without a clear direction. When the market is unclear, "
                      "the best move is often to wait.")
            votes = d.votes[:8]
            n_buy = sum(1 for v in votes if v.signal == BUY)
            n_sell = sum(1 for v in votes if v.signal == SELL)
            msg = "%s on %s. %s %d of the 8 analysis agents lean buy and %d lean sell. " % (
                tf_name(itv), sym, tr, n_buy, n_sell)
            msg += "Momentum is %s and volatility is %s. " % (
                sh["momentum"].lower(), "high" if sh["vol_ratio"] >= 1.5 else "normal")
            if sh["cautions"] or sh["blocks"]:
                msg += "Be careful: %s. " % "; ".join(sh["blocks"] + sh["cautions"])
            msg += ("Lesson: a good trade risks one rupee to try to win at least two. "
                    "This is learning mode, so no real order was placed.")
            self.say(msg, priority=True)
        except Exception as exc:
            self.log("Explain failed: %s" % exc)
            self.say("I could not read the market right now. %s" % exc, priority=True)

    @safe()
    def _learn_coach(self):
        if self.broker is None or self.engine is None:
            self.say("The market engine is not available.", priority=True)
            return
        self.say("Let me find the key price levels for you.")
        threading.Thread(target=self._coach_worker, daemon=True).start()

    @safe()
    def _coach_worker(self):
        try:
            exch, sym, itv = self.cfg["exchange"], self.cfg["symbol"], self.cfg["interval"]
            candles = self.broker.get_candles(exch, sym, itv, 200)
            if len(candles) < MIN_CANDLES:
                self.say("There is not enough price history for %s yet." % sym, priority=True)
                return
            ctx = build_context(sym, candles)
            price = self.broker.get_ltp(exch, sym)
            sup = [s.price for s in ctx.lows if s.price < price]
            res = [s.price for s in ctx.highs if s.price > price]
            sup_p = max(sup) if sup else None
            res_p = min(res) if res else None
            sh = compute_shield(candles, None)
            msg = "%s is at %.2f. " % (sym, price)
            if sup_p is not None:
                msg += "The nearest support, a floor where buyers stepped in before, is %.2f. " % sup_p
            if res_p is not None:
                msg += "The nearest resistance, a ceiling where sellers appeared, is %.2f. " % res_p
            if sup_p is not None and res_p is not None:
                if price - sup_p < res_p - price:
                    msg += "Price is closer to support, so a buy is the safer idea, with the stop just below %.2f. " % sup_p
                else:
                    msg += "Price is closer to resistance, so a sell is the safer idea, with the stop just above %.2f. " % res_p
            msg += ("Entry rule: enter near a floor or ceiling, not in the middle, because the middle gives a poor "
                    "reward for the risk. Exit rule: put the target at least twice as far as the stop, and never "
                    "move the stop further away. ")
            msg += shield_sentence(sh)
            self.say(msg, priority=True)
        except Exception as exc:
            self.log("Coach failed: %s" % exc)
            self.say("I could not read the levels right now. %s" % exc, priority=True)

    @safe()
    def _learn_practice(self, side):
        if self.broker is None or self.engine is None:
            self.say("The market data is not available.", priority=True)
            return
        if self.token_expired:
            self.say("Your Zerodha login has expired, so I cannot read live prices for practice.",
                     priority=True)
            return
        threading.Thread(target=self._practice_worker, args=(side,), daemon=True).start()

    @safe()
    def _practice_worker(self, side):
        try:
            exch, sym, itv = self.cfg["exchange"], self.cfg["symbol"], self.cfg["interval"]
            candles = self.broker.get_candles(exch, sym, itv, 200)
            if len(candles) < MIN_CANDLES:
                self.say("There is not enough price history for %s yet." % sym, priority=True)
                return
            ctx = build_context(sym, candles)
            d = self.engine.evaluate(sym, candles)
            sh = compute_shield(candles, side)
            entry = self.broker.get_ltp(exch, sym)
            a = ctx.atr if ctx.atr > 0 else entry * 0.003
            sl = tp = None
            if d.direction == side and d.rr > 0:
                sl, tp = d.sl, d.tp
            ok = (side == BUY and sl is not None and sl < entry < tp) or \
                 (side == SELL and sl is not None and tp < entry < sl)
            if not ok:
                if side == BUY:
                    sl, tp = entry - 1.5 * a, entry + 3.0 * a
                else:
                    sl, tp = entry + 1.5 * a, entry - 3.0 * a
            grade, why = grade_entry(side, d, sh, self.engine.min_agree)
            pos = self.ledger.open(exch, sym, side, entry, sl, tp, grade)
            if pos is None:
                self.say("I could not open that practice trade. You may already have one on %s." % sym,
                         priority=True)
            else:
                self.say("Practice %s opened on %s, quantity %d, at %.2f. Stop loss %.2f. Target %.2f. "
                         "The target is at least twice as far as the stop, which is the 1 to 2 rule. "
                         "Entry grade %s, because %s. I will tell you how it ends." % (
                             side.lower(), sym, pos["qty"], pos["entry"], pos["sl"], pos["tp"], grade, why),
                         priority=True)
        except Exception as exc:
            self.log("Practice failed: %s" % exc)
            self.say("The practice trade could not be opened. %s" % exc, priority=True)
        Clock.schedule_once(lambda dt: self._learn_refresh())

    @safe()
    def _learn_close_all(self):
        if not self.ledger.positions():
            self.say("You have no open practice trades.")
            return
        threading.Thread(target=self._learn_close_worker, daemon=True).start()

    @safe()
    def _learn_close_worker(self):
        try:
            total, n = 0.0, 0
            for p in self.ledger.positions():
                try:
                    ltp = self.broker.get_ltp(p["exchange"], p["symbol"])
                except Exception:
                    continue
                t = self.ledger.close(p["id"], ltp, "MANUAL")
                if t:
                    total += t["pnl"]
                    n += 1
            if n:
                lesson = ("You took profit early. Fine, but waiting for the target earns more over time."
                          if total >= 0 else
                          "You exited before the stop. That is fine if the market changed, but plan the exit "
                          "before you enter.")
                self.say("%s closed. Virtual %s of %.0f rupees. %s" % (
                    plural(n, "practice trade"), "profit" if total >= 0 else "loss", abs(total), lesson),
                    priority=True)
            else:
                self.say("I could not read prices to close the practice trades. Try again.")
        except Exception as exc:
            self.log("Practice close failed: %s" % exc)
        Clock.schedule_once(lambda dt: self._learn_refresh())

    @safe()
    def _learn_tick(self, dt):
        if self.broker is None or self.ledger is None or self.learn_busy or self.token_expired \
                or not self.ledger.positions():
            return
        self.learn_busy = True
        threading.Thread(target=self._learn_monitor, daemon=True).start()

    @safe()
    def _learn_monitor(self):
        try:
            seen = set()
            for p in self.ledger.positions():
                key = (p["exchange"], p["symbol"])
                if key in seen:
                    continue
                seen.add(key)
                try:
                    ltp = self.broker.get_ltp(p["exchange"], p["symbol"])
                except Exception:
                    continue
                self.learn_prices[p["symbol"]] = ltp
                for t in self.ledger.check(p["symbol"], ltp):
                    how = "hit its target" if t["reason"] == "TARGET" else "hit its stop loss"
                    lesson = ("Good. You waited for the target." if t["pnl"] >= 0 else
                              "That is normal. The stop loss kept the loss small, which protects real money later.")
                    g = t.get("grade") or ""
                    if g:
                        lesson += " Your entry grade was %s." % g
                        if g in ("C", "D") and t["pnl"] < 0:
                            lesson += " Low grade entries lose more often, so wait for an A."
                    self.say("Practice trade on %s %s. Virtual %s of %.0f rupees. %s" % (
                        t["symbol"], how, "profit" if t["pnl"] >= 0 else "loss", abs(t["pnl"]), lesson),
                        priority=True)
            Clock.schedule_once(lambda dt: self._learn_refresh())
        except Exception as exc:
            self.log("Practice monitor error: %s" % exc)
        finally:
            self.learn_busy = False

    # ---- settings popup ----
    @safe()
    def open_settings(self):
        c = self.cfg
        self._fields = {}
        grid = GridLayout(cols=2, spacing=dp(6), padding=dp(6), size_hint_y=None,
                          row_default_height=dp(40), row_force_default=True)
        grid.bind(minimum_height=grid.setter("height"))

        def add_field(key, label, value, password=False):
            grid.add_widget(tlabel(label, color=DIM, font=11))
            ti = TextInput(text=str(value), multiline=False, password=password,
                           background_color=PANEL_HI, foreground_color=TEXT, cursor_color=CYAN,
                           padding=[dp(8), dp(10)], font_size=sp(13))
            grid.add_widget(ti)
            self._fields[key] = ti

        def add_spinner(label, text, values):
            grid.add_widget(tlabel(label, color=DIM, font=11))
            spn = Spinner(text=text, values=values, background_normal="",
                          background_color=PANEL_HI, color=CYAN)
            grid.add_widget(spn)
            return spn

        add_field("api_key", "KITE API KEY", c["api_key"])
        add_field("api_secret", "KITE API SECRET", c["api_secret"], password=True)
        add_field("request_token", "TOKEN / REDIRECT URL", "")
        add_field("exchange", "EXCHANGE", c["exchange"])
        add_field("symbol", "SYMBOL", c["symbol"])
        self._sp_interval = add_spinner("INTERVAL", c["interval"], (
            "minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day"))
        modes = ("PAPER",) if PAPER_MODE else ("PAPER", "LIVE")
        cur = self._mode() if self._mode() in modes else "PAPER"
        self._sp_mode = add_spinner("MODE (LIVE LOCKED)" if PAPER_MODE else "MODE", cur, modes)
        self._sp_product = add_spinner("PRODUCT", c["product"], ("MIS", "CNC", "NRML"))
        add_field("capital", "CAPITAL", c["capital"])
        add_field("risk_pct", "RISK % / TRADE (MAX 2)", c["risk_pct"])
        add_field("min_rr", "MIN RISK:REWARD (>=2)", c["min_rr"])
        add_field("min_agree", "MIN AGENTS (6-11)", c["min_agree"])
        add_field("max_qty", "MAX QTY / ORDER", c["max_qty"])
        self._sp_auto = add_spinner("AUTO SCAN", "ON" if c["auto_scan"] else "OFF", ("ON", "OFF"))
        add_field("scan_secs", "SCAN EVERY (SECONDS)", c["scan_secs"])
        self._sp_autotrade = add_spinner("AUTO PAPER TRADES", "ON" if c["auto_trade"] else "OFF", ("ON", "OFF"))
        add_field("eleven_key", "ELEVENLABS API KEY", c["eleven_key"], password=True)
        add_field("voice_id", "VOICE ID", c["voice_id"])
        self._sp_voice = add_spinner("VOICE ALERTS", "ON" if c["voice_alerts"] else "OFF", ("ON", "OFF"))

        sv = ScrollView(do_scroll_x=False)
        sv.add_widget(grid)

        def mk(text, accent, fn):
            b = NeonButton(text=text, accent=accent)
            b.bind(on_release=fn)
            return b

        r1 = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        r1.add_widget(mk("OPEN LOGIN", CYAN, lambda *a: self._open_login()))
        r1.add_widget(mk("COPY URL", CYAN, lambda *a: self._copy_login()))
        r1.add_widget(mk("LOGIN", GREEN, lambda *a: self._do_login()))
        r2 = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        r2.add_widget(mk("USE DEMO DATA", AMBER, lambda *a: self._use_demo()))
        r2.add_widget(mk("TEST VOICE", CYAN, lambda *a: self._test_voice()))
        r2b = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        r2b.add_widget(mk("RE-ARM AFTER KILL", RED, lambda *a: self._ask_rearm()))
        r3 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        r3.add_widget(mk("SAVE", GREEN, lambda *a: self._save_settings()))
        r3.add_widget(mk("CLOSE", DIM, lambda *a: self._settings_pop.dismiss()))

        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(6))
        for w in (sv, r1, r2, r2b, r3):
            box.add_widget(w)
        self._settings_pop = Popup(title="CONFIG", content=box, size_hint=(0.96, 0.94),
                                   auto_dismiss=False, background="", background_color=BG,
                                   title_color=CYAN, separator_color=CYAN)
        self._settings_pop.open()

    @safe()
    def _read_creds(self):
        self.broker.update_credentials(self._fields["api_key"].text, self._fields["api_secret"].text)

    @safe()
    def _open_login(self):
        if self.broker is None:
            return
        try:
            self._read_creds()
            url = self.broker.login_url()
        except BrokerError as exc:
            self.say(str(exc))
            return
        if not open_url(url):
            Clipboard.copy(url)
            self.log("Could not open the browser; login URL copied to the clipboard.")

    @safe()
    def _copy_login(self):
        if self.broker is None:
            return
        try:
            self._read_creds()
            Clipboard.copy(self.broker.login_url())
            self.log("Login URL copied. Open it, sign in, copy the redirect URL, then tap LOGIN.")
        except BrokerError as exc:
            self.say(str(exc))

    @safe()
    def _do_login(self):
        if self.broker is None:
            self.say("The broker is not available.")
            return
        try:
            self._read_creds()
        except BrokerError as exc:
            self.say(str(exc))
            return
        token = self._fields["request_token"].text.strip() or (Clipboard.paste() or "").strip()
        self.log("Logging in to Kite ...")

        def work():
            try:
                uid = self.broker.login(token)
                self.conn_fail = 0
                self.conn_down = False
                self.token_expired = False
                self.had_session = True
                self.say("Zerodha login successful.", priority=True)
                self.log("Kite session active (%s)." % uid)
            except BrokerError as exc:
                self.say("Zerodha login failed. %s" % exc, priority=True)
            except Exception as exc:
                self.say("Zerodha login hit an unexpected error.", priority=True)
                self.log("Login error: %s" % exc)
            Clock.schedule_once(lambda dt: self._refresh_header())

        threading.Thread(target=work, daemon=True).start()

    @safe()
    def _use_demo(self):
        if self.broker is None:
            return
        if self._positions():
            self.say("Close open positions first.")
            return
        self.broker.clear_session()
        try:
            self.broker.set_mode("PAPER")
        except BrokerError as exc:
            self.log(str(exc))
        self.cfg["mode"] = self.broker.mode
        self._sp_mode.text = "PAPER"
        self.conn_fail = 0
        self.conn_down = False
        self.token_expired = False
        self.had_session = False
        self._refresh_header()
        self.say("Zerodha session cleared. Practice mode on demo data.")

    @safe()
    def _test_voice(self):
        if self.speaker is None:
            self.log("Voice system is not available.")
            return
        self.speaker.configure(self._fields["eleven_key"].text.strip(),
                               self._fields["voice_id"].text.strip() or DEFAULT_VOICE_ID,
                               self._sp_voice.text == "ON")
        self.say("Tactical Neural Matrix voice link is online.", priority=True)

    @safe()
    def _save_settings(self):
        f = self._fields
        try:
            new = {
                "api_key": f["api_key"].text.strip(), "api_secret": f["api_secret"].text.strip(),
                "exchange": f["exchange"].text.strip().upper(), "symbol": f["symbol"].text.strip().upper(),
                "interval": self._sp_interval.text, "product": self._sp_product.text,
                "capital": float(f["capital"].text), "risk_pct": float(f["risk_pct"].text),
                "min_rr": float(f["min_rr"].text), "min_agree": int(f["min_agree"].text),
                "max_qty": int(f["max_qty"].text), "scan_secs": int(f["scan_secs"].text),
                "auto_scan": self._sp_auto.text == "ON", "auto_trade": self._sp_autotrade.text == "ON",
                "eleven_key": f["eleven_key"].text.strip(),
                "voice_id": f["voice_id"].text.strip() or DEFAULT_VOICE_ID,
                "voice_alerts": self._sp_voice.text == "ON",
            }
            if not new["symbol"] or not new["exchange"]:
                raise ValueError("symbol or exchange is empty")
            if new["capital"] <= 0 or not (0 < new["risk_pct"] <= MAX_RISK_PCT) \
                    or new["min_rr"] < MIN_RR_FLOOR \
                    or not (MIN_AGREE_FLOOR <= new["min_agree"] <= 11) or new["max_qty"] < 1 \
                    or not (30 <= new["scan_secs"] <= 3600):
                raise ValueError("risk percent must be 0 to %.0f, risk reward at least %.0f, agents %d to 11, "
                                 "scan interval 30 to 3600 seconds" % (MAX_RISK_PCT, MIN_RR_FLOOR, MIN_AGREE_FLOOR))
        except ValueError as exc:
            self.say("Settings not saved. %s" % exc, priority=True)
            return
        if self.broker is not None:
            try:
                self.broker.update_credentials(new["api_key"], new["api_secret"])
                wanted = self._sp_mode.text
                if wanted == "LIVE" and PAPER_MODE:
                    wanted = "PAPER"
                self.broker.set_mode(wanted)
            except BrokerError as exc:
                self.say("Mode not changed. %s" % exc)
            new["mode"] = self.broker.mode
            self.broker.configure(new["product"], new["max_qty"], new["capital"])
        self.cfg.update(new)
        if self.engine is not None:
            self.engine.configure(new["min_agree"], new["min_rr"])
        if self.speaker is not None:
            self.speaker.configure(new["eleven_key"], new["voice_id"], new["voice_alerts"])
        self._save_cfg()
        self._schedule_auto()
        self._refresh_all()
        self.say("Settings saved. %s mode, %s." % (self._mode().title(), new["symbol"]), priority=True)
        self._settings_pop.dismiss()


if __name__ == "__main__":
    try:
        TacticalApp().run()
    except Exception as exc:  # last resort: log instead of a silent exit
        Logger.error("TNM: fatal: %s" % exc)
        Logger.error(traceback.format_exc())
