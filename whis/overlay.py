"""Dynamic-Island overlay. tkinter on the MAIN thread only; other threads post to bus.overlay_q.
Messages: ("pill", text, state) | ("show", elements) | ("hide",) | ("flash", rect)
States: idle (small capsule) | listening (expanded, live transcript) | thinking | acting | asking.
Transparency: colour-key layered window; the key must be re-applied AFTER changing ex-style or it goes opaque."""
import tkinter as tk
import ctypes, queue, time
from . import bus

KEY = "#ff00ff"
COL = {"idle": "#4a4a50", "hearing": "#9a9aa0", "listening": "#00e5ff", "thinking": "#ffd500", "acting": "#3cff8a", "asking": "#ff7b00"}
_root = _canvas = None
_badges, _island = [], []
_state, _text, _t = "idle", "", 0.0
_pulse = 0
_used = False
_ox = _oy = 0           # canvas origin = virtual-screen origin; screen coords -> canvas = (x - vx, y - vy)


def pill(text: str, state: str = "listening"):
    bus.overlay_q.put(("pill", text, state))


def show(elements):
    bus.overlay_q.put(("show", elements))


def hide():
    bus.overlay_q.put(("hide",))


def flash(rect):
    bus.overlay_q.put(("flash", rect))


def toast(text):          # kept for compatibility: toast == acting pill
    bus.overlay_q.put(("pill", text, "acting"))


# ---------- drawing
def _rrect(x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return _canvas.create_polygon(pts, smooth=True, **kw)


_y = -70.0      # slide-down animation (target 8)


def _draw_island():
    global _island, _pulse, _y
    for i in _island:
        _canvas.delete(i)
    _island = []
    if _state == "idle" and not _used:
        _y = -70.0
        return                                  # hidden until first use
    _y += (8 - _y) * 0.5                        # ease toward y=8
    w = _root.winfo_screenwidth()
    col = COL.get(_state, COL["listening"])
    txt = "" if _state == "idle" else (_text[:110] if _state == "asking" else (_text[-70:] if len(_text) > 70 else _text))
    pw = 150 if _state == "idle" else max(420, min(int(w * 0.75), 120 + 11 * len(txt)))
    x0, x1, y0, y1 = _ox + w // 2 - pw // 2, _ox + w // 2 + pw // 2, _oy + int(_y), _oy + int(_y) + 54
    _island.append(_rrect(x0, y0, x1, y1, 27, fill="#0b0b0e", outline=""))
    _pulse = (_pulse + 1) % 12
    cy = (y0 + y1) // 2
    live = _state in ("listening", "hearing")
    heights = [6, 14, 10, 16] if live else [8, 8, 8, 8]
    bx0 = (x0 + pw // 2 - 14) if _state == "idle" else (x0 + 22)
    for k, h in enumerate(heights):                     # animated waveform bars
        hh = h + (4 if live and (k + _pulse) % 3 == 0 else 0)
        bx = bx0 + k * 7
        _island.append(_canvas.create_rectangle(bx, cy - hh // 2, bx + 4, cy + hh // 2, fill=col, outline=""))
    if _state != "idle":
        _island.append(_canvas.create_text(x0 + 62, cy, text=txt, anchor="w", fill="#f2f2f2", font=("Segoe UI", 15)))
        _island.append(_canvas.create_rectangle(x1 - 34, cy - 6, x1 - 22, cy + 6, fill="#e8e8e8", outline=""))   # stop glyph


def _handle(msg):
    global _state, _text, _t, _badges
    k = msg[0]
    if k == "pill":
        global _used
        _text, _state, _t = msg[1], msg[2], time.perf_counter()
        if _state in ("listening", "acting", "asking", "thinking"):
            _used = True
        _draw_island()
    elif k == "show":
        for i in _badges:
            _canvas.delete(i)
        _badges = []
        for n, e in enumerate(msg[1][:40], 1):
            l, t, r, b = e.rect
            l, t, r, b = l + _ox, t + _oy, r + _ox, b + _oy
            x, y = l, max(0, t - 20)
            _badges += [_canvas.create_rectangle(x, y, x + 28, y + 20, fill="#ffd500", outline="#000"),
                        _canvas.create_text(x + 14, y + 10, text=str(n), font=("Segoe UI", 10, "bold"), fill="#000"),
                        _canvas.create_rectangle(l, t, r, b, outline="#ffd500", width=2)]
    elif k == "hide":
        for i in _badges:
            _canvas.delete(i)
        _badges = []
    elif k == "flash":
        l, t, r, b = msg[1]
        l, t, r, b = l + _ox, t + _oy, r + _ox, b + _oy
        rid = _canvas.create_rectangle(l - 3, t - 3, r + 3, b + 3, outline="#00e5ff", width=4)
        _root.after(350, lambda: _canvas.delete(rid))


def _tick():
    global _state
    try:
        while True:
            _handle(bus.overlay_q.get_nowait())
    except queue.Empty:
        pass
    if bus.stop.is_set():
        _root.destroy(); return
    if _state != "idle" and time.perf_counter() - _t > (5.0 if _state in ("acting", "asking") else 3.0):
        _state = "idle"
    _draw_island()                   # redraw every tick: pulse / slide
    _root.after(60, _tick)


def _click_through(hwnd):
    GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, LWA_COLORKEY = -20, 0x80000, 0x20, 0x08000000, 0x80, 0x1
    u = ctypes.windll.user32
    for h in {hwnd, u.GetAncestor(hwnd, 2) or hwnd}:
        st = u.GetWindowLongW(h, GWL_EXSTYLE)
        u.SetWindowLongW(h, GWL_EXSTYLE, st | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        u.SetLayeredWindowAttributes(h, 0x00FF00FF, 0, LWA_COLORKEY)     # COLORREF 0x00BBGGRR → magenta


def run_mainloop():
    global _root, _canvas, _ox, _oy
    _root = tk.Tk()
    _root.overrideredirect(True)
    _root.attributes("-topmost", True)
    u = ctypes.windll.user32
    vx, vy, vw, vh = u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79)
    _root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    _ox, _oy = -vx, -vy                  # a monitor left of/above the primary makes vx/vy negative
    _root.configure(bg=KEY)
    _canvas = tk.Canvas(_root, bg=KEY, highlightthickness=0, bd=0)
    _canvas.pack(fill="both", expand=True)
    _root.update()
    _click_through(_root.winfo_id())
    _root.attributes("-transparentcolor", KEY)
    _draw_island()
    _root.after(60, _tick)
    _root.mainloop()
