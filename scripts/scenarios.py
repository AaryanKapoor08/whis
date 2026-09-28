"""A/B scenarios for whis brains (Jev vs Claude). Every check() verifies success from the OUTSIDE via Windows / UIA
state (foreground window, Notepad text, Spotify now-playing bar, Brave address bar, VS Code panel, PSReadLine history,
master volume). Only the answer-type tasks (ask_screen) are checked from whis's own logs/stdout.

SAFETY: helpers here never type text and never close a window. Setup may focus windows, launch an app that is not
running, click Spotify's Pause button / VS Code's "Hide Panel" button through UIA Invoke, and lower the master volume
when it is above 90 % (restored by ab_test.py). With ctx["dry"] every side-effecting helper is a no-op (baselines are
still captured read-only).

    .venv\\Scripts\\python.exe scripts\\scenarios.py        # read-only probe: baselines + every check once, no input sent
"""
import os, re, sys, time, json, ctypes, statistics, subprocess
from dataclasses import dataclass, field
from typing import Callable

import psutil, win32gui, win32process, win32con
import uiautomation as auto
from uiautomation.uiautomation import _AutomationClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_FILE = r"C:\whis-demo\whis-demo.txt"
PS_HISTORY = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "PowerShell", "PSReadLine",
                          "ConsoleHost_history.txt")
VSCODE_EXE = next((p for p in [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "Code.exe"),
                               r"C:\Program Files\Microsoft VS Code\Code.exe"] if os.path.exists(p)), None)
IDLE_SPOTIFY_TITLES = {"spotify", "spotify premium", "spotify free"}
OCEAN_WORDS = {"ocean", "oceans", "sea", "seas", "wave", "waves", "tide", "tides", "shore", "salt", "surf", "foam",
               "sand", "beach", "deep", "blue", "water", "waters", "coral", "gull", "gulls", "swell", "brine", "horizon"}
TREE_NAME, TREE_CTRLTYPE, TREE_DESC = 30005, 30003, 4
BUTTON, HYPERLINK = 50000, 50005


def _d2l_host() -> str:
    url = os.getenv("D2L_URL", "")
    if not url:
        try:
            for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
                if ln.strip().startswith("D2L_URL="):
                    url = ln.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    m = re.match(r"(?:https?://)?([^/]+)", url or "")
    return (m.group(1) if m else "d2l").lower()


D2L_HOST = _d2l_host()


# ============================================================ low-level read-only state
def _ia():
    return _AutomationClient.instance().IUIAutomation


def _cond(prop, val):
    return _ia().CreatePropertyCondition(prop, val)


def proc_of(hwnd) -> tuple[int, str]:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid, psutil.Process(pid).name().lower().replace(".exe", "")
    except Exception:
        return 0, ""


def fg() -> tuple[int, str, str]:
    """(hwnd, process, title) of the foreground window."""
    h = win32gui.GetForegroundWindow()
    return h, proc_of(h)[1], win32gui.GetWindowText(h) if h else ""


def windows(proc: str | None = None) -> list[tuple[int, str, str]]:
    """Visible titled top-level windows [(hwnd, process, title)], z-order (topmost first)."""
    out = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h):
            p = proc_of(h)[1]
            if proc is None or p == proc.lower():
                if p == "code" and proc is not None and "whis-demo" not in win32gui.GetWindowText(h).lower():
                    return True     # only the whis demo VS Code window: never touch the user's own (Claude Code runs there)
                out.append((h, p, win32gui.GetWindowText(h)))
    win32gui.EnumWindows(cb, None)
    return out


_whis_brave = {"t": 0.0, "pids": set()}


def whis_brave_pids() -> set[int]:
    """Brave processes launched by whis (Playwright persistent profile C:\\whis-profile). Cached 5 s."""
    if time.time() - _whis_brave["t"] > 5:
        pids = set()
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if (p.info["name"] or "").lower() == "brave.exe" and any("whis-profile" in c.lower() for c in (p.info["cmdline"] or [])):
                    pids.add(p.pid)
            except Exception:
                pass
        _whis_brave.update(t=time.time(), pids=pids)
    return _whis_brave["pids"]


def browser_window() -> tuple[int, bool]:
    """(hwnd, is_whis_profile): the foreground Brave window if Brave is in front, else the topmost whis Brave window."""
    h, p, _ = fg()
    if p == "brave":
        return h, proc_of(h)[0] in whis_brave_pids()
    pids = whis_brave_pids()
    for hw, _, _ in windows("brave"):
        if proc_of(hw)[0] in pids:
            return hw, True
    return 0, False


def browser_state() -> dict:
    """{'hwnd', 'whis', 'title', 'url', 'fg'} for the relevant Brave window (url from the UIA address bar)."""
    h, mine = browser_window()
    if not h:
        return {"hwnd": 0, "whis": False, "title": "", "url": "", "fg": False}
    url = ""
    try:
        w = auto.ControlFromHandle(h)
        e = w.Element.FindFirst(TREE_DESC, _cond(TREE_NAME, "Address and search bar"))
        if e:
            url = auto.Control.CreateControlFromElement(e).GetValuePattern().Value or ""
    except Exception:
        pass
    return {"hwnd": h, "whis": mine, "title": win32gui.GetWindowText(h), "url": url, "fg": fg()[0] == h}


def _spotify_window():
    for w in auto.GetRootControl().GetChildren():
        try:
            if w.ClassName == "Chrome_WidgetWin_1" and psutil.Process(w.ProcessId).name().lower() == "spotify.exe":
                return w
        except Exception:
            pass
    return None


def spotify_state() -> dict:
    """{'running', 'playing', 'links' (title/artist hyperlinks of the Now playing bar), 'title'}.
    Playing = a button named exactly 'Pause' in the bar (paused shows 'Play'); falls back to the window title."""
    w = _spotify_window()
    if w is None:
        return {"running": False, "playing": False, "links": "", "title": ""}
    title = w.Name or ""
    st = {"running": True, "playing": title.strip().lower() not in IDLE_SPOTIFY_TITLES, "links": "", "title": title}
    try:
        bar = w.Element.FindFirst(TREE_DESC, _cond(TREE_NAME, "Now playing bar"))
        if bar:
            btns = bar.FindAll(TREE_DESC, _cond(TREE_CTRLTYPE, BUTTON))
            names = {btns.GetElement(i).CurrentName for i in range(btns.Length)}
            if "Pause" in names or "Play" in names:
                st["playing"] = "Pause" in names
            links = bar.FindAll(TREE_DESC, _cond(TREE_CTRLTYPE, HYPERLINK))
            st["links"] = " | ".join(links.GetElement(i).CurrentName for i in range(links.Length))
    except Exception:
        pass
    return st


def notepad_texts() -> dict[int, str]:
    """{hwnd: text of the active tab's editor} for every Notepad window (Win11 RichEditD2DPT 'Text editor')."""
    out = {}
    for h, _, _ in windows("notepad"):
        try:
            w = auto.ControlFromHandle(h)
            e = w.Element.FindFirst(TREE_DESC, _cond(30012, "RichEditD2DPT"))          # ClassNameProperty
            if not e:
                e = w.Element.FindFirst(TREE_DESC, _cond(TREE_NAME, "Text editor"))
            if e:
                c = auto.Control.CreateControlFromElement(e)
                try:
                    out[h] = c.GetValuePattern().Value or ""
                except Exception:
                    out[h] = c.GetTextPattern().DocumentRange.GetText(-1) or ""
        except Exception:
            pass
    return out


def _named_buttons(hwnd, prefixes) -> list:
    try:
        w = auto.ControlFromHandle(hwnd)
        btns = w.Element.FindAll(TREE_DESC, _cond(TREE_CTRLTYPE, BUTTON))
        out = []
        for i in range(btns.Length):
            e = btns.GetElement(i)
            n = e.CurrentName or ""
            if any(n.startswith(p) for p in prefixes) and not e.CurrentIsOffscreen:
                out.append(e)
        return out
    except Exception:
        return []


def vscode_state() -> dict:
    """{'hwnd', 'fg', 'terminal'}: terminal = a visible 'Kill Terminal' button (terminal panel shown) in the top Code window."""
    ws = windows("code")
    if not ws:
        return {"hwnd": 0, "fg": False, "terminal": False}
    f = fg()
    demo_fg = f[0] in {w[0] for w in ws}           # ws = whis demo VS Code windows only
    h = f[0] if demo_fg else ws[0][0]
    return {"hwnd": h, "fg": demo_fg, "terminal": bool(_named_buttons(h, ("Kill Terminal",)))}


def ps_history() -> list[str]:
    try:
        with open(PS_HISTORY, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except Exception:
        return []


def _endpoint():
    from pycaw.pycaw import AudioUtilities
    s = AudioUtilities.GetSpeakers()
    if hasattr(s, "EndpointVolume"):
        return s.EndpointVolume
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume
    return cast(s.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))


def master_volume() -> float:
    try:
        return float(_endpoint().GetMasterVolumeLevelScalar())
    except Exception:
        return -1.0


def spotify_session_volume() -> float:
    try:
        from pycaw.pycaw import AudioUtilities
        for s in AudioUtilities.GetAllSessions():
            if s.Process and s.Process.name().lower() == "spotify.exe":
                return float(s.SimpleAudioVolume.GetMasterVolume())
    except Exception:
        pass
    return -1.0


def doc_marks(hwnd, limit_nodes=400, budget_s=3.0) -> dict[str, int]:
    """{element name: top y} for named elements of the largest on-screen web document in a Brave window."""
    try:
        w = auto.ControlFromHandle(hwnd)
        docs = w.Element.FindAll(TREE_DESC, _cond(TREE_CTRLTYPE, 50030))             # DocumentControl
        best, area = None, 0
        for i in range(docs.Length):
            e = docs.GetElement(i)
            if e.CurrentIsOffscreen:
                continue
            r = e.CurrentBoundingRectangle
            a = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            if a > area:
                best, area = e, a
        if best is None:
            return {}
        marks, n, t0 = {}, 0, time.perf_counter()
        for c, _ in auto.WalkControl(auto.Control.CreateControlFromElement(best), maxDepth=25):
            n += 1
            if n > limit_nodes or time.perf_counter() - t0 > budget_s or len(marks) >= 60:
                break
            try:
                nm = (c.Name or "").strip()
                if nm and c.ControlTypeName in ("HyperlinkControl", "TextControl", "ImageControl", "ButtonControl") and nm not in marks:
                    marks[nm] = c.BoundingRectangle.top
            except Exception:
                continue
        return marks
    except Exception:
        return {}


# ============================================================ side-effecting setup helpers (no-ops when ctx["dry"])
def _set_fg(hwnd) -> bool:
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        cur = u32.GetForegroundWindow()
        a, b = u32.GetWindowThreadProcessId(cur, None), k32.GetCurrentThreadId()
        u32.AttachThreadInput(b, a, True)
        try:
            u32.BringWindowToTop(hwnd); u32.SetForegroundWindow(hwnd)
        finally:
            u32.AttachThreadInput(b, a, False)
        time.sleep(0.3)
        return u32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def focus_desktop(ctx):
    if ctx.get("dry"):
        return
    h = win32gui.FindWindow("Progman", None)
    if h:
        _set_fg(h)


def focus_proc(ctx, proc: str) -> bool:
    if ctx.get("dry"):
        return False
    ws = windows(proc)
    return bool(ws) and _set_fg(ws[0][0])


def _wait(pred, timeout):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.3)
    return False


def ensure_notepad(ctx):
    if ctx.get("dry") or windows("notepad"):
        return
    os.makedirs(os.path.dirname(DEMO_FILE), exist_ok=True)
    if not os.path.exists(DEMO_FILE):
        open(DEMO_FILE, "w").write("whis demo file\n")
    os.startfile(DEMO_FILE)
    _wait(lambda: windows("notepad"), 8)


def ensure_spotify(ctx):
    if ctx.get("dry") or _spotify_window() is not None:
        return
    try:
        os.startfile("spotify:")
    except Exception:
        subprocess.Popen(["cmd", "/c", "start", "", "spotify"], shell=False)
    _wait(lambda: _spotify_window() is not None, 12)


def ensure_vscode(ctx):
    if ctx.get("dry") or windows("code") or not VSCODE_EXE:
        return
    subprocess.Popen([VSCODE_EXE, "--new-window", r"C:\whis-demo"])
    _wait(lambda: windows("code"), 15)


def pause_spotify(ctx=None) -> bool:
    """Click the Now playing bar's Pause button (UIA Invoke). Media key only if Invoke didn't take. Returns paused."""
    if ctx is not None and ctx.get("dry"):
        return not spotify_state()["playing"]
    st = spotify_state()
    if not st["playing"]:
        return True
    try:
        w = _spotify_window()
        bar = w.Element.FindFirst(TREE_DESC, _cond(TREE_NAME, "Now playing bar"))
        e = bar.FindFirst(TREE_DESC, _ia().CreateAndCondition(_cond(TREE_CTRLTYPE, BUTTON), _cond(TREE_NAME, "Pause")))
        auto.Control.CreateControlFromElement(e).GetInvokePattern().Invoke()
    except Exception:
        pass
    if _wait(lambda: not spotify_state()["playing"], 2.5):
        return True
    ctypes.windll.user32.keybd_event(0xB3, 0, 0, 0); ctypes.windll.user32.keybd_event(0xB3, 0, 2, 0)   # VK_MEDIA_PLAY_PAUSE
    return _wait(lambda: not spotify_state()["playing"], 2.5)


def hide_vscode_panel(ctx):
    """Hide VS Code's bottom panel through its own 'Hide Panel' button (the terminal keeps running)."""
    if ctx.get("dry"):
        return
    st = vscode_state()
    if st["hwnd"] and st["terminal"]:
        for e in _named_buttons(st["hwnd"], ("Hide Panel", "Close Panel")):
            try:
                auto.Control.CreateControlFromElement(e).GetInvokePattern().Invoke()
                break
            except Exception:
                continue
        _wait(lambda: not vscode_state()["terminal"], 3)


def ensure_volume_headroom(ctx):
    """'make it louder' needs room to go up: at > 90 % set 50 % (ab_test restores the user's level at the end)."""
    if ctx.get("dry"):
        return
    v = master_volume()
    if v > 0.9:
        try:
            _endpoint().SetMasterVolumeLevelScalar(0.5, None)
        except Exception:
            pass


# ============================================================ baselines + checks
def base_all(ctx):
    """Read-only baseline every check may compare against."""
    ctx["b_notepad"] = notepad_texts()
    ctx["b_spotify"] = spotify_state()
    ctx["b_hist_n"] = len(ps_history())
    ctx["b_vol"] = master_volume()
    ctx["b_spvol"] = spotify_session_volume()


def c_fg(proc):
    def check(ctx):
        h, p, t = fg()
        return p == proc, f"fg={p} '{t[:50]}'"
    return check


def c_fg_brave(ctx):
    b = browser_state()
    return b["fg"], f"fg={fg()[1]} whis_profile={b['whis']} title='{b['title'][:50]}'"


def c_url(*needles, any_of=None):
    """Brave (fg, else whis Brave) URL or title contains all needles (+ one of any_of)."""
    def check(ctx):
        b = browser_state()
        hay = (b["url"] + " " + b["title"]).lower()
        ok = bool(b["hwnd"]) and all(n in hay for n in needles) and (not any_of or any(a in hay for a in any_of))
        return ok, f"url='{b['url'][:80]}' title='{b['title'][:50]}' fg={b['fg']} whis={b['whis']}"
    return check


def _norm_links(s):
    return s.lower()


def c_playing(artist, title=None, must_change=True):
    """Spotify playing, now-playing links contain artist (and title); must_change: track differs from the baseline if the
    baseline already matched (a bare 'resume' of an already-matching paused track must not pass)."""
    def check(ctx):
        st = spotify_state()
        l = _norm_links(st["links"])
        ok = st["playing"] and artist in l and (title is None or title in l)
        b = ctx.get("b_spotify") or {}
        if ok and must_change and b.get("playing") and artist in _norm_links(b.get("links", ""))                 and (title is None or title in _norm_links(b.get("links", ""))):
            ok = st["links"] != b.get("links")      # was already playing it: must have changed; paused -> playing is proof
        return ok, f"playing={st['playing']} links='{st['links'][:80]}'"
    return check


def c_paused_on(title_word):
    def check(ctx):
        st = spotify_state()
        return (not st["playing"]) and title_word in st["links"].lower(), f"playing={st['playing']} links='{st['links'][:80]}'"
    return check


def _notepad_new(ctx) -> list[tuple[int, str, str]]:
    """[(hwnd, full text, text added since baseline)]"""
    out = []
    base = ctx.get("b_notepad", {})
    for h, t in notepad_texts().items():
        b = base.get(h, "")
        new = t.replace(b, "", 1) if b and b in t else (t if t != b else "")
        out.append((h, t, new))
    return out


def c_notepad_contains(word, need_fg=True):
    def check(ctx):
        base = ctx.get("b_notepad", {})
        hit = any(t.lower().count(word) > base.get(h, "").lower().count(word) for h, t, _ in _notepad_new(ctx))
        p = fg()[1]
        return hit and (p == "notepad" or not need_fg), f"'{word}' added={hit} fg={p}"
    return check


def c_haiku(ctx):
    best = ""
    for _, _, new in _notepad_new(ctx):
        if len(new) > len(best):
            best = new
    words = re.findall(r"[a-z']+", best.lower())
    ocean = sorted(set(words) & OCEAN_WORDS)
    literal = "haiku about the ocean" in best.lower() and len(words) < 12
    ok = len(words) >= 8 and bool(ocean) and not literal and fg()[1] == "notepad"
    return ok, f"new_words={len(words)} ocean={ocean[:4]} literal={literal} fg={fg()[1]} text='{best[:60]!r}'"


def c_terminal_open(ctx):
    st = vscode_state()
    return st["fg"] and st["terminal"], f"code_fg={st['fg']} terminal_panel={st['terminal']}"


def c_ran(cmd_re):
    """A new PSReadLine history line matching cmd_re since the baseline, and VS Code in front (the VS Code terminal)."""
    rx = re.compile(cmd_re, re.I)

    def check(ctx):
        new = ps_history()[ctx.get("b_hist_n", 0):]
        hit = [l for l in new if rx.search(l)]
        p = fg()[1]
        if not hit:     # VS Code's terminal may not be PowerShell (bash history is only written on exit): use whis's own logs
            for rec in _jsonl_since(os.path.join(ROOT, "logs", "brain.jsonl"), ctx.get("t0", 0)):
                steps = rec.get("steps") if isinstance(rec.get("steps"), list) else []
                texts = [str((s.get("args") or {}).get("command", "")) for s in steps if isinstance(s, dict) and s.get("tool") == "run_command" and s.get("ok")]
                if rec.get("intent") == "run_command" and rec.get("ok"):
                    texts.append(str((rec.get("args") or {}).get("text", "")) or str(rec.get("utterance", "")))
                hit += [t for t in texts if rx.search(t) or rx.search("echo " + t.split("echo")[-1])]
            for rec in _jsonl_since(os.path.join(ROOT, "logs", "exec.jsonl"), ctx.get("t0", 0)):
                if rec.get("intent") == "run_command" and rec.get("ok") and "echo hello" in str(rec.get("msg", "")):
                    hit.append(rec["msg"])
        return bool(hit) and p == "code", f"new_history={new[-3:]} logged={hit[:1]} fg={p}"
    return check


_ANS_KEYS = ("answer", "say", "reply", "speak", "spoken", "response", "spoke")


def c_answered(ctx):
    """An answer was produced: exec.jsonl ask_screen ok, or a brain.jsonl record with an answer-like field, or a whis
    stdout line showing a successful ask_screen (log-based: an answer has no outside UI state)."""
    t0 = ctx.get("t0", 0)
    for rec in _jsonl_since(os.path.join(ROOT, "logs", "exec.jsonl"), t0):
        if rec.get("intent") == "ask_screen" and rec.get("ok") and rec.get("msg"):
            return True, f"exec ask_screen: {str(rec.get('msg'))[:80]}"
    for rec in _jsonl_since(os.path.join(ROOT, "logs", "brain.jsonl"), t0):
        if rec.get("intent") == "ask_screen" and rec.get("ok") and len(str(rec.get("result") or "")) > 3 and rec.get("result") != "done":
            return True, f"jev ask_screen: {str(rec['result'])[:80]}"
        if not isinstance(rec.get("steps"), list):          # hybrid/jev lines log steps as a count
            rec = {**rec, "steps": []}
        if rec.get("route") == "claude" or rec.get("brain") == "claude":
            ft = str(rec.get("final_text") or "").strip()
            acts = [s.get("tool") for s in rec["steps"] if isinstance(s, dict)]
            if len(ft) > 15 and set(acts) <= {"say", "done", "look", "d2l_assignments", "d2l_courses"}:
                return True, f"claude answer: {ft[:80]}"      # plain-text or say/done answer, no actions taken
        for s in rec.get("steps") or []:        # Claude brain: a successful 'say' tool call is the spoken answer
            if isinstance(s, dict) and s.get("tool") == "say" and s.get("ok", True) and len(str((s.get("args") or {}).get("text", ""))) > 3:
                return True, f"claude say: {str(s['args']['text'])[:80]}"
        steps = rec.get("steps") if isinstance(rec.get("steps"), list) else []     # hybrid/jev lines log steps as a count
        rec = {**rec, "steps": steps}
        tools = [s.get("tool") for s in steps if isinstance(s, dict)]
        if tools and set(tools) <= {"done", "say", "look", "d2l_assignments", "d2l_courses", "click_web", "scroll"}                 and len(str(rec.get("final_text") or "").strip()) > 3:
            return True, f"claude answer: {str(rec['final_text'])[:80]}"   # answered via done(summary), no action taken
        for k in _ANS_KEYS:
            v = rec.get(k)
            if isinstance(v, str) and len(v.strip()) > 3:
                return True, f"brain {k}: {v[:80]}"
        for s in rec.get("steps") or []:
            if isinstance(s, dict) and (s.get("intent") or s.get("tool") or s.get("name")) in ("ask_screen", "answer", "say", "speak", "d2l_assignments") \
                    and s.get("ok", True):
                return True, f"brain step: {json.dumps(s)[:80]}"
    for t, line in (ctx.get("stdout") or (lambda _t: []))(t0):
        if re.search(r"ask_screen.*->\s*True", line) or re.search(r"\b(ANSWER|SAY)\b", line):
            return True, f"stdout: {line[:80]}"
    return False, "no answer in exec/brain logs or stdout"


def _jsonl_since(path, t0):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if float(r.get("t", 0)) >= t0:
            out.append(r)
    return out


def c_louder(ctx):
    v, sv = master_volume(), spotify_session_volume()
    bv, bsv = ctx.get("b_vol", -1), ctx.get("b_spvol", -1)
    ok = (v >= 0 and bv >= 0 and v > bv + 0.005) or (sv >= 0 and bsv >= 0 and sv > bsv + 0.005)
    return ok, f"master {bv:.2f}->{v:.2f} spotify_session {bsv:.2f}->{sv:.2f}"


def c_all(*checks):
    def check(ctx):
        details, ok = [], True
        for c in checks:
            o, d = c(ctx)
            ok &= o
            details.append(("OK " if o else "NO ") + d)
        return ok, " ; ".join(details)
    return check


def c_step_mark_scroll(ctx):
    """Step check for 'open youtube': page loaded, then record element positions for the scroll check."""
    ok, d = c_url("youtube")(ctx)
    if not ok:
        return ok, d
    h = browser_state()["hwnd"]
    time.sleep(1.5)
    marks = doc_marks(h)
    if len(marks) < 5:
        return False, f"page not ready ({len(marks)} marks)"
    ctx["scroll_marks"], ctx["scroll_hwnd"] = marks, h
    return True, f"{len(marks)} marks"


def c_scrolled(ctx):
    base = ctx.get("scroll_marks") or {}
    h = ctx.get("scroll_hwnd") or browser_state()["hwnd"]
    now = doc_marks(h) if h else {}
    common = [now[k] - base[k] for k in base if k in now]
    if not base:
        return False, "no baseline marks"
    if not common:
        return len(now) > 0, f"no common marks (page replaced?) now={len(now)}"
    med = statistics.median(common)
    return med <= -40, f"median dy={med:.0f}px over {len(common)} marks"


# ============================================================ scenarios
@dataclass
class Scenario:
    id: str
    difficulty: str                      # simple | multi-step | generalization
    utterances: list[str]
    check: Callable
    setup: Callable = lambda ctx: None
    timeout: float = 25.0                # s from the first utterance for the final check
    step_checks: list = field(default_factory=list)   # optional per-utterance checks (None = just wait for the brain)
    note: str = ""


def _setup(*fns):
    def run(ctx):
        for f in fns:
            f(ctx)
        base_all(ctx)                   # baselines AFTER the setup actions
    return run


SP = lambda ctx: (ensure_spotify(ctx), pause_spotify(ctx))
DESK = focus_desktop
NP = ensure_notepad
VS_READY = lambda ctx: (ensure_vscode(ctx), focus_proc(ctx, "code"), hide_vscode_panel(ctx))
D2L = c_url(D2L_HOST)

PITCH_STEPS = ["open spotify", "play tame impala", "then open browser", "open d2l", "look if I have an assignment left",
               "then open vs code", "open the terminal in it", "and run echo hello"]
PITCH_ONE = ("open spotify, play tame impala, then open browser, open d2l, look if I have an assignment left, "
             "then open vs code, open the terminal in it and run echo hello")
ECHO = c_ran(r"^\s*echo\s+hello\b")

SCENARIOS: list[Scenario] = [
    # ---- pitch script, clause by clause (simple)
    Scenario("p_open_spotify", "simple", ["open spotify"], c_fg("spotify"), _setup(SP, DESK), 15),
    Scenario("p_play_tame_impala", "simple", ["play tame impala"], c_playing("tame impala"),
             _setup(SP, lambda c: focus_proc(c, "spotify")), 25),
    Scenario("p_open_browser", "simple", ["open browser"], c_fg_brave, _setup(DESK), 15),
    Scenario("p_open_d2l", "simple", ["open d2l"], D2L, _setup(DESK), 20),
    Scenario("p_assignment_left", "simple", ["open d2l", "look if I have an assignment left"], c_answered, _setup(DESK), 30,
             [D2L, None], note="answer-type: checked from logs"),
    Scenario("p_open_vscode", "simple", ["open vs code"], c_fg("code"), _setup(lambda c: ensure_vscode(c), DESK), 15),
    Scenario("p_vscode_terminal", "simple", ["open the terminal in it"], c_terminal_open, _setup(VS_READY), 15,
             note="setup hides VS Code's panel via its Hide Panel button"),
    Scenario("p_run_command", "multi-step", ["open vs code", "open the terminal in it", "run echo hello"],
             c_all(ECHO, c_terminal_open), _setup(lambda c: ensure_vscode(c), lambda c: focus_proc(c, "code"), hide_vscode_panel, DESK), 35,
             [c_fg("code"), c_terminal_open, None], note="'run claude code' replaced by a harmless echo; checked in PSReadLine history"),
    # ---- pitch script end to end
    Scenario("pitch_clauses", "multi-step", PITCH_STEPS,
             c_all(ECHO, c_terminal_open), _setup(SP, lambda c: ensure_vscode(c), lambda c: focus_proc(c, "code"), hide_vscode_panel, DESK), 90,
             [c_fg("spotify"), c_playing("tame impala"), c_fg_brave, D2L, c_answered, c_fg("code"), c_terminal_open, None]),
    Scenario("pitch_one_breath", "multi-step", [PITCH_ONE],
             c_all(c_playing("tame impala"), c_answered, ECHO, c_terminal_open, D2L),
             _setup(SP, lambda c: ensure_vscode(c), lambda c: focus_proc(c, "code"), hide_vscode_panel, DESK), 90,
             note="D2L checked on the whis Brave (VS Code is in front at the end)"),
    # ---- multi-step / multi-turn
    Scenario("m_play_then_pause", "multi-step", ["play loser by tame impala", "pause"], c_paused_on("loser"),
             _setup(SP, DESK), 30, [c_playing("tame impala", "loser", must_change=False), None]),
    Scenario("m_notepad_hello_spotify", "multi-step", ["open notepad, type hello, then open spotify"],
             c_all(c_notepad_contains("hello", need_fg=False), c_fg("spotify")), _setup(NP, SP, DESK), 30),
    Scenario("m_go_back", "multi-step", ["open github", "open youtube", "go back"], c_url("github"), _setup(DESK), 35,
             [c_url("github"), c_url("youtube"), None]),
    Scenario("m_play_that_again", "multi-step", ["play loser by tame impala", "pause", "play that again"],
             c_playing("tame impala", "loser", must_change=False), _setup(SP, DESK), 40,
             [c_playing("tame impala", "loser", must_change=False), c_paused_on("loser"), None]),
    # ---- generalization (phrasings Jev was never tuned on)
    Scenario("g_haiku", "generalization", ["open notepad and write a haiku about the ocean"], c_haiku, _setup(NP, DESK), 30,
             note="fails on the literal phrase; needs >=8 new words incl. an ocean word"),
    Scenario("g_daft_punk", "generalization", ["in spotify play something by daft punk"], c_playing("daft punk"), _setup(SP, DESK), 25),
    Scenario("g_youtube_lofi", "generalization", ["search youtube for lo-fi beats"], c_url("youtube", any_of=("lo-fi", "lofi", "lo+fi", "lo fi")),
             _setup(DESK), 25),
    Scenario("g_google_weather", "generalization", ["open google and search for the weather in fredericton"],
             c_url("fredericton", "weather"), _setup(DESK), 25, note="engine shown in detail (google vs bing)"),
    Scenario("g_switch_back_notepad", "generalization", ["switch back to notepad"], c_fg("notepad"),
             _setup(NP, SP, lambda c: focus_proc(c, "spotify")), 15),
    Scenario("g_scroll_down", "generalization", ["open youtube", "scroll down"], c_scrolled, _setup(DESK), 30,
             [c_step_mark_scroll, None]),
    Scenario("g_whats_on_screen", "generalization", ["what's on my screen"], c_answered,
             _setup(NP, lambda c: focus_proc(c, "notepad")), 25, note="answer-type: checked from logs"),
    Scenario("g_make_louder", "generalization", ["make it louder"], c_louder, _setup(ensure_volume_headroom, DESK), 15,
             note="master volume or Spotify session volume"),
]
BY_ID = {s.id: s for s in SCENARIOS}


def probe():
    """Read-only self-test: capture baselines (dry setup, no side effects) and run each check once."""
    print(f"fg={fg()}  d2l_host={D2L_HOST}")
    print(f"spotify={spotify_state()}")
    print(f"browser={browser_state()}")
    print(f"vscode={vscode_state()}  volume={master_volume():.2f} spotify_session={spotify_session_volume():.2f}")
    print(f"notepad={ {h: t[:40] for h, t in notepad_texts().items()} }  ps_history={len(ps_history())} lines")
    for s in SCENARIOS:
        ctx = {"dry": True, "t0": time.time() - 3600}
        s.setup(ctx)
        t = time.perf_counter()
        try:
            ok, d = s.check(ctx)
        except Exception as e:
            ok, d = False, f"EXC {e!r}"
        print(f"  {s.id:26s} {s.difficulty:14s} {'PASS' if ok else 'fail'} {1000 * (time.perf_counter() - t):5.0f}ms  {d[:150]}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    probe()
