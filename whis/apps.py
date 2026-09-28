"""App launch/focus. Windows enumerated via win32gui; fuzzy name match; Playwright window is handled by browser.py."""
import os, subprocess, difflib, ctypes, time
import win32gui, win32con
from . import config


def running_windows() -> list[tuple[int, str, str]]:
    """[(hwnd, title, process)] for visible top-level windows with a title."""
    from .tree import _proc_name
    out = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h):
            out.append((h, win32gui.GetWindowText(h), _proc_name(h)))
    win32gui.EnumWindows(cb, None)
    return out


WHIS_TERMINALS: set[int] = set()     # terminal windows whis opened itself: the only terminals commands are typed into
_CODE_NAMES = {"vs code", "code", "visual studio code", "vscode"}


def is_demo_code(h: int) -> bool:
    """The whis VS Code window (opened on config.DEMO_WORKSPACE). The user's own VS Code windows - where their
    Claude Code session may be running - are never focused, typed into or given a terminal."""
    try:
        return os.path.basename(config.DEMO_WORKSPACE).lower() in win32gui.GetWindowText(h).lower()
    except Exception:
        return False


def safe_terminal(h: int) -> bool:
    """May whis type a command + Enter into window h? Only its own terminals or the whis VS Code window."""
    from .tree import _proc_name
    return bool(h) and (h in WHIS_TERMINALS or (_proc_name(h).lower() == "code" and is_demo_code(h)))


def apps_running() -> list[str]:
    seen, names = set(), []
    for _, _, p in running_windows():
        if p and p.lower() not in seen and p.lower() not in ("explorer", "textinputhost", "applicationframehost"):
            seen.add(p.lower()); names.append(p)
    return names


def find_window(name: str) -> int | None:
    """Best running window for a spoken app name. Process name beats window title (a Brave tab called
    'claude code' must not win 'open vs code'); topmost window wins ties."""
    n = name.lower().strip()
    if n in _CODE_NAMES:                  # only the whis VS Code window, never the user's own
        return next((h for h, _, p in running_windows() if p.lower() == "code" and is_demo_code(h)), None)
    if n == "terminal":                   # only terminals whis opened (the user's may be running Claude Code)
        return next((h for h in list(WHIS_TERMINALS) if win32gui.IsWindow(h)), None)
    want_proc = (config.APP_PROCS.get(n) or "").lower()
    best, score = None, 0.0
    for h, title, proc in running_windows():
        p, t = proc.lower(), title.lower()
        if t == "program manager":
            continue            # the desktop (explorer.exe) is not a File Explorer window
        if want_proc and p == want_proc:
            s = 1.0
        elif want_proc and p not in ("applicationframehost", ""):
            continue            # known app: a Brave tab titled "notepad" must not stop us launching Notepad
        elif p and (n == p or n in p):
            s = 0.95
        elif n in t:
            s = 0.8
        else:
            s = 0.75 * max(difflib.SequenceMatcher(None, n, p).ratio(), difflib.SequenceMatcher(None, n, t).ratio())
        if s > score:
            best, score = h, s
    return best if score >= 0.6 else None


def focus_hwnd(hwnd: int) -> bool:
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        u32 = ctypes.windll.user32
        # Windows refuses SetForegroundWindow from a background process; sharing the foreground thread's input
        # queue for the call makes it legal (was taking 3+ s or silently failing for Spotify)
        fg = u32.GetForegroundWindow()
        fg_tid, me = u32.GetWindowThreadProcessId(fg, None), ctypes.windll.kernel32.GetCurrentThreadId()
        attached = fg_tid and fg_tid != me and u32.AttachThreadInput(me, fg_tid, True)
        try:
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u32.AttachThreadInput(me, fg_tid, False)
        for _ in range(10):
            if u32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.03)
        u32.keybd_event(0x12, 0, 0, 0); u32.keybd_event(0x12, 0, 2, 0)   # fallback: an Alt tap unlocks the foreground switch
        u32.SwitchToThisWindow(hwnd, True)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        return True
    except Exception:
        return False


def launch(name: str) -> bool:
    key = name.lower().strip()
    target = config.APPS.get(key)
    if target is None:
        m = difflib.get_close_matches(key, list(config.APPS), n=1, cutoff=0.6)
        target = config.APPS[m[0]] if m else key
    if target == "spotify":
        target = "spotify:"
    if key in _CODE_NAMES or target == config.APPS.get("vs code"):
        try:
            os.makedirs(config.DEMO_WORKSPACE, exist_ok=True)
            subprocess.Popen([config.APPS["vs code"], "--new-window", config.DEMO_WORKSPACE], creationflags=0x08000000)
            return True
        except Exception:
            return False
    if key == "terminal":
        try:
            subprocess.Popen(["wt.exe", "-w", "new"], creationflags=0x08000000)     # always a NEW window
            return True
        except Exception:
            return False
    try:
        os.startfile(target)
        return True
    except Exception:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=0x08000000)
            return True
        except Exception:
            return False


def open_or_focus(name: str) -> tuple[bool, str]:
    h = find_window(name)
    if h:
        return focus_hwnd(h), f"switched to {name}"
    n = name.lower().strip()
    term = n == "terminal"
    before = {w for w, _, p in running_windows() if p.lower() == "windowsterminal"} if term else set()
    if launch(name):
        for _ in range(60 if n in _CODE_NAMES else 30):     # wait for the window (VS Code cold start is slower), then focus it
            time.sleep(0.1)
            if term:
                new = [w for w, _, p in running_windows() if p.lower() == "windowsterminal" and w not in before]
                if new:
                    WHIS_TERMINALS.add(new[0])
            h = find_window(name)
            if h:
                focus_hwnd(h); break
        return True, f"opened {name}"
    return False, f"couldn't open {name}"
