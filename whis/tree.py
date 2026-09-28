"""UIA tree thread. Owns ALL COM. Keeps a warm Snapshot of the foreground window and runs ui_call() jobs.
Pattern: Windows-Use uia walker + Windows-MCP single-thread rule."""
import threading, queue, time, ctypes
from concurrent.futures import Future
import win32gui, win32process
from . import config, bus, eyes
from .types import Element, Snapshot

_snap: Snapshot | None = None
_lock = threading.Lock()
_calls: "queue.Queue" = queue.Queue()
_force = threading.Event()
skip_hwnds: set[int] = set()          # windows handled by browser.py (Playwright) — no UIA walk needed


def get_snapshot() -> Snapshot | None:
    with _lock:
        return _snap


def invalidate():
    _force.set()


def ui_call(fn, *a, timeout=2.0):
    """Run fn(*a) on the tree thread; returns its result (or raises)."""
    f = Future()
    _calls.put((f, fn, a))
    try:
        return f.result(timeout=timeout)
    except TimeoutError:
        f.cancel()          # never run a stale key/click after the caller gave up (e.g. a late Alt+F4)
        raise


def _drain():
    """Run queued ui_call jobs (tree thread only). Also called inside long walks so the executor never waits 1.5 s."""
    try:
        while True:
            f, fn, a = _calls.get_nowait()
            if not f.set_running_or_notify_cancel():
                continue
            try:
                f.set_result(fn(*a))
            except Exception as e:
                f.set_exception(e)
    except queue.Empty:
        pass


def _proc_name(hwnd) -> str:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        buf = ctypes.create_unicode_buffer(1024); n = ctypes.c_ulong(1024)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
        ctypes.windll.kernel32.CloseHandle(h)
        return buf.value.rsplit("\\", 1)[-1].replace(".exe", "")
    except Exception:
        return ""


def walk(auto, hwnd: int) -> Snapshot:
    root = auto.ControlFromHandle(hwnd)
    title = win32gui.GetWindowText(hwnd)
    app = _proc_name(hwnd)
    els, n, t0 = [], 0, time.perf_counter()
    for c, depth in auto.WalkControl(root, includeTop=False, maxDepth=config.TREE_MAX_DEPTH):
        if time.perf_counter() - t0 > 1.5 or len(els) >= config.TREE_MAX_ELEMENTS:
            break
        if not _calls.empty():
            _drain()
        try:
            tn = c.ControlTypeName
            if tn not in config.INTERACTIVE_TYPES or tn == "DocumentControl":
                continue
            if c.IsOffscreen:
                continue
            r = c.BoundingRectangle
            if r.right - r.left < 4 or r.bottom - r.top < 4:
                continue
            name = (c.Name or "").strip()
            if not name and tn == "EditControl":
                name = "text field"
            if not name:
                continue
            n += 1
            els.append(Element(f"e{n:02d}", tn.replace("Control", "").lower(), name[:60], (r.left, r.top, r.right, r.bottom), c, "uia"))
        except Exception:
            continue
    return Snapshot(hwnd, app, title, els)


def _loop():
    import uiautomation as auto
    global _snap
    with auto.UIAutomationInitializerInThread():
        last = (0, "", 0.0)
        while not bus.stop.is_set():
            # 1. drain UI calls first (executor is waiting)
            _drain()
            # 2. re-walk on foreground change / title change / force / staleness
            try:
                hwnd = win32gui.GetForegroundWindow()
                title = win32gui.GetWindowText(hwnd)
                stale = time.perf_counter() - last[2] > 3.0
                if hwnd and hwnd not in skip_hwnds and (hwnd != last[0] or title != last[1] or _force.is_set() or stale):
                    _force.clear()
                    t0 = time.perf_counter()
                    s = eyes.walk(auto, hwnd)           # deep cached UIA walk, ranked (legacy walk() for heavy trees)
                    if len(s.elements) < 3 and hwnd != last[0]:
                        _drain(); time.sleep(0.4); _drain()   # Chrome/Edge: first query activates a11y; retry once
                        s = eyes.walk(auto, hwnd)
                    s = eyes.augment(s)                 # + cached OCR lines when UIA is thin (OCR itself runs async)
                    with _lock:
                        _snap = s
                    last = (hwnd, title, time.perf_counter())
                    bus.log("tree", app=s.app, title=s.title[:60], n=len(s.elements), ms=round((time.perf_counter() - t0) * 1000),
                            nodes=getattr(s, "nodes", None), ocr=sum(1 for e in s.elements if e.source == "ocr"))
            except Exception as e:
                bus.log("events", kind="tree_error", err=repr(e)[:200])
            time.sleep(config.TREE_POLL_MS / 1000)


def start():
    from . import watchdog
    watchdog.spawn("tree", _loop)
    watchdog.spawn("eyes-web", eyes._web_loop)      # shadow-DOM web elements for eyes.snapshot() while the whis Brave is in front
