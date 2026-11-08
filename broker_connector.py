# -*- coding: utf-8 -*-
"""
broker_connector.py - Zerodha Kite Connect order management (PAPER + LIVE).

Talks to the Kite REST API directly with `requests` (no kiteconnect package,
which pulls heavy dependencies that break python-for-android builds).

PAPER mode : simulated MARKET fills, SL/TP simulated by check_exits().
LIVE mode  : MARKET entry + protective SL-M order on the exchange; target is
             monitored by the app (check_exits) and exited with a MARKET order.
"""
import csv
import hashlib
import io
import json
import math
import os
import random
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

from agents_module import BUY, SELL, Candle

KITE_ROOT = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"

INTERVAL_MINUTES = {
    "minute": 1, "3minute": 3, "5minute": 5, "10minute": 10,
    "15minute": 15, "30minute": 30, "60minute": 60, "day": 375,
}
INTERVAL_MAX_DAYS = {
    "minute": 55, "3minute": 95, "5minute": 95, "10minute": 95,
    "15minute": 190, "30minute": 190, "60minute": 390, "day": 1900,
}


class BrokerError(Exception):
    pass


def _opposite(side: str) -> str:
    return SELL if side == BUY else BUY


def extract_request_token(text: str) -> str:
    """Accepts a bare request token or the full redirect URL and returns the token."""
    text = (text or "").strip()
    if "request_token=" in text:
        qs = parse_qs(urlparse(text).query)
        if qs.get("request_token"):
            return qs["request_token"][0].strip()
        return text.split("request_token=")[1].split("&")[0].strip()
    return text


class KiteBroker:
    def __init__(self, data_dir: str, mode: str = "PAPER", api_key: str = "", api_secret: str = "",
                 product: str = "MIS", paper_capital: float = 100000.0, max_order_qty: int = 1000,
                 tick: float = 0.05):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.mode = mode.upper() if mode.upper() in ("PAPER", "LIVE") else "PAPER"
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.product = product
        self.paper_capital = float(paper_capital)
        self.max_order_qty = int(max_order_qty)
        self.tick = tick
        self.http = requests.Session()
        self._lock = threading.RLock()
        self._paper_orders: Dict[str, Dict] = {}
        self._inst_cache: Dict[str, Dict[str, str]] = {}
        self._demo: Dict[str, Dict] = {}
        self.access_token = ""
        self.user_id = ""
        self._load_session()
        self.state = self._load_state()
        if self.mode == "LIVE" and not self.has_session():
            self.mode = "PAPER"

    # ------------------------------------------------------------------ #
    # Settings / session
    # ------------------------------------------------------------------ #
    @property
    def _session_path(self):
        return os.path.join(self.data_dir, "kite_session.json")

    @property
    def _state_path(self):
        return os.path.join(self.data_dir, "broker_state.json")

    def update_credentials(self, api_key: str, api_secret: str) -> None:
        api_key = api_key.strip()
        if api_key != self.api_key:
            self.clear_session()
        self.api_key = api_key
        self.api_secret = api_secret.strip()

    def configure(self, product: str, max_order_qty: int, paper_capital: float) -> None:
        self.product = product
        self.max_order_qty = int(max_order_qty)
        self.paper_capital = float(paper_capital)

    def set_mode(self, mode: str) -> None:
        mode = mode.upper()
        if mode not in ("PAPER", "LIVE"):
            raise BrokerError("Mode must be PAPER or LIVE")
        if mode == self.mode:
            return
        if self.state["positions"]:
            raise BrokerError("Close all open positions before switching mode")
        if mode == "LIVE" and not self.has_session():
            raise BrokerError("LIVE mode needs an active Kite session (login first)")
        self.mode = mode

    def has_session(self) -> bool:
        return bool(self.api_key and self.access_token)

    def login_url(self) -> str:
        if not self.api_key:
            raise BrokerError("Enter your API key first")
        return LOGIN_URL.format(api_key=self.api_key)

    def login(self, request_token: str) -> str:
        request_token = extract_request_token(request_token)
        if not (self.api_key and self.api_secret and request_token):
            raise BrokerError("API key, API secret and request token are all required")
        checksum = hashlib.sha256((self.api_key + request_token + self.api_secret).encode()).hexdigest()
        try:
            r = requests.post(KITE_ROOT + "/session/token",
                              data={"api_key": self.api_key, "request_token": request_token,
                                    "checksum": checksum},
                              headers={"X-Kite-Version": "3"}, timeout=20)
            payload = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise BrokerError("Login failed: %s" % exc)
        if r.status_code != 200 or payload.get("status") != "success":
            raise BrokerError("Login failed: %s" % payload.get("message", "HTTP %s" % r.status_code))
        data = payload["data"]
        self.access_token = data["access_token"]
        self.user_id = data.get("user_id", "")
        self._save_session()
        return self.user_id or "OK"

    def clear_session(self) -> None:
        self.access_token = ""
        self.user_id = ""
        try:
            os.remove(self._session_path)
        except OSError:
            pass

    def _save_session(self) -> None:
        with open(self._session_path, "w", encoding="utf-8") as f:
            json.dump({"api_key": self.api_key, "access_token": self.access_token,
                       "user_id": self.user_id, "date": date.today().isoformat()}, f)

    def _load_session(self) -> None:
        try:
            with open(self._session_path, "r", encoding="utf-8") as f:
                s = json.load(f)
            # Kite tokens expire early morning; only trust same-day tokens.
            if s.get("date") == date.today().isoformat() and s.get("api_key") == self.api_key:
                self.access_token = s.get("access_token", "")
                self.user_id = s.get("user_id", "")
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ #
    # State persistence
    # ------------------------------------------------------------------ #
    def _load_state(self) -> Dict:
        base = {"positions": [], "trades": [], "paper_realised": 0.0, "seq": 0}
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                base.update(json.load(f))
        except (OSError, ValueError):
            pass
        return base

    def _save_state(self) -> None:
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def _next_seq(self) -> int:
        with self._lock:
            self.state["seq"] += 1
            return self.state["seq"]

    def open_positions(self) -> List[Dict]:
        with self._lock:
            return [dict(p) for p in self.state["positions"]]

    def recent_trades(self, n: int = 20) -> List[Dict]:
        with self._lock:
            return [dict(t) for t in self.state["trades"][-n:]]

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        return {"X-Kite-Version": "3",
                "Authorization": "token %s:%s" % (self.api_key, self.access_token)}

    def _request(self, method: str, path: str, params=None, data=None, raw: bool = False, timeout: int = 15):
        if not self.has_session():
            raise BrokerError("No active Kite session - login in CONFIG")
        try:
            r = self.http.request(method, KITE_ROOT + path, params=params, data=data,
                                  headers=self._headers(), timeout=timeout)
        except requests.RequestException as exc:
            raise BrokerError("Network error: %s" % exc)
        if raw:
            if r.status_code != 200:
                raise BrokerError("HTTP %s from %s" % (r.status_code, path))
            return r.text
        try:
            payload = r.json()
        except ValueError:
            raise BrokerError("Invalid response (HTTP %s)" % r.status_code)
        if r.status_code != 200 or payload.get("status") != "success":
            if payload.get("error_type") == "TokenException":
                self.clear_session()
            raise BrokerError(payload.get("message") or "HTTP %s" % r.status_code)
        return payload.get("data")

    def round_tick(self, x: float) -> float:
        return round(round(x / self.tick) * self.tick, 2)

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #
    @property
    def data_source(self) -> str:
        return "KITE" if self.has_session() else "DEMO"

    def get_ltp(self, exchange: str, symbol: str) -> float:
        if self.has_session():
            key = "%s:%s" % (exchange, symbol)
            data = self._request("GET", "/quote/ltp", params={"i": key})
            if key not in data:
                raise BrokerError("No LTP for %s" % key)
            return float(data[key]["last_price"])
        if self.mode == "LIVE":
            raise BrokerError("LIVE mode requires a Kite session")
        return self._demo_ltp(symbol)

    def get_candles(self, exchange: str, symbol: str, interval: str, count: int = 200) -> List[Candle]:
        if interval not in INTERVAL_MINUTES:
            raise BrokerError("Unsupported interval: %s" % interval)
        if self.has_session():
            return self._historical(exchange, symbol, interval, count)
        if self.mode == "LIVE":
            raise BrokerError("LIVE mode requires a Kite session")
        return self._demo_candles(symbol, interval, count)

    def _instrument_token(self, exchange: str, symbol: str) -> str:
        symbol = symbol.upper()
        mapping = self._inst_cache.get(exchange)
        path = os.path.join(self.data_dir, "instruments_%s.json" % exchange)
        if mapping is None:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    blob = json.load(f)
                if blob.get("date") == date.today().isoformat():
                    mapping = blob["map"]
            except (OSError, ValueError, KeyError):
                mapping = None
        if mapping is None:
            text = self._request("GET", "/instruments/%s" % exchange, raw=True, timeout=60)
            mapping = {}
            for row in csv.DictReader(io.StringIO(text)):
                ts = row.get("tradingsymbol", "").upper()
                if ts and (ts not in mapping or row.get("instrument_type") == "EQ"):
                    mapping[ts] = row.get("instrument_token", "")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"date": date.today().isoformat(), "map": mapping}, f)
            except OSError:
                pass
        self._inst_cache[exchange] = mapping
        token = mapping.get(symbol)
        if not token:
            raise BrokerError("Instrument %s:%s not found" % (exchange, symbol))
        return token

    def _historical(self, exchange: str, symbol: str, interval: str, count: int) -> List[Candle]:
        token = self._instrument_token(exchange, symbol)
        mins = INTERVAL_MINUTES[interval]
        if interval == "day":
            days = int(count * 1.6) + 5
        else:
            days = int(math.ceil(count * mins / 375.0 * 1.7)) + 3
        days = max(3, min(days, INTERVAL_MAX_DAYS[interval]))
        to = datetime.now()
        frm = to - timedelta(days=days)
        data = self._request("GET", "/instruments/historical/%s/%s" % (token, interval),
                             params={"from": frm.strftime("%Y-%m-%d %H:%M:%S"),
                                     "to": to.strftime("%Y-%m-%d %H:%M:%S")}, timeout=30)
        rows = data.get("candles", []) if isinstance(data, dict) else []
        out = [Candle(str(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                      float(r[5]) if len(r) > 5 else 0.0) for r in rows]
        return out[-count:]

    # ---- demo feed (seeded random walk with trending regimes) ----------- #
    def _demo_state(self, symbol: str) -> Dict:
        symbol = symbol.upper()
        st = self._demo.get(symbol)
        if st is None:
            rng = random.Random(sum(ord(ch) * (i + 1) for i, ch in enumerate(symbol)))
            st = {"rng": rng, "price": rng.uniform(80, 2500), "drift": 0.0, "left": 0,
                  "candles": [], "clock": datetime.now() - timedelta(minutes=5 * 420)}
            self._demo[symbol] = st
            for _ in range(400):
                self._demo_step(st)
        return st

    def _demo_step(self, st: Dict) -> None:
        rng = st["rng"]
        if st["left"] <= 0:
            st["drift"] = rng.choice([-1, 0, 1]) * rng.uniform(0.0005, 0.002)
            st["left"] = rng.randint(15, 45)
        st["left"] -= 1
        o = st["price"]
        c = max(1.0, o * (1.0 + st["drift"] + rng.gauss(0, 0.0025)))
        h = max(o, c) * (1.0 + abs(rng.gauss(0, 0.0012)))
        lo = min(o, c) * (1.0 - abs(rng.gauss(0, 0.0012)))
        v = rng.uniform(1000, 5000) * (3.0 if rng.random() < 0.08 else 1.0)
        st["clock"] += timedelta(minutes=5)
        st["candles"].append(Candle(st["clock"].strftime("%Y-%m-%d %H:%M"), round(o, 2),
                                    round(h, 2), round(lo, 2), round(c, 2), round(v)))
        st["candles"] = st["candles"][-600:]
        st["price"] = c

    def _demo_candles(self, symbol: str, interval: str, count: int) -> List[Candle]:
        st = self._demo_state(symbol)
        self._demo_step(st)
        return [Candle(c.t, c.o, c.h, c.l, c.c, c.v) for c in st["candles"][-count:]]

    def _demo_ltp(self, symbol: str) -> float:
        st = self._demo_state(symbol)
        last = st["candles"][-1]
        new_c = max(1.0, round(last.c * (1.0 + st["rng"].gauss(0, 0.0008)), 2))
        last.c = new_c
        last.h = max(last.h, new_c)
        last.l = min(last.l, new_c)
        st["price"] = new_c
        return new_c

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    def place_order(self, exchange: str, symbol: str, side: str, qty: int, order_type: str = "MARKET",
                    price: Optional[float] = None, trigger_price: Optional[float] = None,
                    product: Optional[str] = None, tag: str = "TNM") -> str:
        side = side.upper()
        order_type = order_type.upper()
        if side not in (BUY, SELL):
            raise BrokerError("Invalid side")
        qty = int(qty)
        if qty < 1 or qty > self.max_order_qty:
            raise BrokerError("Quantity %d outside 1..%d" % (qty, self.max_order_qty))

        if self.mode == "PAPER":
            if order_type != "MARKET":
                raise BrokerError("PAPER mode simulates MARKET orders only (SL/TP handled by check_exits)")
            ltp = self.get_ltp(exchange, symbol)
            oid = "PAPER-%d" % self._next_seq()
            self._paper_orders[oid] = {"status": "COMPLETE", "average_price": ltp,
                                       "filled_quantity": qty, "message": ""}
            return oid

        data = {"tradingsymbol": symbol, "exchange": exchange, "transaction_type": side,
                "order_type": order_type, "quantity": qty, "product": product or self.product,
                "validity": "DAY", "tag": tag[:20]}
        if order_type in ("LIMIT", "SL"):
            if price is None:
                raise BrokerError("Price required for %s orders" % order_type)
            data["price"] = self.round_tick(price)
        if order_type in ("SL", "SL-M"):
            if trigger_price is None:
                raise BrokerError("Trigger price required for %s orders" % order_type)
            data["trigger_price"] = self.round_tick(trigger_price)
        res = self._request("POST", "/orders/regular", data=data)
        return str(res["order_id"])

    def order_status(self, order_id: str) -> Dict:
        if order_id.startswith("PAPER-"):
            return dict(self._paper_orders.get(order_id, {"status": "UNKNOWN", "average_price": 0.0,
                                                         "filled_quantity": 0, "message": ""}))
        data = self._request("GET", "/orders/%s" % order_id)
        last = data[-1] if isinstance(data, list) and data else {}
        return {"status": last.get("status", "UNKNOWN"),
                "average_price": float(last.get("average_price") or 0.0),
                "filled_quantity": int(last.get("filled_quantity") or 0),
                "message": last.get("status_message") or ""}

    def cancel_order(self, order_id: str) -> None:
        if order_id.startswith("PAPER-"):
            if order_id in self._paper_orders:
                self._paper_orders[order_id]["status"] = "CANCELLED"
            return
        self._request("DELETE", "/orders/regular/%s" % order_id)

    def _wait_fill(self, order_id: str, fallback: float) -> float:
        status = {}
        for _ in range(8):
            status = self.order_status(order_id)
            if status["status"] == "COMPLETE":
                return status["average_price"] or fallback
            if status["status"] in ("REJECTED", "CANCELLED"):
                raise BrokerError("Order %s: %s" % (status["status"], status["message"]))
            time.sleep(0.6)
        try:
            self.cancel_order(order_id)
        except BrokerError:
            pass
        raise BrokerError("Entry order not filled (status %s) - cancel attempted" % status.get("status"))

    # ------------------------------------------------------------------ #
    # Trade lifecycle
    # ------------------------------------------------------------------ #
    def open_trade(self, exchange: str, symbol: str, direction: str, qty: int, sl: float, tp: float) -> Dict:
        direction = direction.upper()
        if direction not in (BUY, SELL):
            raise BrokerError("Invalid direction")
        with self._lock:
            if any(p["symbol"] == symbol and p["exchange"] == exchange for p in self.state["positions"]):
                raise BrokerError("Position already open in %s" % symbol)
        ltp = self.get_ltp(exchange, symbol)
        if direction == BUY and not (sl < ltp < tp):
            raise BrokerError("Setup invalid at LTP %.2f (need SL < LTP < TP)" % ltp)
        if direction == SELL and not (tp < ltp < sl):
            raise BrokerError("Setup invalid at LTP %.2f (need TP < LTP < SL)" % ltp)

        entry_oid = self.place_order(exchange, symbol, direction, qty, "MARKET")
        sl_oid = None
        if self.mode == "PAPER":
            entry = self._paper_orders[entry_oid]["average_price"]
        else:
            entry = self._wait_fill(entry_oid, ltp)
            try:
                sl_oid = self.place_order(exchange, symbol, _opposite(direction), qty, "SL-M",
                                          trigger_price=sl)
            except BrokerError as exc:
                try:  # never stay in a position without a protective stop
                    self.place_order(exchange, symbol, _opposite(direction), qty, "MARKET")
                except BrokerError as exc2:
                    raise BrokerError("SL order failed (%s) AND flatten failed (%s) - CLOSE MANUALLY"
                                      % (exc, exc2))
                raise BrokerError("SL order failed, position flattened: %s" % exc)

        pos = {"id": "T%d" % self._next_seq(), "exchange": exchange, "symbol": symbol,
               "side": direction, "qty": int(qty), "entry": round(entry, 2),
               "sl": self.round_tick(sl), "tp": self.round_tick(tp), "mode": self.mode,
               "entry_order_id": entry_oid, "sl_order_id": sl_oid,
               "opened_at": datetime.now().isoformat(timespec="seconds")}
        with self._lock:
            self.state["positions"].append(pos)
            self._save_state()
        return dict(pos)

    def _finalize(self, pos: Dict, exit_price: float, reason: str) -> Dict:
        qty, entry = pos["qty"], pos["entry"]
        pnl = (exit_price - entry) * qty if pos["side"] == BUY else (entry - exit_price) * qty
        trade = {"id": pos["id"], "symbol": pos["symbol"], "exchange": pos["exchange"],
                 "side": pos["side"], "qty": qty, "entry": entry, "exit": round(exit_price, 2),
                 "pnl": round(pnl, 2), "reason": reason, "mode": pos["mode"],
                 "opened_at": pos["opened_at"],
                 "closed_at": datetime.now().isoformat(timespec="seconds")}
        with self._lock:
            self.state["positions"] = [p for p in self.state["positions"] if p["id"] != pos["id"]]
            self.state["trades"].append(trade)
            self.state["trades"] = self.state["trades"][-500:]
            if pos["mode"] == "PAPER":
                self.state["paper_realised"] = round(self.state["paper_realised"] + pnl, 2)
            self._save_state()
        return trade

    def _market_exit(self, pos: Dict, fallback: float) -> float:
        if pos.get("sl_order_id"):
            try:
                self.cancel_order(pos["sl_order_id"])
            except BrokerError:
                pass
            try:  # the stop may have filled just before we cancelled
                st = self.order_status(pos["sl_order_id"])
                if st["status"] == "COMPLETE":
                    return st["average_price"] or fallback
            except BrokerError:
                pass
        oid = self.place_order(pos["exchange"], pos["symbol"], _opposite(pos["side"]), pos["qty"], "MARKET")
        if self.mode == "PAPER":
            return self._paper_orders[oid]["average_price"]
        return self._wait_fill(oid, fallback)

    def _evaluate_exit(self, pos: Dict, ltp: float) -> Optional[Dict]:
        buy = pos["side"] == BUY
        sl_hit = ltp <= pos["sl"] if buy else ltp >= pos["sl"]
        tp_hit = ltp >= pos["tp"] if buy else ltp <= pos["tp"]

        if pos["mode"] == "PAPER":
            if sl_hit:
                return self._finalize(pos, ltp, "STOP")
            if tp_hit:
                return self._finalize(pos, pos["tp"], "TARGET")
            return None

        # LIVE: check whether the exchange-side stop already executed
        if pos.get("sl_order_id"):
            st = self.order_status(pos["sl_order_id"])
            if st["status"] == "COMPLETE":
                return self._finalize(pos, st["average_price"] or pos["sl"], "STOP")
            if st["status"] in ("REJECTED", "CANCELLED") and not sl_hit and not tp_hit:
                try:  # protective stop vanished - re-arm it
                    new_id = self.place_order(pos["exchange"], pos["symbol"], _opposite(pos["side"]),
                                              pos["qty"], "SL-M", trigger_price=pos["sl"])
                    with self._lock:
                        for p in self.state["positions"]:
                            if p["id"] == pos["id"]:
                                p["sl_order_id"] = new_id
                        self._save_state()
                except BrokerError:
                    pass
        if tp_hit:
            return self._finalize(pos, self._market_exit(pos, pos["tp"]), "TARGET")
        if sl_hit:
            return self._finalize(pos, self._market_exit(pos, ltp), "STOP")
        return None

    def check_exits(self) -> List[Dict]:
        closed = []
        for pos in self.open_positions():
            try:
                ltp = self.get_ltp(pos["exchange"], pos["symbol"])
                result = self._evaluate_exit(pos, ltp)
            except BrokerError:
                continue
            if result:
                closed.append(result)
        return closed

    def close_position(self, pos_id: str, reason: str = "MANUAL") -> Dict:
        pos = next((p for p in self.open_positions() if p["id"] == pos_id), None)
        if pos is None:
            raise BrokerError("Position %s not found" % pos_id)
        ltp = self.get_ltp(pos["exchange"], pos["symbol"])
        return self._finalize(pos, self._market_exit(pos, ltp), reason)

    def close_all(self) -> List[Dict]:
        closed, errors = [], []
        for pos in self.open_positions():
            try:
                closed.append(self.close_position(pos["id"], "MANUAL"))
            except BrokerError as exc:
                errors.append("%s: %s" % (pos["symbol"], exc))
        if errors:
            raise BrokerError("Some exits failed - " + "; ".join(errors))
        return closed

    def account_summary(self) -> Dict:
        if self.mode == "PAPER":
            realised = self.state["paper_realised"]
            return {"mode": "PAPER", "equity": round(self.paper_capital + realised, 2), "realised": realised}
        try:
            m = self._request("GET", "/user/margins")
            eq = m["equity"]["available"]["live_balance"]
        except (BrokerError, KeyError, TypeError):
            eq = None
        return {"mode": "LIVE", "equity": eq, "realised": None}
