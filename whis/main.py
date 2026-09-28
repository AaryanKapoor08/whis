"""whis entry point.  python -m whis [--fake-stt] [--dry-run] [--fake-snapshot] [--no-overlay] [--no-browser]
Main thread owns the tkinter overlay; workers are daemon threads."""
import argparse, sys, threading, time, ctypes
from . import config, bus, jev
from .controller import Controller
from .types import Transcript, Snapshot, Element, Outcome


def fake_snapshot():
    els = [Element("e01", "button", "Submit", (100, 100, 200, 130)), Element("e02", "link", "Assignment 3", (100, 150, 300, 170)),
           Element("e03", "link", "Assignment 2", (100, 180, 300, 200)), Element("e04", "edit", "Search", (400, 40, 700, 70))]
    return Snapshot(0, "Google Chrome", "D2L - Assignments", els)


def _fmt(args):
    return {k: (getattr(v, "name", v)) for k, v in args.items()}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake-stt", action="store_true", help="read final transcripts from stdin")
    ap.add_argument("--dry-run", action="store_true", help="print decisions, do not execute")
    ap.add_argument("--fake-snapshot", action="store_true", help="use a canned D2L snapshot (implies no tree)")
    ap.add_argument("--no-overlay", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--phone", action="store_true", help="start the Retell webhook server on :8000 (P1)")
    ap.add_argument("--no-verify", action="store_true", help="skip X-Retell-Signature verification")
    ap.add_argument("--ambient", action="store_true", help="proactive D2L due-date nudges from D2L_ICAL_URL (A1)")
    ap.add_argument("--demo-due-in", type=float, default=None, metavar="MIN", help="inject a synthetic assignment due in MIN minutes (A2, implies --ambient)")
    ap.add_argument("--stt", choices=["auto", "whisper", "deepgram"], default="auto", help="speech backend (auto = deepgram when DEEPGRAM_API_KEY is set)")
    ap.add_argument("--brain", choices=["jev", "claude", "hybrid"], default="jev", help="decision brain: Jev classifier + recipes, a Claude tool-use agent, or hybrid (Jev fast path for simple commands, Claude for the rest)")
    ap.add_argument("--claude-model", default="claude-sonnet-5", help="Claude brain model (claude-sonnet-5 | claude-haiku-4-5-20251001 | claude-opus-5-5)")
    ap.add_argument("--claude-effort", default="low", choices=["low", "medium", "high"], help="Claude brain effort (ignored for Haiku)")
    a = ap.parse_args(argv)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    # ---- element source
    if a.fake_snapshot:
        get_snapshot = lambda: fake_snapshot()
        get_apps = lambda: ["Chrome", "Notepad", "Spotify"]
    else:
        from . import tree, apps
        tree.start()

        def _legacy_snapshot():
            if not a.no_browser:
                from . import browser
                if browser.is_foreground():
                    return browser.get_snapshot()
            return tree.get_snapshot()

        def get_snapshot():
            if a.no_browser:
                return tree.get_snapshot()
            try:                        # eyes: whis Brave in front -> shadow-DOM web elements (eyes.refresh_web), else deep UIA
                from . import eyes
                return eyes.snapshot()
            except Exception as e:
                bus.log("events", kind="eyes_snapshot_error", err=repr(e)[:200])
                return _legacy_snapshot()
        _apps_cache = {"t": 0.0, "v": []}

        def get_apps():
            if time.perf_counter() - _apps_cache["t"] > 2.0:
                try:
                    _apps_cache["v"] = apps.apps_running()
                except Exception:
                    pass
                _apps_cache["t"] = time.perf_counter()
            return _apps_cache["v"]
        if not a.no_browser:
            from . import browser
            browser.start()

    # ---- feedback + overlay bridges
    from . import feedback
    feedback.start()
    use_overlay = not a.no_overlay and not a.fake_stt
    if use_overlay:
        from . import overlay

    executor = None
    if not a.dry_run:
        from . import executor as _ex
        executor = _ex

    def on_decision(d, tr):
        if use_overlay:
            overlay.pill({"act": f"{d.action.intent.replace('_', ' ')}: {_fmt(d.action.args)}" if d.action else tr.text,
                          "confirm": "Say yes to confirm", "disambiguate": "Which one? say a number",
                          "cancel": "Cancelled", "wait": tr.text, "ignore": tr.text}.get(d.kind, tr.text),
                         {"act": "acting", "confirm": "asking", "disambiguate": "asking", "wait": "thinking"}.get(d.kind, "idle"))
        if d.kind == "act":
            feedback.ack()
            tgt = d.action.args.get("target")
            if use_overlay:
                overlay.hide()
                if tgt is not None:
                    overlay.flash(tgt.rect)
                overlay.toast(f"{d.action.intent}: {_fmt(d.action.args)}")
            if executor is None:
                print(f"  [DRY] {d.action.intent} {_fmt(d.action.args)}   <- '{tr.text}'")
                return Outcome(True, "done")
            out = executor.run(d.action, get_snapshot())
            print(f"  [ACT] {d.action.intent} {_fmt(d.action.args)} -> {out.ok} {out.msg}")
            if use_overlay and d.action.intent == "ask_screen":
                overlay.pill(out.msg, "asking")
            if not out.ok:
                feedback.say("fail")
            return out
        print(f"  [{d.kind.upper()}] {d.reason}  say={d.say}  <- '{tr.text}'")
        if d.kind == "disambiguate":
            snap = get_snapshot()
            if use_overlay and snap:
                overlay.show(snap.elements)
            feedback.say("which")
        elif d.kind in ("confirm", "cancel") and d.say:
            feedback.say(d.say)
        return None

    brain = None
    if a.brain in ("claude", "hybrid"):
        from . import brain_claude
        brain = brain_claude.ClaudeBrain(model=a.claude_model, get_snapshot=get_snapshot, get_apps=get_apps,
                                         progress=(overlay.pill if use_overlay else None), dry_run=a.dry_run,
                                         live=not a.fake_snapshot, use_browser=not a.no_browser, effort=a.claude_effort)
        brain.warm()
    ctrl = Controller(get_snapshot, get_apps, on_decision, dry_run=a.dry_run,
                      on_transcript=(overlay.pill if use_overlay else None), brain=brain,
                      hybrid=(a.brain == "hybrid"))
    from . import watchdog
    watchdog.spawn("controller", ctrl.run)
    jev.client.warm()
    if a.phone:
        from . import server
        server.start(get_snapshot, verify=not a.no_verify)
    if a.ambient or a.demo_due_in is not None:
        from . import ambient
        ambient.start(ctrl, get_snapshot, a.demo_due_in)
    print(f"whis ready. brain={a.brain}{('/' + a.claude_model) if brain else ''} provider={jev.client.provider['name']} jev_warm={jev.client.last_latency_ms and round(jev.client.last_latency_ms)}ms"
          f"{' phone=:%d' % config.SERVER_PORT if a.phone else ''}{' ambient' if (a.ambient or a.demo_due_in is not None) else ''}")

    if a.fake_stt:
        time.sleep(2.0 if not a.fake_snapshot else 0)   # let tree/browser warm
        uid = 0
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            uid += 1
            print(f"> {line}")
            bus.transcript_q.put(Transcript(line, True, uid, source="fake"))
            if brain is not None:           # Claude turns take several seconds: send the next line when it is done
                time.sleep(0.5); brain.wait_idle(90); time.sleep(1.0)
            else:
                time.sleep(3.0)
        time.sleep(1 if brain is not None else 4)
        if a.phone or a.ambient or a.demo_due_in is not None:
            print("fake-stt input done; serving phone/ambient until Ctrl+C")
            try:
                while not bus.stop.is_set():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
        bus.stop.set()
        return

    backend = a.stt if a.stt != "auto" else ("deepgram" if config.DEEPGRAM_API_KEY else "whisper")
    import importlib
    stt = importlib.import_module(".stt_deepgram" if backend == "deepgram" else ".stt", __package__)
    print(f"stt backend: {backend}")
    feedback.bind_stt(stt.pause, stt.resume)
    stt.start()
    feedback.say("ready")
    if use_overlay:
        overlay.run_mainloop()
    else:
        while not bus.stop.is_set():
            time.sleep(0.5)


if __name__ == "__main__":
    main()
