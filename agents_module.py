# -*- coding: utf-8 -*-
"""
agents_module.py - 11-agent consensus engine for Tactical Neural Matrix.

Agents (display order):
  01 Market Structure   (HH / HL / LH / LL from confirmed swings)
  02 Liquidity Sweep    (stop-run beyond a swing level + rejection)
  03 CHOCH              (change of character: break of last opposing swing)
  04 Trend EMA          (EMA20 / EMA50 alignment + slope)
  05 Momentum RSI       (Wilder RSI, avoids exhausted zones)
  06 Volume Flow        (volume spike confirming candle direction)
  07 Support/Resist     (price at a recent swing level)
  08 Candle Pattern     (engulfing / hammer / shooting star)
  09 Volatility Guard   (ATR% must be inside a tradable band)      [filter]
  10 Risk:Reward        (structure-based SL/TP, minimum RR 1:2)    [filter]
  11 Risk Lockout       (2 consecutive losses -> locked for the day, on disk) [filter + veto]

Hard limits (cannot be configured away):
  MAX_RISK_PCT = 2.0     per-trade risk cap (% of capital)
  MIN_RR_FLOOR = 2.0     minimum risk:reward
  MIN_AGREE_FLOOR = 6    minimum agreeing agents out of 11
"""
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

BUY = "BUY"
SELL = "SELL"
NEUTRAL = "NEUTRAL"
MIN_CANDLES = 60

MAX_RISK_PCT = 2.0
MIN_RR_FLOOR = 2.0
MIN_AGREE_FLOOR = 6


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass
class Candle:
    t: str
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass
class Swing:
    index: int
    price: float
    kind: str  # "H" or "L"


@dataclass
class Vote:
    agent: str
    signal: str = NEUTRAL
    confidence: float = 0.0
    reason: str = ""
    veto: bool = False


@dataclass
class Decision:
    symbol: str
    direction: str = NEUTRAL
    approved: bool = False
    agree: int = 0
    total: int = 11
    buy_score: float = 0.0
    sell_score: float = 0.0
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    rr: float = 0.0
    reason: str = ""
    votes: List[Vote] = field(default_factory=list)
    ts: float = field(default_factory=time.time)


@dataclass
class Context:
    symbol: str
    candles: List[Candle]
    swings: List[Swing]
    highs: List[Swing]
    lows: List[Swing]
    atr: float
    close: float
    trend: str          # BULL / BEAR / RANGE
    high_label: str     # HH / LH / N/A
    low_label: str      # HL / LL / N/A
    direction: Optional[str] = None
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    rr: float = 0.0


# --------------------------------------------------------------------------- #
# Indicators / structure helpers
# --------------------------------------------------------------------------- #
def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g = gains / period
    avg_l = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def atr(candles: List[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        pc = candles[i - 1].c
        trs.append(max(c.h - c.l, abs(c.h - pc), abs(c.l - pc)))
    if len(trs) < period:
        return sum(trs) / len(trs)
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def find_swings(candles: List[Candle], left: int = 2, right: int = 2) -> List[Swing]:
    swings: List[Swing] = []
    n = len(candles)
    for i in range(left, n - right):
        h = candles[i].h
        lo = candles[i].l
        if all(h > candles[j].h for j in range(i - left, i)) and \
           all(h >= candles[j].h for j in range(i + 1, i + right + 1)):
            swings.append(Swing(i, h, "H"))
        if all(lo < candles[j].l for j in range(i - left, i)) and \
           all(lo <= candles[j].l for j in range(i + 1, i + right + 1)):
            swings.append(Swing(i, lo, "L"))
    swings.sort(key=lambda s: s.index)
    return swings


def build_context(symbol: str, candles: List[Candle]) -> Context:
    swings = find_swings(candles)
    highs = [s for s in swings if s.kind == "H"]
    lows = [s for s in swings if s.kind == "L"]
    high_label = low_label = "N/A"
    trend = "RANGE"
    if len(highs) >= 2:
        high_label = "HH" if highs[-1].price > highs[-2].price else "LH"
    if len(lows) >= 2:
        low_label = "HL" if lows[-1].price > lows[-2].price else "LL"
    if high_label == "HH" and low_label == "HL":
        trend = "BULL"
    elif high_label == "LH" and low_label == "LL":
        trend = "BEAR"
    return Context(
        symbol=symbol, candles=candles, swings=swings, highs=highs, lows=lows,
        atr=atr(candles), close=candles[-1].c, trend=trend,
        high_label=high_label, low_label=low_label,
    )


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
class BaseAgent:
    name = "AGENT"
    kind = "signal"  # "signal" agents run first; "filter" agents need a direction

    def evaluate(self, ctx: Context) -> Vote:
        raise NotImplementedError

    def safe(self, ctx: Context) -> Vote:
        try:
            return self.evaluate(ctx)
        except Exception as exc:  # an agent must never crash the engine
            return Vote(self.name, NEUTRAL, 0.0, "agent error: %s" % exc)


class MarketStructureAgent(BaseAgent):
    name = "MARKET STRUCTURE"

    def evaluate(self, ctx):
        label = "%s + %s" % (ctx.high_label, ctx.low_label)
        if ctx.trend == "BULL":
            return Vote(self.name, BUY, 0.85, "Bullish structure %s" % label)
        if ctx.trend == "BEAR":
            return Vote(self.name, SELL, 0.85, "Bearish structure %s" % label)
        return Vote(self.name, NEUTRAL, 0.30, "Mixed / ranging: %s" % label)


class LiquiditySweepAgent(BaseAgent):
    name = "LIQUIDITY SWEEP"
    WINDOW = 3
    MAX_AGE = 40

    def evaluate(self, ctx):
        n = len(ctx.candles)
        cutoff = n - 1 - self.WINDOW
        recent = ctx.candles[-self.WINDOW:]
        tol = 0.15 * ctx.atr
        ref_lows = [s for s in ctx.lows if cutoff >= s.index >= n - self.MAX_AGE]
        ref_highs = [s for s in ctx.highs if cutoff >= s.index >= n - self.MAX_AGE]

        bull = bear = None
        if ref_lows:
            ref = ref_lows[-1]
            if any(c.l < ref.price for c in recent) and ctx.close > ref.price:
                pool = sum(1 for s in ref_lows[-5:] if abs(s.price - ref.price) <= tol)
                bull = (ref.price, pool)
        if ref_highs:
            ref = ref_highs[-1]
            if any(c.h > ref.price for c in recent) and ctx.close < ref.price:
                pool = sum(1 for s in ref_highs[-5:] if abs(s.price - ref.price) <= tol)
                bear = (ref.price, pool)

        if bull and not bear:
            conf = min(0.95, 0.70 + 0.10 * (bull[1] - 1))
            return Vote(self.name, BUY, conf,
                        "Sell-side liquidity swept below %.2f, closed back above (pool x%d)" % bull)
        if bear and not bull:
            conf = min(0.95, 0.70 + 0.10 * (bear[1] - 1))
            return Vote(self.name, SELL, conf,
                        "Buy-side liquidity swept above %.2f, closed back below (pool x%d)" % bear)
        return Vote(self.name, NEUTRAL, 0.2, "No fresh liquidity sweep")


class ChochAgent(BaseAgent):
    name = "CHOCH"
    FRESH_BARS = 5
    MAX_AGE = 30

    def evaluate(self, ctx):
        n = len(ctx.candles)
        closes = [c.c for c in ctx.candles]
        if ctx.trend == "BEAR" and ctx.highs:
            lvl = ctx.highs[-1]
            if lvl.index >= n - self.MAX_AGE:
                k = next((i for i in range(lvl.index + 1, n) if closes[i] > lvl.price), None)
                if k is not None and k >= n - self.FRESH_BARS:
                    return Vote(self.name, BUY, 0.85,
                                "Bullish CHOCH: close above last LH %.2f" % lvl.price)
        if ctx.trend == "BULL" and ctx.lows:
            lvl = ctx.lows[-1]
            if lvl.index >= n - self.MAX_AGE:
                k = next((i for i in range(lvl.index + 1, n) if closes[i] < lvl.price), None)
                if k is not None and k >= n - self.FRESH_BARS:
                    return Vote(self.name, SELL, 0.85,
                                "Bearish CHOCH: close below last HL %.2f" % lvl.price)
        return Vote(self.name, NEUTRAL, 0.2, "No fresh change of character")


class TrendAgent(BaseAgent):
    name = "TREND EMA"

    def evaluate(self, ctx):
        closes = [c.c for c in ctx.candles]
        e20 = ema(closes, 20)
        e50 = ema(closes, 50)
        slope = e20[-1] - e20[-4]
        if closes[-1] > e20[-1] > e50[-1] and slope > 0:
            return Vote(self.name, BUY, 0.75, "Price > EMA20 > EMA50, rising")
        if closes[-1] < e20[-1] < e50[-1] and slope < 0:
            return Vote(self.name, SELL, 0.75, "Price < EMA20 < EMA50, falling")
        return Vote(self.name, NEUTRAL, 0.25, "EMAs not aligned")


class MomentumAgent(BaseAgent):
    name = "MOMENTUM RSI"

    def evaluate(self, ctx):
        val = rsi([c.c for c in ctx.candles])
        if val is None:
            return Vote(self.name, NEUTRAL, 0.0, "Not enough data for RSI")
        if 55 <= val <= 75:
            return Vote(self.name, BUY, 0.6, "RSI %.1f bullish momentum" % val)
        if 25 <= val <= 45:
            return Vote(self.name, SELL, 0.6, "RSI %.1f bearish momentum" % val)
        if val > 75:
            return Vote(self.name, NEUTRAL, 0.3, "RSI %.1f overbought" % val)
        if val < 25:
            return Vote(self.name, NEUTRAL, 0.3, "RSI %.1f oversold" % val)
        return Vote(self.name, NEUTRAL, 0.2, "RSI %.1f neutral" % val)


class VolumeAgent(BaseAgent):
    name = "VOLUME FLOW"

    def evaluate(self, ctx):
        vols = [c.v for c in ctx.candles[-21:-1]]
        avg = sum(vols) / len(vols) if vols else 0.0
        if avg <= 0:
            return Vote(self.name, NEUTRAL, 0.0, "No volume data")
        last = ctx.candles[-1]
        ratio = last.v / avg
        if ratio >= 1.3:
            if last.c > last.o:
                return Vote(self.name, BUY, min(0.9, 0.4 + 0.2 * ratio),
                            "Volume %.1fx avg on green candle" % ratio)
            if last.c < last.o:
                return Vote(self.name, SELL, min(0.9, 0.4 + 0.2 * ratio),
                            "Volume %.1fx avg on red candle" % ratio)
        return Vote(self.name, NEUTRAL, 0.2, "Volume %.1fx avg, no spike" % ratio)


class SupportResistanceAgent(BaseAgent):
    name = "SUPPORT / RESIST"

    def evaluate(self, ctx):
        near = 0.6 * ctx.atr
        d_sup = d_res = None
        if ctx.lows:
            d_sup = ctx.close - ctx.lows[-1].price
        if ctx.highs:
            d_res = ctx.highs[-1].price - ctx.close
        at_sup = d_sup is not None and 0 <= d_sup <= near
        at_res = d_res is not None and 0 <= d_res <= near
        if at_sup and (not at_res or d_sup <= d_res):
            return Vote(self.name, BUY, 0.6, "At support %.2f" % ctx.lows[-1].price)
        if at_res:
            return Vote(self.name, SELL, 0.6, "At resistance %.2f" % ctx.highs[-1].price)
        return Vote(self.name, NEUTRAL, 0.2, "Price between key levels")


class CandlePatternAgent(BaseAgent):
    name = "CANDLE PATTERN"

    def evaluate(self, ctx):
        cur, prev = ctx.candles[-1], ctx.candles[-2]
        body = abs(cur.c - cur.o)
        rng = cur.h - cur.l
        if rng <= 0:
            return Vote(self.name, NEUTRAL, 0.0, "Flat candle")
        upper = cur.h - max(cur.o, cur.c)
        lower = min(cur.o, cur.c) - cur.l
        if prev.c < prev.o and cur.c > cur.o and cur.o <= prev.c and cur.c >= prev.o:
            return Vote(self.name, BUY, 0.7, "Bullish engulfing")
        if prev.c > prev.o and cur.c < cur.o and cur.o >= prev.c and cur.c <= prev.o:
            return Vote(self.name, SELL, 0.7, "Bearish engulfing")
        if lower >= 2 * max(body, rng * 0.05) and upper <= 0.5 * max(body, rng * 0.05):
            return Vote(self.name, BUY, 0.6, "Hammer / bullish pin bar")
        if upper >= 2 * max(body, rng * 0.05) and lower <= 0.5 * max(body, rng * 0.05):
            return Vote(self.name, SELL, 0.6, "Shooting star / bearish pin bar")
        return Vote(self.name, NEUTRAL, 0.2, "No actionable pattern")


class VolatilityGuardAgent(BaseAgent):
    name = "VOLATILITY GUARD"
    kind = "filter"

    def __init__(self, min_pct: float = 0.02, max_pct: float = 5.0):
        self.min_pct = min_pct
        self.max_pct = max_pct

    def evaluate(self, ctx):
        pct = (ctx.atr / ctx.close * 100.0) if ctx.close else 0.0
        if ctx.direction not in (BUY, SELL):
            return Vote(self.name, NEUTRAL, 0.0, "Awaiting directional bias (ATR %.2f%%)" % pct)
        if self.min_pct <= pct <= self.max_pct:
            return Vote(self.name, ctx.direction, 0.6, "ATR %.2f%% inside tradable band" % pct)
        return Vote(self.name, NEUTRAL, 0.0,
                    "ATR %.2f%% outside %.2f-%.2f%% band" % (pct, self.min_pct, self.max_pct))


class RiskRewardAgent(BaseAgent):
    name = "RISK : REWARD"
    kind = "filter"

    def __init__(self, min_rr: float = MIN_RR_FLOOR):
        self.min_rr = max(float(min_rr), MIN_RR_FLOOR)

    def evaluate(self, ctx):
        d = ctx.direction
        if d not in (BUY, SELL):
            return Vote(self.name, NEUTRAL, 0.0, "Awaiting directional bias")
        entry = ctx.close
        a = ctx.atr if ctx.atr > 0 else entry * 0.002
        buf = 0.15 * a
        floor_idx = len(ctx.candles) - 80
        if d == BUY:
            stops = [s.price for s in ctx.lows if s.price < entry]
            sl = (stops[-1] if stops else entry - 1.5 * a) - buf
            targets = [s.price for s in ctx.highs if s.price > entry and s.index >= floor_idx]
            tp = max(targets) if targets else 0.0
            risk = entry - sl
            reward = (tp - entry) if tp else 0.0
        else:
            stops = [s.price for s in ctx.highs if s.price > entry]
            sl = (stops[-1] if stops else entry + 1.5 * a) + buf
            targets = [s.price for s in ctx.lows if s.price < entry and s.index >= floor_idx]
            tp = min(targets) if targets else 0.0
            risk = sl - entry
            reward = (entry - tp) if tp else 0.0

        if risk <= 0:
            return Vote(self.name, NEUTRAL, 0.0, "Invalid stop distance")
        if risk > 3.0 * a:
            return Vote(self.name, NEUTRAL, 0.0, "Stop too wide (%.2f > 3 ATR)" % risk)
        rr = reward / risk if reward > 0 else 0.0
        ctx.entry, ctx.sl, ctx.tp, ctx.rr = round(entry, 2), round(sl, 2), round(tp, 2), round(rr, 2)
        if rr >= self.min_rr:
            return Vote(self.name, d, min(0.95, 0.5 + 0.1 * rr),
                        "RR 1:%.2f  SL %.2f  TP %.2f" % (rr, sl, tp))
        return Vote(self.name, NEUTRAL, 0.0, "RR 1:%.2f below required 1:%.1f" % (rr, self.min_rr))


class RiskLockoutAgent(BaseAgent):
    name = "RISK LOCKOUT"
    kind = "filter"

    def __init__(self, risk_manager: "RiskManager"):
        self.risk = risk_manager

    def evaluate(self, ctx):
        st = self.risk.status()
        if st["locked"]:
            return Vote(self.name, NEUTRAL, 1.0,
                        "LOCKED for today: %d/%d consecutive losses" % (st["consecutive_losses"], st["limit"]),
                        veto=True)
        if ctx.direction not in (BUY, SELL):
            return Vote(self.name, NEUTRAL, 0.0,
                        "Clear (%d/%d losses) - awaiting bias" % (st["consecutive_losses"], st["limit"]))
        return Vote(self.name, ctx.direction, 1.0,
                    "Clear: %d/%d consecutive losses" % (st["consecutive_losses"], st["limit"]))


# --------------------------------------------------------------------------- #
# Risk manager (disk-saved hard daily lockout, no manual override)
# --------------------------------------------------------------------------- #
class RiskManager:
    """
    Persists loss streak and lockout state in risk_data.json (atomic writes).
    After MAX_CONSECUTIVE_LOSSES consecutive losing trades the lock engages and
    clears only when the calendar date changes. There is deliberately no manual
    unlock method.
    """
    MAX_CONSECUTIVE_LOSSES = 2

    def __init__(self, path: str, unlock_next_day: bool = True):
        self.path = path
        self.unlock_next_day = unlock_next_day
        self._lock = threading.RLock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.data = self._load()
        self._roll_day()

    # -- persistence -------------------------------------------------------
    @staticmethod
    def _default() -> Dict:
        return {
            "consecutive_losses": 0, "locked": False, "locked_on": None,
            "lock_reason": "", "total_wins": 0, "total_losses": 0,
            "daily_pnl": 0.0, "pnl_date": date.today().isoformat(), "history": [],
        }

    def _write(self, data: Dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _save(self) -> None:
        try:
            self._write(self.data)
        except OSError:
            pass

    def _load(self) -> Dict:
        if not os.path.exists(self.path):
            d = self._default()
            try:
                self._write(d)
            except OSError:
                pass
            return d
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("bad format")
            d = self._default()
            d.update(raw)
            d["consecutive_losses"] = int(d["consecutive_losses"])
            return d
        except Exception:
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            d = self._default()
            d["locked"] = True  # fail safe: unreadable state means locked
            d["locked_on"] = date.today().isoformat()
            d["lock_reason"] = "risk_data.json unreadable - locked as precaution"
            try:
                self._write(d)
            except OSError:
                pass
            return d

    # -- logic -------------------------------------------------------------
    def _roll_day(self) -> None:
        today = date.today().isoformat()
        changed = False
        if self.data.get("pnl_date") != today:
            self.data["pnl_date"] = today
            self.data["daily_pnl"] = 0.0
            changed = True
        if self.data.get("locked") and self.unlock_next_day and self.data.get("locked_on") != today:
            self.data["locked"] = False
            self.data["consecutive_losses"] = 0
            self.data["lock_reason"] = ""
            changed = True
        if changed:
            self._save()

    def is_locked(self) -> bool:
        with self._lock:
            self._roll_day()
            return bool(self.data["locked"])

    def record_trade_result(self, pnl: float, symbol: str = "") -> None:
        with self._lock:
            self._roll_day()
            if pnl < 0:
                self.data["consecutive_losses"] += 1
                self.data["total_losses"] += 1
                if self.data["consecutive_losses"] >= self.MAX_CONSECUTIVE_LOSSES:
                    self.data["locked"] = True
                    self.data["locked_on"] = date.today().isoformat()
                    self.data["lock_reason"] = "%d consecutive losses - locked until next day" % \
                        self.data["consecutive_losses"]
            elif pnl > 0:
                self.data["consecutive_losses"] = 0
                self.data["total_wins"] += 1
            self.data["daily_pnl"] = round(self.data["daily_pnl"] + pnl, 2)
            self.data["history"].append({
                "t": datetime.now().isoformat(timespec="seconds"),
                "symbol": symbol, "pnl": round(pnl, 2),
            })
            self.data["history"] = self.data["history"][-100:]
            self._save()

    def status(self) -> Dict:
        with self._lock:
            self._roll_day()
            return {
                "locked": bool(self.data["locked"]),
                "consecutive_losses": int(self.data["consecutive_losses"]),
                "limit": self.MAX_CONSECUTIVE_LOSSES,
                "wins": int(self.data["total_wins"]),
                "losses": int(self.data["total_losses"]),
                "daily_pnl": float(self.data["daily_pnl"]),
                "reason": self.data.get("lock_reason", ""),
            }


# --------------------------------------------------------------------------- #
# Consensus engine
# --------------------------------------------------------------------------- #
class ConsensusEngine:
    def __init__(self, risk_manager: RiskManager, min_agree: int = 6, min_rr: float = 2.0):
        self.risk = risk_manager
        self.min_agree = max(MIN_AGREE_FLOOR, min(11, int(min_agree)))
        self.min_rr = max(MIN_RR_FLOOR, float(min_rr))
        self.signal_agents: List[BaseAgent] = [
            MarketStructureAgent(), LiquiditySweepAgent(), ChochAgent(), TrendAgent(),
            MomentumAgent(), VolumeAgent(), SupportResistanceAgent(), CandlePatternAgent(),
        ]
        self.filter_agents: List[BaseAgent] = [
            VolatilityGuardAgent(), RiskRewardAgent(self.min_rr), RiskLockoutAgent(risk_manager),
        ]

    @property
    def agent_names(self) -> List[str]:
        return [a.name for a in self.signal_agents + self.filter_agents]

    def configure(self, min_agree: int, min_rr: float) -> None:
        self.min_agree = max(MIN_AGREE_FLOOR, min(11, int(min_agree)))
        self.min_rr = max(MIN_RR_FLOOR, float(min_rr))
        self.filter_agents[1].min_rr = self.min_rr

    def evaluate(self, symbol: str, candles: List[Candle]) -> Decision:
        everyone = self.signal_agents + self.filter_agents
        if len(candles) < MIN_CANDLES:
            votes = [Vote(a.name, NEUTRAL, 0.0, "Need %d candles, have %d" % (MIN_CANDLES, len(candles)))
                     for a in everyone]
            return Decision(symbol=symbol, votes=votes, total=len(everyone),
                            reason="Insufficient market data")

        ctx = build_context(symbol, candles)
        votes = [a.safe(ctx) for a in self.signal_agents]

        buy_score = sum(v.confidence for v in votes if v.signal == BUY)
        sell_score = sum(v.confidence for v in votes if v.signal == SELL)
        n_buy = sum(1 for v in votes if v.signal == BUY)
        n_sell = sum(1 for v in votes if v.signal == SELL)
        direction = None
        if n_buy > n_sell and buy_score > sell_score:
            direction = BUY
        elif n_sell > n_buy and sell_score > buy_score:
            direction = SELL
        ctx.direction = direction

        votes += [a.safe(ctx) for a in self.filter_agents]

        agree = sum(1 for v in votes if direction and v.signal == direction)
        veto = any(v.veto for v in votes)
        rr_ok = direction is not None and votes[9].signal == direction
        approved = bool(direction) and not veto and rr_ok and agree >= self.min_agree

        if direction is None:
            reason = "No directional consensus"
        elif veto:
            reason = "VETO: risk lockout active for today"
        elif not rr_ok:
            reason = "%s bias rejected: risk-reward below 1:%.1f" % (direction, self.min_rr)
        elif agree < self.min_agree:
            reason = "%s bias has %d/%d agents (need %d)" % (direction, agree, len(everyone), self.min_agree)
        else:
            reason = "%s APPROVED: %d/%d agents, RR 1:%.2f" % (direction, agree, len(everyone), ctx.rr)

        return Decision(
            symbol=symbol, direction=direction or NEUTRAL, approved=approved, agree=agree,
            total=len(everyone), buy_score=round(buy_score, 2), sell_score=round(sell_score, 2),
            entry=ctx.entry, sl=ctx.sl, tp=ctx.tp, rr=ctx.rr, reason=reason, votes=votes,
        )


def position_size(capital: float, risk_pct: float, entry: float, sl: float, max_qty: int = 1000) -> int:
    """Quantity such that a stop-out loses at most risk_pct% of capital (hard-capped at MAX_RISK_PCT)."""
    risk_pct = min(float(risk_pct), MAX_RISK_PCT)
    per_unit = abs(entry - sl)
    if per_unit <= 0 or capital <= 0 or risk_pct <= 0:
        return 0
    qty = int((capital * risk_pct / 100.0) // per_unit)
    return max(0, min(qty, int(max_qty)))
