"""P1 phone path. Retell custom functions POST here (through ngrok / cloudflared).
Request shape (Retell docs, Sep 2026): {"name": <fn>, "args": {...}, "call": {call_id, from_number, transcript, ...}}.
With "Payload: args only" (args_at_root) the body is just the args, so we also serve POST /retell/{fn}.
  do_on_laptop(command)  -> push Transcript(final=True, source="phone", future) and wait <= PHONE_WAIT_S for the
                            controller to resolve it (act -> outcome, confirm -> question, disambiguate -> list, else "didn't catch")
  whats_on_screen()      -> app + title + a few element names
Always answers 200 with a short plain string the Retell LLM reads aloud (Retell stringifies any body; cap 15k chars).
Call events (call_started / call_ended) -> POST /retell/events: island pill so the audience sees the call land.
Signature: X-Retell-Signature = "v=<ms>,d=<hex hmac_sha256(api_key, raw_body + ms)>", 5-min window (re-verified
against docs.retellai.com/features/secure-webhook, Sep 2026). The key must be the one with the webhook badge.
Verification is skipped when RETELL_API_KEY is empty or --no-verify is passed (BuildFlow P1)."""
import hmac, hashlib, json, re, time, threading, asyncio
from concurrent.futures import Future
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse, JSONResponse
from . import config, bus, jev
from .types import Transcript

app = FastAPI(title="whis")
_uid = -1                      # phone utterance ids are negative so they never collide with mic ids
_uid_lock = threading.Lock()
_get_snapshot = lambda: None
_verify = True
_calls = 0
_started = time.time()
_last_call: dict = {}
_inflight: dict = {}           # (call_id, command) -> (t, Future): a retry of a still-running act shares it
_inflight_lock = threading.Lock()
DEDUPE_S = 3.0
MAX_SPOKEN = 300               # chars; the caller hears every one of them

# ---- call-active flag: the laptop mic can hear the caller through a speakerphone -> controller ignores mic input.
# Active from call_started (or the first tool call) until call_ended; without call events, until 20 s after the
# last tool call. A call that never reports its end expires after CALL_MAX_S.
CALL_IDLE_S = 20.0
CALL_MAX_S = 15 * 60.0
_call_lock = threading.Lock()
_open_calls: dict = {}         # call_id -> start time (perf_counter), from call_started events
_last_tool_t = 0.0


def _mark_call(call_id: str, started: bool = False, ended: bool = False, tool: bool = False):
    global _last_tool_t
    now = time.perf_counter()
    with _call_lock:
        if started and call_id:
            _open_calls[call_id] = now
        if tool:
            _last_tool_t = now
        if ended:
            _open_calls.pop(call_id, None)
            if not _open_calls:
                _last_tool_t = 0.0


def call_active() -> bool:
    """Thread-safe: True while a Retell phone call is in progress. Import: `from whis.server import call_active`
    (or `sys.modules.get("whis.server")` to avoid importing fastapi when --phone is off)."""
    now = time.perf_counter()
    with _call_lock:
        for cid in [c for c, t in _open_calls.items() if now - t > CALL_MAX_S]:
            _open_calls.pop(cid, None)
        return bool(_open_calls) or (_last_tool_t > 0 and now - _last_tool_t < CALL_IDLE_S)


def verify_signature(body: bytes, header: str | None, api_key: str) -> bool:
    if not api_key or not _verify:
        return True
    if not header:
        return False
    parts = dict(p.strip().split("=", 1) for p in header.split(",") if "=" in p)
    ts, digest = parts.get("v"), parts.get("d")
    if not ts or not digest:
        return False
    try:
        if abs(time.time() * 1000 - int(ts)) > 5 * 60 * 1000:
            return False
    except ValueError:
        return False
    want = hmac.new(api_key.encode(), body + ts.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, digest.lower())


def sign(body: bytes, api_key: str, ts_ms: int | None = None) -> str:
    """Build a Retell-style signature header (used by scripts/phone_check.py)."""
    ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
    return f"v={ts},d={hmac.new(api_key.encode(), body + ts.encode(), hashlib.sha256).hexdigest()}"


def speakable(text: str) -> str:
    """One short, TTS-safe string: no newlines/markup, no 'done. done.' stutter, capped at a sentence boundary."""
    s = re.sub(r"[`*_#<>\[\]{}|]", " ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "Done."
    out, prev = [], None
    for sent in re.split(r"(?<=[.!?])\s+", s):
        key = sent.lower().rstrip(".!? ")
        if key and key == prev:
            continue
        out.append(sent)
        prev = key
    s = " ".join(out)
    if len(s) > MAX_SPOKEN:
        cut = s[:MAX_SPOKEN]
        s = cut[:cut.rfind(". ") + 1] if ". " in cut else cut.rsplit(" ", 1)[0] + "."
    return s


def do_on_laptop(command: str, timeout: float = config.PHONE_WAIT_S, call_id: str = "") -> str:
    global _uid
    command = (command or "").strip().strip('"').strip()
    if not command:
        return "I didn't catch a command."
    key = (call_id, command.lower())
    now = time.time()
    with _inflight_lock:
        for k in [k for k, (t, _) in _inflight.items() if now - t > DEDUPE_S]:
            _inflight.pop(k, None)
        hit = _inflight.get(key)
        if hit and not hit[1].done():            # only join a still-running act; a repeat after it finished is new
            f, dup = hit[1], True
        else:
            with _uid_lock:
                _uid -= 1
                uid = _uid
            f, dup = Future(), False
            _inflight[key] = (now, f)
    if not dup:
        bus.transcript_q.put(Transcript(command, True, uid, source="phone", future=f))
    try:
        return speakable(f.result(timeout=timeout) or "Done.")
    except Exception:
        return "Still working on it. Ask me what's on screen in a moment."


def whats_on_screen() -> str:
    snap = _get_snapshot()
    if not snap:
        return "I can't see the screen right now."
    names, seen = [], set()
    for e in snap.elements:
        n = re.sub(r"\s+", " ", (e.name or "")).strip()[:40]
        if n and n.lower() not in seen:
            seen.add(n.lower())
            names.append(n)
        if len(names) >= 8:
            break
    title = re.sub(r"\s+", " ", snap.title or "").strip()[:80]
    where = f"{snap.app or 'an app'}, {title}" if title else (snap.app or "the desktop")
    return speakable(f"On screen: {where}. " + (f"I can see {', '.join(names)}." if names else ""))


def _parse(body: bytes, path_fn: str = "") -> tuple[str, dict, dict]:
    try:
        data = json.loads(body or b"{}")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    fn = path_fn or str(data.get("name") or "")
    args = data.get("args")
    if isinstance(args, str):                    # tolerate a JSON-encoded args string
        try:
            args = json.loads(args)
        except Exception:
            args = {"command": args}
    if not isinstance(args, dict):
        args = {k: v for k, v in data.items() if k not in ("name", "call")} if path_fn else {}
    call = data.get("call") if isinstance(data.get("call"), dict) else {}
    return fn, args, call


async def _handle(req: Request, path_fn: str = ""):
    global _calls, _last_call
    body = await req.body()
    if not verify_signature(body, req.headers.get("x-retell-signature"), config.RETELL_API_KEY):
        bus.log("events", kind="phone_bad_signature", header=(req.headers.get("x-retell-signature") or "")[:40])
        return Response("bad signature", status_code=401)
    fn, args, call = _parse(body, path_fn)
    call_id = str(call.get("call_id") or "")
    _mark_call(call_id, tool=True)
    t0 = time.perf_counter()
    _calls += 1
    _last_call = {"t": time.time(), "fn": fn, "call_id": call_id[:12], "from": call.get("from_number")}
    try:
        if fn == "do_on_laptop":                 # blocking wait for the laptop -> off the event loop
            cmd = args.get("command") or args.get("text") or args.get("instruction") or args.get("query") or ""
            out = await asyncio.to_thread(do_on_laptop, str(cmd), config.PHONE_WAIT_S, call_id)
        elif fn == "whats_on_screen":
            out = await asyncio.to_thread(whats_on_screen)
        else:
            out = "I don't know that function."
    except Exception as e:                       # never a 5xx toward Retell: the agent must keep talking
        bus.log("events", kind="phone_error", err=repr(e)[:200])
        out = "Something went wrong on the laptop."
    ms = round((time.perf_counter() - t0) * 1000)
    bus.log("phone", fn=fn, args=args, call_id=call_id[:12], reply=out[:160], ms=ms)
    print(f"  [PHONE] {fn}({args.get('command', '') if isinstance(args, dict) else ''}) -> {out!r} {ms}ms")
    return PlainTextResponse(out)


@app.get("/")
@app.get("/health")
def health():
    return {"ok": True, "provider": jev.client.provider["name"], "jev_ms": jev.client.last_latency_ms, "calls": _calls,
            "verify": bool(_verify and config.RETELL_API_KEY), "uptime_s": round(time.time() - _started),
            "last_call": _last_call, "wait_s": config.PHONE_WAIT_S, "call_active": call_active()}


@app.post("/ambient/arm")
async def ambient_arm(req: Request):
    """Cue the --demo-due-in nudge (localhost only): curl -X POST localhost:8000/ambient/arm [-d '{"minutes": 110}']"""
    tunneled = any(h in req.headers for h in ("x-forwarded-for", "cf-connecting-ip", "ngrok-trace-id"))
    if tunneled or (req.client.host if req.client else "") not in ("127.0.0.1", "::1", "localhost", "testclient"):
        return Response("local only", status_code=403)
    try:
        data = json.loads(await req.body() or b"{}")
    except Exception:
        data = {}
    from . import ambient
    ok = ambient.arm(data.get("minutes"))
    return JSONResponse({"armed": ok, "call_active": call_active()})


@app.post("/retell")
async def retell(req: Request):
    return await _handle(req)


@app.post("/retell/events")
async def retell_events(req: Request):
    """Agent webhook (call_started / call_ended / call_analyzed). Show the call on the island; always 204 fast."""
    body = await req.body()
    if not verify_signature(body, req.headers.get("x-retell-signature"), config.RETELL_API_KEY):
        return Response("bad signature", status_code=401)
    try:
        data = json.loads(body or b"{}")
        ev, call = data.get("event", ""), data.get("call") or {}
        cid = str(call.get("call_id", ""))
        if ev == "call_started":
            _mark_call(cid, started=True)
        elif ev in ("call_ended", "call_analyzed"):
            _mark_call(cid, ended=True)
        bus.log("events", kind="phone_" + str(ev), call_id=str(call.get("call_id", ""))[:12], frm=call.get("from_number"))
        text = {"call_started": "Phone call connected", "call_ended": "Phone call ended"}.get(ev)
        if text:
            print(f"  [PHONE] {text} {call.get('from_number') or ''}")
            try:
                from . import overlay
                overlay.pill(text, "acting" if ev == "call_started" else "listening")
            except Exception:
                pass
    except Exception as e:
        bus.log("events", kind="phone_event_error", err=repr(e)[:200])
    return Response(status_code=204)


@app.post("/retell/{fn}")
async def retell_fn(fn: str, req: Request):
    """Same functions with "Payload: args only" on: the body is the bare args object."""
    return await _handle(req, fn)


@app.post("/say")
async def say(req: Request):
    """Local convenience for testing without Retell: curl -d '{"command":"open notepad"}' localhost:8000/say"""
    data = await req.json()
    return JSONResponse({"reply": await asyncio.to_thread(do_on_laptop, data.get("command", ""))})


def start(get_snapshot, verify: bool = True):
    """Run uvicorn in a daemon thread. Call from main after the controller thread is up."""
    global _get_snapshot, _verify
    _get_snapshot, _verify = get_snapshot, verify
    import uvicorn

    def run():
        uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT, log_level="warning")
    threading.Thread(target=run, daemon=True, name="server").start()
    on = verify and bool(config.RETELL_API_KEY)
    bus.log("events", kind="server_started", port=config.SERVER_PORT, verify=on)
    if not on:
        print(f"  [PHONE] signature check OFF ({'--no-verify' if not verify else 'RETELL_API_KEY empty'})")
