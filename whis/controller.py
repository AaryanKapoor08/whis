"""Transcript -> Jev -> policy -> decision. Debounce, in-flight cap, epoch invalidation, silence timers,
wake word + follow-up window, chaining (utterance stays open after an act), pending confirm, overlay state.
Ported from jev-voice-browser controller.js + jev-voice main.py."""
import threading, time, queue, re, sys
from concurrent.futures import ThreadPoolExecutor
from . import config, bus, spans, questions, policy, jev
from .types import Transcript, Decision, Action


class Ctx:
    """Snapshot of everything policy needs for one decision (built per Jev call)."""
    def __init__(self, c: "Controller", transcript: str, final: bool):
        self.transcript = transcript
        self.final = final
        self.silent_ms = (time.perf_counter() - c.last_transcript_t) * 1000
        self.snapshot = c.get_snapshot()
        self.recent = c.recent
        self.pending = c.pending
        self.pending_desc = c.pending.intent if c.pending else None
        self.named = c.named_active()
        self.apps_running = c.get_apps_running()
        self.overlay_n = c.overlay_n


class Controller:
    def __init__(self, get_snapshot, get_apps_running, on_decision, dry_run=False, on_transcript=None, brain=None, hybrid=False):
        self.brain = brain                         # brain_claude.ClaudeBrain for `--brain claude`; None = Jev pipeline
        self._claude_pool = ThreadPoolExecutor(max_workers=1) if brain is not None else None
        self._media_utt = -1
        self.on_transcript_cb = on_transcript      # UI hook: (text, state)
        self.get_snapshot = get_snapshot
        self.get_apps_running = get_apps_running
        self.on_decision = on_decision          # callable(decision, transcript) -> Outcome|None (executor bridge)
        self.dry_run = dry_run
        self.recent: list[dict] = []
        self.pending: Action | None = None
        self.overlay_n = 0
        self.epoch = 0
        self.utterance_id = -1
        self.consumed_chars = 0
        self.last_transcript_t = time.perf_counter()
        self.last_wake_t = 0.0
        self.current: Transcript | None = None
        self._pool = ThreadPoolExecutor(max_workers=config.MAX_INFLIGHT)
        self._inflight = 0
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._tlock = threading.Lock()
        self._chain: list[str] = []
        self._woke_utt = -1
        self._phone_msgs: list[str] = []     # outcomes of earlier clauses of one phone command
        self._last_act = None                # (action key, perf time, utterance id): double-act guard
        self.router = None                   # `--brain hybrid`: router.Router picks Jev fast path or the Claude brain
        if hybrid and brain is not None:
            from .router import Router
            self.router = Router(self, brain)

    def set_pending(self, action: Action | None):
        """Used by ambient nudges: the next 'yes' runs this action."""
        self.pending = action
        if action is not None:
            self.last_wake_t = time.perf_counter()    # a nudge opens the follow-up window for the answer

    # ---- wake / addressing
    def named_active(self):
        return (time.perf_counter() - self.last_wake_t) < config.FOLLOWUP_S

    # ---- entry
    def run(self):
        while not bus.stop.is_set():
            try:
                tr: Transcript = bus.transcript_q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._on_transcript(tr)

    def _on_transcript(self, tr: Transcript):
        srv = sys.modules.get("whis.server")      # only loaded with --phone (no fastapi import otherwise)
        if tr.source != "phone" and srv and srv.call_active():
            return                                # the laptop mic hears the caller through the speaker: phone owns the turn
        if self.router is not None:
            return self.router.on_transcript(tr)
        if self.brain is not None:
            return self._on_transcript_claude(tr)
        if tr.utterance_id != self.utterance_id:
            self.utterance_id = tr.utterance_id
            self.consumed_chars = 0
        self.last_transcript_t = tr.t
        named, text = spans.strip_wake(tr.text, config.WAKE_WORDS)
        if named:
            self.last_wake_t = time.perf_counter()
        rest = text[self.consumed_chars:]
        lead = re.match(r"[\s,.;?!]*(?:(?:and then|after that|and|then)\b[\s,]*)?", rest, re.I).end()
        self.consumed_chars += lead               # separator between acted words and the rest counts as consumed
        text = rest[lead:].strip()
        if not getattr(tr, "_chained", False):
            clauses = spans.split_clauses(text)
            if tr.final:
                text, self._chain = clauses[0], clauses[1:]   # a new sentence replaces any stalled leftover chain
            elif len(clauses) > 1:
                text = clauses[0]                  # partial: decide (and consume) only the first clause
        if tr.source == "phone":
            self.last_wake_t = time.perf_counter()   # phone is always addressed
            if not getattr(tr, "_chained", False):
                self._phone_msgs = []
        self.current = Transcript(text, tr.final, tr.utterance_id, tr.t, tr.source, tr.future)
        if named and tr.utterance_id != self._woke_utt:
            self._woke_utt = tr.utterance_id
            from . import feedback
            feedback.pop()
            if self.on_transcript_cb:
                self.on_transcript_cb("Go ahead, I'm listening…", "listening")
        if self.on_transcript_cb and text:
            addressed = named or self.named_active() or tr.source == "phone"
            self.on_transcript_cb(text, ("thinking" if tr.final else "listening") if addressed else "hearing")
        if not text:
            self._resolve_phone(tr, "notcmd")
            return
        # local fast path: a bare media word ("pause", "volume down") acts with no Jev call. Low-risk, so it is
        # accepted even unaddressed on finals; on partials only for words that cannot start a longer command
        # ("play" may be "play tame impala"); the toggle debounce in _apply absorbs repeated partials.
        bare = re.sub(r"[^a-z ]", " ", text.lower()).split()
        bare = " ".join(dict.fromkeys(bare)) if len(set(bare)) == 1 else " ".join(bare)   # "pause pause" -> "pause"
        if bare in config.MEDIA_WORDS and (tr.final or bare not in ("play", "stop", "resume")):
            intent = config.MEDIA_WORDS[bare]
            if intent:
                self.epoch += 1
                bus.log("decisions", transcript=text, final=True, kind="act", reason="local media", intent=intent, jev_ms=0, total_ms=0)
                self._apply(Decision("act", Action(intent, {}, said=text), reason="local media"), self.current, Ctx(self, text, True))
                return
        self.epoch += 1
        if tr.final:
            self._decide_now(self.current)     # finals (incl. chained/phone clauses) dispatch now: a mic partial
        else:                                  # arriving a moment later must not cancel their timer
            self._schedule(config.DEBOUNCE_MS / 1000)

    # ---- Claude brain: gate here (wake word / follow-up window / command heuristic / media fast path / yes-no), then
    # hand the whole FINAL utterance to brain_claude (partials never reach Claude)
    def _on_transcript_claude(self, tr: Transcript):
        from . import brain_claude
        self.utterance_id = tr.utterance_id
        self.last_transcript_t = tr.t
        named, text = spans.strip_wake(tr.text, config.WAKE_WORDS)
        if named:
            self.last_wake_t = time.perf_counter()
            if tr.utterance_id != self._woke_utt:
                self._woke_utt = tr.utterance_id
                from . import feedback
                feedback.pop()
                if self.on_transcript_cb:
                    self.on_transcript_cb("Go ahead, I'm listening…", "listening")
        if tr.source == "phone":
            self.last_wake_t = time.perf_counter()
        addressed = named or self.named_active() or tr.source == "phone"
        text = text.strip()
        if self.on_transcript_cb and text:
            self.on_transcript_cb(text, ("thinking" if tr.final else "listening") if addressed else "hearing")
        if not text:
            if tr.final:
                self._resolve_phone(tr, "notcmd")
            return
        bare = re.sub(r"[^a-z ]", " ", text.lower()).split()
        bare = " ".join(dict.fromkeys(bare)) if len(set(bare)) == 1 else " ".join(bare)
        if bare in config.MEDIA_WORDS and (tr.final or bare not in ("play", "stop", "resume")) and not self.brain.pending:
            if tr.utterance_id != self._media_utt:          # once per utterance (partial + final of the same "pause")
                self._media_utt = tr.utterance_id
                self._claude_submit(self.brain.local_media, tr, config.MEDIA_WORDS[bare], text, tr.t)
            return
        if not tr.final:
            return
        if self.brain.pending:
            yn = brain_claude.yes_no(text)
            if yn is not None:
                self._claude_submit(self.brain.confirm, tr, yn, text, tr.t)
                return
            self.brain.pending = None                        # a new command supersedes the pending one
        if not (addressed or brain_claude.looks_like_command(text) or _request_like(text)):
            print(f"  [IGNORE] not addressed / not a command  <- '{text}'", flush=True)
            bus.log("brain", brain="claude", utterance=text, steps=[], total_ms=0, first_action_ms=None, final_text="",
                    model="gate", input_tokens=0, output_tokens=0, stop="ignored", ok=False)
            self._resolve_phone(tr, "notcmd")
            return
        self.last_wake_t = time.perf_counter()
        if self.brain.busy:
            self.brain.cancel.set()                          # the running task stops at its next step
        self._claude_submit(self.brain.handle, tr, text, tr.t)

    def _claude_submit(self, fn, tr, *args):
        def job():
            try:
                msg = fn(*args)
            except Exception as e:
                bus.log("events", kind="claude_brain_job_error", err=repr(e)[:300]); msg = "Couldn't do that."
            self.last_wake_t = time.perf_counter()           # follow-up window after the answer
            if tr.future is not None and not tr.future.done():
                tr.future.set_result(msg or "Done.")
        self.brain._begin()                                  # busy from submit time (wait_idle in --fake-stt)
        fut = self._claude_pool.submit(job)
        fut.add_done_callback(lambda _: self.brain._end())

    def _schedule(self, delay, tr: Transcript | None = None):
        with self._tlock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self._decide_now, args=(tr,))
            self._timer.daemon = True
            self._timer.start()

    # ---- decision
    def _decide_now(self, tr: Transcript | None = None):
        tr = tr or self.current
        if tr is None:
            return
        with self._lock:
            if self._inflight >= config.MAX_INFLIGHT:
                self._schedule(config.DEBOUNCE_MS / 1000, tr if tr.final else None)   # retry the same final; partials take the newest text
                return
            self._inflight += 1
        epoch = self.epoch
        self._pool.submit(self._ask_and_apply, tr, epoch)

    def _ask_and_apply(self, tr: Transcript, epoch: int):
        t0 = time.perf_counter()
        try:
            ctx = Ctx(self, tr.text, tr.final)
            state, qs = questions.build(ctx)
            own = tr.source == "phone" or getattr(tr, "_chained", False)
            ans = jev.client.ask(state, qs, epoch, (lambda: epoch) if own else (lambda: self.epoch))
            if ans is None:
                if tr.future is not None:
                    self._chain = []
                    self._resolve_phone(tr, "notcmd")
                return
            d = policy.decide(ans, ctx)
            bus.log("decisions", transcript=tr.text, final=tr.final, kind=d.kind, reason=d.reason,
                    intent=(d.action.intent if d.action else None), jev_ms=round(jev.client.last_latency_ms or 0),
                    total_ms=round((time.perf_counter() - t0) * 1000))
            if self._should_escalate(d, tr, ans, ctx):
                if self._escalate(state, tr, ctx):
                    return
            self._apply(d, tr, ctx)
        except Exception as e:
            bus.log("events", kind="controller_error", err=repr(e)[:300])
        finally:
            with self._lock:
                self._inflight -= 1

    # ---- System-2 escalation (Claude Haiku planner) when Jev is unsure about a final, addressed command
    def _should_escalate(self, d, tr, ans, ctx):
        if not tr.final or self.pending is not None:
            return False
        if d.kind == "act" and d.action and d.action.intent in config.FREE_TEXT_INTENTS:
            txt = (d.action.args.get("text") or "").strip(" .?!").lower()
            return bool(txt) and txt == tr.text.strip(" .?!").lower()      # span == whole sentence: Jev didn't understand it
        if d.kind not in ("ignore", "wait"):
            return False
        # "stop it" (cancel, nothing pending) went to the planner and pressed Ctrl+C in the terminal running Claude Code
        if d.reason in ("not a command", "not addressed", "pending: not yes/no", "confirm/cancel with nothing pending") \
                or d.reason == "no app" or d.reason.endswith("not said"):   # unknown app: planner open_app is filtered anyway
            return False
        is_cmd = float(ans.get("is_command", {}).get("noul", 0))
        iconf = float((ans.get("intent") or {}).get("confidence", 0))
        addressed = ctx.named or float(ans.get("addressed", {}).get("noul", 1.0)) >= config.T["addressed"]
        # vague speech ("open a new project": top intent 0.19) is not worth 1-4 s of planner; it pressed Ctrl+T live
        return (is_cmd >= config.T["isCommand"] and addressed and len(tr.text.split()) >= 2
                and iconf >= config.T.get("escalateIntent", 0.3))

    def _escalate(self, state, tr, ctx) -> bool:
        from . import planner
        if self.on_transcript_cb:
            self.on_transcript_cb(tr.text, "thinking")
        acts, say = planner.plan(state)
        if not acts:
            return False
        snap = ctx.snapshot
        acts = [a for a in acts if a.intent in config.CLOSED_INTENTS | config.FREE_TEXT_INTENTS and a.intent not in ("confirm", "cancel")
                and _planner_ok(a, tr.text)]
        if not acts:
            return False
        chain, self._chain = self._chain, []
        for a in acts:
            if a.intent == "click_element":
                el = snap.by_id(str(a.args.get("target", ""))) if snap else None
                if el is None:
                    continue
                a.args["target"] = el
            kind = "confirm" if a.intent in config.ALWAYS_CONFIRM else "act"
            self._apply(Decision(kind, a, say=("confirm_generic" if kind == "confirm" else ""), reason="planner"), tr, ctx)
            if kind == "confirm":
                return True
            time.sleep(0.4)
        self._chain = chain
        self._next_clause(tr)
        return True

    def _apply(self, d: Decision, tr: Transcript, ctx: Ctx):
        if d.kind == "wait":
            if not tr.final:
                # re-evaluate once the next silence threshold passes (free-text 600 ms, closed-set 900 ms);
                # past both, stop re-asking and let the next partial/final drive it (no tight Jev loop)
                nxt = next((m for m in (config.SILENCE_FREE_MS, config.SILENCE_CLOSED_MS) if ctx.silent_ms < m), None)
                if nxt is not None:
                    self._schedule((nxt - ctx.silent_ms) / 1000 + 0.02)
            else:
                self._resolve_phone(tr, "notcmd")
            return
        if d.kind == "ignore":
            if tr.final:
                _log_jev(tr, d, None)
            self._resolve_phone(tr, "notcmd")
            self._next_clause(tr)
            return
        if d.kind == "cancel":
            self.pending = None
            self._close(tr)
            self.on_decision(d, tr)
            _log_jev(tr, d, None)
            self._resolve_phone(tr, "Cancelled.")
            return
        if d.kind == "confirm":
            self.pending = d.action
            self._close(tr)
            self.on_decision(d, tr)
            _log_jev(tr, d, None)
            self._chain = []          # a confirm interrupts the chain (clear before resolving the phone reply)
            self._resolve_phone(tr, config.PHRASES.get(d.say, d.say))
            return
        if d.kind == "disambiguate":
            snap = ctx.snapshot
            self.pending = None
            self.overlay_n = len(snap.elements) if snap else 0
            self._chain = []
            self._close(tr)
            self.on_decision(d, tr)
            _log_jev(tr, d, None)
            names = ", ".join(f"{i+1} {e.name[:25]}" for i, e in enumerate(snap.elements[:8])) if snap else ""
            self._resolve_phone(tr, f"Which one? {names}")
            return
        if d.kind == "act":
            last = self.recent[-1] if self.recent else None
            if last and last["action"] == d.action.intent and d.action.intent in ("media_play_pause", "volume_up", "volume_down")                     and time.time() - last["t"] < 1.5 and not tr.final:
                return                                   # same toggle from a later partial of the same phrase
            # two in-flight answers for the same partial ("Please open Spotify." x2, 20 ms apart) both acted live:
            # the same action for the same utterance within 2 s runs once (checked+recorded before executing)
            key = (d.action.intent, repr(sorted((k, _target_name(d.action) if k == "target" else str(v)) for k, v in d.action.args.items())))
            with self._lock:
                prev = self._last_act
                same_utt = prev and prev[2] == tr.utterance_id   # "I'll play that" / "Play that" = two utterances, one act
                if prev and prev[0] == key and (same_utt or d.action.intent in config.FREE_TEXT_INTENTS | {"open_app", "focus_app"}) \
                        and time.perf_counter() - prev[1] < (2.0 if same_utt else 4.0) \
                        and not getattr(tr, "_chained", False) and d.reason != "confirmed":
                    return
                self._last_act = (key, time.perf_counter(), tr.utterance_id)
            self.pending = None
            self.overlay_n = 0
            self._close(tr)
            self.epoch += 1                 # drop any in-flight answers for the consumed text
            outcome = self.on_decision(d, tr)
            _log_jev(tr, d, outcome)
            self.recent.append({"said": tr.text[:60], "action": d.action.intent,
                                "target": _target_name(d.action), "text": d.action.args.get("text"), "outcome": ("ok" if (outcome is None or outcome.ok) else "fail"),
                                "t": time.time()})
            for r in self.recent:
                r["seconds_ago"] = int(time.time() - r["t"])
            self.recent = self.recent[-5:]
            self._resolve_phone(tr, (outcome.msg if outcome else "Done."))
            self._next_clause(tr)

    def _close(self, tr: Transcript):
        """Chaining: keep the utterance open but consume the words already acted on."""
        self.consumed_chars += len(tr.text)
        self.last_wake_t = time.perf_counter()   # acting counts as being addressed (follow-up window)

    def _next_clause(self, tr: Transcript):
        """Run the next queued clause of a multi-command sentence."""
        if not self._chain or not tr.final:
            return
        nxt = self._chain.pop(0)
        self.consumed_chars = 0
        t2 = Transcript(nxt, True, tr.utterance_id, source=tr.source, future=tr.future)
        t2._chained = True
        self._on_transcript(t2)

    def _resolve_phone(self, tr: Transcript, text: str):
        """Phone commands get one spoken reply for the whole sentence: collect per-clause outcomes,
        resolve when the last clause is done (or when a confirm/disambiguate stops the chain)."""
        if tr.future is None or tr.future.done():
            return
        text = config.PHRASES.get(text, text) or ""
        if self._chain:                      # more clauses coming for this future
            if text and text not in ("I didn't catch a command.",):
                self._phone_msgs.append(text)
            return
        msgs = self._phone_msgs + ([text] if text else [])
        self._phone_msgs = []
        tr.future.set_result(". ".join(m.rstrip(".") for m in msgs) + "." if msgs else "Done.")


_KEY_WORDS = re.compile(r"\b(?:press|hit|key|keys|enter|escape|esc|tab|backspace|control|ctrl|shortcut|undo|paste|copy|new tab|close (?:the )?tab)\b", re.I)


def _planner_ok(a: Action, said: str) -> bool:
    """Planner output guard (live misses: 'go to the file' -> Ctrl+L, 'open a new project' -> Ctrl+T,
    'stop it' -> Ctrl+C in the Claude Code terminal, 'Camera. Open camera' -> launched Camera)."""
    if a.intent == "press_key":
        return a.args.get("key") in config.KEYS and bool(_KEY_WORDS.search(said))
    if a.intent in ("open_app", "focus_app"):
        app = str(a.args.get("app") or "").lower()
        known = app in config.APPS or app in config.APP_PROCS or app in config.BROWSER_NAMES
        return known and policy.app_mentioned(app, said)
    if a.intent == "run_command":
        cmd = policy._command(str(a.args.get("text") or ""))
        if not cmd:
            return False
        a.args["text"] = cmd
    if a.intent in ("play_song", "search_in_app", "search_web") and a.args.get("text"):
        t = policy._clean(str(a.args["text"]), a.intent)
        a.args["text"] = policy._song(t) if a.intent == "play_song" else t
        return bool(a.args["text"])
    return True


def _log_jev(tr: Transcript, d: Decision, outcome):
    """One logs/brain.jsonl line per handled Jev decision, comparable with the Claude brain's lines (A/B)."""
    a = d.action
    ok = (outcome is None or outcome.ok) if d.kind == "act" else False
    bus.log("brain", brain="jev", utterance=tr.text, kind=d.kind, reason=d.reason, intent=(a.intent if a else None),
            args=({k: str(getattr(v, "name", v))[:80] for k, v in a.args.items()} if a else {}), ok=ok,
            result=(outcome.msg[:200] if outcome is not None else ""), total_ms=round((time.perf_counter() - tr.t) * 1000))


_REQUEST = re.compile(r"\b(?:i (?:want|need) you to|i'?d like you to|can you|could you|would you|will you|please|go ahead and)"
                      r"\s+(?:please\s+|just\s+|now\s+)?(\w+)", re.I)


def _request_like(text: str) -> bool:
    """'Okay, now on D2L, I want you to open CS3873.' - a request aimed at the computer whose verb isn't the first word
    (brain_claude.looks_like_command only checks the first word)."""
    from . import brain_claude
    return any(m.group(1).lower() in brain_claude._VERBS for m in _REQUEST.finditer(text))


def _target_name(a: Action):
    t = a.args.get("target")
    return t.name if t is not None and hasattr(t, "name") else (a.args.get("app") or a.args.get("key") or a.args.get("url") or a.args.get("text"))
