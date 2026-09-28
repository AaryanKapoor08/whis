"""`--brain hybrid`: Jev's speed on simple confident commands, the Claude tool-use brain's accuracy on everything else.

Per FINAL utterance (and speculatively on a partial that has been stable for SPEC_SILENCE_S) two things start at once:
  - Jev (questions.build -> jev.client.ask -> policy.decide), ~0.2 s
  - a Claude turn (brain_claude.ClaudeBrain.handle) in HOLD: its API call runs, but it cannot execute a tool (or even
    process the reply) until the route is decided. Gates are installed on the brain *instance* (run_tool / _call /
    progress wrappers); brain_claude.py itself is untouched.
The router then takes the Jev fast path only when every rule in `fast_path()` holds (single clause, whitelisted closed-set
intent, confident, the app/site literally said, no pronoun / question / free text, nothing but the command words).
Fast path -> the held Claude turn is ABORTED (raises _Abort, a BaseException, at its next checkpoint: no tool, no memory
line, no log line) and the action runs through the normal executor bridge (controller.on_decision). Otherwise the turn
is released (GO) and Jev's answer is ignored. A speculative turn only ever runs if the final text equals its text.
Whatever the fast path did is appended to the Claude brain's memory, so "play that again" / "close it" work across paths.
Every routed utterance logs one logs/brain.jsonl line: brain="hybrid", route=jev|claude|local, timings from the final."""
import re, threading, time
from concurrent.futures import ThreadPoolExecutor
from . import config, bus, spans, questions, policy, jev
from .types import Transcript, Decision, Action

HOLD, GO, ABORT = "hold", "go", "abort"
SPEC_SILENCE_S = 0.25      # a partial unchanged this long starts a speculative turn
JEV_WAIT_S = 0.9           # Jev p50 ~190 ms over 1.5k calls; slower than this -> Claude (already in flight) takes it
HOLD_MAX_S = 10.0          # a held turn that is never routed (partial with no matching final) aborts itself

# ---- fast-path rules (confidences are Jev's; every routed utterance logs them for re-tuning)
FAST_INTENTS = {"open_app", "focus_app", "media_play_pause", "volume_up", "volume_down", "navigate_url",
                "scroll_up", "scroll_down", "go_back", "press_key", "open_terminal"}
MIN_INTENT = 0.5           # live "open spotify" scored 0.58-0.84; the verb+name-only rule is the real guard
MIN_IS_CMD = 0.5
MIN_ARG = 0.4              # app / key choice (policy.appConfidence; the literal-name + no-extra-words rules carry it)
MIN_SITE = 0.6
SITE_ALONE = 0.85          # "open google": intent 0.41 but site 0.97 -> still fast
MAX_WORDS = 8
SIMPLE_KEYS = {"enter", "escape", "tab", "backspace"}

_LEAD = re.compile(r"^(?:(?:okay|ok|so|now|hey|alright|all right|um|uh|well|right)\b[\s,.!]*)+", re.I)
_CHAIN = re.compile(r"\b(?:and|then|after|afterwards|also|plus|before|while|until|next)\b|[,;:]\s*\w", re.I)
_QUESTIONISH = re.compile(r"\?|^(?:what|what's|whats|which|how|why|when|who|where|is|are|am|do|does|did|should|tell|check|look|read)\b", re.I)
_PRONOUN = re.compile(r"\b(?:it|that|this|them|those|these|again|same|there|here|one|last|other)\b", re.I)
_PRONOUN_OK = {"volume_up": {"it"}, "volume_down": {"it"}, "open_terminal": {"it", "there", "here"}}
_FILL = set("please now just okay ok hey so can could would will you for me the a an to go i want need like whis".split())
_WORDS = {
    "open_app": "open launch start app application program up bring",
    "focus_app": "switch back focus bring show open window app up",
    "navigate_url": "open go navigate take show pull bring website site page visit browse load up",
    "volume_up": "volume turn make it louder raise increase sound music bit little more up",
    "volume_down": "volume turn make it quieter softer lower decrease reduce sound music bit little down less",
    "media_play_pause": "pause resume play stop unpause music song playback track",
    "scroll_up": "scroll page up bit little more",
    "scroll_down": "scroll page down bit little more",
    "go_back": "back previous page navigate return",
    "press_key": "press hit key tap button enter return escape esc tab backspace",
    "open_terminal": "open launch start new terminal console shell command prompt powershell window in it there here integrated up",
}
_WORDS = {k: set(v.split()) for k, v in _WORDS.items()}
_ALIASES = {"vs code": {"vs", "code", "vscode", "visual", "studio"}, "code": {"vs", "code", "vscode", "visual", "studio"},
            "explorer": {"file", "files", "explorer"}, "files": {"file", "files", "explorer"}}


class _Abort(BaseException):
    """Raised inside a held Claude turn when the fast path took the utterance (BaseException: brain.handle's
    `except Exception` doesn't swallow it, its `finally: _end()` still runs, and it logs/memorizes nothing)."""


def _norm(t: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", (t or "").lower()))


def _c(ans, k):
    a = (ans or {}).get(k) or {}
    return a.get("choice"), float(a.get("confidence", 0) or 0)


def fast_path(text: str, ans: dict | None, d: Decision | None) -> tuple[bool, str]:
    """(take Jev's decision?, why). Every rule must hold; anything unusual goes to Claude."""
    if ans is None or d is None:
        return False, "no jev answer"
    if d.kind != "act" or d.action is None:
        return False, f"jev {d.kind}: {d.reason}"
    intent = d.action.intent
    if intent not in FAST_INTENTS:
        return False, f"intent {intent} not fast"
    t = _LEAD.sub("", text.strip()).strip(" .!")
    if len(spans.split_clauses(t)) > 1 or _CHAIN.search(t):
        return False, "multi-clause"
    if _QUESTIONISH.search(t):
        return False, "question"
    words = re.findall(r"[a-z0-9']+", t.lower())
    if not words or len(words) > MAX_WORDS:
        return False, f"{len(words)} words"
    prons = {w.lower() for w in _PRONOUN.findall(t)} - _PRONOUN_OK.get(intent, set())
    if prons:
        return False, f"pronoun {sorted(prons)[0]}"
    _, iconf = _c(ans, "intent")
    is_cmd = float((ans.get("is_command") or {}).get("noul", 0) or 0)
    if is_cmd < MIN_IS_CMD:
        return False, f"is_command {is_cmd:.2f}"
    entity: set[str] = set()
    a = d.action.args
    if intent in ("open_app", "focus_app"):
        app = str(a.get("app") or "").lower()
        _, aconf = _c(ans, "app")
        if app in config.BROWSER_NAMES or app in ("chrome", "edge", "msedge"):
            return False, "browser app"                    # live A/B: Jev's "open browser" fails; Claude opens Brave
        if not (app in config.APPS or app in config.APP_PROCS):
            return False, f"unknown app {app}"
        if aconf < MIN_ARG or not policy.app_mentioned(app, t):
            return False, f"app {app} {aconf:.2f} not sure/said"
        entity = set(re.findall(r"[a-z0-9]+", app)) | _ALIASES.get(app, set())
    elif intent == "navigate_url":
        site, sconf = _c(ans, "site")
        url = a.get("url")
        key = next((k for k, v in config.BOOKMARKS.items() if v == url), None)
        if key is None or key != site or key not in words:
            return False, f"url {url} not a said bookmark"
        if sconf < MIN_SITE or (iconf < MIN_INTENT and sconf < SITE_ALONE):
            return False, f"site {site} {sconf:.2f} / intent {iconf:.2f}"
        entity = {key}
        iconf = max(iconf, MIN_INTENT)
    elif intent == "press_key":
        key, kconf = _c(ans, "key")
        if a.get("key") not in SIMPLE_KEYS or kconf < MIN_ARG:
            return False, f"key {a.get('key')} {kconf:.2f}"
    if iconf < MIN_INTENT:
        return False, f"intent {intent} {iconf:.2f}"
    extra = [w for w in words if w not in _FILL and w not in _WORDS[intent] and w not in entity]
    if extra:
        return False, f"extra words {extra[:3]}"          # anything beyond verb + name (a query, a place, a detail)
    return True, f"{intent} {iconf:.2f}"


class Turn:
    """One utterance (or speculative partial) with its Jev answer and its held Claude run."""
    def __init__(self, text: str, spec: bool):
        self.text, self.norm, self.spec = text, _norm(text), spec
        self.t0 = time.perf_counter()
        self.state = HOLD
        self._ev = threading.Event()
        self.jev_ev = threading.Event()
        self.ans = self.ctx = self.d = None
        self.jev_ms = None
        self.t_final = None
        self.first_tool_t = None
        self.steps: list[str] = []
        self.route = None
        self.cfut = None

    def go(self):
        self.state = GO; self._ev.set()

    def abort(self):
        if self.state == HOLD or self.route == "claude":
            self.state = ABORT; self._ev.set()

    def wait(self, timeout):
        if not self._ev.wait(timeout):
            self.state = ABORT                               # never routed: a dead speculative turn
        return self.state


class Router:
    def __init__(self, ctrl, brain):
        self.ctrl, self.brain = ctrl, brain
        self._jev_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hyb-jev")
        self._claude_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hyb-claude")   # a dead turn's API call
        self._tls = threading.local()                                                               # must not block the next
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self.spec: Turn | None = None
        self.active: Turn | None = None
        self._final_utt = -1
        self._media_utt = -1
        self._install_gates()

    # ------------------------------------------------------------------ gates on the brain instance
    def _checkpoint(self, turn: Turn, block: bool):
        if turn.state == ABORT:
            raise _Abort()
        if block and turn.state == HOLD and turn.wait(HOLD_MAX_S) == ABORT:
            raise _Abort()
        if turn.state == ABORT:
            raise _Abort()

    def _install_gates(self):
        b, tls = self.brain, self._tls
        orig_run, orig_prog = b.run_tool, b.progress

        def run_tool(name, a, confirmed=False):
            turn = getattr(tls, "turn", None)
            if turn is not None:                          # outside a hybrid turn (confirm(), local) nothing is gated
                self._checkpoint(turn, True)
                if turn.first_tool_t is None:
                    turn.first_tool_t = time.perf_counter()
            r = orig_run(name, a, confirmed)
            if turn is not None:
                v = next((str(x) for k, x in (a or {}).items() if not str(k).startswith("_")), "")[:40]
                turn.steps.append(f"{name}({v}) {'ok' if r.get('ok') else 'FAILED'}")
            return r
        b.run_tool = run_tool

        if hasattr(b, "_call"):                           # stop a dead turn right after its API reply (before any text/tool)
            orig_call = b._call

            def _call(messages):
                turn = getattr(tls, "turn", None)
                if turn is not None:
                    self._checkpoint(turn, False)         # first call runs while HOLD: that's the parallelism
                out = orig_call(messages)
                if turn is not None:
                    self._checkpoint(turn, True)
                return out
            b._call = _call

        def progress(text, state):
            turn = getattr(tls, "turn", None)
            if turn is None or turn.state == GO:
                orig_prog(text, state)
        b.progress = progress

    # ------------------------------------------------------------------ transcript entry (controller delegates here)
    def on_transcript(self, tr: Transcript):
        from . import brain_claude
        from .controller import _request_like
        c, b = self.ctrl, self.brain
        c.utterance_id = tr.utterance_id
        c.last_transcript_t = tr.t
        named, text = spans.strip_wake(tr.text, config.WAKE_WORDS)
        if named:
            c.last_wake_t = time.perf_counter()
            if tr.utterance_id != c._woke_utt:
                c._woke_utt = tr.utterance_id
                from . import feedback
                feedback.pop()
                if c.on_transcript_cb:
                    c.on_transcript_cb("Go ahead, I'm listening…", "listening")
        if tr.source == "phone":
            c.last_wake_t = time.perf_counter()
        addressed = named or c.named_active() or tr.source == "phone"
        text = text.strip()
        if c.on_transcript_cb and text:
            c.on_transcript_cb(text, ("thinking" if tr.final else "listening") if addressed else "hearing")
        if not text:
            if tr.final:
                c._resolve_phone(tr, "notcmd")
            return
        bare = re.sub(r"[^a-z ]", " ", text.lower()).split()
        bare = " ".join(dict.fromkeys(bare)) if len(set(bare)) == 1 else " ".join(bare)
        if bare in config.MEDIA_WORDS and (tr.final or bare not in ("play", "stop", "resume")) and not b.pending:
            if tr.utterance_id != self._media_utt:        # once per utterance (partial + final of the same "pause")
                self._media_utt = tr.utterance_id
                self._local_media(config.MEDIA_WORDS[bare], text, tr)
            return
        commandish = addressed or brain_claude.looks_like_command(text) or _request_like(text)
        if not tr.final:
            if commandish and not b.pending and tr.utterance_id != self._final_utt:
                self._arm_spec(text, tr.utterance_id)
            return
        self._final_utt = tr.utterance_id
        self._cancel_timer()
        if b.pending:
            yn = brain_claude.yes_no(text)
            if yn is not None:
                self._drop_spec()
                c._claude_submit(b.confirm, tr, yn, text, tr.t)
                return
            b.pending = None                              # a new command supersedes the pending one
        if not commandish:
            self._drop_spec()
            print(f"  [IGNORE] not addressed / not a command  <- '{text}'", flush=True)
            bus.log("brain", brain="hybrid", route="ignore", utterance=text, total_ms=0, first_action_ms=None, ok=False)
            c._resolve_phone(tr, "notcmd")
            return
        c.last_wake_t = time.perf_counter()
        with self._lock:
            turn, spec = None, self.spec
            if spec is not None and spec.norm == _norm(text) and spec.state == HOLD:
                turn = spec                               # the speculation was right: its Jev + Claude calls are ahead
            elif spec is not None:
                spec.abort()
            self.spec = None
            prev, self.active = self.active, None
        if prev is not None and prev.cfut is not None and not prev.cfut.done():
            prev.abort()                                  # a new command stops the running Claude task at its next step
        if turn is None:
            turn = self._start(text, spec=False)
        turn.t_final = tr.t
        b._begin()                                        # busy until routed + executed (--fake-stt wait_idle)
        threading.Thread(target=self._route, args=(turn, tr), daemon=True, name="hyb-route").start()

    # ------------------------------------------------------------------ speculation on a stable partial
    def _cancel_timer(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _arm_spec(self, text, uid):
        self._cancel_timer()
        self._timer = threading.Timer(SPEC_SILENCE_S, self._fire_spec, args=(text, uid))
        self._timer.daemon = True
        self._timer.start()

    def _fire_spec(self, text, uid):
        with self._lock:
            if uid == self._final_utt or self.brain.pending:
                return
            if self.spec is not None:
                if self.spec.norm == _norm(text):
                    return
                self.spec.abort()
            self.spec = self._start(text, spec=True)
        bus.log("events", kind="hybrid_spec", text=text[:80])

    def _drop_spec(self):
        with self._lock:
            if self.spec is not None:
                self.spec.abort()
            self.spec = None

    # ------------------------------------------------------------------ the two brains
    def _start(self, text, spec) -> Turn:
        turn = Turn(text, spec)
        self._jev_pool.submit(self._jev_job, turn)
        turn.cfut = self._claude_pool.submit(self._claude_job, turn)
        return turn

    def _jev_job(self, turn: Turn):
        from .controller import Ctx
        t = time.perf_counter()
        try:
            ctx = Ctx(self.ctrl, turn.text, True)
            state, qs = questions.build(ctx)
            ans = jev.client.ask(state, qs)
            turn.ctx, turn.ans = ctx, ans
            turn.d = policy.decide(ans, ctx) if ans is not None else None
        except Exception as e:
            bus.log("events", kind="hybrid_jev_error", err=repr(e)[:300])
        finally:
            turn.jev_ms = round((time.perf_counter() - t) * 1000)
            turn.jev_ev.set()

    def _claude_job(self, turn: Turn):
        self._tls.turn = turn
        msg, aborted = "", False
        try:
            msg = self.brain.handle(turn.text, turn.t0)
        except _Abort:
            aborted = True
        except Exception as e:
            bus.log("events", kind="hybrid_claude_error", err=repr(e)[:300]); msg = "Couldn't do that."
        finally:
            self._tls.turn = None
        if aborted and turn.route == "claude" and turn.steps:       # superseded mid-task: keep what it did in memory
            self.brain.memory.append({"said": turn.text[:120], "did": "; ".join(turn.steps)[:300], "result": "interrupted by the next command"})
        elif not aborted and turn.route != "claude":                 # ended without reaching a gate (no _call hook)
            m = self.brain.memory
            if m and m[-1].get("said") == turn.text[:120]:
                m.pop()
        return msg, aborted

    # ------------------------------------------------------------------ routing
    def _route(self, turn: Turn, tr: Transcript):
        c = self.ctrl
        try:
            turn.jev_ev.wait(max(0.0, JEV_WAIT_S - (time.perf_counter() - tr.t)))
            if not turn.jev_ev.is_set():
                ok, why = False, f"jev slow (>{JEV_WAIT_S:.1f}s)"
            else:
                ok, why = fast_path(turn.text, turn.ans, turn.d)
            t_route = time.perf_counter()
            iname, iconf = _c(turn.ans, "intent")
            common = dict(brain="hybrid", utterance=turn.text, reason=why, spec=turn.spec, jev_ms=turn.jev_ms,
                          jev_intent=iname, jev_conf=round(iconf, 2), route_ms=round((t_route - tr.t) * 1000))
            if ok:
                turn.route = "jev"
                turn.abort()                                         # the held Claude turn must never act
                d = turn.d
                t_act = time.perf_counter()
                outcome = c.on_decision(d, tr)
                ok_run = outcome is None or outcome.ok
                self._after_fast(d, turn, outcome)
                total = round((time.perf_counter() - tr.t) * 1000)
                fa = round((t_act - tr.t) * 1000)
                print(f"  [HYBRID] route=jev  {why}  jev {turn.jev_ms} ms, first action {fa} ms, total {total} ms"
                      f"{' (spec)' if turn.spec else ''}  <- '{turn.text}'", flush=True)
                bus.log("brain", route="jev", intent=d.action.intent,
                        args={k: str(getattr(v, "name", v))[:80] for k, v in d.action.args.items()}, steps=1, ok=ok_run,
                        result=(outcome.msg[:200] if outcome is not None else ""), first_action_ms=fa, total_ms=total, **common)
                c.last_wake_t = time.perf_counter()
                c._resolve_phone(tr, outcome.msg if outcome else "Done.")
                return
            turn.route = "claude"
            with self._lock:
                self.active = turn
            print(f"  [HYBRID] route=claude  {why}  (jev {turn.jev_ms} ms, routed {common['route_ms']} ms"
                  f"{', spec' if turn.spec else ''})  <- '{turn.text}'", flush=True)
            turn.go()
            msg, aborted = turn.cfut.result()
            total = round((time.perf_counter() - tr.t) * 1000)
            t_first = turn.first_tool_t or (time.perf_counter() if msg else None)   # an answer-only turn: the answer is the act
            fa = round((t_first - tr.t) * 1000) if t_first else None
            print(f"  [HYBRID] claude done: first action {fa} ms, total {total} ms{' (interrupted)' if aborted else ''}", flush=True)
            bus.log("brain", route="claude", steps=0, ok=not aborted and bool(turn.steps) and all(s.endswith(" ok") for s in turn.steps),
                    first_action_ms=fa, total_ms=total, interrupted=aborted, final_text=msg or "", **common)
            c.last_wake_t = time.perf_counter()
            if tr.future is not None and not tr.future.done():
                tr.future.set_result(msg or "Done.")
        except Exception as e:
            bus.log("events", kind="hybrid_route_error", err=repr(e)[:300])
            turn.abort()
        finally:
            self.brain._end()

    def _after_fast(self, d: Decision, turn: Turn, outcome):
        """Feed the fast path's act into both memories (Claude's: follow-ups like 'close it' / 'play that again')."""
        from .controller import _target_name
        a = d.action
        ok = outcome is None or outcome.ok
        tool = {"open_app": "open_app", "focus_app": "focus_app", "navigate_url": "navigate", "go_back": "go_back",
                "scroll_up": "scroll", "scroll_down": "scroll", "press_key": "press_key", "open_terminal": "open_terminal"}.get(a.intent, "media")
        arg = {"media": a.intent, "scroll": a.intent.split("_")[1]}.get(tool) or str(_target_name(a) or "")
        self.brain.memory.append({"said": turn.text[:120], "did": f"{tool}({arg[:40]}) {'ok' if ok else 'FAILED'}",
                                  "result": (outcome.msg if outcome is not None else "done")[:120]})
        c = self.ctrl
        c.recent.append({"said": turn.text[:60], "action": a.intent, "target": _target_name(a), "text": a.args.get("text"),
                         "outcome": "ok" if ok else "fail", "t": time.time()})
        c.recent = c.recent[-5:]
        if a.intent == "open_terminal" and ok and not self.brain.dry_run:
            try:                                             # Claude's run_command only types into terminals whis opened
                from .brain_claude import _fg
                self.brain._term_hwnds.add(_fg()[0])
            except Exception:
                pass

    def _local_media(self, intent, text, tr):
        """Bare media word ("pause", "volume up"): no model at all."""
        c = self.ctrl
        t = time.perf_counter()
        if getattr(self.brain, "_scrolling", None) and intent == "media_play_pause":   # "stop" while scrolling ends the scroll
            self.brain.local_media(intent, text, tr.t)
            c.last_wake_t = time.perf_counter()
            c._resolve_phone(tr, "stopped scrolling")
            return
        outcome = c.on_decision(Decision("act", Action(intent, {}, said=text), reason="local media"), tr)
        ok = outcome is None or outcome.ok
        self.brain.memory.append({"said": text[:60], "did": f"media({intent}) {'ok' if ok else 'FAILED'}", "result": intent.replace("_", " ")})
        fa = round((t - tr.t) * 1000)
        print(f"  [HYBRID] route=local  {intent}  first action {fa} ms  <- '{text}'", flush=True)
        bus.log("brain", brain="hybrid", route="local", utterance=text, intent=intent, steps=1, ok=ok, first_action_ms=fa,
                total_ms=round((time.perf_counter() - tr.t) * 1000))
        c.last_wake_t = time.perf_counter()
        c._resolve_phone(tr, intent.replace("_", " "))
