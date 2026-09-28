"""Rehearsal: push utterances through the real decision pipeline (controller -> questions -> Jev -> policy) with a
fake D2L snapshot and a recording executor. Nothing is executed, no app opens, focus never moves. Planner
escalations are recorded (and count as a FAIL: they cost 2-4 s live) but not called unless --planner.

    .venv\\Scripts\\python.exe scripts\\rehearse.py [--only TEXT] [--repeat N] [--planner] [-v]
"""
import sys, os, time, argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from whis import config                                   # noqa: E402
config.LOG_DIR = os.path.join("logs", "rehearse")          # keep the live logs clean
from whis import controller as C, policy, jev, feedback    # noqa: E402
from whis.types import Transcript, Outcome                 # noqa: E402
from whis.main import fake_snapshot                        # noqa: E402

feedback.pop = feedback.ack = lambda *a, **k: None        # no sounds
BROWSER = tuple(config.BROWSER_NAMES)
D2L = config.BOOKMARKS["d2l"]
PITCH = ("open spotify, play tame impala, then open browser, open d2l, look if I have an assignment left, "
         "then open vs code, open the terminal in it and run claude code")

# (utterance, expected acts). Each act: (intent, {arg: value | tuple of accepted values}).
# A leading "confirm:" / "disambiguate:" on the intent expects that decision kind instead of an act.
CASES = [
    # --- pitch, clause by clause
    ("open spotify", [("open_app", {"app": "spotify"})]),
    ("play tame impala", [("play_song", {"text": "tame impala"})]),
    ("then open browser", [(("open_app", "focus_app"), {"app": BROWSER})]),
    ("open d2l", [("navigate_url", {"url": D2L})]),
    ("look if I have an assignment left", [("ask_screen", {})]),
    ("then open vs code", [("open_app", {"app": ("vs code", "code")})]),
    ("open the terminal in it", [("open_terminal", {})]),
    ("and run claude code", [("run_command", {"text": "claude"})]),
    # --- pitch, one breath
    (PITCH, [("open_app", {"app": "spotify"}), ("play_song", {"text": "tame impala"}),
             (("open_app", "focus_app"), {"app": BROWSER}), ("navigate_url", {"url": D2L}), ("ask_screen", {}),
             ("open_app", {"app": ("vs code", "code")}), ("open_terminal", {}), ("run_command", {"text": "claude"})]),
    # --- misses from the live logs
    ("Now play a Tame Impala song for me.", [("play_song", {"text": "tame impala"})]),
    ("Okay, now play a Tame Impala song for me", [("play_song", {"text": "tame impala"})]),
    ("In Spotify, I need you to search for Tame Impala.", [("search_in_app", {"text": "tame impala"})]),
    ("Run Claude Code and terminal", [("run_command", {"text": "claude"})]),
    ("Run cloud code and terminal", [("run_command", {"text": "claude"})]),
    ("run claude code in the terminal", [("run_command", {"text": "claude"})]),
    ("Go to D2L in Brave. Open Notepad", [("navigate_url", {"url": D2L}), ("open_app", {"app": "notepad"})]),
    ("Pause. Pause", [("media_play_pause", {})]),
    ("open google", [("navigate_url", {"url": config.BOOKMARKS["google"]})]),
    ("open the browser", [(("open_app", "focus_app"), {"app": BROWSER})]),
    ("search for hack atlantic.", [("search_web", {"text": "hack atlantic"})]),
    ("can you write hello in it", [("type_text", {"text": "hello"})]),
    ("open notepad and go to d2l and type hello there", [("open_app", {"app": "notepad"}),
                                                         ("navigate_url", {"url": D2L}), ("type_text", {"text": "hello there"})]),
    ("switch to spotify", [(("focus_app", "open_app"), {"app": "spotify"})]),
    ("volume down", [("volume_down", {})]),
    ("click submit", [("confirm:click_element", {})]),
    # --- must NOT act (chit-chat / vague speech the planner used to turn into key presses or apps)
    ("so anyway I think we should get lunch", []),
    ("Thank you", []),
    ("Hello, are you there?", []),
    ("That's what I'm trying to do", []),
    ("Thanks for listening to Tablet", []),
    ("Camera. Open camera", []),
    ("open a new project", []),
    ("go to the file", []),
    ("stop it", []),
]


def _norm(v):
    v = getattr(v, "name", v)
    return str(v).strip().lower() if v is not None else ""


def _match(got, want):
    if len(got) != len(want):
        return False
    for (gi, ga), (wi, wa) in zip(got, want):
        if gi not in (wi if isinstance(wi, tuple) else (wi,)):
            return False
        for k, v in wa.items():
            ok = v if isinstance(v, tuple) else (v,)
            if _norm(ga.get(k)) not in [_norm(x) for x in ok]:
                return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="run cases whose utterance contains this text")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--planner", action="store_true", help="actually call the planner on escalation")
    ap.add_argument("-v", action="store_true", help="print every decision")
    a = ap.parse_args()

    log = []
    orig = policy.decide

    def decide(ans, ctx):
        d = orig(ans, ctx)
        log.append(("dec", ctx.transcript, d, jev.client.last_latency_ms or 0))
        return d
    policy.decide = decide

    if not a.planner:
        def _esc(self, state, tr, ctx):
            log.append(("esc", tr.text, None, 0))
            return False
        C.Controller._escalate = _esc

    def on_decision(d, tr):
        log.append(("on", tr.text, d, 0))
        return Outcome(True, "done") if d.kind == "act" else None

    ctrl = C.Controller(fake_snapshot, lambda: ["Spotify", "Code", "brave"], on_decision, dry_run=True)
    jev.client.warm()

    def idle():
        t = ctrl._timer
        return ctrl._inflight == 0 and not ctrl._chain and not (t and t.is_alive())

    rows, fails, uid = [], 0, 0
    cases = [c for c in CASES if not a.only or a.only.lower() in c[0].lower()] * a.repeat
    for utt, want in cases:
        log.clear()
        ctrl.pending, ctrl.overlay_n, ctrl._chain, ctrl._last_act = None, 0, [], None   # cases are independent (the cross-utterance double-act guard would merge near-identical ones)
        ctrl.last_wake_t = time.perf_counter()     # inside the follow-up window, as mid-demo
        uid += 1
        t0 = time.perf_counter()
        ctrl._on_transcript(Transcript(utt, True, uid, source="fake"))
        quiet = 0
        while quiet < 4 and time.perf_counter() - t0 < 20:
            time.sleep(0.05)
            quiet = quiet + 1 if idle() else 0
        wall = (time.perf_counter() - t0) * 1000 - 200
        got = []
        for kind, _t, d, _ in log:
            if kind == "on" and d.action is not None:
                intent = d.action.intent if d.kind == "act" else f"{d.kind}:{d.action.intent}"
                got.append((intent, d.action.args))
            elif kind == "on" and d.kind != "act":
                got.append((d.kind, {}))
        esc = [t for k, t, _, _ in log if k == "esc"]
        jms = [round(ms) for k, _, _, ms in log if k == "dec"]
        ok = _match(got, want) and not esc
        fails += not ok
        gs = "; ".join(f"{i} {({k: _norm(v)[:24] for k, v in g.items()})}" if g else i for i, g in got) or "-"
        ws = "; ".join(f"{'/'.join(i) if isinstance(i, tuple) else i}" for i, _ in want) or "-"
        rows.append((("PASS" if ok else "FAIL"), utt, gs + (f"  [PLANNER x{len(esc)}]" if esc else ""), ws,
                     max(jms) if jms else 0, round(wall)))
        if a.v or not ok:
            for k, t, d, ms in log:
                if k == "dec":
                    print(f"     . {t[:50]!r} -> {d.kind} {d.action.intent if d.action else ''} ({d.reason}) {round(ms)}ms")
        print(f"{rows[-1][0]} {utt[:60]!r:64} -> {rows[-1][2][:110]}  jev {rows[-1][4]}ms")

    print("\n" + "=" * 120)
    print(f"{'':4} {'utterance':44} {'got':60} {'jev':>5} {'wall':>6}")
    for st, utt, gs, ws, jm, wall in rows:
        print(f"{st:4} {utt[:44]:44} {gs[:60]:60} {jm:5} {wall:6}")
        if st == "FAIL":
            print(f"{'':49}expected: {ws}")
    print(f"\n{len(rows) - fails}/{len(rows)} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
