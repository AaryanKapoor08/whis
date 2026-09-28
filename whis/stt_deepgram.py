"""Cloud STT backend: Deepgram streaming over a websocket, with keyterm prompting for the demo vocabulary.
Same contract as stt.py: pushes Transcript(text, final, utterance_id) to bus.transcript_q; partials are the running
text of the current turn. pause()/resume() feed silence instead of the mic while feedback audio plays. No GPU used.
  Flux (DG_MODEL flux-general-en, /v2/listen): TurnInfo events; StartOfTurn/Update -> partial, EndOfTurn -> final
    (model-native turn detection: eot_threshold, eot_timeout_ms). Docs: developers.deepgram.com/docs/flux/quickstart
  Nova-3 (DG_MODEL nova-3, /v1/listen): interim results -> partial, speech_final / UtteranceEnd -> final.
Falls back to local Whisper (stt.py) when the key is missing or the socket does not open at startup.
Selected by `python -m whis --stt deepgram` or automatically when DEEPGRAM_API_KEY is set."""
import json, threading, time, queue, urllib.parse, atexit
import numpy as np
import sounddevice as sd
import websocket                     # websocket-client (sync)
from . import config, bus
from .types import Transcript

SR = 16000
BLOCK = 1280                         # 80 ms of int16 mono (Flux: 80 ms chunks recommended)
_ws = None
_uid = 0
_paused = False
_segs: list[str] = []                # nova-3: is_final segments of the current utterance
_last_partial = ""
_audio_q: "queue.Queue[bytes]" = queue.Queue(maxsize=200)
_stream = None
_connected = threading.Event()
_rejected = threading.Event()        # handshake 400/401/403: bad key or params, do not wait out the timeout
_fix = lambda t: t                   # vocabulary corrector shared with stt.py when present
_flux = False
_dead = False                        # startup failed -> local Whisper took over
_local = None                        # whis.stt after a fallback


def _url() -> str:
    if _flux:                        # v2: no language/interim/endpointing params (model-native turn detection)
        params = [("model", config.DG_MODEL), ("encoding", "linear16"), ("sample_rate", str(SR)),
                  ("eot_threshold", str(config.DG_EOT_THRESHOLD)), ("eot_timeout_ms", str(config.DG_EOT_TIMEOUT_MS))]
        path = "v2"
    else:
        params = [("model", config.DG_MODEL), ("language", "en"), ("encoding", "linear16"), ("sample_rate", str(SR)),
                  ("channels", "1"), ("interim_results", "true"), ("endpointing", str(config.DG_ENDPOINTING_MS)),
                  ("utterance_end_ms", "1000"), ("punctuate", "true"), ("smart_format", "false"), ("vad_events", "true")]
        path = "v1"
    params += [("keyterm", k) for k in config.STT_KEYTERMS]
    return f"wss://api.deepgram.com/{path}/listen?" + urllib.parse.urlencode(params)


# ---- transcript emission (same shape as stt.py)
def _emit_partial(text: str):
    global _last_partial
    text = _fix(text.strip())
    if not text or text == _last_partial:
        return
    _last_partial = text
    bus.transcript_q.put(Transcript(text, False, _uid, source="mic"))


def _emit_final(text: str):
    global _uid, _segs, _last_partial
    text = _fix(text.strip())
    if text:
        bus.transcript_q.put(Transcript(text, True, _uid, source="mic"))
        bus.log("stt", backend="deepgram", final=text[:80])
    _uid += 1
    _segs, _last_partial = [], ""


def _hearing():
    try:
        from . import overlay; overlay.pill("…", "hearing")
    except Exception:
        pass


def _on_message(ws, msg):
    try:
        d = json.loads(msg)
    except Exception:
        return
    t = d.get("type")
    if t == "TurnInfo":                                   # Flux
        ev, text = d.get("event"), (d.get("transcript") or "").strip()
        if ev == "EndOfTurn":
            _emit_final(text)
        else:                                             # StartOfTurn / Update / EagerEndOfTurn / TurnResumed
            if ev == "StartOfTurn":
                _hearing()
            if text:
                _emit_partial(text)
    elif t == "Results":                                  # nova-3
        alt = (d.get("channel") or {}).get("alternatives") or [{}]
        text = (alt[0].get("transcript") or "").strip()
        if d.get("is_final"):
            if text:
                _segs.append(text)
            full = " ".join(_segs)
            if d.get("speech_final"):
                _emit_final(full)
            elif full:
                _emit_partial(full)
        elif text:
            _emit_partial(" ".join(_segs + [text]))
    elif t == "UtteranceEnd":
        if _segs:
            _emit_final(" ".join(_segs))
    elif t == "SpeechStarted":
        _hearing()
    elif t in ("Error", "ConfigureFailure"):
        bus.log("events", kind="stt_error", backend="deepgram", err=str(d)[:200])


def _on_error(ws, err):
    if getattr(err, "status_code", None) in (400, 401, 402, 403):
        _rejected.set()
    bus.log("events", kind="stt_error", backend="deepgram", err=repr(err)[:200])


def _on_close(ws, code, reason):
    _connected.clear()
    bus.log("events", kind="stt_ws_closed", code=code, reason=str(reason)[:100])


def _on_open(ws):
    _connected.set()
    bus.log("events", kind="stt_ws_open", model=config.DG_MODEL, keyterms=len(config.STT_KEYTERMS))


# ---- audio
def _mic_cb(indata, frames, t, status):
    data = bytes(indata) if not _paused else bytes(len(indata))     # silence while feedback plays
    try:
        _audio_q.put_nowait(data)
    except queue.Full:
        pass


def _sender():
    """Push mic bytes to the socket; keep the connection alive during long silences."""
    last_keepalive = time.time()
    while not bus.stop.is_set() and not _dead:
        try:
            chunk = _audio_q.get(timeout=0.5)
        except queue.Empty:
            chunk = None
        if not _connected.is_set() or _ws is None:
            continue
        try:
            if chunk is not None:
                _ws.send(chunk, opcode=websocket.ABNF.OPCODE_BINARY)
            elif not _flux and time.time() - last_keepalive > 5:      # Flux: pings suffice (60 s timeout)
                _ws.send(json.dumps({"type": "KeepAlive"})); last_keepalive = time.time()
        except Exception as e:
            bus.log("events", kind="stt_send_error", err=repr(e)[:120]); time.sleep(0.2)


def _ws_loop():
    """Connect, run until closed, reconnect. Runs under the watchdog."""
    global _ws
    while not bus.stop.is_set() and not _dead:
        _ws = websocket.WebSocketApp(_url(), header={"Authorization": f"Token {config.DEEPGRAM_API_KEY}"},
                                     on_open=_on_open, on_message=_on_message, on_error=_on_error, on_close=_on_close)
        _ws.run_forever(ping_interval=20, ping_timeout=10)
        if bus.stop.is_set() or _dead:
            break
        time.sleep(1.0)                                       # dropped: reconnect


def _fallback(why: str):
    """Startup failed: stop the socket/mic and run local Whisper instead (pause/resume delegate to it)."""
    global _dead, _local
    _dead = True
    try:
        _ws and _ws.close()
        if _stream:
            _stream.stop(); _stream.close()
    except Exception:
        pass
    bus.log("events", kind="stt_fallback", backend="whisper", why=why)
    print(f"deepgram unavailable ({why}) -> local whisper", flush=True)
    from . import stt as local
    local.start()
    _local = local


def stop():
    """CloseStream flushes the last turn and ends the session; then close the socket."""
    try:
        if _ws is not None and _connected.is_set():
            _ws.send(json.dumps({"type": "CloseStream"})); time.sleep(0.2); _ws.close()
    except Exception:
        pass


def start():
    global _stream, _fix, _flux
    if not config.DEEPGRAM_API_KEY:
        return _fallback("DEEPGRAM_API_KEY missing in .env")
    _flux = config.DG_MODEL.startswith("flux")
    try:
        from . import stt as _whisper                          # reuse the vocabulary corrector if stt.py defines one
        _fix = getattr(_whisper, "fix_vocab", _fix)
    except Exception:
        pass
    from . import watchdog
    try:
        _stream = sd.RawInputStream(samplerate=SR, channels=1, dtype="int16", blocksize=BLOCK, callback=_mic_cb)
        _stream.start()
    except Exception as e:
        return _fallback(f"mic: {e!r}"[:100])
    watchdog.spawn("stt-ws", _ws_loop, restart_delay=1.0)
    watchdog.spawn("stt-send", _sender)
    t_end = time.time() + config.DG_CONNECT_TIMEOUT_S
    while not _connected.wait(0.05):
        if _rejected.is_set() or time.time() > t_end:
            return _fallback("handshake rejected (key?)" if _rejected.is_set() else f"no websocket after {config.DG_CONNECT_TIMEOUT_S:.0f} s")
    atexit.register(stop)
    bus.log("events", kind="stt_started", backend="deepgram", model=config.DG_MODEL)


def pause():
    global _paused
    if _local:
        return _local.pause()
    _paused = True


def resume():
    global _paused
    if _local:
        return _local.resume()
    _paused = False
