# -*- coding: utf-8 -*-
"""
Tactical Neural Matrix - voice-first command center (Kivy / Android).

* Every update is spoken: ElevenLabs (Riya K. Rao) when a key is set, otherwise
  Android's built-in text-to-speech as a fallback.
* Multi-timeframe analysis runs in the background; the user only hears a verdict.
* Every startup step, broker call and voice call is wrapped so errors are
  logged and spoken instead of crashing the app.
* Hard limits come from agents_module: 2% risk cap, 1:2 minimum RR, minimum 6
  agreeing agents, hard daily lockout after 2 consecutive losses (risk_data.json).
* LIVE orders always need a spoken or tapped confirmation. Only PAPER mode can
  auto-execute.
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
                           MIN_RR_FLOOR, ConsensusEngine, RiskManager, build_context,
                           position_size)
from broker_connector import BrokerError, KiteBroker

try:
    from voice_alerts import DEFAULT_VOICE_ID, VoiceAlerts
except Exception:  # voice must never stop the app from starting
    DEFAULT_VOICE_ID = "mAcTWQg9kibLro6Z2ouY"
    VoiceAlerts = None

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

# higher timeframe used to confirm the trend of the traded timeframe
HTF_MAP = {"minute": "15minute", "3minute": "15minute", "5minute": "15minute",
           "10minute": "30minute", "15minute": "60minute", "30minute": "60minute",
           "60minute": "day", "day": None}

CONFIRM_WORDS = {"confirm", "yes", "go", "proceed", "okay"}
CANCEL_WORDS = {"cancel", "abort", "no", "stop"}


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
    """Decorator: any exception is logged and (if possible) shown in the app log."""
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
    """Split speech into sentence groups that fit the ElevenLabs per-call length cap."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    out, cur = [], ""
    for p in parts:
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
            self.tts.speak(text, 0 if priority else 1, None)
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
            intent.putExtra(RecognizerIntent.EXTRA_PROMPT, "Say: scan, execute, status, close all")
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


class NullRisk:
    """Used only if risk storage cannot be opened anywhere: trading stays locked."""
    def status(self):
        return {"locked": True, "consecutive_losses": 0, "limit": 2, "wins": 0, "losses": 0,
                "daily_pnl": 0.0, "reason": "Risk storage unavailable - trading disabled"}

    def is_locked(self):
        return True

    def record_trade_result(self, *a, **k):
        pass


# ----------------------------- application -------------------------------- #
class TacticalApp(App):
    title = "Tactical Neural Matrix"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.log_lines = deque(maxlen=40)
        self.cfg = dict(DEFAULT_CFG)
        self.data_dir = ""
        self.risk = NullRisk()
        self.engine = None
        self.broker = None
        self.speaker = None
        self.voice = VoiceInput()
        self.last_decision = None
        self.busy = False
        self.monitor_busy = False
        self.pending = None
        self._pending_ev = None
        self._live_pop = None
        self._auto_ev = None
        self._exec_after = False
        self._last_key = None
        self._last_spoken = 0.0
        self._closed_said = False
        self.lbl_say = self.lbl_log = self.lbl_header = None
        self.lbl_dir = self.lbl_agree = self.lbl_levels = self.lbl_reason = None
        self.lbl_risk = self.lbl_pos = None
        self.card_cons = self.card_risk = None
        self.cards = []
        self.txt_cmd = None
        self._fields = {}

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
        self.risk = self._make_risk()
        self.engine = self._make_engine()
        self.broker = self._make_broker()
        self.speaker = self._make_speaker()
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
        try:
            return KiteBroker(self.data_dir, mode=c["mode"], api_key=c["api_key"],
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
        self._schedule_auto()
        self._refresh_all()
        st = self.risk.status()
        msg = "Tactical Neural Matrix is online. %s mode." % self._mode().title()
        if st["locked"]:
            msg += " Safety lockout is active, so trading is blocked for today."
        else:
            msg += " Safety lockout is clear. Say scan to check the market."
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
        if key == 27:  # Android back button: do not exit by accident
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
                            size_hint_y=None, height=dp(120))
        self.lbl_say = tlabel("Tap the big MIC button and say: scan.", color=TEXT, font=18, bold=True,
                              valign="top")
        say_card.add_widget(self.lbl_say)
        root.add_widget(say_card)

        row = BoxLayout(size_hint_y=None, height=dp(62), spacing=dp(8))
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
        self.txt_cmd = TextInput(hint_text="type: scan / execute / status / close all",
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
        self.lbl_header.text = "MODE %s | DATA %s | %s:%s %s | AUTO %s" % (
            self._mode(), self._source(), self.cfg["exchange"], self.cfg["symbol"],
            self.cfg["interval"], "ON" if self.cfg.get("auto_scan") else "OFF")
        self.lbl_header.color = RED if self._mode() == "LIVE" else GREEN

    @safe()
    def _refresh_risk(self):
        if self.lbl_risk is None:
            return
        st = self.risk.status()
        locked = st["locked"]
        self.card_risk.accent = list(RED if locked else GREEN)
        self.lbl_risk.text = (
            "HARD DAILY LOCKOUT: %s\nCONSECUTIVE LOSSES  %d / %d\nW/L  %d / %d    DAY P&L  %.2f%s" % (
                "ACTIVE - TRADING BLOCKED TODAY" if locked else "CLEAR",
                st["consecutive_losses"], st["limit"], st["wins"], st["losses"], st["daily_pnl"],
                ("\n" + st["reason"]) if locked and st["reason"] else ""))
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

        if self.pending:
            if words & CONFIRM_WORDS:
                self._confirm_yes()
                return
            if words & CANCEL_WORDS:
                self._confirm_no()
                return

        if any(k in t for k in ("close all", "exit all", "square off", "squareoff", "close everything")):
            self.close_all()
        elif any(k in t for k in ("execute", "place trade", "take trade", "enter trade")):
            self.execute()
        elif "auto" in words:
            on = not (words & {"off", "stop", "disable"})
            self.cfg["auto_scan"] = on
            self._save_cfg()
            self._schedule_auto()
            self._refresh_header()
            self.say("Automatic scanning is %s." % ("on" if on else "off"), priority=True)
        elif any(k in t for k in ("scan", "analyze", "analyse", "evaluate", "check market")):
            self.scan(manual=True)
        elif "status" in words or "report" in words:
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
            self.say("You can say: scan, execute, status, close all, auto on, auto off, mute or unmute. "
                     "When I ask for confirmation, say confirm or cancel.")
        else:
            self.say("Sorry, I did not understand. Say scan, execute, status or close all.")

    @safe()
    def start_voice(self):
        if platform != "android":
            self.log("Voice input works on Android only - type in the command box.")
            if self.txt_cmd is not None:
                self.txt_cmd.focus = True
            return
        self.voice.listen(lambda text: self.handle_command(text), lambda err: self.say(err))

    # ---- scan (multi-timeframe) ----
    @safe()
    def scan(self, manual=True, then_execute=False):
        if self.broker is None or self.engine is None:
            self.say("The trading engine is not available, so I cannot scan.", priority=True)
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

    @safe()
    def _scan_worker(self, exch, sym, itv, manual):
        decision, htf, note = None, None, ""
        try:
            candles = self.broker.get_candles(exch, sym, itv, 200)
            decision = self.engine.evaluate(sym, candles)
            htf_itv = HTF_MAP.get(itv)
            if self._source() == "DEMO":
                note = "demo data, so the higher timeframe check was skipped"
            elif htf_itv:
                try:
                    hc = self.broker.get_candles(exch, sym, htf_itv, 120)
                    if len(hc) >= MIN_CANDLES:
                        htf = build_context(sym, hc).trend
                except Exception as exc:
                    note = "higher timeframe data was unavailable"
                    self.log("HTF check failed: %s" % exc)
        except Exception as exc:
            self.log("Scan failed: %s" % exc)
            self._exec_after = False
            if manual:
                self.say("The scan failed. %s" % exc, priority=True)
            return
        finally:
            self.busy = False
        Clock.schedule_once(lambda dt: self._apply_decision(decision, htf, note, manual))

    @safe()
    def _apply_decision(self, d, htf, note, manual):
        go = self._exec_after
        self._exec_after = False

        htf_phrase = ""
        if d.direction in (BUY, SELL) and htf in ("BULL", "BEAR"):
            opposed = (d.direction == BUY and htf == "BEAR") or (d.direction == SELL and htf == "BULL")
            if opposed:
                d.approved = False
                d.reason = "%s bias rejected: the higher timeframe trend is against it" % d.direction
                htf_phrase = "The higher timeframe trend is against this trade."
            else:
                htf_phrase = "The higher timeframe trend agrees."
        elif htf == "RANGE":
            htf_phrase = "The higher timeframe is sideways."
        elif note:
            htf_phrase = "Note: %s." % note

        self.last_decision = d
        for card, vote in zip(self.cards, d.votes):
            card.set_vote(vote)
        directional = d.direction in (BUY, SELL)
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
        self.log("Scan %s: %s" % (d.symbol, d.reason))

        key = (d.direction, bool(d.approved))
        changed = key != self._last_key
        self._last_key = key
        stale = time.time() - self._last_spoken > 600
        if manual or go or changed or stale:
            self._narrate(d, htf_phrase)

        if d.approved:
            if go:
                self.execute()
            elif not manual and self._mode() == "PAPER" and self.cfg.get("auto_trade"):
                self.execute()
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

    def _narrate(self, d, htf_phrase):
        self._last_spoken = time.time()
        sym = d.symbol
        if d.reason == "Insufficient market data":
            self.say("I do not have enough price history for %s yet. Staying out." % sym, priority=True)
            return
        if d.approved:
            qty = position_size(float(self.cfg["capital"]), float(self.cfg["risk_pct"]), d.entry, d.sl,
                                int(self.cfg["max_qty"]))
            risk_amt = qty * abs(d.entry - d.sl)
            parts = [
                "%s setup approved on %s. %d of %d agents agree." % (side_word(d.direction), sym, d.agree, d.total),
                "Entry about %.2f. Stop loss %.2f. Target %.2f. Risk reward is 1 to %.1f, which is favorable." % (
                    d.entry, d.sl, d.tp, d.rr),
                "Suggested quantity %d, risking about %.0f rupees." % (qty, risk_amt),
                self._edge_text(d.rr),
            ]
            if htf_phrase:
                parts.append(htf_phrase)
            if self._mode() == "LIVE":
                parts.append("This is live mode. Say execute, then confirm, to place it.")
            elif self.cfg.get("auto_trade"):
                parts.append("Paper mode, so I will place this practice trade automatically.")
            else:
                parts.append("Say execute to place this practice trade.")
            self.say(" ".join(parts), priority=True)
        elif d.direction in (BUY, SELL):
            msg = "%s bias on %s, but I am not taking it. %s." % (side_word(d.direction), sym, d.reason)
            if htf_phrase:
                msg += " " + htf_phrase
            self.say(msg, priority=True)
        else:
            self.say("No clear trade on %s. The agents disagree, so I am staying out." % sym, priority=True)

    # ---- execute ----
    @safe()
    def execute(self):
        if self.broker is None:
            self.say("The broker is not available, so I cannot trade.", priority=True)
            return
        if self.risk.is_locked():
            self._refresh_risk()
            self.say("Trade blocked. The daily safety lockout is active. Trading resumes tomorrow.",
                     priority=True)
            return
        if self._mode() == "LIVE" and not market_open():
            self.say("The market is closed, so live orders are blocked.", priority=True)
            return
        d = self.last_decision
        if d is None or not d.approved or time.time() - d.ts > 120:
            self.scan(manual=True, then_execute=True)
            return
        qty = position_size(float(self.cfg["capital"]), float(self.cfg["risk_pct"]), d.entry, d.sl,
                            int(self.cfg["max_qty"]))
        if qty < 1:
            self.say("The calculated quantity is zero. Check capital, risk percent and stop distance in config.",
                     priority=True)
            return
        if self._mode() == "LIVE":
            self._ask_live_confirm(d, qty)
        else:
            self._do_execute(d, qty)

    @safe()
    def _ask_live_confirm(self, d, qty):
        risk_amt = qty * abs(d.entry - d.sl)
        self.pending = {"d": d, "qty": qty, "ts": time.time()}
        self.say("Live order, real money. %s %s, quantity %d. Entry about %.2f, stop loss %.2f, target %.2f. "
                 "Risking about %.0f rupees. Tap the big MIC button and say confirm to place it, or say cancel." % (
                     side_word(d.direction), d.symbol, qty, d.entry, d.sl, d.tp, risk_amt), priority=True)
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        box.add_widget(tlabel(
            "LIVE ORDER - REAL MONEY\n\n%s %s x%d\nEntry ~%.2f   SL %.2f   TP %.2f\nRR 1:%.2f\nRisk ~%.0f" % (
                d.direction, d.symbol, qty, d.entry, d.sl, d.tp, d.rr, risk_amt), color=TEXT, font=16))
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
        if self.risk.is_locked():
            self.say("Blocked. The safety lockout is active.", priority=True)
            return
        self._do_execute(p["d"], p["qty"])

    @safe()
    def _confirm_no(self):
        self._close_pending()
        self.say("Cancelled. No order was placed.", priority=True)

    @safe()
    def _do_execute(self, d, qty):
        if self.busy:
            self.say("I am busy with another request. Please try again in a moment.")
            return
        self.busy = True
        self.last_decision = None  # a signal can only be used once
        self.say("Placing the %s order on %s." % (d.direction.lower(), d.symbol))
        threading.Thread(target=self._execute_worker, args=(d, qty), daemon=True).start()

    @safe()
    def _execute_worker(self, d, qty):
        try:
            pos = self.broker.open_trade(self.cfg["exchange"], d.symbol, d.direction, qty, d.sl, d.tp)
            self.log("OPENED %s %s x%d @ %.2f SL %.2f TP %.2f [%s]" % (
                pos["side"], pos["symbol"], pos["qty"], pos["entry"], pos["sl"], pos["tp"], pos["mode"]))
            risk_amt = pos["qty"] * abs(pos["entry"] - pos["sl"])
            self.say("%s order filled on %s, quantity %d, at %.2f. Stop loss %.2f. Target %.2f. "
                     "Maximum risk about %.0f rupees. I am watching it for you." % (
                         side_word(pos["side"]), pos["symbol"], pos["qty"], pos["entry"], pos["sl"],
                         pos["tp"], risk_amt), priority=True)
        except BrokerError as exc:
            self.log("Order failed: %s" % exc)
            self.say("The order failed. %s" % exc, priority=True)
        except Exception as exc:
            self.log("Unexpected order error: %s" % exc)
            self.say("Something unexpected went wrong placing the order. Please check your positions.",
                     priority=True)
        finally:
            self.busy = False
            Clock.schedule_once(lambda dt: self._refresh_positions())

    # ---- monitoring / closing ----
    @safe()
    def _monitor_tick(self, dt):
        if self.broker is None or self.monitor_busy or not self._positions():
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
    def _register_closed(self, closed):
        for t in closed:
            self.risk.record_trade_result(t["pnl"], t["symbol"])
            self.log("CLOSED %s %s %s: exit %.2f P&L %.2f" % (
                t["reason"], t["side"], t["symbol"], t["exit"], t["pnl"]))
            how = {"TARGET": "hit its target", "STOP": "hit its stop loss", "MANUAL": "was closed"}.get(
                t["reason"], "was closed")
            result = "Profit of %.0f rupees." % t["pnl"] if t["pnl"] >= 0 else "Loss of %.0f rupees." % abs(t["pnl"])
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
            closed = self.broker.close_all()
            self._register_closed(closed)
        except BrokerError as exc:
            self.log("Close all: %s" % exc)
            self.say("Some positions could not be closed. %s" % exc, priority=True)
        except Exception as exc:
            self.log("Close all error: %s" % exc)
            self.say("Closing positions hit an error. Please check your positions.", priority=True)
        finally:
            self.busy = False
            Clock.schedule_once(lambda dt: self._refresh_all())

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
            if acct.get("equity") is not None:
                msg.append("Account value %.0f rupees." % acct["equity"])
            pos = self._positions()
            if not pos:
                msg.append("No open positions.")
            else:
                msg.append("%d open position%s." % (len(pos), "" if len(pos) == 1 else "s"))
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
            self.say(" ".join(msg), priority=True)
        except Exception as exc:
            self.log("Status error: %s" % exc)
            self.say("I could not read the status right now.", priority=True)

    # ---- automatic scanning ----
    @safe()
    def _schedule_auto(self):
        if self._auto_ev is not None:
            self._auto_ev.cancel()
            self._auto_ev = None
        if self.cfg.get("auto_scan"):
            self._auto_ev = Clock.schedule_interval(self._auto_tick, max(30, int(self.cfg.get("scan_secs", 60))))

    @safe()
    def _auto_tick(self, dt):
        if not self.cfg.get("auto_scan") or self.broker is None or self.busy or self.pending:
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
            sp_ = Spinner(text=text, values=values, background_normal="",
                          background_color=PANEL_HI, color=CYAN)
            grid.add_widget(sp_)
            return sp_

        add_field("api_key", "KITE API KEY", c["api_key"])
        add_field("api_secret", "KITE API SECRET", c["api_secret"], password=True)
        add_field("request_token", "TOKEN / REDIRECT URL", "")
        add_field("exchange", "EXCHANGE", c["exchange"])
        add_field("symbol", "SYMBOL", c["symbol"])
        self._sp_interval = add_spinner("INTERVAL", c["interval"], (
            "minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day"))
        self._sp_mode = add_spinner("MODE", self._mode() if self._mode() in ("PAPER", "LIVE") else "PAPER",
                                    ("PAPER", "LIVE"))
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
        r3 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        r3.add_widget(mk("SAVE", GREEN, lambda *a: self._save_settings()))
        r3.add_widget(mk("CLOSE", DIM, lambda *a: self._settings_pop.dismiss()))

        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(6))
        for w in (sv, r1, r2, r3):
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
                self.broker.set_mode(self._sp_mode.text)
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
