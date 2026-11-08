# -*- coding: utf-8 -*-
"""Tactical Neural Matrix - cyberpunk command center (Kivy)."""
import json
import os
import threading
import time
from collections import deque
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.graphics import Color, Line, RoundedRectangle
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

from agents_module import (BUY, SELL, MAX_RISK_PCT, MIN_AGREE_FLOOR, MIN_RR_FLOOR,
                           ConsensusEngine, RiskManager, position_size)
from broker_connector import BrokerError, KiteBroker

# ----------------------------- palette ------------------------------------ #
BG = (0.016, 0.016, 0.024, 1)        # deep obsidian
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
}


# ----------------------------- widgets ------------------------------------ #
def tlabel(text="", color=TEXT, font=13, bold=False, halign="left", valign="middle", **kw):
    lb = Label(text=text, color=color, font_size=sp(font), bold=bold, halign=halign, valign=valign, **kw)
    lb.bind(size=lambda inst, val: setattr(inst, "text_size", val))
    return lb


class NeonCard(BoxLayout):
    accent = ListProperty(CYAN)

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
    accent = ListProperty(CYAN)

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


# ----------------------------- voice -------------------------------------- #
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
            intent.putExtra(RecognizerIntent.EXTRA_PROMPT, "Say: scan, execute, status, close all")
            PythonActivity.mActivity.startActivityForResult(intent, self.REQUEST_CODE)
        except Exception as exc:
            on_error("Voice unavailable: %s" % exc)

    def _on_result(self, request_code, result_code, intent):
        if request_code != self.REQUEST_CODE:
            return
        if result_code != self.RESULT_OK or intent is None:
            Clock.schedule_once(lambda dt: self._on_error("Voice cancelled"))
            return
        try:
            from jnius import autoclass
            RecognizerIntent = autoclass("android.speech.RecognizerIntent")
            results = intent.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            text = str(results.get(0)) if results is not None and results.size() > 0 else ""
        except Exception as exc:
            msg = "Voice decode failed: %s" % exc
            Clock.schedule_once(lambda dt: self._on_error(msg))
            return
        if text:
            Clock.schedule_once(lambda dt: self._on_text(text))
        else:
            Clock.schedule_once(lambda dt: self._on_error("Nothing heard"))


def open_url(url):
    try:
        if platform == "android":
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
            PythonActivity.mActivity.startActivity(intent)
        else:
            import webbrowser
            webbrowser.open(url)
        return True
    except Exception:
        return False


# ----------------------------- application -------------------------------- #
class TacticalApp(App):
    title = "Tactical Neural Matrix"

    # ---- lifecycle ----
    def build(self):
        Window.clearcolor = BG
        Window.softinput_mode = "below_target"
        self.data_dir = self.user_data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.cfg = self._load_cfg()
        self.risk = RiskManager(os.path.join(self.data_dir, "risk_data.json"))
        self.engine = ConsensusEngine(self.risk, int(self.cfg["min_agree"]), float(self.cfg["min_rr"]))
        self.broker = KiteBroker(self.data_dir, mode=self.cfg["mode"], api_key=self.cfg["api_key"],
                                 api_secret=self.cfg["api_secret"], product=self.cfg["product"],
                                 paper_capital=float(self.cfg["capital"]),
                                 max_order_qty=int(self.cfg["max_qty"]))
        self.cfg["mode"] = self.broker.mode
        self.voice = VoiceInput()
        self.last_decision = None
        self.busy = False
        self.monitor_busy = False
        self.log_lines = deque(maxlen=40)
        root = self._build_ui()
        self._refresh_header()
        self._refresh_risk()
        self._refresh_positions()
        self.log("System online. Mode %s, data source %s." % (self.broker.mode, self.broker.data_source))
        Clock.schedule_interval(self._monitor_tick, 5)
        Clock.schedule_once(lambda dt: self._request_permissions(), 1.0)
        return root

    def _request_permissions(self):
        if platform != "android":
            return
        try:
            from android.permissions import Permission, request_permissions
            request_permissions([Permission.RECORD_AUDIO])
        except Exception as exc:
            self.log("Permission request failed: %s" % exc)

    # ---- config ----
    def _cfg_path(self):
        return os.path.join(self.data_dir, "config.json")

    def _load_cfg(self):
        cfg = dict(DEFAULT_CFG)
        try:
            with open(self._cfg_path(), "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        # enforce hard limits even if the file was edited by hand
        try:
            cfg["risk_pct"] = max(0.1, min(float(cfg["risk_pct"]), MAX_RISK_PCT))
            cfg["min_rr"] = max(float(cfg["min_rr"]), MIN_RR_FLOOR)
            cfg["min_agree"] = max(MIN_AGREE_FLOOR, min(11, int(cfg["min_agree"])))
            cfg["capital"] = max(1.0, float(cfg["capital"]))
            cfg["max_qty"] = max(1, int(cfg["max_qty"]))
        except (TypeError, ValueError):
            cfg = dict(DEFAULT_CFG)
        return cfg

    def _save_cfg(self):
        tmp = self._cfg_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
            os.replace(tmp, self._cfg_path())
        except OSError as exc:
            self.log("Config save failed: %s" % exc)

    # ---- UI construction ----
    def _build_ui(self):
        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        header = NeonCard(accent=CYAN, orientation="vertical", padding=dp(10), spacing=dp(2),
                          size_hint_y=None, height=dp(78))
        header.add_widget(tlabel("TACTICAL NEURAL MATRIX", color=CYAN, font=19, bold=True,
                                 size_hint_y=None, height=dp(30)))
        self.lbl_header = tlabel("", color=DIM, font=11)
        header.add_widget(self.lbl_header)
        root.add_widget(header)

        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        self.btn_scan = NeonButton(text="SCAN", accent=CYAN)
        self.btn_exec = NeonButton(text="EXECUTE", accent=GREEN)
        self.btn_mic = NeonButton(text="MIC", accent=AMBER)
        self.btn_cfg = NeonButton(text="CONFIG", accent=DIM)
        self.btn_scan.bind(on_release=lambda *a: self.scan())
        self.btn_exec.bind(on_release=lambda *a: self.execute())
        self.btn_mic.bind(on_release=lambda *a: self.start_voice())
        self.btn_cfg.bind(on_release=lambda *a: self.open_settings())
        for b in (self.btn_scan, self.btn_exec, self.btn_mic, self.btn_cfg):
            row.add_widget(b)
        root.add_widget(row)

        scroll = ScrollView(do_scroll_x=False, bar_width=dp(3), bar_color=CYAN)
        content = GridLayout(cols=1, spacing=dp(8), size_hint_y=None)
        content.bind(minimum_height=content.setter("height"))

        # consensus card
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

        # risk + positions card
        self.card_risk = NeonCard(accent=GREEN, orientation="vertical", padding=dp(12), spacing=dp(2),
                                  size_hint_y=None, height=dp(160))
        self.lbl_risk = tlabel("", color=TEXT, font=12, valign="top", size_hint_y=None, height=dp(72))
        self.lbl_pos = tlabel("", color=TEXT, font=11, valign="top")
        self.card_risk.add_widget(self.lbl_risk)
        self.card_risk.add_widget(self.lbl_pos)
        content.add_widget(self.card_risk)

        # agents
        grid = GridLayout(cols=2, spacing=dp(8), size_hint_y=None)
        grid.bind(minimum_height=grid.setter("height"))
        self.cards = []
        for i, name in enumerate(self.engine.agent_names, start=1):
            card = AgentCard(i, name)
            self.cards.append(card)
            grid.add_widget(card)
        content.add_widget(grid)

        # log
        self.card_log = NeonCard(accent=DIM, orientation="vertical", padding=dp(10),
                                 size_hint_y=None, height=dp(190))
        self.lbl_log = tlabel("", color=DIM, font=10, valign="top")
        self.card_log.add_widget(self.lbl_log)
        content.add_widget(self.card_log)

        scroll.add_widget(content)
        root.add_widget(scroll)

        bar = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.txt_cmd = TextInput(hint_text="command: scan / execute / status / close all",
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

    # ---- UI refreshers ----
    def log(self, msg):
        def _do(dt):
            self.log_lines.append("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg))
            self.lbl_log.text = "\n".join(list(self.log_lines)[-12:])
        Clock.schedule_once(_do)

    def _refresh_header(self):
        live = self.broker.mode == "LIVE"
        self.lbl_header.text = "MODE %s  |  DATA %s  |  %s:%s  %s" % (
            self.broker.mode, self.broker.data_source, self.cfg["exchange"],
            self.cfg["symbol"], self.cfg["interval"])
        self.lbl_header.color = RED if live else GREEN

    def _refresh_risk(self):
        st = self.risk.status()
        locked = st["locked"]
        self.card_risk.accent = list(RED if locked else GREEN)
        self.lbl_risk.text = (
            "HARD DAILY LOCKOUT: %s\nCONSECUTIVE LOSSES  %d / %d\nW/L  %d / %d    DAY P&L  %.2f%s" % (
                "ACTIVE - TRADING BLOCKED TODAY" if locked else "CLEAR",
                st["consecutive_losses"], st["limit"], st["wins"], st["losses"], st["daily_pnl"],
                ("\n" + st["reason"]) if locked and st["reason"] else ""))
        self.lbl_risk.color = RED if locked else TEXT

    def _refresh_positions(self):
        pos = self.broker.open_positions()
        if not pos:
            self.lbl_pos.text = "OPEN POSITIONS: none"
            return
        lines = ["OPEN POSITIONS:"]
        for p in pos:
            lines.append("%s %s x%d @ %.2f  SL %.2f  TP %.2f  [%s]" % (
                p["side"], p["symbol"], p["qty"], p["entry"], p["sl"], p["tp"], p["mode"]))
        self.lbl_pos.text = "\n".join(lines)

    def _apply_decision(self, d):
        self.last_decision = d
        for card, vote in zip(self.cards, d.votes):
            card.set_vote(vote)
        col = GREEN if d.direction == BUY else RED if d.direction == SELL else DIM
        self.card_cons.accent = list(col if d.approved else (AMBER if d.direction in (BUY, SELL) else DIM))
        self.lbl_dir.text = ("%s  APPROVED" % d.direction) if d.approved else (
            "%s  REJECTED" % d.direction if d.direction in (BUY, SELL) else "NO CONSENSUS")
        self.lbl_dir.color = col if d.approved else (AMBER if d.direction in (BUY, SELL) else DIM)
        self.lbl_agree.text = "%d / %d AGENTS AGREE   (NEED %d)" % (d.agree, d.total, self.engine.min_agree)
        self.lbl_levels.text = "ENTRY %.2f   SL %.2f   TP %.2f   RR 1:%.2f" % (d.entry, d.sl, d.tp, d.rr)
        self.lbl_reason.text = d.reason
        self.lbl_reason.color = TEXT
        self._refresh_header()
        self._refresh_risk()
        self.log("Scan %s: %s" % (d.symbol, d.reason))

    # ---- commands ----
    def _send_cmd(self):
        text = self.txt_cmd.text.strip()
        self.txt_cmd.text = ""
        if text:
            self.handle_command(text)

    def handle_command(self, text):
        t = text.lower().strip()
        self.log("CMD> %s" % t)
        if any(k in t for k in ("close all", "exit all", "square off", "squareoff")):
            self.close_all()
        elif any(k in t for k in ("execute", "place trade", "take trade", "enter trade")):
            self.execute()
        elif any(k in t for k in ("scan", "analyze", "analyse", "evaluate")):
            self.scan()
        elif "status" in t or "report" in t:
            self._status_report()
        elif "help" in t:
            self.log("Commands: scan | execute | status | close all")
        else:
            self.log("Unknown command. Say: scan, execute, status, close all")

    def start_voice(self):
        if platform != "android":
            self.log("Voice input works on Android only - type in the command box.")
            self.txt_cmd.focus = True
            return
        self.voice.listen(lambda text: self.handle_command(text), lambda err: self.log(err))

    def _status_report(self):
        st = self.risk.status()
        acct = self.broker.account_summary()
        eq = "n/a" if acct["equity"] is None else "%.2f" % acct["equity"]
        self.log("Status: %s | equity %s | losses %d/%d | locked %s | open %d" % (
            self.broker.mode, eq, st["consecutive_losses"], st["limit"], st["locked"],
            len(self.broker.open_positions())))

    # ---- scan ----
    def scan(self):
        if self.busy:
            self.log("Engine busy - wait for current operation.")
            return
        self.busy = True
        sym, exch, itv = self.cfg["symbol"], self.cfg["exchange"], self.cfg["interval"]
        self.log("Scanning %s:%s %s ..." % (exch, sym, itv))
        threading.Thread(target=self._scan_worker, args=(exch, sym, itv), daemon=True).start()

    def _scan_worker(self, exch, sym, itv):
        try:
            candles = self.broker.get_candles(exch, sym, itv, 200)
            decision = self.engine.evaluate(sym, candles)
            Clock.schedule_once(lambda dt: self._apply_decision(decision))
        except Exception as exc:
            self.log("Scan failed: %s" % exc)
        finally:
            self.busy = False

    # ---- execute ----
    def execute(self):
        d = self.last_decision
        if d is None or not d.approved:
            self.log("No approved setup. Run SCAN first.")
            return
        if self.risk.is_locked():
            self.log("BLOCKED: hard daily lockout is active.")
            self._refresh_risk()
            return
        if time.time() - d.ts > 120:
            self.log("Signal is stale (>2 min). Rescan first.")
            return
        qty = position_size(float(self.cfg["capital"]), float(self.cfg["risk_pct"]), d.entry, d.sl,
                            int(self.cfg["max_qty"]))
        if qty < 1:
            self.log("Position size computed as 0 - check capital / risk % / stop distance.")
            return
        if self.broker.mode == "LIVE":
            self._confirm_live(d, qty)
        else:
            self._do_execute(d, qty)

    def _confirm_live(self, d, qty):
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        box.add_widget(tlabel(
            "LIVE ORDER - REAL MONEY\n\n%s %s x%d\nEntry ~%.2f   SL %.2f   TP %.2f\nRR 1:%.2f\n\nPlace this order?" % (
                d.direction, d.symbol, qty, d.entry, d.sl, d.tp, d.rr), color=TEXT, font=14))
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        yes = NeonButton(text="YES - EXECUTE", accent=RED)
        no = NeonButton(text="ABORT", accent=DIM)
        row.add_widget(yes)
        row.add_widget(no)
        box.add_widget(row)
        pop = Popup(title="CONFIRM LIVE TRADE", content=box, size_hint=(0.9, 0.5), auto_dismiss=False,
                    background="", background_color=BG, title_color=RED, separator_color=RED)
        yes.bind(on_release=lambda *a: (pop.dismiss(), self._do_execute(d, qty)))
        no.bind(on_release=lambda *a: (pop.dismiss(), self.log("Live order aborted.")))
        pop.open()

    def _do_execute(self, d, qty):
        if self.busy:
            self.log("Engine busy.")
            return
        self.busy = True
        self.last_decision = None  # a signal can only be executed once
        self.log("Submitting %s %s x%d ..." % (d.direction, d.symbol, qty))
        threading.Thread(target=self._execute_worker, args=(d, qty), daemon=True).start()

    def _execute_worker(self, d, qty):
        try:
            pos = self.broker.open_trade(self.cfg["exchange"], d.symbol, d.direction, qty, d.sl, d.tp)
            self.log("OPENED %s %s x%d @ %.2f  SL %.2f  TP %.2f [%s]" % (
                pos["side"], pos["symbol"], pos["qty"], pos["entry"], pos["sl"], pos["tp"], pos["mode"]))
        except BrokerError as exc:
            self.log("Order failed: %s" % exc)
        except Exception as exc:
            self.log("Unexpected error: %s" % exc)
        finally:
            self.busy = False
            Clock.schedule_once(lambda dt: self._refresh_positions())

    # ---- monitoring / closing ----
    def _monitor_tick(self, dt):
        if self.monitor_busy or not self.broker.open_positions():
            return
        self.monitor_busy = True
        threading.Thread(target=self._monitor_worker, daemon=True).start()

    def _monitor_worker(self):
        try:
            closed = self.broker.check_exits()
            self._register_closed(closed)
        except Exception as exc:
            self.log("Monitor error: %s" % exc)
        finally:
            self.monitor_busy = False

    def _register_closed(self, closed):
        for t in closed:
            self.risk.record_trade_result(t["pnl"], t["symbol"])
            self.log("CLOSED %s %s %s: exit %.2f  P&L %.2f" % (
                t["reason"], t["side"], t["symbol"], t["exit"], t["pnl"]))
        if closed:
            Clock.schedule_once(lambda dt: (self._refresh_positions(), self._refresh_risk()))
            if self.risk.is_locked():
                self.log("HARD DAILY LOCKOUT ENGAGED - new trades blocked until tomorrow.")

    def close_all(self):
        if not self.broker.open_positions():
            self.log("No open positions.")
            return
        if self.busy:
            self.log("Engine busy.")
            return
        self.busy = True
        threading.Thread(target=self._close_all_worker, daemon=True).start()

    def _close_all_worker(self):
        try:
            closed = self.broker.close_all()
            self._register_closed(closed)
        except BrokerError as exc:
            self.log("Close all: %s" % exc)
        finally:
            self.busy = False
            Clock.schedule_once(lambda dt: (self._refresh_positions(), self._refresh_risk()))

    # ---- settings popup ----
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

        add_field("api_key", "API KEY", c["api_key"])
        add_field("api_secret", "API SECRET", c["api_secret"], password=True)
        add_field("request_token", "TOKEN / REDIRECT URL", "")
        add_field("exchange", "EXCHANGE", c["exchange"])
        add_field("symbol", "SYMBOL", c["symbol"])
        grid.add_widget(tlabel("INTERVAL", color=DIM, font=11))
        self._sp_interval = Spinner(text=c["interval"],
                                    values=("minute", "3minute", "5minute", "10minute", "15minute",
                                            "30minute", "60minute", "day"),
                                    background_normal="", background_color=PANEL_HI, color=CYAN)
        grid.add_widget(self._sp_interval)
        grid.add_widget(tlabel("MODE", color=DIM, font=11))
        self._sp_mode = Spinner(text=self.broker.mode, values=("PAPER", "LIVE"),
                                background_normal="", background_color=PANEL_HI, color=CYAN)
        grid.add_widget(self._sp_mode)
        grid.add_widget(tlabel("PRODUCT", color=DIM, font=11))
        self._sp_product = Spinner(text=c["product"], values=("MIS", "CNC", "NRML"),
                                   background_normal="", background_color=PANEL_HI, color=CYAN)
        grid.add_widget(self._sp_product)
        add_field("capital", "CAPITAL", c["capital"])
        add_field("risk_pct", "RISK % / TRADE (MAX 2)", c["risk_pct"])
        add_field("min_rr", "MIN RISK:REWARD (>=2)", c["min_rr"])
        add_field("min_agree", "MIN AGENTS (6-11)", c["min_agree"])
        add_field("max_qty", "MAX QTY / ORDER", c["max_qty"])

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
        r3 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        r3.add_widget(mk("SAVE", GREEN, lambda *a: self._save_settings()))
        r3.add_widget(mk("CLOSE", DIM, lambda *a: self._settings_pop.dismiss()))

        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(6))
        box.add_widget(sv)
        box.add_widget(r1)
        box.add_widget(r2)
        box.add_widget(r3)
        self._settings_pop = Popup(title="CONFIG", content=box, size_hint=(0.96, 0.94),
                                   auto_dismiss=False, background="", background_color=BG,
                                   title_color=CYAN, separator_color=CYAN)
        self._settings_pop.open()

    def _read_creds(self):
        self.broker.update_credentials(self._fields["api_key"].text, self._fields["api_secret"].text)

    def _open_login(self):
        try:
            self._read_creds()
            url = self.broker.login_url()
        except BrokerError as exc:
            self.log(str(exc))
            return
        if not open_url(url):
            Clipboard.copy(url)
            self.log("Could not open browser; login URL copied to clipboard.")

    def _copy_login(self):
        try:
            self._read_creds()
            Clipboard.copy(self.broker.login_url())
            self.log("Login URL copied. Open it, log in, then copy the redirect URL and tap LOGIN.")
        except BrokerError as exc:
            self.log(str(exc))

    def _do_login(self):
        try:
            self._read_creds()
        except BrokerError as exc:
            self.log(str(exc))
            return
        # token field accepts a bare token or the full redirect URL; falls back to the clipboard
        token = self._fields["request_token"].text.strip() or (Clipboard.paste() or "").strip()
        self.log("Logging in to Kite ...")

        def work():
            try:
                uid = self.broker.login(token)
                self.log("Kite session active (%s)." % uid)
            except BrokerError as exc:
                self.log(str(exc))
            Clock.schedule_once(lambda dt: self._refresh_header())

        threading.Thread(target=work, daemon=True).start()

    def _use_demo(self):
        if self.broker.open_positions():
            self.log("Close open positions first.")
            return
        self.broker.clear_session()
        try:
            self.broker.set_mode("PAPER")
        except BrokerError as exc:
            self.log(str(exc))
        self.cfg["mode"] = self.broker.mode
        self._sp_mode.text = "PAPER"
        self._refresh_header()
        self.log("Kite session cleared. PAPER mode on DEMO data.")

    def _save_settings(self):
        f = self._fields
        try:
            new = {
                "api_key": f["api_key"].text.strip(), "api_secret": f["api_secret"].text.strip(),
                "exchange": f["exchange"].text.strip().upper(), "symbol": f["symbol"].text.strip().upper(),
                "interval": self._sp_interval.text, "product": self._sp_product.text,
                "capital": float(f["capital"].text), "risk_pct": float(f["risk_pct"].text),
                "min_rr": float(f["min_rr"].text), "min_agree": int(f["min_agree"].text),
                "max_qty": int(f["max_qty"].text),
            }
            if not new["symbol"] or not new["exchange"]:
                raise ValueError("symbol/exchange empty")
            if new["capital"] <= 0 or not (0 < new["risk_pct"] <= MAX_RISK_PCT) \
                    or new["min_rr"] < MIN_RR_FLOOR \
                    or not (MIN_AGREE_FLOOR <= new["min_agree"] <= 11) or new["max_qty"] < 1:
                raise ValueError("risk %% must be 0-%.0f, RR >= %.0f, agents %d-11" % (
                    MAX_RISK_PCT, MIN_RR_FLOOR, MIN_AGREE_FLOOR))
        except ValueError as exc:
            self.log("Invalid settings: %s" % exc)
            return
        try:
            self.broker.update_credentials(new["api_key"], new["api_secret"])
            self.broker.set_mode(self._sp_mode.text)
        except BrokerError as exc:
            self.log("Mode not changed: %s" % exc)
        new["mode"] = self.broker.mode
        self.cfg.update(new)
        self.broker.configure(new["product"], new["max_qty"], new["capital"])
        self.engine.configure(new["min_agree"], new["min_rr"])
        self._save_cfg()
        self._refresh_header()
        self.log("Settings saved. Mode %s, %s:%s %s." % (
            self.broker.mode, new["exchange"], new["symbol"], new["interval"]))
        self._settings_pop.dismiss()


if __name__ == "__main__":
    TacticalApp().run()
