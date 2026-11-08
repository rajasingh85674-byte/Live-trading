# -*- coding: utf-8 -*-
"""Tactical Neural Matrix - cyberpunk command center (Kivy) with Voice Alerts."""
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
from voice_alerts import DEFAULT_VOICE_ID, VoiceAlerts

# --- palette ---
BG = (0.016, 0.015, 0.034, 1)  # deep obsidian
PANEL = (0.047, 0.055, 0.075, 1)
PANEL_HI = (0.07, 0.08, 0.12, 1)
CYAN = (0.0, 0.95, 1.0, 1)
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
}

def tlabel(text="", color=TEXT, font=13, bold=False, halign="left", valign="middle"):
    lb = Label(text=text, color=color, font_size=sp(font), bold=bold,
               halign=halign, valign=valign)
    lb.bind(size=lambda inst, val: setattr(inst, 'text_size', val))
    return lb

class NeonCard(BoxLayout):
    accent = ListProperty(CYAN)

    def __init__(self, accent=CYAN, **kw):
        super().__init__(**kw)
        self.accent = list(accent)
        with self.canvas.before:
            Color(*PANEL)
            self._bg = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(8)])
            Color(*self.accent)
            self._ic = Color(*self.accent)
            self._ln = Line(rounded_rectangle=(self.x, self.y, self.width, self.height, dp(8)),
                            width=dp(1.2))
        self.bind(pos=self._redraw, size=self._redraw)

    def _redraw(self, *args):
        self._bg.pos = self.pos
        self._bg.size = self.size
        self._ic.rgba = self.accent
        self._ln.rounded_rectangle = (self.x, self.y, self.width, self.height, dp(8))

class NeonButton(Button):
    accent = ListProperty(CYAN)

    def __init__(self, accent=CYAN, **kw):
        kw.setdefault('background_normal', '')
        kw.setdefault('background_down', '')
        kw.setdefault('background_color', (0, 0, 0, 0))
        kw.setdefault('bold', True)
        kw.setdefault('font_size', sp(13))
        super().__init__(**kw)
        self.accent = list(accent)
        with self.canvas.before:
            Color(*PANEL_HI)
            self._fc = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(6)])
            Color(*self.accent)
            self._lc = Color(*self.accent)
            self._ln = Line(rounded_rectangle=(self.x, self.y, self.width, self.height, dp(6)),
                            width=dp(1.3))
        self.bind(pos=self._redraw, size=self._redraw)

    def _redraw(self, *args):
        acc = self.accent
        self._fc.pos = self.pos
        self._fc.size = self.size
        self._lc.rgba = (acc[0] * 0.35, acc[1] * 0.35, acc[2] * 0.35, 1)
        self._ln.rounded_rectangle = (self.x, self.y, self.width, self.height, dp(6))

class AgentCard(NeonCard):
    def __init__(self, index, name, **kw):
        super().__init__(accent=DIM, orientation="vertical", padding=dp(8), spacing=dp(4), **kw)
        self.title = tlabel(f"{index:02d} {name}", color=DIM, font=11, bold=True)
        self.signal = tlabel("STANDBY", color=DIM, font=18, bold=True, halign="center")
        self.conf = tlabel("", color=TEXT, font=10, halign="center")
        self.reason = tlabel("waiting scan", color=DIM, font=10, valign="top")
        for w in (self.title, self.signal, self.conf, self.reason):
            self.add_widget(w)

    def set_vote(self, vote):
        if vote.vote:
            col, txt = AMBER, "LOCKED" if vote.locked else "STANDBY"
            if vote.signal == BUY: col, txt = GREEN, "BUY"
            elif vote.signal == SELL: col, txt = RED, "SELL"
        else:
            col, txt = DIM, "NEUTRAL"
        self.accent = list(col)
        self.signal.text = txt
        self.signal.color = col
        self.title.color = col
        self.conf.text = f"CONF {int(vote.confidence * 100)}%" if vote.vote else ""
        self.reason.text = vote.reason

class VoiceInput:
    REQUEST_CODE = 3131
    RESULT_OK = -1

    def __init__(self):
        self.bound = False
        self.on_text = None
        self.on_error = None

    def listen(self, on_text, on_error):
        if platform != "android":
            on_error("Voice input works on Android only - use the command box")
            return
        try:
            from jnius import autoclass
            from android import python_activity
            Intent = autoclass('android.content.Intent')
            RecognizerIntent = autoclass('android.speech.RecognizerIntent')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                            RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            intent.putExtra(RecognizerIntent.EXTRA_PROMPT, "Speak tactical command...")
            PythonActivity.mActivity.startActivityForResult(intent, self.REQUEST_CODE)
            self.on_text = on_text
            self.on_error = on_error
            self.bound = True
            from jnius import autoclass
            Activity = autoclass('org.kivy.android.PythonActivity')
            Activity.mActivity.registerOnActivityResultListener(self._on_result)
        except Exception as ex:
            on_error(str(ex))

    def _on_result(self, request_code, result_code, intent):
        if request_code == self.REQUEST_CODE:
            self.bound = False
            if result_code == self.RESULT_OK and intent is not None:
                try:
                    from android import python_activity
                    results = intent.getStringArrayListExtra(
                        autoclass('android.speech.RecognizerIntent').EXTRA_RESULTS)
                    if results and results.size() > 0:
                        text = results.get(0)
                        if self.on_text:
                            self.on_text(text)
                except Exception as ex:
                    if self.on_error:
                        self.on_error(str(ex))
            return True
        return False

class TacticalApp(App):
    title = "Tactical Neural Matrix"

    def build(self):
        Window.clearcolor = BG
        Window.softinput_mode = "below_target"
        self.self_data_dir = self.user_data_dir
        os.makedirs(self.self_data_dir, exist_ok=True)
        self.cfg = self._load_cfg()
        self.risk = RiskManager(os.path.join(self.self_data_dir, "risk_data.json"))
        self.engine = ConsensusEngine(self.risk, int(self.cfg["min_agree"]))
        self.broker = KiteBroker(
            api_key=self.cfg["api_key"],
            api_secret=self.cfg["api_secret"],
            prod=self.cfg["node"] == "LIVE",
            paper_capital=float(self.cfg["capital"]),
            max_qty=int(self.cfg["max_qty"])
        )
        self.broker.set_mode(self.cfg["node"])
        
        # Initialize Voice TTS
        self.tts = VoiceAlerts(
            os.path.join(self.self_data_dir, "tts_cache"),
            self.cfg["eleven_key"],
            self.cfg["voice_id"],
            self.cfg["voice_alerts"]
        )
        
        self.voice = VoiceInput()
        self.last_decision = None
        self.busy = False
        self.monitor_busy = False
        self.log_lines = deque(maxlen=40)
        root = self._build_ui()
        self._refresh_header()
        self._refresh_risk()
        self._refresh_positions()
        self.log(f"System online. Mode {self.cfg['node']}, data source {self.cfg['exchange']}.")
        Clock.schedule_interval(lambda dt: self._monitor_tick(dt), 5)
        Clock.schedule_once(lambda dt: self._request_permissions(), 1.0)
        return root

    def _request_permissions(self):
        if platform != "android":
            return
        try:
            from android.permissions import Permission, request_permissions
            request_permissions([Permission.RECORD_AUDIO])
        except Exception as ex:
            self.log(f"Permission request failed: {ex}")

    def _cfg_path(self):
        return os.path.join(self.self_data_dir, "config.json")

    def _load_cfg(self):
        cfg = dict(DEFAULT_CFG)
        try:
            with open(self._cfg_path(), "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        try:
            cfg["risk_pct"] = max(0.1, min(float(cfg["risk_pct"]), MAX_RISK_PCT))
            cfg["min_rr"] = max(1.0, float(cfg["min_rr"]))
            cfg["min_agree"] = max(MIN_AGREE_FLOOR, min(11, int(cfg["min_agree"])))
            cfg["max_qty"] = max(1, int(cfg["max_qty"]))
            cfg["eleven_key"] = str(cfg.get("eleven_key") or "").strip()
            cfg["voice_id"] = str(cfg.get("voice_id") or DEFAULT_VOICE_ID).strip()
            cfg["voice_alerts"] = bool(cfg.get("voice_alerts", True))
        except (TypeError, ValueError):
            cfg = dict(DEFAULT_CFG)
        return cfg

    def _save_cfg(self):
        tmp = self._cfg_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
            os.replace(tmp, self._cfg_path())
        except OSError as ex:
            self.log(f"Config save failed: {ex}")

    def _build_ui(self):
        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
        
        header = NeonCard(accent=CYAN, orientation="vertical", padding=dp(8),
                          size_hint_y=None, height=dp(70))
        header.add_widget(tlabel("TACTICAL NEURAL MATRIX", color=CYAN, font=16, bold=True))
        self.lbl_header = tlabel("", color=DIM, font=11)
        header.add_widget(self.lbl_header)
        root.add_widget(header)

        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        self.btn_scan = NeonButton(text="SCAN", accent=CYAN)
        self.btn_exec = NeonButton(text="EXECUTE", accent=GREEN)
        self.btn_mic = NeonButton(text="MIC", accent=AMBER)
        self.btn_cfg = NeonButton(text="CONFIG", accent=DIM)
        self.btn_scan.bind(on_release=lambda a: self.scan())
        self.btn_exec.bind(on_release=lambda a: self.execute())
        self.btn_mic.bind(on_release=lambda a: self.start_voice())
        self.btn_cfg.bind(on_release=lambda a: self.open_settings())
        for b in (self.btn_scan, self.btn_exec, self.btn_mic, self.btn_cfg):
            row.add_widget(b)
        root.add_widget(row)

        scroll = ScrollView(do_scroll_x=False, bar_width=dp(3), bar_color=CYAN)
        content = GridLayout(cols=1, spacing=dp(8), size_hint_y=None)
        content.bind(minimum_height=content.setter("height"))

        self.card_cons = NeonCard(accent=DIM, orientation="vertical", padding=dp(10),
                                  size_hint_y=None, height=dp(150))
        self.lbl_dir = tlabel("NO SIGNAL", color=DIM, font=24, bold=True)
        self.lbl_agree = tlabel("0 / 11 AGENTS AGREE (need 6)", color=DIM, font=13)
        self.lbl_levels = tlabel("ENTRY - SL - TP - RR -", color=TEXT, font=12)
        self.lbl_reason = tlabel("Run a scan to evaluate the matrix.", color=DIM, font=10, valign="top")
        for m in (self.lbl_dir, self.lbl_agree, self.lbl_levels, self.lbl_reason):
            self.card_cons.add_widget(m)
        content.add_widget(self.card_cons)

        self.card_risk = NeonCard(accent=GREEN, orientation="vertical", padding=dp(10),
                                  size_hint_y=None, height=dp(160))
        self.lbl_risk = tlabel("", color=TEXT, font=12, valign="top", size_hint_y=None, height=dp(60))
        self.lbl_pos = tlabel("", color=TEXT, font=11, valign="top")
        self.card_risk.add_widget(self.lbl_risk)
        self.card_risk.add_widget(self.lbl_pos)
        content.add_widget(self.card_risk)

        grid = GridLayout(cols=2, spacing=dp(8), size_hint_y=None)
        grid.bind(minimum_height=grid.setter("height"))
        self.cards = []
        for i, name in enumerate(self.engine.agent_names, start=1):
            card = AgentCard(i, name)
            self.cards.append(card)
            grid.add_widget(card)
        content.add_widget(grid)

        self.card_log = NeonCard(accent=DIM, orientation="vertical", padding=dp(10),
                                 size_hint_y=None, height=dp(150))
        self.lbl_log = tlabel("", color=TEXT, font=10, valign="top")
        self.card_log.add_widget(self.lbl_log)
        content.add_widget(self.card_log)

        scroll.add_widget(content)
        root.add_widget(scroll)

        bar = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.txt_cmd = TextInput(hint_text="command: scan / execute / status / close all",
                                 multiline=False, background_color=PANEL, foreground_color=TEXT,
                                 hint_text_color=DIM, cursor_color=CYAN, font_size=sp(13))
        self.txt_cmd.bind(on_text_validate=lambda a: self.send_cmd())
        btn_send = NeonButton(text="SEND", accent=CYAN, size_hint_x=None, width=dp(80))
        btn_send.bind(on_release=lambda a: self.send_cmd())
        bar.add_widget(self.txt_cmd)
        bar.add_widget(btn_send)
        root.add_widget(bar)
        return root

    def log(self, msg):
        self.log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        self.lbl_log.text = "\n".join(list(self.log_lines)[-12:])
        Clock.schedule_once(lambda dt: None)

    def _refresh_header(self):
        live = self.broker.mode == "LIVE"
        self.lbl_header.text = f"MODE: {self.broker.mode} | DATA: {self.broker.data_source} | {self.cfg['exchange']}:{self.cfg['symbol']} | {self.cfg['interval']}"
        self.lbl_header.color = RED if live else GREEN

    def _refresh_risk(self):
        st = self.risk.status()
        self.card_risk.accent = list(RED if st["locked"] else GREEN)
        self.lbl_risk.text = (
            f"HARD DAILY LOCKOUT: {st['consecutive_losses']} / {st['max_loss']} losses\n"
            f"STATUS: {'TRADING BLOCKED TODAY' if st['locked'] else 'CLEAR'} "
            f"(losses: {st['consecutive_losses']}, limit: {st['max_loss']})\n"
            f"REASON: {st['reason'] if st['locked'] and st['reason'] else 'No restrictions active.'}"
        )
        self.lbl_risk.color = RED if st['locked'] else TEXT

    def _refresh_positions(self):
        pos = self.broker.open_positions()
        if not pos:
            self.lbl_pos.text = "OPEN POSITIONS: none"
            return
        lines = ["OPEN POSITIONS:"]
        for p in pos:
            lines.append(f"{p['side']} {p['symbol']} x{p['qty']} @ {p['entry']:.2f} SL {p['sl']:.2f} TP {p['tp']:.2f} [PnL] ({p.get('pnl', 0.0):.2f})")
        self.lbl_pos.text = "\n".join(lines)

    def _apply_decision(self, d):
        self.last_decision = d
        for card, vote in zip(self.cards, d.votes):
            card.set_vote(vote)
        col = GREEN if d.direction == BUY else RED if d.direction == SELL else DIM
        self.card_cons.accent = list(col)
        self.lbl_dir.text = f"{d.direction} ({'APPROVED' if d.approved else 'REJECTED'})"
        self.lbl_dir.color = col
        self.lbl_agree.text = f"{d.agree_count} / 11 AGENTS AGREE (need 6)"
        self.lbl_levels.text = f"ENTRY {d.entry:.2f}  SL {d.sl:.2f}  TP {d.tp:.2f}  RR {d.rr:.1f}"
        self.lbl_reason.text = d.reason
        self.lbl_reason.color = TEXT
        self._refresh_header()
        self._refresh_risk()
        self.log(f"Scan: {d.direction} on {d.symbol} ({d.reason})")
        
        if d.approved:
            self.tts.speak("%s setup approved on %s. %d of %d agents agree. Risk reward 1 to %.1f." % (
                d.direction.title(), d.symbol, d.agree_count, 11, d.rr))

    def send_cmd(self):
        text = self.txt_cmd.text.strip()
        self.txt_cmd.text = ""
        if text:
            self.handle_command(text)

    def handle_command(self, text):
        t = text.lower().strip()
        self.log(f"CMD > {t}")
        if any(k in t for k in ("close all", "exit all", "square off")):
            self.close_all()
        elif any(k in t for k in ("execute", "place trade", "take trade")):
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
            self.log("Voice input works on Android only - type in the command box")
            return
        self.voice.listen(lambda text: self.handle_command(text), lambda err: self.log(f"Voice error: {err}"))

    def _status_report(self):
        st = self.risk.status()
        acct = self.broker.account_summary()
        eq = "n/a" if acct["equity"] is None else f"K {acct['equity']:.2f}"
        self.log(f"Status: eq {eq} | losses {st['consecutive_losses']} | locked {st['locked']} | op {len(self.broker.open_positions())}")
        self.tts.speak("Status. %s mode. %d consecutive losses. %d open positions. Lockout %s." % (
            self.broker.mode.title(), st["consecutive_losses"],
            len(self.broker.open_positions()), "active" if st["locked"] else "clear"))

    def scan(self):
        if self.busy:
            self.log("Engine busy - wait for current operation.")
            return
        self.busy = True
        sym, exch, iv = self.cfg["symbol"], self.cfg["exchange"], self.cfg["interval"]
        self.log(f"Scanning {sym} on {exch}, {iv}...")
        threading.Thread(target=self._scan_worker, args=(exch, sym, iv)).start()

    def _scan_worker(self, exch, sym, iv):
        try:
            candles = self.broker.get_candles(exch, sym, iv, 200)
            decision = self.engine.evaluate(sym, candles)
            Clock.schedule_once(lambda dt: self._apply_decision(decision))
        except Exception as ex:
            self.log(f"Scan failed: {ex}")
        finally:
            self.busy = False

    def execute(self):
        d = self.last_decision
        if d is None or not d.approved:
            self.log("No approved setup. Run SCAN first.")
            return
        if self.risk.is_locked():
            self.log("LOCKED: hard daily lockout is active.")
            self._refresh_risk()
            return
        if time.time() - d.ts > 120:
            self.log("Signal is stale (>2 min). Rescan first.")
            return
        qty = position_size(float(self.cfg["capital"]), float(self.cfg["risk_pct"]), d.entry, d.sl, int(self.cfg["max_qty"]))
        if qty < 1:
            self.log("Position size computed as 0 - check capital / risk")
            return
        if self.broker.mode == "LIVE":
            self._confirm_live(d, qty)
        else:
            self._do_execute(d, qty)

    def _confirm_live(self, d, qty):
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        box.add_widget(tlabel(f"LIVE ORDER - REAL MONEY!\n\n{d.direction} x{qty} @ {d.entry:.2f}  SL {d.sl:.2f}  TP {d.tp:.2f}\nDirection: {d.direction}, Symbol: {d.symbol}, Qty: {qty}", color=RED, font=13))
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        yes = NeonButton(text="YES - EXECUTE", accent=RED)
        no = NeonButton(text="ABORT", accent=DIM)
        row.add_widget(yes)
        row.add_widget(no)
        box.add_widget(row)
        pop = Popup(title="CONFIRM LIVE TRADE", content=box, size_hint=(0.85, 0.45),
                    auto_dismiss=False, background="", background_color=PANEL)
        yes.bind(on_release=lambda a: (pop.dismiss(), self._do_execute(d, qty)))
        no.bind(on_release=lambda a: (pop.dismiss(), self.log("Live order spent.")))
        pop.open()

    def _do_execute(self, d, qty):
        if self.busy:
            self.log("Engine busy")
            return
        self.busy = True
        self.last_decision = None
        self.log(f"Submitting {d.direction} {d.symbol} x{qty}...")
        threading.Thread(target=self._execute_worker, args=(d, qty)).start()

    def _execute_worker(self, d, qty):
        try:
            pos = self.broker.open_trade(self.cfg["exchange"], d.symbol, d.direction, qty, d.entry, d.sl, d.tp)
            self.log(f"OPENED {pos['side']} {pos['symbol']} @ {pos['entry']:.2f} SL {pos['sl']:.2f} TP {pos['tp']:.2f} [Live]")
            self.tts.speak("%s order filled on %s. Stop loss and target are set." % (
                pos["side"].title(), pos["symbol"]))
            self._register_closed([p for p in [pos] if p.get('closed')])
        except BrokerError as ex:
            self.log(f"Order failed: {ex}")
            self.tts.speak("Order failed.", priority=True)
        except Exception as ex:
            self.log(f"Unexpected error: {ex}")
        finally:
            self.busy = False
            Clock.schedule_once(lambda dt: self._refresh_positions())

    def _monitor_tick(self, dt):
        if self.monitor_busy or not self.broker.open_positions():
            return
        self.monitor_busy = True
        threading.Thread(target=self._monitor_worker, daemon=True).start()

    def _monitor_worker(self):
        try:
            closed = self.broker.check_exits()
            if closed:
                self._register_closed(closed)
        except Exception as ex:
            self.log(f"Monitor error: {ex}")
        finally:
            self.monitor_busy = False

    def _register_closed(self, closed):
        for t in closed:
            self.risk.record_trade_result(t['pnl'], t['symbol'])
            self.log(f"CLOSED {t['side']} {t['symbol']} P&L {t['pnl']:.2f} ({t['reason']})")
            self.tts.speak("%s closed by %s. %s of %.0f rupees." % (
                t["symbol"], t["reason"].lower(), "Profit" if t["pnl"] >= 0 else "Loss", abs(t["pnl"])))
        if closed:
            Clock.schedule_once(lambda dt: (self._refresh_positions(), self._refresh_risk()))
            if self.risk.is_locked():
                self.log("HARD DAILY LOCKOUT ENGAGED - new trades blocked.")
                self.tts.speak("Hard daily lockout engaged. Trading is blocked until tomorrow.", priority=True)

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
        except BrokerError as ex:
            self.log(f"Close all: {ex}")
        finally:
            self.busy = False
            Clock.schedule_once(lambda dt: self._refresh_positions())

    def open_settings(self):
        c = dict(self.cfg)
        self._fields = {}
        grid = GridLayout(cols=2, spacing=dp(6), padding=dp(6), size_hint_y=None)
        grid.bind(minimum_height=grid.setter("height"))

        def add_field(key, label, password=False):
            grid.add_widget(tlabel(label, color=DIM, font=11))
            ti = TextInput(text=str(c[key]), multiline=False, password=password,
                           background_color=PANEL_HI, foreground_color=TEXT,
                           hint_text_color=DIM, cursor_color=CYAN, font_size=sp(13))
            grid.add_widget(ti)
            self._fields[key] = ti

        add_field("api_key", "API KEY", True)
        add_field("api_secret", "API SECRET", True)
        add_field("request_token", "TOKEN / REDIRECT URL", "")
        add_field("exchange", "EXCHANGE", c["exchange"])
        add_field("symbol", "SYMBOL", c["symbol"])

        grid.add_widget(tlabel("INTERVAL", color=DIM, font=11))
        self.sp_interval = Spinner(text=c["interval"],
                                     values=("minute", "3minute", "5minute",
                                             "15minute", "30minute", "60minute", "day"),
                                     background_normal="", background_color=PANEL_HI)
        grid.add_widget(self.sp_interval)

        grid.add_widget(tlabel("MODE", color=DIM, font=11))
        self.sp_node = Spinner(text=c["node"], values=("PAPER", "LIVE"),
                                background_normal="", background_color=PANEL_HI)
        grid.add_widget(self.sp_node)

        grid.add_widget(tlabel("PRODUCT", color=DIM, font=11))
        self.sp_product = Spinner(text=c["product"], values=("MIS", "CNC"),
                                  background_normal="", background_color=PANEL_HI)
        grid.add_widget(self.sp_product)

        add_field("capital", "CAPITAL", str(c["capital"]))
        add_field("risk_pct", "RISK % / TRADE (MAX 2)", str(c["risk_pct"]))
        add_field("min_rr", "MIN RR (MINWARD)", str(c["min_rr"]))
        add_field("min_agree", "MIN AGENTS (6-11)", str(c["min_agree"]))
        add_field("max_qty", "MAX QTY / ORDER", str(c["max_qty"]))

        add_field("eleven_key", "ELEVENLABS API KEY", True)
        add_field("voice_id", "VOICE ID", c["voice_id"])
        grid.add_widget(tlabel("VOICE ALERTS", color=DIM, font=11))
        self._sp_voice = Spinner(text="ON" if c["voice_alerts"] else "OFF", values=("ON", "OFF"),
                                 background_normal="", background_color=PANEL_HI, color=CYAN)
        grid.add_widget(self._sp_voice)

        sv = ScrollView(do_scroll_x=False)
        sv.add_widget(grid)

        r1 = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        r1.add_widget(NeonButton(text="OPEN LOGIN", accent=CYAN, on_release=lambda a: self._open_login()))
        r1.add_widget(NeonButton(text="COPY URL", accent=CYAN, on_release=lambda a: self._copy_login()))
        r1.add_widget(NeonButton(text="LOGIN", accent=GREEN, on_release=lambda a: self._do_login()))

        r2 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        r2.add_widget(NeonButton(text="USE DEMO", accent=AMBER, on_release=lambda a: self._use_demodata()))
        r2.add_widget(NeonButton(text="TEST VOICE", accent=CYAN, on_release=lambda a: self._test_voice()))
        r2.add_widget(NeonButton(text="SAVE", accent=CYAN, on_release=lambda a: self._save_settings()))

        r3 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        r3.add_widget(NeonButton(text="CLOSE", accent=DIM, on_release=lambda a: self._settings_pop.dismiss()))

        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        box.add_widget(sv)
        box.add_widget(r1)
        box.add_widget(r2)
        box.add_widget(r3)
        self._settings_pop = Popup(title="CONFIG", content=box, size_hint=(0.9, 0.9),
                                   auto_dismiss=False, background="",
                                   title_color=CYAN, separator_color=CYAN)
        self._settings_pop.open()

    def _test_voice(self):
        self.tts.configure(self._fields["eleven_key"].text.strip(), self._fields["voice_id"].text.strip(),
                           self._sp_voice.text == "ON")
        self.log("Testing ElevenLabs voice ...")
        self.tts.speak("Tactical Neural Matrix voice link online.", priority=True)

    def _read_creds(self):
        self.broker.update_credentials(self._fields["api_key"].text.strip(), self._fields["api_secret"].text.strip())

    def _open_login(self):
        try:
            self._read_creds()
            url = self.broker.login_url()
        except BrokerError as ex:
            self.log(str(ex))
            return
        if not open_url(url):
            Clipboard.copy(url)
            self.log("Could not open browser; login URL copied to clipboard.")

    def _copy_login(self):
        try:
            self._read_creds()
            Clipboard.copy(self.broker.login_url())
            self.log("Login URL copied. Open it, log in, then copy the redirect URL.")
        except BrokerError as ex:
            self.log(str(ex))

    def _do_login(self):
        try:
            self._read_creds()
        except BrokerError as ex:
            self.log(str(ex))
            return
        token = self._fields["request_token"].text.strip() or Clipboard.paste()
        self.log("Logging in to Kite...")

        def work():
            try:
                uid = self.broker.login(token)
                self.log(f"Kite session active ({uid}).")
            except BrokerError as ex:
                self.log(str(ex))
            Clock.schedule_once(lambda dt: self._refresh_header())

        threading.Thread(target=work, daemon=True).start()

    def _use_demodata(self):
        if self.broker.open_positions():
            self.log("Close open positions first.")
            return
        self.broker.clear_account()
        try:
            self.broker.set_mode("PAPER")
        except BrokerError as ex:
            self.log(str(ex))
        self.cfg["node"] = self.broker.mode
        self.sp_node.text = "PAPER"
        self._refresh_header()
        self.log("Kite session cleared. PAPER mode on DEMO data.")

    def _save_settings(self):
        f = self._fields
        try:
            new = {
                "api_key": f["api_key"].text.strip(), "api_secret": f["api_secret"].text.strip(),
                "exchange": f["exchange"].text.strip().upper(), "symbol": f["symbol"].text.strip().upper(),
                "interval": self.sp_interval.text, "product": self.sp_product.text,
                "capital": float(f["capital"].text), "risk_pct": float(f["risk_pct"].text),
                "min_rr": float(f["min_rr"].text), "min_agree": int(f["min_agree"].text),
                "max_qty": int(f["max_qty"].text),
                "eleven_key": f["eleven_key"].text.strip(),
                "voice_id": f["voice_id"].text.strip() or DEFAULT_VOICE_ID,
                "voice_alerts": self._sp_voice.text == "ON",
            }
            if not new["symbol"] or not new["exchange"]:
                raise ValueError("Symbol/exchange empty")
        except ValueError as ex:
            self.log(f"Invalid settings: {ex}")
            return
        try:
            self.broker.update_credentials(new["api_key"], new["api_secret"])
            self.broker.set_mode(self.sp_node.text)
        except BrokerError as ex:
            self.log(f"Mode not changed: {ex}")
        self.cfg["node"] = self.broker.mode
        self.cfg.update(new)
        self.broker.configure(new["product"], new["max_qty"], new["capital"])
        self.engine.configure(new["min_agree"], new["min_rr"])
        self.tts.configure(new["eleven_key"], new["voice_id"], new["voice_alerts"])
        self._save_cfg()
        self._refresh_header()
        self.log(f"Settings saved. Mode {self.cfg['node']}, {self.cfg['exchange']}:{self.cfg['symbol']}")
        self._settings_pop.dismiss()

if __name__ == "__main__":
    TacticalApp().run()
