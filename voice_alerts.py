# -*- coding: utf-8 -*-
"""
voice_alerts.py - ElevenLabs text-to-speech alerts for Tactical Neural Matrix.

Default voice: Riya K. Rao (voice id mAcTWQg9kibLro6Z2ouY).
Audio is requested as MP3, cached on disk by text (repeat alerts cost no
quota), and played with android.media.MediaPlayer on Android.
"""
import hashlib
import os
import queue
import threading
import time

import requests
from kivy.utils import platform

DEFAULT_VOICE_ID = "mAcTWQg9kibLro6Z2ouY"
API_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
MODEL_ID = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"
MAX_CHARS = 250
CACHE_KEEP = 60


class VoiceAlerts:
    def __init__(self, cache_dir, api_key="", voice_id=DEFAULT_VOICE_ID, enabled=True, log=None):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.api_key = (api_key or "").strip()
        self.voice_id = (voice_id or DEFAULT_VOICE_ID).strip()
        self.enabled = bool(enabled)
        self._log = log or (lambda msg: None)
        self._q = queue.Queue(maxsize=6)
        self._last = {}
        self._lock = threading.Lock()
        self._auth_failed = False
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    def configure(self, api_key, voice_id, enabled):
        self.api_key = (api_key or "").strip()
        self.voice_id = (voice_id or DEFAULT_VOICE_ID).strip()
        self.enabled = bool(enabled)
        self._auth_failed = False

    def speak(self, text, priority=False):
        """Queue a short alert. Non-blocking; safe to call from any thread."""
        if not self.enabled:
            return
        if not self.api_key:
            if priority:
                self._log("Voice: add your ElevenLabs API key in CONFIG.")
            return
        if self._auth_failed:
            return
        text = " ".join(str(text).split())[:MAX_CHARS]
        if not text:
            return
        now = time.time()
        with self._lock:
            if not priority and self._last.get(text, 0.0) > now - 10.0:
                return
            self._last[text] = now
        if priority:  # drop pending alerts so urgent ones play first
            try:
                while True:
                    self._q.get_nowait()
            except queue.Empty:
                pass
        try:
            self._q.put_nowait(text)
        except queue.Full:
            pass

    # ------------------------------------------------------------------ #
    def _worker(self):
        while True:
            text = self._q.get()
            try:
                path = self._synthesize(text)
                if path:
                    self._play(path)
            except Exception as exc:
                self._log("Voice error: %s" % exc)

    def _synthesize(self, text):
        digest = hashlib.sha1(("%s|%s|%s" % (self.voice_id, MODEL_ID, text)).encode("utf-8")).hexdigest()
        path = os.path.join(self.cache_dir, digest + ".mp3")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        try:
            r = requests.post(
                API_URL.format(voice_id=self.voice_id),
                params={"output_format": OUTPUT_FORMAT},
                headers={"xi-api-key": self.api_key, "Content-Type": "application/json",
                         "Accept": "audio/mpeg"},
                json={"text": text, "model_id": MODEL_ID,
                      "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
                timeout=30)
        except requests.RequestException as exc:
            self._log("Voice network error: %s" % exc)
            return None
        if r.status_code != 200:
            detail = ""
            try:
                body = r.json().get("detail", "")
                detail = body.get("message", "") if isinstance(body, dict) else str(body)
            except ValueError:
                pass
            if r.status_code == 401:
                self._auth_failed = True
                self._log("ElevenLabs rejected the API key. Fix it in CONFIG (voice paused).")
            elif r.status_code in (402, 403):
                self._log("ElevenLabs: voice/plan not permitted for this key. %s" % detail)
            elif r.status_code == 429:
                self._log("ElevenLabs rate/quota limit reached. %s" % detail)
            else:
                self._log("ElevenLabs HTTP %d. %s" % (r.status_code, detail))
            return None
        if not r.content:
            self._log("ElevenLabs returned empty audio.")
            return None
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, path)
        self._prune_cache()
        return path

    def _prune_cache(self):
        try:
            files = [os.path.join(self.cache_dir, n) for n in os.listdir(self.cache_dir)
                     if n.endswith(".mp3")]
            files.sort(key=os.path.getmtime, reverse=True)
            for old in files[CACHE_KEEP:]:
                os.remove(old)
        except OSError:
            pass

    def _play(self, path):
        if platform == "android":
            from jnius import autoclass
            MediaPlayer = autoclass("android.media.MediaPlayer")
            mp = MediaPlayer()
            try:
                mp.setDataSource(path)
                mp.prepare()
                mp.start()
                deadline = time.time() + 40
                while mp.isPlaying() and time.time() < deadline:
                    time.sleep(0.1)
            finally:
                mp.release()
        else:
            from kivy.core.audio import SoundLoader
            snd = SoundLoader.load(path)
            if snd is None:
                self._log("No MP3 audio backend on this machine (works on Android).")
                return
            snd.play()
            time.sleep((snd.length or 3.0) + 0.3)
