"""A/B evaluation of whis brains (Jev vs Claude) on REAL desktop tasks.

For each brain: ONE `python -m whis --fake-stt --brain <b>` process is kept alive across scenarios (no model reloads);
utterances are written to its stdin with realistic gaps; success is verified from the OUTSIDE (scripts/scenarios.py:
foreground window, Notepad text, Spotify now-playing bar, Brave address bar, VS Code panel, PSReadLine history, volume).
Per-utterance latency / steps / tokens come from logs/brain.jsonl.

    .venv\\Scripts\\python.exe scripts\\ab_test.py --brain both                      # live: drives the desktop!
    .venv\\Scripts\\python.exe scripts\\ab_test.py --brain claude --claude-model claude-haiku-4-5 --only pitch,g_
    .venv\\Scripts\\python.exe scripts\\ab_test.py --brain jev --repeat 3
    .venv\\Scripts\\python.exe scripts\\ab_test.py --brain jev --dry-run            # plumbing only: nothing executes
    .venv\\Scripts\\python.exe scripts\\ab_test.py --list

Output: logs/ab_results.md (scenario x brain table + totals per brain per difficulty) and logs/ab_results.json (every
run). Running one brain keeps the other brain's previous runs in the files, so `--brain jev` then `--brain claude`
still produces one combined table.

SAFETY: the harness never types (all input goes through whis), never closes a window, and never kills anything but
its own whis child. At the end it pauses Spotify (UIA Pause button) and restores the master volume. Live runs steal
focus: don't use the PC meanwhile, and never run two live UI automations at once.
"""
import os, re, sys, json, time, argparse, statistics, subprocess, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)
import scenarios as S                                     # noqa: E402

PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
LOGS = os.path.join(ROOT, "logs")
BRAIN_LOG = os.path.join(LOGS, "brain.jsonl")
DECISIONS_LOG = os.path.join(LOGS, "decisions.jsonl")
OUT_MD, OUT_JSON = os.path.join(LOGS, "ab_results.md"), os.path.join(LOGS, "ab_results.json")
STEP_TIMEOUT = 20.0      # s per intermediate utterance (brain record / step check)
SETTLE = 8.0             # s the final check keeps polling after the last brain record


# ============================================================ log tailing
class Tail:
    """New JSON lines appended to a file since construction."""

    def __init__(self, path):
        self.path = path
        self.pos = os.path.getsize(path) if os.path.exists(path) else 0
        self.buf = ""

    def read(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        size = os.path.getsize(self.path)
        if size < self.pos:
            self.pos = 0                                    # truncated / rotated
        if size == self.pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read()
        self.pos += len(data)
        self.buf += data.decode("utf-8", "replace")
        lines, self.buf = self.buf.split("\n")[:-1], self.buf.split("\n")[-1]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return out


# ============================================================ whis child process
class Whis:
    def __init__(self, brain, model, dry, tag):
        src = open(os.path.join(ROOT, "whis", "main.py"), encoding="utf-8").read()
        cmd = [PY, "-u", "-m", "whis", "--fake-stt", "--no-overlay"]
        if "--brain" in src:
            cmd += ["--brain", brain]
            if brain in ("claude", "hybrid") and model:
                if "--claude-model" not in src:
                    raise SystemExit("whis/main.py has no --claude-model flag yet")
                cmd += ["--claude-model", model]
        elif brain != "jev":
            raise SystemExit("whis/main.py has no --brain flag yet (the Claude brain isn't merged)")
        if dry:
            cmd += ["--dry-run", "--fake-snapshot", "--no-browser"]   # no tree, no Brave: can't collide with a live whis
        self.cmd, self.lines, self.lock = cmd, [], threading.Lock()
        self.logf = open(os.path.join(LOGS, f"ab_whis_{tag}.log"), "a", encoding="utf-8")
        self.logf.write(f"\n===== {time.strftime('%H:%M:%S')} {' '.join(cmd)}\n")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.p.stdout:
            line = line.rstrip("\n")
            with self.lock:
                self.lines.append((time.time(), line))
            self.logf.write(line + "\n"); self.logf.flush()

    def since(self, t0) -> list[tuple[float, str]]:
        with self.lock:
            return [x for x in self.lines if x[0] >= t0]

    def wait_ready(self, timeout=120.0):
        t = time.time()
        while time.time() - t < timeout:
            if any("whis ready" in l for _, l in self.since(t - 1e9)):
                return True
            if self.p.poll() is not None:
                break
            time.sleep(0.2)
        tail = "\n".join(l for _, l in self.since(0)[-15:])
        raise SystemExit(f"whis did not become ready (exit={self.p.poll()}):\n{tail}")

    def say(self, text):
        self.p.stdin.write(text + "\n"); self.p.stdin.flush()

    def stop(self):
        try:
            self.p.stdin.close()                           # EOF -> whis drains its queue and exits cleanly
        except Exception:
            pass
        try:
            self.p.wait(25)
        except subprocess.TimeoutExpired:
            self.p.terminate()
            try:
                self.p.wait(5)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.logf.close()


# ============================================================ one scenario
def _tokens(rec) -> int:
    if isinstance(rec.get("input_tokens"), (int, float)) or isinstance(rec.get("output_tokens"), (int, float)):
        return int(rec.get("input_tokens") or 0) + int(rec.get("output_tokens") or 0)   # input already includes cache r/w
    n = 0
    for k, v in rec.items():
        if isinstance(v, (int, float)) and "token" in k.lower():
            n += int(v)
        elif isinstance(v, dict):
            n += _tokens(v)
    return n


def _steps(rec) -> int:
    s = rec.get("steps")
    if isinstance(s, list):
        return len(s)
    if isinstance(s, int):
        return s
    return 1 if rec.get("intent") else 0


def _safe(check, ctx):
    try:
        return check(ctx)
    except Exception as e:
        return False, f"check error {e!r}"


def run_scenario(w: Whis, sc: S.Scenario, brain: str, a) -> dict:
    ctx = {"dry": a.dry_run, "stdout": w.since}
    t_setup = time.time()
    try:
        sc.setup(ctx)
    except Exception as e:
        print(f"    setup error {e!r}")
    time.sleep(0.2 if a.dry_run else 1.0)
    brain_tail, dec_tail = Tail(BRAIN_LOG), Tail(DECISIONS_LOG)
    brain_recs, dec_recs = [], []
    t0 = ctx["t0"] = time.time()
    scale = a.timeout_scale
    failed_step, step_details, t_pass, detail = None, [], None, ""

    def pump():
        brain_recs.extend(brain_tail.read()); dec_recs.extend(dec_tail.read())

    for i, utt in enumerate(sc.utterances):
        text = f"whis, {utt}" if (a.wake == "all" or (a.wake == "first" and i == 0)) else utt
        n_b, n_d = len(brain_recs), len(dec_recs)
        t_send = time.time()
        w.say(text)
        print(f"    > {text}")
        last = i == len(sc.utterances) - 1
        step_check = None if last else (sc.step_checks[i] if i < len(sc.step_checks) else None)
        deadline = (t0 + sc.timeout * scale) if last else (t_send + STEP_TIMEOUT * scale)
        if a.dry_run:
            deadline = t_send + 30.0
        t_rec, seen = None, 0
        while time.time() < deadline:
            time.sleep(0.4)
            pump()
            cnt = (len(brain_recs) - n_b) + (0 if a.brain_log else len(dec_recs) - n_d)
            got = cnt > 0
            if cnt > seen:                 # a chained utterance logs one record per clause: settle from the LAST one
                seen, t_rec = cnt, time.time()
            if a.dry_run:
                if t_rec and time.time() - t_rec > 2.5:
                    break
                continue
            if last:
                ok, detail = _safe(sc.check, ctx)
                if ok:
                    t_pass = time.time(); break
                if t_rec and time.time() - t_rec > SETTLE * scale:
                    break
            elif step_check:
                ok, d = _safe(step_check, ctx)
                if ok:
                    step_details.append(f"{i}:ok"); break
            elif got:
                break
        if not last:
            if step_check and not a.dry_run and not step_details[-1:] == [f"{i}:ok"]:
                ok, d = _safe(step_check, ctx)
                if not ok:
                    failed_step, detail = i, f"step {i} '{utt}' failed: {d}"
                    print(f"    step {i} FAILED: {d}")
                    break
            time.sleep(a.gap)
    if a.dry_run or (t_pass is None and failed_step is None):
        ok, detail = _safe(sc.check, ctx)
        if ok and t_pass is None:
            t_pass = time.time()
    time.sleep(0.5)
    pump()
    ok = t_pass is not None and failed_step is None
    tms = [r.get("total_ms") for r in brain_recs if isinstance(r.get("total_ms"), (int, float))]
    fms = [r.get("first_action_ms") for r in brain_recs if isinstance(r.get("first_action_ms"), (int, float))]
    res = {"scenario": sc.id, "difficulty": sc.difficulty, "brain": brain, "model": a.claude_model if brain in ("claude", "hybrid") else None,
           "pass": ok, "secs": round(t_pass - t0, 2) if ok else None, "failed_step": failed_step, "detail": detail[:300],
           "utterances": sc.utterances, "n_records": len(brain_recs),
           "brain_total_ms": tms, "first_action_ms": fms, "tokens": sum(_tokens(r) for r in brain_recs),
           "steps": sum(_steps(r) for r in brain_recs), "records": [_trim(r) for r in brain_recs],
           "decisions": [{k: r.get(k) for k in ("transcript", "kind", "intent", "total_ms")} for r in dec_recs][:12],
           "setup_s": round(t0 - t_setup, 2), "t": t0, "dry_run": a.dry_run}
    print(f"  {'PASS' if ok else 'FAIL'} {sc.id} {res['secs'] if ok else ''}  {detail[:140]}")
    return res


def _trim(r):
    out = {}
    for k, v in r.items():
        s = json.dumps(v, default=str)
        out[k] = v if len(s) < 600 else s[:600] + "..."
    return out


# ============================================================ report
def _med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 2) if xs else None


def summarize(runs) -> tuple[str, dict]:
    brains = sorted({r["brain"] for r in runs}, key=lambda b: (b != "jev", b))
    ids = [s.id for s in S.SCENARIOS if any(r["scenario"] == s.id for r in runs)]
    diff = {s.id: s.difficulty for s in S.SCENARIOS}
    md = [f"# whis brain A/B — {time.strftime('%Y-%m-%d %H:%M')}", ""]
    models = {r["brain"]: r.get("model") for r in runs if r.get("model")}
    if models:
        md += [f"Claude model: {', '.join(sorted(set(filter(None, models.values()))))}", ""]
    if any(r.get("dry_run") for r in runs):
        md += ["**DRY RUN** (plumbing only: nothing executed; pass/fail reflects whatever the desktop already showed, not the brain)", ""]
    md += ["| scenario | difficulty | " + " | ".join(brains) + " |", "|---|---|" + "---|" * len(brains)]
    for sid in ids:
        cells = []
        for b in brains:
            rs = [r for r in runs if r["scenario"] == sid and r["brain"] == b]
            if not rs:
                cells.append("—"); continue
            k = sum(r["pass"] for r in rs)
            med = _med([r["secs"] for r in rs if r["pass"]])
            if len(rs) == 1:
                cells.append(f"PASS {med}s" if k else "fail")
            else:
                cells.append(f"{k}/{len(rs)}" + (f" {med}s" if med is not None else ""))
        md.append(f"| {sid} | {diff.get(sid, '?')} | " + " | ".join(cells) + " |")
    md += ["", "## Totals", "", "| brain | difficulty | passed | rate | median s to success | median brain ms/utt | "
           "median first action ms | steps | tokens |", "|---|---|---|---|---|---|---|---|---|"]
    summary = {}
    for b in brains:
        for d in ("simple", "multi-step", "generalization", "ALL"):
            rs = [r for r in runs if r["brain"] == b and (d == "ALL" or r["difficulty"] == d)]
            if not rs:
                continue
            k = sum(r["pass"] for r in rs)
            row = {"passed": k, "runs": len(rs), "rate": round(k / len(rs), 2),
                   "median_secs": _med([r["secs"] for r in rs if r["pass"]]),
                   "median_brain_ms": _med([x for r in rs for x in r["brain_total_ms"]]),
                   "median_first_action_ms": _med([x for r in rs for x in r["first_action_ms"]]),
                   "steps": sum(r["steps"] for r in rs), "tokens": sum(r["tokens"] for r in rs)}
            summary.setdefault(b, {})[d] = row
            md.append(f"| {b} | {'**ALL**' if d == 'ALL' else d} | {k}/{len(rs)} | {int(100 * row['rate'])}% | "
                      f"{row['median_secs']} | {row['median_brain_ms']} | {row['median_first_action_ms']} | {row['steps']} | {row['tokens']} |")
    fails = [r for r in runs if not r["pass"]]
    if fails:
        md += ["", "## Failures", ""]
        for r in fails:
            md.append(f"- **{r['brain']} / {r['scenario']}**: {r['detail'][:220]}")
    return "\n".join(md) + "\n", summary


def save(runs, brains_now, dry=False):
    global OUT_MD, OUT_JSON
    if dry:                                                 # never clobber real results with a plumbing run
        OUT_MD, OUT_JSON = os.path.join(LOGS, "ab_results_dry.md"), os.path.join(LOGS, "ab_results_dry.json")
    old = []
    if os.path.exists(OUT_JSON):
        try:
            old = [r for r in json.load(open(OUT_JSON, encoding="utf-8")).get("runs", []) if r["brain"] not in brains_now]
        except Exception:
            old = []
    allruns = old + runs
    md, summary = summarize(allruns)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(md)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"written": time.strftime("%Y-%m-%d %H:%M:%S"), "summary": summary, "runs": allruns}, f, indent=1, default=str)
    return md


# ============================================================ main
def pick(only: str | None) -> list:
    if not only:
        return list(S.SCENARIOS)
    keys = [k.strip() for k in only.split(",") if k.strip()]
    sel = [s for s in S.SCENARIOS if any(s.id == k or s.id.startswith(k) or k in s.id for k in keys)]
    if not sel:
        raise SystemExit(f"--only {only!r} matched nothing; see --list")
    return sel


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brain", choices=["jev", "claude", "hybrid", "both"], default="both")
    ap.add_argument("--claude-model", default=None)
    ap.add_argument("--only", default=None, help="comma list of scenario ids / prefixes / substrings")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true", help="whis --dry-run --fake-snapshot --no-browser; no setup side effects")
    ap.add_argument("--wake", choices=["first", "all", "none"], default="first", help="prefix 'whis, ' to utterances")
    ap.add_argument("--gap", type=float, default=1.5, help="s between utterances after the previous one finished")
    ap.add_argument("--timeout-scale", type=float, default=1.0)
    ap.add_argument("--fresh", action="store_true", help="restart whis for every scenario (isolation over speed)")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for s in S.SCENARIOS:
            print(f"{s.id:26s} {s.difficulty:14s} {s.timeout:4.0f}s  {' / '.join(s.utterances)[:90]}")
        return
    scs = pick(a.only)
    brains = ["jev", "claude"] if a.brain == "both" else [a.brain]
    src = "".join(open(os.path.join(ROOT, "whis", f), encoding="utf-8").read() for f in os.listdir(os.path.join(ROOT, "whis")) if f.endswith(".py"))
    a.brain_log = "brain.jsonl" in src or '"brain"' in src
    if not a.brain_log:
        print("note: whis doesn't write logs/brain.jsonl yet -> waiting on decisions.jsonl instead")
    if not a.dry_run:
        print("LIVE run: whis will drive this PC (focus changes, typing, Spotify). Hands off the keyboard. Ctrl+C aborts.")
    vol0 = S.master_volume()
    runs = []
    try:
        for b in brains:
            tag = b + ("-dry" if a.dry_run else "")
            print(f"=== brain {b}" + (f" ({a.claude_model})" if b == "claude" and a.claude_model else ""))
            w = None
            try:
                for rep in range(a.repeat):
                    for sc in scs:
                        if w is None or a.fresh or w.p.poll() is not None:
                            if w is not None:
                                w.stop()
                            t = time.time()
                            w = Whis(b, a.claude_model, a.dry_run, tag)
                            w.wait_ready()
                            print(f"  whis up in {time.time() - t:.1f}s: {' '.join(w.cmd[3:])}")
                            time.sleep(1.0)
                        print(f"  [{b} rep {rep + 1}/{a.repeat}] {sc.id} ({sc.difficulty})")
                        r = run_scenario(w, sc, b, a)
                        r["rep"] = rep
                        runs.append(r)
                        save(runs, brains, a.dry_run)
            finally:
                if w is not None:
                    w.stop()
    except KeyboardInterrupt:
        print("aborted — saving partial results")
    finally:
        if not a.dry_run:
            try:
                print(f"cleanup: spotify paused={S.pause_spotify(None)}")
            except Exception as e:
                print(f"cleanup: pause failed {e!r}")
            try:
                if vol0 >= 0 and abs(S.master_volume() - vol0) > 0.005:
                    S._endpoint().SetMasterVolumeLevelScalar(vol0, None)
                    print(f"cleanup: volume restored to {vol0:.2f}")
            except Exception:
                pass
    if runs:
        md = save(runs, brains, a.dry_run)
        print("\n" + md)
        print(f"saved {OUT_MD} and {OUT_JSON}")


if __name__ == "__main__":
    main()
