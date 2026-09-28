"""Mic -> RealtimeSTT -> Transcript(partial/final) on bus.transcript_q. pause()/resume() mute the mic during feedback."""
import threading, time, re, os, atexit, ctypes
from difflib import SequenceMatcher
from . import config, bus
from .types import Transcript

_rec = None
_uid = 0
_lock = threading.Lock()
_last_partial = ""
_closing = False
_job = None                      # Windows job object: kills the RealtimeSTT child processes if this process dies


# ---- deterministic vocabulary corrector (applied to every partial and final; ~0.1-0.5 ms)
_FIXES = [(re.compile(rf"(?<![\w'])(?:{pat})(?![\w'])", re.I), rep) for pat, rep in getattr(config, "STT_FIXES", {}).items()]
_VOCAB = [(t, t.lower(), len(t.split())) for t in getattr(config, "STT_VOCAB", [])]
_PUNCT = ".,!?;:\"'()"


_PHON = str.maketrans("bdgcqzvl", "ptkkksfr")


def _norm(tok: str) -> str:
    return tok.strip(_PUNCT).lower()


def _key(s: str) -> str:
    """Crude phonetic key: merge voiced/unvoiced consonants, drop inner vowels, collapse repeats ('tim mimbala' -> 'tmpr')."""
    s = re.sub(r"[^a-z0-9]", "", s).replace("ph", "f").replace("ck", "k").translate(_PHON)
    out = s[:1]
    for ch in s[1:]:
        if ch not in "aeiouyhw" and out[-1] != ch:
            out += ch
    return out


def _score(sm_raw: SequenceMatcher, sm_key: SequenceMatcher, win: str, thr: float) -> float:
    sm_raw.set_seq1(win)
    if sm_raw.real_quick_ratio() < 2 * thr - 1 or sm_raw.quick_ratio() < 2 * thr - 1:
        return 0.0
    sm_key.set_seq1(_key(win))
    return 0.5 * sm_raw.ratio() + 0.5 * sm_key.ratio()


def fix_vocab(text: str) -> str:
    """Fix demo proper nouns: exact regex fixes from config.STT_FIXES, then a fuzzy sweep over config.STT_VOCAB
    (score = mean of spelling and phonetic-key difflib ratios). Untouched words keep their capitalization/punctuation;
    a replaced window keeps its outer punctuation."""
    for rx, rep in _FIXES:
        text = rx.sub(rep, text)
    toks = text.split()
    if not toks or not _VOCAB:
        return text
    words = [_norm(t) for t in toks]
    cands = []                                    # (score, -width, start, end, term)
    for term, low, n in _VOCAB:
        thr = config.STT_FUZZY_RATIO if n > 1 else config.STT_FUZZY_RATIO_SHORT
        sm_raw = SequenceMatcher(None, "", low, autojunk=False)       # seq2 cached per term
        sm_key = SequenceMatcher(None, "", _key(low), autojunk=False)
        for k in range(max(1, n - 1), n + 2):
            for i in range(0, len(words) - k + 1):
                win = " ".join(words[i:i + k])
                if not win.strip() or abs(len(win) - len(low)) > len(low) * 0.6:
                    continue
                r = _score(sm_raw, sm_key, win, thr)
                if r >= thr:
                    cands.append((r, -k, i, i + k, term))
    if not cands:
        return text
    cands.sort(reverse=True)                      # best ratio first; exact matches (1.0) claim their words
    taken, repl = [False] * len(toks), []
    for r, _, i, j, term in cands:
        if any(taken[i:j]):
            continue
        taken[i:j] = [True] * (j - i)
        if r < 1.0:
            repl.append((i, j, term))
    for i, j, term in sorted(repl, reverse=True):
        a, b = toks[i], toks[j - 1]
        lead = a[:len(a) - len(a.lstrip(_PUNCT))]
        trail = b[len(b.rstrip(_PUNCT)):]
        toks[i:j] = [lead + term + trail]
    return " ".join(toks)


_HALLU = {"thank you", "thanks", "thank you very much", "thanks everyone", "thanks for watching", "bye",
          "you", "okay", "ok", "um", "uh", "hmm", "first", "rest", "good job", "so", "and", "the"}


def is_hallucination(text: str) -> bool:
    """Whisper invents 'Thank you.' / 'First. First. First.' on silence and breath noise. Drop those before Jev."""
    t = re.sub(r"[^a-z ]", " ", text.lower()).split()
    if not t:
        return True
    phrase = " ".join(t)
    keep = t[0] in config.MEDIA_WORDS or t[0] in ("yes", "no", "yeah", "nope", "cancel", "stop")
    if phrase in _HALLU or (len(set(t)) == 1 and len(t) >= 2 and not keep):   # "First. First." but not "Pause. Pause." / "yes yes"
        return True
    return len(t) <= 3 and all(w in _HALLU for w in t)


def _on_partial(text: str):
    global _last_partial
    text = fix_vocab(text.strip())
    if not text or text == _last_partial or is_hallucination(text):
        return
    _last_partial = text
    bus.transcript_q.put(Transcript(text, False, _uid, source="mic"))


def _on_rec_start():
    try:
        from . import overlay; overlay.pill("…", "hearing")     # instant reaction to voice activity
    except Exception:
        pass


def _final_loop():
    global _uid, _last_partial
    while not bus.stop.is_set() and not _closing:
        try:
            text = _rec.text()          # blocks until end of utterance
        except Exception as e:
            if _closing:
                return
            bus.log("events", kind="stt_error", err=repr(e)[:200]); time.sleep(0.5); continue
        if _closing:
            return
        text = fix_vocab((text or "").strip())
        if text and not is_hallucination(text):
            bus.transcript_q.put(Transcript(text, True, _uid, source="mic"))
        elif text:
            bus.log("stt", dropped=text[:60])
        with _lock:
            _uid += 1
            _last_partial = ""


def _local(name: str) -> str:
    """Cached model dir for a faster-whisper name, so startup never touches the network (prewarm.py caches them)."""
    if os.path.sep in name or "/" in name:
        return name
    try:
        from faster_whisper.utils import download_model
        return download_model(name, local_files_only=True)
    except Exception:
        return name                     # not cached: RealtimeSTT downloads it


def _children():
    return [p for p in (getattr(_rec, "transcript_process", None), getattr(_rec, "reader_process", None)) if getattr(p, "pid", None)]


def _bind_children():
    """Put RealtimeSTT's worker processes in a kill-on-close job: if whis is killed (console closed, Ctrl+C twice),
    Windows kills them too instead of leaving an orphan that spins on its dead pipe (1 GB BrokenPipeError log)."""
    global _job
    if os.name != "nt":
        return
    from ctypes import wintypes as w
    class BASIC(ctypes.Structure):
        _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", w.DWORD), ("c", ctypes.c_size_t),
                    ("d", ctypes.c_size_t), ("e", w.DWORD), ("f", ctypes.c_size_t), ("g", w.DWORD), ("h", w.DWORD)]
    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", ctypes.c_uint64 * 6), ("m", ctypes.c_size_t * 4)]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = k.OpenProcess.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    k.CloseHandle.argtypes = [w.HANDLE]
    try:
        if _job is None:
            job = k.CreateJobObjectW(None, None)
            info = EXT(); info.Basic.LimitFlags = 0x2000                  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not job or not k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error())
            _job = job                  # never closed: the handle dies with this process
        for p in _children():
            h = k.OpenProcess(0x0101, False, p.pid)                        # PROCESS_TERMINATE | PROCESS_SET_QUOTA
            ok = h and k.AssignProcessToJobObject(_job, h)
            h and k.CloseHandle(h)
            if not ok:
                bus.log("events", kind="stt_job_assign_failed", pid=p.pid, err=ctypes.get_last_error())
    except Exception as e:
        bus.log("events", kind="stt_job_failed", err=repr(e)[:120])


def stop(timeout: float = 6.0):
    """Clean RealtimeSTT shutdown (sets its shutdown_event so the children exit), bounded; terminate stragglers.
    Registered with atexit, so it runs before multiprocessing joins the (non-daemon) children at exit."""
    global _closing
    if _rec is None or _closing:
        return
    _closing = True
    import logging; logging.getLogger("realtimestt").setLevel(logging.CRITICAL)   # its engine.close() AttributeError is noise
    t = threading.Thread(target=_rec.shutdown, daemon=True, name="stt-shutdown")
    t.start(); t.join(timeout)
    for p in _children():
        try:
            if p.is_alive():
                p.terminate(); p.join(1.0)
        except Exception:
            pass


def start():
    global _rec
    from RealtimeSTT import AudioToTextRecorder
    cfg = dict(config.STT, model=_local(config.STT["model"]), realtime_model_type=_local(config.STT["realtime_model_type"]))
    _rec = AudioToTextRecorder(on_realtime_transcription_update=_on_partial, on_recording_start=_on_rec_start, **cfg)
    _bind_children()
    atexit.register(stop)               # after multiprocessing's own atexit hook -> runs before it (LIFO)
    threading.Thread(target=_final_loop, daemon=True, name="stt-final").start()
    bus.log("events", kind="stt_started")


def pause():
    try:
        _rec and _rec.set_microphone(False)
    except Exception:
        pass


def resume():
    try:
        _rec and _rec.set_microphone(True)
    except Exception:
        pass
