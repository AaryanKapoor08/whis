"""Ack tone + cached TTS phrases. Pauses STT during playback so whis never hears itself."""
import os, threading, queue, time, hashlib
import numpy as np
import sounddevice as sd
import soundfile as sf
from . import config, bus

_CACHE = os.path.join(config.LOG_DIR, "tts")
_tone = None
_pop = None
_engine_lock = threading.Lock()
_pause_cb = lambda: None
_resume_cb = lambda: None


def bind_stt(pause_fn, resume_fn):
    global _pause_cb, _resume_cb
    _pause_cb, _resume_cb = pause_fn, resume_fn


def ack():
    if config.TONE:
        bus.feedback_q.put(("ack", None))


def pop():
    """Wake-word 'pop': two quick rising notes."""
    if config.TONE:
        bus.feedback_q.put(("pop", None))


def say(text_or_key: str):
    if config.SPEAK:
        bus.feedback_q.put(("say", config.PHRASES.get(text_or_key, text_or_key)))


def _wav_path(text: str) -> str:
    return os.path.join(_CACHE, hashlib.md5(text.encode()).hexdigest()[:12] + ".wav")


def _render(text: str) -> str:
    p = _wav_path(text)
    if os.path.exists(p):
        return p
    os.makedirs(_CACHE, exist_ok=True)
    with _engine_lock:
        import pyttsx3
        e = pyttsx3.init(); e.setProperty("rate", 195)
        e.save_to_file(text, p); e.runAndWait()
        try:
            e.stop()
        except Exception:
            pass
    return p


def _play_file(p: str):
    data, sr = sf.read(p, dtype="float32")
    sd.play(data, sr); sd.wait()


def warm():
    """Pre-render all phrases (call at boot, off the hot path)."""
    for v in config.PHRASES.values():
        if v:
            _render(v)


def _loop():
    global _tone, _pop
    sr = 44100; t = np.linspace(0, 0.09, int(sr * 0.09), False)
    _tone = (0.25 * np.sin(2 * np.pi * 880 * t) * np.hanning(len(t))).astype("float32")
    t2 = np.linspace(0, 0.07, int(sr * 0.07), False); env = np.hanning(len(t2))
    _pop = np.concatenate([0.3 * np.sin(2 * np.pi * 660 * t2) * env, 0.3 * np.sin(2 * np.pi * 990 * t2) * env]).astype("float32")
    while not bus.stop.is_set():
        try:
            kind, payload = bus.feedback_q.get(timeout=0.2)
        except queue.Empty:
            continue
        speech = kind == "say"
        try:
            if speech:
                _pause_cb()           # only speech can re-trigger STT; a 90 ms tone never does, and muting
            if kind == "ack":         # the mic would drop the next words of a chained command
                sd.play(_tone, sr); sd.wait()
            elif kind == "pop":
                sd.play(_pop, sr); sd.wait()
            elif speech and payload:
                _play_file(_render(payload))
                time.sleep(0.3)
        except Exception as e:
            bus.log("events", kind="feedback_error", err=repr(e)[:200])
        finally:
            if speech:
                _resume_cb()


def start():
    from . import watchdog
    watchdog.spawn("feedback", _loop)
    if config.SPEAK:
        threading.Thread(target=warm, daemon=True, name="tts-warm").start()
