"""Eyes: richer screen text for Jev (and anything else that reads the shared snapshot). No GPU, no VLM.

1. deep_walk(hwnd)   - ONE cross-process UIA call (CacheRequest, TreeScope_Subtree) then an in-process walk of the cached
                       tree: depth 32, sidebar/window-chrome penalised, interactive + named elements ranked by relevance.
                       Chromium/Electron apps (Spotify, VS Code, Discord) hide their content at depth 14-30; this reaches it.
2. OCR fallback      - when a window yields < OCR_MIN useful elements, Windows.Media.Ocr (CPU, WinRT) runs on the foreground
                       window in a worker thread; text lines become pseudo-elements (role "text", ref None, source "ocr")
                       that executor._click clicks at the box centre. Cached per window + image hash; never on the hot path.
3. web adapter       - whis Brave in front: browser.py's DOM snapshot, or a richer web-elements command when one exists.

Public (any thread, cheap): snapshot(), state_text(max_elements), lines(snap, n), ocr_window(hwnd) (sync, ~200 ms).
Tree thread only: walk(auto, hwnd), augment(snap)."""
from __future__ import annotations
import threading, time, re, hashlib, asyncio
import win32gui
from . import bus, config
from .types import Element, Snapshot

DEPTH = 32
CAP = config.TREE_MAX_ELEMENTS               # snapshot size (60)
JEV_CAP = config.STATE_MAX_ELEMENTS          # what Jev's `target` choice sees (25) - the best-ranked go first
OCR_MIN = 8                                  # fewer useful UIA elements than this -> OCR the window
OCR_MIN_INTERVAL = 0.8                       # s between OCR checks of one window (a check = 1 capture + hash)
HEAVY_MS = 700                               # cached build slower than this -> legacy walker for that window for 30 s

# UIA ids
P_RECT, P_CT, P_NAME, P_FOCUS, P_ENABLED, P_AID, P_OFF = 30001, 30003, 30005, 30008, 30010, 30011, 30022
ROLE = {50000: "button", 50002: "checkbox", 50003: "combobox", 50004: "edit", 50005: "link", 50007: "listitem",
        50011: "menuitem", 50013: "radiobutton", 50019: "tabitem", 50024: "treeitem", 50031: "splitbutton",
        50029: "row", 50015: "slider", 50020: "text"}
BASE = {"edit": 3.0, "button": 2.5, "link": 2.5, "row": 2.4, "listitem": 2.2, "tabitem": 2.0, "menuitem": 2.0,
        "checkbox": 2.0, "radiobutton": 2.0, "combobox": 3.0, "treeitem": 1.8, "splitbutton": 2.0, "slider": 0.8, "text": 0.3}
CT_DOCUMENT, CT_TITLEBAR, CT_TEXT = 50030, 50037, 50020
MAIN_AIDS = {"main-view", "workbench.parts.editor", "workbench.panel.terminal"}
CONTAINERS = {50026, 50033, 50028, 50008, 50036, 50021}  # group, pane, datagrid, list, table, toolbar
PLAYER = re.compile(r"^(now playing bar|player controls|playback controls|media controls)$", re.I)
NOISE_NAMES = {"your library"}
GENERIC = {"play", "pause", "more options", "save", "like", "remove", "delete", "edit", "open", "more", "download", "share"}
NOISE_AID_PREFIX = ("Desktop_LeftSidebar", "workbench.parts.statusbar", "workbench.parts.titlebar")
CAPTION = {"minimize", "maximize", "restore", "close", "restore down", "system", "arrange split view"}

_heavy: dict[int, float] = {}                # hwnd -> until (perf_counter)


def _clean(s) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# ---------------------------------------------------------------- 1. deep UIA walk (tree thread)
def deep_walk(hwnd: int) -> Snapshot:
    """Tree thread only. One BuildUpdatedCache over the whole window, then rank. Raises on COM failure."""
    import uiautomation as auto
    from uiautomation.uiautomation import _AutomationClient
    from .tree import _proc_name
    ia = _AutomationClient.instance().IUIAutomation
    root = ia.ElementFromHandle(hwnd)
    cr = ia.CreateCacheRequest()
    for p in (P_RECT, P_CT, P_NAME, P_FOCUS, P_ENABLED, P_AID, P_OFF):
        cr.AddProperty(p)
    cr.TreeScope = 7                                          # element | children | descendants
    t0 = time.perf_counter()
    rc = root.BuildUpdatedCache(cr)
    build_ms = (time.perf_counter() - t0) * 1000
    if build_ms > HEAVY_MS:
        _heavy[hwnd] = time.perf_counter() + 30.0
    wr = rc.CachedBoundingRectangle
    W = (wr.left, wr.top, wr.right, wr.bottom)
    cands: dict[tuple, tuple] = {}                            # (role, name) -> (score, order, el, rect, focus)
    order = [0]
    nodes = [0]

    def kid_name(e, d=0) -> str:                              # first named descendant (unnamed Chromium buttons/rows)
        if d > 3:
            return ""
        kids = e.GetCachedChildren()
        if not kids:
            return ""
        for i in range(min(kids.Length, 6)):
            k = kids.GetElement(i)
            n = _clean(k.CachedName)
            if n:
                return n
            n = kid_name(k, d + 1)
            if n:
                return n
        return ""

    ww, wh = max(1, W[2] - W[0]), max(1, W[3] - W[1])

    def rec(e, d, region, web, doc, row):
        nodes[0] += 1
        if d > DEPTH or nodes[0] > 8000:
            return
        try:
            ct = e.CachedControlType
            aid = e.CachedAutomationId or ""
            off = e.CachedIsOffscreen
        except Exception:
            return
        if off and d > 0:
            return                                            # scrolled out / hidden subtree
        cname = ""
        if ct == CT_DOCUMENT:
            web, doc = True, _clean(e.CachedName)
        elif ct == CT_TITLEBAR:
            region = "chrome"
        elif ct in CONTAINERS and d > 0:
            cname = _clean(e.CachedName)
            try:
                r = e.CachedBoundingRectangle
            except Exception:
                r = None
            if aid in MAIN_AIDS or (doc and cname == doc):   # Spotify: the main view group is named like the page
                region = "main"
            elif aid.startswith(NOISE_AID_PREFIX) or cname.lower() in NOISE_NAMES:
                region = "noise"
            elif PLAYER.match(cname):
                region = "player"
            elif r is not None and region != "main" and r.left - W[0] < 24 and (r.right - r.left) < 0.35 * ww                     and (r.bottom - r.top) > 0.5 * wh:
                region = "side"                               # narrow full-height left column: nav / library sidebar
        if ct == 50029:
            row = kid_name(e) or _clean(e.CachedName) or row
        role = ROLE.get(ct)
        if role and d > 0:
            try:
                r = e.CachedBoundingRectangle
                rect = (r.left, r.top, r.right, r.bottom)
                w, h = rect[2] - rect[0], rect[3] - rect[1]
                cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
                if w >= 4 and h >= 4 and W[0] <= cx <= W[2] and W[1] <= cy <= W[3]:
                    name = _clean(e.CachedName)
                    if ct == 50029 or (not name and role in ("button", "link", "listitem", "row", "menuitem", "tabitem")):
                        name = kid_name(e) or name            # Spotify rows: own name is stale; the first child is current
                    if not name and role == "edit":
                        name = "text field"
                    if name and not (role == "text" and (len(name) < 2 or not web)):
                        if row and name.lower() in GENERIC and ct != 50029 and row.lower() != name.lower():
                            name = f"{name} {row}"            # 'Play' inside the 'Loser' row -> 'Play Loser'
                        s = BASE[role] + {"main": 2.0, "player": 2.5, "side": -1.5, "noise": -4.0, "chrome": -3.0}.get(region, 0.0)
                        if web:
                            s += 0.5
                        if role in ("edit", "combobox"):
                            s += 1.5                          # search boxes / inputs: always worth a slot
                        if name.lower() in CAPTION and cy - W[1] < 60:
                            s -= 3.0
                        focus = bool(e.CachedHasKeyboardFocus)
                        if focus:
                            s += 1.5
                        if not e.CachedIsEnabled:
                            s -= 1.5
                        if len(name) > 80:
                            s -= 0.5
                        order[0] += 1
                        key = (role if role != "text" else "text", name.lower()[:80])
                        prev = cands.get(key)
                        if role == "text" and any((rl, key[1]) in cands for rl in BASE if rl != "text"):
                            pass                              # text that only repeats a control's label
                        elif prev is None or s > prev[0]:
                            cands[key] = (s, prev[1] if prev else order[0], e, rect, name, role, region)
            except Exception:
                pass
        kids = e.GetCachedChildren()
        if kids:
            for i in range(kids.Length):
                rec(kids.GetElement(i), d + 1, region, web, doc, row)

    rec(rc, 0, "", False, "", "")
    ranked = sorted(cands.values(), key=lambda c: -c[0])[:CAP]
    top, later, n_main = [], [], 0
    for c in ranked:                                          # Jev's slice: best first, but keep ~8 slots for the
        if len(top) < JEV_CAP and (c[6] != "main" or n_main < JEV_CAP - 8):   # player bar / search box / top bar
            top.append(c); n_main += c[6] == "main"
        else:
            later.append(c)
    while len(top) < JEV_CAP and later:
        top.append(later.pop(0))
    top.sort(key=lambda c: c[1])                              # reading order ("the first result")
    later.sort(key=lambda c: c[1])
    els = []
    for s, _, e, rect, name, role, _r in top + later:
        try:
            ref = auto.Control.CreateControlFromElement(e)
        except Exception:
            ref = None
        els.append(Element(f"e{len(els)+1:02d}", role, name[:70], rect, ref, "uia"))
    snap = Snapshot(hwnd, _proc_name(hwnd), win32gui.GetWindowText(hwnd), els)
    snap.nodes, snap.build_ms = nodes[0], round(build_ms)
    return snap


def walk(auto, hwnd: int) -> Snapshot:
    """Tree thread: deep cached walk, legacy WalkControl walker for windows whose tree is too heavy to cache."""
    from . import tree
    if _heavy.get(hwnd, 0) > time.perf_counter():
        return tree.walk(auto, hwnd)
    try:
        s = deep_walk(hwnd)
    except Exception as e:
        bus.log("events", kind="eyes_walk_error", err=repr(e)[:200])
        return tree.walk(auto, hwnd)
    if len(s.elements) < OCR_MIN:                             # thin cached tree (e.g. XAML title bars): add the legacy walk
        try:
            have = {(e.role, e.name.lower()) for e in s.elements}
            for e in tree.walk(auto, hwnd).elements:
                role = "link" if e.role == "hyperlink" else e.role
                if (role, e.name.lower()) not in have:
                    have.add((role, e.name.lower()))
                    s.elements.append(Element(f"e{len(s.elements)+1:02d}", role, e.name, e.rect, e.ref, e.source))
        except Exception:
            pass
    return s


# ---------------------------------------------------------------- 2. OCR fallback (worker thread)
_ocr_cache: dict[int, dict] = {}              # hwnd -> {"hash", "t", "lines": [(text, rect)], "ms"}
_ocr_lock = threading.Lock()
_ocr_want = threading.Event()
_ocr_job: int | None = None                   # hwnd of the latest request; older requests are dropped
_ocr_thread: threading.Thread | None = None
_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from winrt.windows.media.ocr import OcrEngine
        _engine = OcrEngine.try_create_from_user_profile_languages()
    return _engine


def _capture(rect):
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=rect, all_screens=True)


def _img_hash(img) -> str:
    return hashlib.blake2b(img.convert("L").resize((96, 54)).tobytes(), digest_size=12).hexdigest()


async def _recognize(img):
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat
    from winrt.windows.storage.streams import DataWriter
    eng = _get_engine()
    from winrt.windows.media.ocr import OcrEngine
    mx = OcrEngine.max_image_dimension or 10000
    scale = 1.0
    if max(img.size) > mx:
        scale = mx / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    data = img.convert("RGBA").tobytes("raw", "BGRA")
    dw = DataWriter()
    dw.write_bytes(data)
    sb = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, img.width, img.height)
    res = await eng.recognize_async(sb)
    out = []
    for ln in res.lines:
        ws = [w.bounding_rect for w in ln.words]
        if not ws:
            continue
        l = min(b.x for b in ws); t = min(b.y for b in ws)
        r = max(b.x + b.width for b in ws); b_ = max(b.y + b.height for b in ws)
        out.append((_clean(ln.text), (int(l / scale), int(t / scale), int(r / scale), int(b_ / scale))))
    return out


def ocr_image(img) -> list[tuple[str, tuple]]:
    """Sync OCR of a PIL image -> [(text, (l, t, r, b) image px)]. Any thread that has no running event loop."""
    return asyncio.run(_recognize(img))


def _phys_rect(hwnd: int) -> tuple:
    """Window rect in physical px (same space as UIA rects and the click path), whatever this thread's DPI mode."""
    import ctypes
    u = ctypes.windll.user32
    old = u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))          # PER_MONITOR_AWARE_V2
    try:
        return tuple(win32gui.GetWindowRect(hwnd))
    finally:
        if old:
            u.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))


def ocr_window(hwnd: int) -> list[tuple[str, tuple]]:
    """Sync: capture the window's on-screen rect and OCR it -> [(text, screen rect, physical px)]. ~60-250 ms, CPU.
    The window must be in front (it is a screen capture)."""
    l, t, r, b = _phys_rect(hwnd)
    img = _capture((l, t, r, b))
    return [(tx, (x0 + l, y0 + t, x1 + l, y1 + t)) for tx, (x0, y0, x1, y1) in ocr_image(img)]


def _ocr_loop():
    global _ocr_job
    while not bus.stop.is_set():
        if not _ocr_want.wait(0.5):
            continue
        _ocr_want.clear()
        with _ocr_lock:
            job, _ocr_job = _ocr_job, None
        if not job:
            continue
        hwnd = job
        try:
            if win32gui.GetForegroundWindow() != hwnd or win32gui.IsIconic(hwnd):
                continue                                      # screen capture only shows the front window
            t0 = time.perf_counter()
            rect = _phys_rect(hwnd)
            img = _capture(rect)
            h = _img_hash(img)
            prev = _ocr_cache.get(hwnd)
            if prev and prev["hash"] == h:
                prev["t"] = time.perf_counter()
                continue
            raw = ocr_image(img)
            lines = [(tx, (x0 + rect[0], y0 + rect[1], x1 + rect[0], y1 + rect[1])) for tx, (x0, y0, x1, y1) in raw]
            ms = round((time.perf_counter() - t0) * 1000)
            _ocr_cache[hwnd] = {"hash": h, "t": time.perf_counter(), "lines": lines, "ms": ms}
            for k in list(_ocr_cache)[:-8]:
                _ocr_cache.pop(k, None)                       # keep the 8 most recent windows
            bus.log("tree", app="ocr", n=len(lines), ms=ms)
            from . import tree
            tree.invalidate()                                 # next tree pass merges the new lines
        except Exception as e:
            bus.log("events", kind="ocr_error", err=repr(e)[:200])


def _ensure_ocr_thread():
    global _ocr_thread
    if _ocr_thread is None or not _ocr_thread.is_alive():
        _ocr_thread = threading.Thread(target=_ocr_loop, name="eyes-ocr", daemon=True)
        _ocr_thread.start()


def request_ocr(hwnd: int):
    """Non-blocking. Latest request wins."""
    global _ocr_job
    with _ocr_lock:
        _ocr_job = hwnd
    _ensure_ocr_thread()
    _ocr_want.set()


_last_req: dict[int, float] = {}
_OCR_JUNK = re.compile(r"^[\W_]{0,3}$")


def augment(snap: Snapshot) -> Snapshot:
    """Tree thread: append cached OCR text lines when UIA is thin, and schedule a (cheap, hash-checked) OCR refresh.
    Never blocks."""
    if snap is None or not snap.hwnd:
        return snap
    useful = sum(1 for e in snap.elements if e.role != "text")
    if useful >= OCR_MIN:
        return snap
    hwnd = snap.hwnd
    try:
        if win32gui.IsIconic(hwnd):
            return snap
    except Exception:
        return snap
    now = time.perf_counter()
    if now - _last_req.get(hwnd, 0) > OCR_MIN_INTERVAL:
        _last_req[hwnd] = now
        request_ocr(hwnd)
    c = _ocr_cache.get(hwnd)
    if not c or now - c["t"] > 6.0:
        return snap                                           # no fresh OCR for this window yet
    have = {e.name.lower() for e in snap.elements}
    els = list(snap.elements)
    room = max(0, JEV_CAP + 10 - len(els))
    seen = set()
    for tx, r in sorted(c["lines"], key=lambda x: (x[1][1] // 12, x[1][0])):   # reading order
        if len(seen) >= room:
            break
        k = tx.lower()
        if _OCR_JUNK.match(tx) or len(tx) < 2 or k in have or k in seen:
            continue
        seen.add(k)
        els.append(Element(f"e{len(els)+1:02d}", "text", tx[:70], r, None, "ocr"))
    out = Snapshot(snap.hwnd, snap.app, snap.title, els, snap.t, snap.source)
    for a in ("nodes", "build_ms"):
        if hasattr(snap, a):
            setattr(out, a, getattr(snap, a))
    out.ocr_ms = c.get("ms")
    return out


# ---------------------------------------------------------------- 3. browser adapter
# browser.py's WEB_JS (shadow-DOM piercing, ranked in-view + main first) is reused through its generic `run` command.
# Each element gets a data-whis tag (its existing numeric one, else "w<i>"), so executor's click path
# (`browser.call("click", ref)` -> locator('[data-whis=ref]'), which pierces open shadow roots) works unchanged.
# window.__whisEls (the Claude brain's web_click ids) is restored afterwards.
_TAG_JS = """(max) => {
  const prev = window.__whisEls;
  const res = (%s)(max);
  const els = window.__whisEls || [];
  for (const o of (window.__eyesEls || [])) { try { const t = o.getAttribute('data-whis'); if (t && t[0] === 'w') o.removeAttribute('data-whis'); } catch (e) {} }
  const out = [];
  els.forEach((el, i) => { const x = res.els[i] || {}; const r = el.getBoundingClientRect();
    if (!el.hasAttribute('data-whis')) el.setAttribute('data-whis', 'w' + i);
    out.push({tag: el.getAttribute('data-whis'), role: x.role, name: x.name, val: x.val, off: x.off, rect: [r.left, r.top, r.right, r.bottom]}); });
  window.__eyesEls = els;
  window.__whisEls = prev;
  return {els: out, dpr: window.devicePixelRatio || 1, vh: window.innerHeight, url: res.url, title: document.title};
}"""
_ROLE_WEB = {"textbox": "edit", "searchbox": "edit", "radio": "radiobutton", "tab": "tabitem", "option": "listitem"}
_web: Snapshot | None = None
_web_ok = True                                # False once the browser.py in this checkout lacks WEB_JS / run


def _web_eval(page, js, cap):
    return page.evaluate(js, cap)


def web_snapshot_from(raw: dict, hwnd: int) -> Snapshot | None:
    """Tagged WEB_JS rows (CSS px) -> Snapshot in physical screen px (same maths as browser._snapshot)."""
    if not isinstance(raw, dict) or not raw.get("els"):
        return None
    zoom = raw.get("dpr") or 1.0
    left = top = 0
    if hwnd and win32gui.IsWindow(hwnd):
        x0, y0 = win32gui.ClientToScreen(hwnd, (0, 0))
        ch = win32gui.GetClientRect(hwnd)[3]
        left, top = x0, y0 + max(0, ch - int((raw.get("vh") or 0) * zoom))
    rows = [d for d in raw["els"] if d.get("tag") and d.get("name")]
    rows = [d for d in rows if not d.get("off")] + [d for d in rows if d.get("off")]      # on screen first
    els = []
    for d in rows[:CAP]:
        r = d.get("rect") or [0, 0, 0, 0]
        role = _ROLE_WEB.get(d.get("role") or "", d.get("role") or "control")
        name = _clean(d["name"]) + (f' (text: {_clean(d["val"])[:30]})' if d.get("val") else "")
        els.append(Element(f"e{len(els)+1:02d}", role, name[:70],
                           tuple(int(left + v * zoom) if k % 2 == 0 else int(top + v * zoom) for k, v in enumerate(r)),
                           d["tag"], "browser"))
    s = Snapshot(hwnd, "Brave", raw.get("title") or "", els, source="browser")
    s.url = raw.get("url")
    return s


def refresh_web(timeout: float = 2.0) -> Snapshot | None:
    """Blocking Playwright round trip (~10-40 ms) - call OFF the utterance path (eyes' web thread does).
    Shadow-DOM-aware web elements when possible, else browser.py's plain DOM snapshot, else a deep UIA walk."""
    global _web, _web_ok
    from . import browser, tree
    base = browser.get_snapshot()
    hwnd = getattr(browser, "_hwnd", 0) or (base.hwnd if base else 0)
    js = getattr(browser, "WEB_JS", None)
    if _web_ok and js:
        try:
            raw = browser.call("run", _web_eval, _TAG_JS % js.strip(), 70, timeout=timeout)
            if raw is False:
                _web_ok = False                               # no generic `run` command in this browser.py
            s = web_snapshot_from(raw, hwnd)
            if s and s.elements:
                _web = s
                return s
        except Exception as e:
            bus.log("events", kind="eyes_web_error", err=repr(e)[:200])
    if base and base.elements:
        return base
    if hwnd:
        try:
            s = tree.ui_call(deep_walk, hwnd, timeout=2.0)   # page with no DOM hits (canvas, PDF viewer): UIA
            _web = s
            return s
        except Exception:
            pass
    return base


def _web_loop():
    """Keeps the web snapshot fresh while the whis Brave is in front: after every browser.py snapshot, and every 2.5 s."""
    import sys
    last_base, last_t = None, 0.0
    while not bus.stop.is_set():
        time.sleep(0.4)
        br = sys.modules.get(__package__ + ".browser")
        if br is None or not br.ready.is_set():
            continue
        try:
            if not br.is_foreground():
                continue
            b = br.get_snapshot()
            bt = b.t if b else None
            if bt != last_base or time.perf_counter() - last_t > 2.5:
                last_base, last_t = bt, time.perf_counter()
                refresh_web()
        except Exception as e:
            bus.log("events", kind="eyes_web_error", err=repr(e)[:200])


# ---------------------------------------------------------------- public, cheap
def snapshot() -> Snapshot | None:
    """Best current snapshot, instant (cached). whis Brave in front -> shadow-DOM-aware web elements when they are at
    least as fresh as browser.py's snapshot, else browser.py's; otherwise deep UIA (+ OCR) from the tree thread.
    Drop-in for main.get_snapshot()."""
    from . import tree
    try:
        from . import browser
        if browser.is_foreground():
            b = browser.get_snapshot()
            if _web is not None and (b is None or (_web.t >= b.t and (not b.hwnd or _web.hwnd == b.hwnd))):
                return _web
            if b is not None:
                return b
    except Exception:
        pass
    return tree.get_snapshot()


def lines(snap: Snapshot | None, n: int = JEV_CAP) -> list[str]:
    """Element lines for Jev: `e01 button "Play"`; OCR text gets `text` as its role (clickable by its box)."""
    return snap.lines(n) if snap else []


def state_text(max_elements: int = JEV_CAP, snap: Snapshot | None = None) -> str:
    """One compact text block of what is on screen (app, title, top-ranked elements). Cheap: reads the cache."""
    s = snap if snap is not None else snapshot()
    if not s:
        return "screen: unknown"
    head = f'app: {s.app}\ntitle: "{s.title[:80]}"'
    body = "\n".join(lines(s, max_elements))
    return head + ("\nelements:\n" + body if body else "\nelements: none")
