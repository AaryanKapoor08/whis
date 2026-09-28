"""Playwright thread (sync API, single owner). Persistent Chrome profile. Commands via call(name, *args).
Element snapshot pattern from jev-ultrafast snapshot.js (visible interactive controls, indexed)."""
import threading, queue, time, ctypes, re
from concurrent.futures import Future
import win32gui, win32process
from . import config, bus, tree
from .types import Element, Snapshot

_q: "queue.Queue" = queue.Queue()
_page = None
_snap: Snapshot | None = None
_lock = threading.Lock()
_hwnd = 0
_brought = False           # whis has asked for the browser at least once (until then it must not take the foreground)
_launch_t = 0.0            # wall clock at launch; our browser's process was created after this
last_action_t = 0.0
ready = threading.Event()
_SSO = re.compile(r"^https?://(?:[^/]*\b(?:idp|login|sso|auth|adfs)\b[^/]*/|[^?]*/(?:saml2?|sso)\b)", re.I)

SNAPSHOT_JS = r"""
() => {
  const sel = 'a[href], button, input, select, textarea, [role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], [onclick], [tabindex]:not([tabindex="-1"])';
  const out = []; const seen = new Set();
  const vw = window.innerWidth, vh = window.innerHeight;
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= 60) break;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
    const st = getComputedStyle(el); if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    const cx = r.left + r.width/2, cy = r.top + r.height/2;
    const top = document.elementFromPoint(cx, cy); if (!top || !(el === top || el.contains(top))) continue;
    let name = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder || el.title || el.alt || '').trim().replace(/\s+/g,' ').slice(0,60);
    if (!name && el.tagName === 'INPUT') name = (el.type || 'text') + ' field';
    if (!name) continue;
    const key = name + '|' + Math.round(r.top);
    if (seen.has(key)) continue; seen.add(key);
    const role = el.tagName === 'A' ? 'link' : el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ? 'edit' : (el.getAttribute('role') || el.tagName.toLowerCase());
    el.setAttribute('data-whis', String(out.length));
    out.push({idx: out.length, role, name, rect: [r.left, r.top, r.right, r.bottom]});
  }
  return out;
}
"""


# Claude brain's web eyes: interactive elements across the whole page, PIERCING OPEN SHADOW ROOTS (D2L/Brightspace course
# cards, YouTube, ...). Ordered: in-viewport main content, in-viewport rest, then off-screen (main first). The elements are
# kept in window.__whisEls so click/type act on the exact node (Playwright ElementHandle: scrolls into view, real mouse).
WEB_JS = r"""
(max) => {
  const t0 = performance.now(), vw = innerWidth, vh = innerHeight;
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=tab],' +
    '[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=option],[role=checkbox],[role=radio],[role=switch],' +
    '[role=combobox],[role=textbox],[role=searchbox],[role=treeitem],[contenteditable=""],[contenteditable=true]';
  const found = [];
  const walk = (root) => {
    let all; try { all = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of all) {
      if (performance.now() - t0 > 400) return;
      if (el.matches(SEL)) found.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  const up = (n) => n.parentNode instanceof ShadowRoot ? n.parentNode.host : (n.parentElement || (n.parentNode && n.parentNode.host) || null);
  const inside = (n, anc) => { for (let k = 0; n && k < 200; k++, n = up(n)) if (n === anc) return true; return false; };
  const closestDeep = (n, sel) => { for (let k = 0; n && k < 200; k++, n = up(n)) if (n.matches && n.matches(sel)) return n; return null; };
  const deepPoint = (x, y) => { let e = document.elementFromPoint(x, y);
    for (let k = 0; e && e.shadowRoot && k < 30; k++) { const f = e.shadowRoot.elementFromPoint(x, y); if (!f || f === e) break; e = f; } return e; };
  const deepText = (n, budget) => { let s = '';
    const rec = (m) => { if (s.length > budget) return;
      if (m.nodeType === 3) { s += m.textContent + ' '; return; }
      if (m.nodeType !== 1 && m.nodeType !== 11) return;
      if (m.tagName === 'STYLE' || m.tagName === 'SCRIPT') return;
      if (m.shadowRoot) rec(m.shadowRoot);
      for (const c of (m.tagName === 'SLOT' ? m.assignedNodes() : m.childNodes)) rec(c); };
    rec(n); return s; };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const nameOf = (el) => {
    let n = el.getAttribute('aria-label');
    if (!n && el.getAttribute('aria-labelledby')) { const r = el.getRootNode();
      n = el.getAttribute('aria-labelledby').split(/\s+/).map(id => { const x = (r.getElementById ? r.getElementById(id) : null) || document.getElementById(id); return x ? x.textContent : ''; }).join(' '); }
    const tag = el.tagName;
    if (!clean(n) && (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT')) {
      if (el.labels && el.labels[0]) n = el.labels[0].innerText;
      n = clean(n) || el.placeholder || el.title || (['submit', 'button', 'reset'].includes(el.type) ? el.value : '') || el.name || '';
    }
    if (!clean(n)) n = el.innerText;
    if (!clean(n)) n = deepText(el, 200);
    if (!clean(n)) n = el.title || el.getAttribute('alt') || el.getAttribute('text') || '';
    if (!clean(n)) { const img = el.querySelector && el.querySelector('img[alt],[aria-label]'); if (img) n = img.getAttribute('alt') || img.getAttribute('aria-label'); }
    if (!clean(n) && el.getRootNode() instanceof ShadowRoot) { const h = el.getRootNode().host; n = h.getAttribute('text') || h.getAttribute('aria-label') || h.getAttribute('title') || ''; }
    return clean(n).slice(0, 80);
  };
  const roleOf = (el) => { const r = el.getAttribute('role'); if (r) return r;
    const t = el.tagName; if (t === 'A') return 'link'; if (t === 'SELECT') return 'combobox'; if (t === 'TEXTAREA') return 'textbox';
    if (t === 'INPUT') { const ty = (el.type || 'text').toLowerCase();
      return ['checkbox', 'radio'].includes(ty) ? ty : ['submit', 'button', 'reset', 'image'].includes(ty) ? 'button' : ty === 'search' ? 'searchbox' : 'textbox'; }
    if (t === 'SUMMARY') return 'button'; if (el.isContentEditable) return 'textbox'; return t === 'BUTTON' ? 'button' : t.toLowerCase(); };
  const rows = [], seen = new Set();
  for (const el of found) {
    if (el.disabled || el.getAttribute('aria-hidden') === 'true') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) continue;
    if (el.checkVisibility && !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    const inView = r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
    if (inView) {       // covered by a dialog/overlay -> not clickable
      const cx = Math.min(vw - 1, Math.max(0, r.left + r.width / 2)), cy = Math.min(vh - 1, Math.max(0, r.top + r.height / 2));
      const top = deepPoint(cx, cy);
      const sameComp = top && el.getRootNode() instanceof ShadowRoot && inside(top, el.getRootNode().host);   // card overlay link
      if (top && !inside(top, el) && !inside(el, top) && !sameComp) continue;
    }
    const name = nameOf(el), role = roleOf(el);
    if (!name && !['textbox', 'searchbox', 'combobox'].includes(role)) continue;
    const href = el.tagName === 'A' ? (el.getAttribute('href') || '') : '';
    const key = role + '|' + name.toLowerCase() + '|' + href;
    if (seen.has(key)) continue; seen.add(key);
    const main = !!closestDeep(el, 'main,[role=main],d2l-my-courses,#contents,#primary,article');
    let val = '';
    if (['textbox', 'searchbox', 'combobox'].includes(role)) val = (el.value ?? el.innerText ?? '').toString().slice(0, 40);
    rows.push({el, role, name: name || (role + ' field'), val, main, inView, y: r.top});
  }
  const rank = (x) => (x.inView ? 0 : 2) + (x.main ? 0 : 1);
  rows.sort((a, b) => rank(a) - rank(b) || (a.inView ? 0 : a.y - b.y));
  const keep = rows.slice(0, max);
  window.__whisEls = keep.map(x => x.el);
  return {n: rows.length, ms: Math.round(performance.now() - t0), url: location.href,
          els: keep.map((x, i) => ({i, role: x.role, name: x.name, val: x.val, off: !x.inView}))};
}
"""

POINT_JS = r"""
([x, y]) => { let e = document.elementFromPoint(x, y);
  for (let k = 0; e && e.shadowRoot && k < 30; k++) { const f = e.shadowRoot.elementFromPoint(x, y); if (!f || f === e) break; e = f; }
  if (!e) return '';
  const c = e.closest ? (e.closest('a,button,[role=button],[role=link],input,label') || e) : e;
  return ((c.getAttribute && c.getAttribute('aria-label')) || c.innerText || c.value || '').replace(/\s+/g, ' ').trim().slice(0, 80); }
"""

SCROLL_JS = r"""
(pxPerSec) => {
  if (window.__whisScroll) { clearInterval(window.__whisScroll); window.__whisScroll = null; }
  if (!pxPerSec) return 'stopped';
  const pick = () => { let e = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
    for (let k = 0; e && e.shadowRoot && k < 30; k++) { const f = e.shadowRoot.elementFromPoint(innerWidth / 2, innerHeight / 2); if (!f || f === e) break; e = f; }
    for (let k = 0; e && k < 60; k++) { const s = getComputedStyle(e);
      if (/(auto|scroll)/.test(s.overflowY) && e.scrollHeight > e.clientHeight + 20) return e;
      e = e.parentNode instanceof ShadowRoot ? e.parentNode.host : e.parentElement; }
    return document.scrollingElement || document.documentElement; };
  const t = pick(); const step = pxPerSec / 60;
  let last = -1, still = 0;
  window.__whisScroll = setInterval(() => { t.scrollBy(0, step);
    if (Math.abs(t.scrollTop - last) < 0.5) { if (++still > 90) { clearInterval(window.__whisScroll); window.__whisScroll = null; } } else still = 0;
    last = t.scrollTop; }, 16);
  return 'scrolling ' + (t === document.scrollingElement ? 'page' : (t.tagName || '').toLowerCase());
}
"""


def get_snapshot() -> Snapshot | None:
    with _lock:
        return _snap


def _proc_start_time(hwnd) -> float:
    """Process creation time (unix seconds) of the window's owner; 0 on failure."""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return 0.0
        ft = (ctypes.c_ulonglong * 4)()
        ok = ctypes.windll.kernel32.GetProcessTimes(h, ctypes.byref(ft, 0), ctypes.byref(ft, 8), ctypes.byref(ft, 16), ctypes.byref(ft, 24))
        ctypes.windll.kernel32.CloseHandle(h)
        return (ft[0] / 1e7 - 11644473600.0) if ok else 0.0
    except Exception:
        return 0.0


def _is_ours(hwnd) -> bool:
    """A Brave window that belongs to the instance we launched (not the user's own Brave)."""
    if not hwnd or not win32gui.IsWindowVisible(hwnd):
        return False
    if not win32gui.GetWindowText(hwnd).endswith(config.BROWSER_TITLE_SUFFIX):
        return False
    return _launch_t > 0 and _proc_start_time(hwnd) >= _launch_t - 1.0


def is_foreground() -> bool:
    """True when our Playwright window is in front (or a browser command just ran and focus is still settling)."""
    try:
        h = win32gui.GetForegroundWindow()
        if _hwnd and h == _hwnd:
            return True
        if _page is not None and _is_ours(h):
            return True
        if time.perf_counter() - last_action_t < 4.0:          # chained browser commands: focus may lag
            from .tree import _proc_name
            return _proc_name(h).lower() in ("brave", "chrome", "python", "pythonw", "")
        return False
    except Exception:
        return False


def call(name: str, *args, timeout=15.0):
    f = Future()
    _q.put((f, name, args))
    return f.result(timeout=timeout)


def _find_hwnd(title: str) -> int:
    """Our own top-level Brave window: process started after launch; prefer a title match."""
    hits = []

    def cb(h, _):
        if _is_ours(h):
            hits.append((0 if title[:30] in win32gui.GetWindowText(h) else 1, h))
    win32gui.EnumWindows(cb, None)
    hits.sort()
    return hits[0][1] if hits else 0


def _snapshot():
    global _snap, _hwnd
    try:
        raw = _page.evaluate(SNAPSHOT_JS)
        # CSS px -> physical px: window position + device scale
        zoom = _page.evaluate("window.devicePixelRatio") or 1.0
        left, top = 0, 0
        if _hwnd:
            # viewport = bottom of the client area (GetWindowRect includes the invisible 8 px resize border when maximized)
            x0, y0 = win32gui.ClientToScreen(_hwnd, (0, 0))
            ch = win32gui.GetClientRect(_hwnd)[3]
            vh = _page.evaluate("window.innerHeight") or 0
            left, top = x0, y0 + max(0, ch - int(vh * zoom))
        els = [Element(f"e{i+1:02d}", e["role"], e["name"], tuple(int(left + v * zoom) if k % 2 == 0 else int(top + v * zoom) for k, v in enumerate(e["rect"])), e["idx"], "browser")
               for i, e in enumerate(raw)]
        title = _page.title()
        if not _hwnd or not win32gui.IsWindow(_hwnd):
            _hwnd = _find_hwnd(title)
            if _hwnd:
                tree.skip_hwnds.add(_hwnd)
        with _lock:
            _snap = Snapshot(_hwnd, "Brave", title, els, source="browser")
        bus.log("tree", app="browser", title=title[:60], n=len(els))
    except Exception as e:
        bus.log("events", kind="browser_snapshot_error", err=repr(e)[:200])


def _loop():
    global _page, _launch_t
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        kw = {"executable_path": config.BROWSER_EXE} if config.BROWSER_EXE else {"channel": "chrome"}
        # Look like the user's normal browser: keep extensions, no "controlled by automation" bar, no webdriver flag.
        ignore = ["--enable-automation"] + (["--disable-extensions", "--disable-component-extensions-with-background-pages"]
                                            if config.BROWSER_KEEP_EXTENSIONS else [])
        args = ["--force-renderer-accessibility", "--start-maximized", "--disable-blink-features=AutomationControlled",
                "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble", "--hide-crash-restore-bubble"]
        _launch_t = time.time()
        ctx = p.chromium.launch_persistent_context(config.BROWSER_PROFILE, headless=False, no_viewport=True,
                                                   args=args, ignore_default_args=ignore, **kw)
        _page = ctx.pages[0] if ctx.pages else ctx.new_page()
        closed = threading.Event()
        ctx.on("close", lambda _: closed.set())     # user closed the window -> relaunch via watchdog
        ctx.on("page", _on_new_page)
        _page.on("load", lambda _: _snapshot())
        _page.on("close", _on_page_closed)
        _snapshot()
        ready.set()
        threading.Thread(target=_yield_focus, daemon=True).start()
        bus.log("events", kind="browser_ready", profile=config.BROWSER_PROFILE, extensions=config.BROWSER_KEEP_EXTENSIONS)
        while not bus.stop.is_set():
            if closed.is_set():
                raise RuntimeError("browser window closed")
            try:
                f, name, args = _q.get(timeout=0.1)
            except queue.Empty:
                try:
                    _page.wait_for_timeout(5)       # pump Playwright: load / new-tab / close events only fire inside API calls
                except Exception:
                    pass
                continue
            try:
                f.set_result(_do(name, *args))
            except Exception as e:
                bus.log("events", kind="browser_error", cmd=name, err=repr(e)[:200])
                f.set_result(False)


def _on_new_page(page):
    """A link opened a new tab: follow it so commands and snapshots target what the user sees."""
    global _page
    _page = page
    page.on("load", lambda _: _snapshot())
    page.on("close", _on_page_closed)   # the "load" handler re-snapshots once the tab has content


def _on_page_closed(page):
    global _page
    if page is _page:
        try:
            others = [p for p in page.context.pages if not p.is_closed()]
            _page = others[-1] if others else page.context.new_page()
            _snapshot()
        except Exception as e:                  # context is closing (last window closed)
            bus.log("events", kind="browser_page_closed_error", err=repr(e)[:200])


def _do(name, *a):
    global last_action_t
    last_action_t = time.perf_counter()
    if name == "goto":
        _page.goto(a[0], wait_until="domcontentloaded")
        if _SSO.search(_page.url):          # SSO bounce (D2L -> idp.unb.ca / Microsoft -> back): wait until it lands
            try:
                _page.wait_for_url(lambda u: not _SSO.search(u), timeout=10000)
                _page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass                        # a real login page: leave it for the user
        _bring()
        try:
            _page.wait_for_load_state("networkidle", timeout=1500)     # JS-rendered content (D2L cards, YouTube grid)
        except Exception:
            pass
        _snapshot()
        if _snap is not None and not _snap.elements:      # JS-rendered page (MS login, D2L): give it one more beat
            time.sleep(0.7); _snapshot()
        return True
    if name == "search":
        from urllib.parse import quote_plus
        return _do("goto", "https://www.bing.com/search?q=" + quote_plus(a[0]))
    if name == "back":
        _page.go_back(wait_until="domcontentloaded"); time.sleep(0.3); _snapshot(); return True
    if name == "bring_to_front":
        _bring(); return True
    if name == "click":
        _page.locator(f'[data-whis="{a[0]}"]').first.click(timeout=3000); time.sleep(0.3); _snapshot(); return True
    if name == "type_focused":
        _page.keyboard.type(a[0], delay=5); return True
    if name == "scroll":
        _page.mouse.wheel(0, 600 * a[0]); time.sleep(0.2); _snapshot(); return True
    if name == "page_text":
        return (_page.title() + chr(10) + _page.inner_text("body"))[:8000]
    if name == "snapshot":
        _snapshot(); return True
    if name == "url":                   # Claude brain: verify navigation
        return _page.url
    if name == "focused_value":         # Claude brain: read typed text back
        return _page.evaluate("() => { const e = document.activeElement; return e ? (e.value ?? e.innerText ?? '') : '' }")
    # ---- Claude brain web tools (shadow-DOM piercing element list, click/type by id or text, viewport mouse, smooth scroll)
    if name == "web_elements":
        return _page.evaluate(WEB_JS, a[0] if a else 70)
    if name in ("web_click", "web_type"):
        try:
            return _web_click(a[0]) if name == "web_click" else _web_type(*a)
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {str(e).splitlines()[0][:150]}"}
    if name == "web_point":             # (x, y) CSS px -> name of the clickable thing there (confirmation check)
        return _page.evaluate(POINT_JS, [a[0], a[1]])
    if name == "mouse_click":           # (x, y) CSS px in the viewport
        before = _page.url
        _page.mouse.click(a[0], a[1]); return _settle(before)
    if name == "screenshot":            # viewport JPEG in CSS px (matches mouse coordinates) + [innerWidth, innerHeight]
        vp = _page.evaluate("[innerWidth, innerHeight]")
        return _page.screenshot(type="jpeg", quality=70, scale="css", timeout=5000), vp
    if name == "scroll_smooth":         # px per second (negative = up), 0 = stop
        return _page.evaluate(SCROLL_JS, a[0])
    if name == "run":                   # run fn(page, *args) on this thread (d2l.py: API calls with the session's cookies)
        return a[0](_page, *a[1:])
    return False


def _settle(before: str, quick=False) -> dict:
    """After a click: wait briefly for a navigation (full load or SPA URL change) and the page to render."""
    t = time.perf_counter()
    while time.perf_counter() - t < 0.6 and _page.url == before:
        _page.wait_for_timeout(50)
    changed = _page.url != before
    if changed:
        try:
            _page.wait_for_load_state("domcontentloaded", timeout=6000)
        except Exception:
            pass
        try:
            _page.wait_for_load_state("networkidle", timeout=1500)
        except Exception:
            pass
        _page.wait_for_timeout(250)         # SPA route change (YouTube, D2L): let the title/content catch up
    else:
        _page.wait_for_timeout(150 if quick else 300)
    try:
        title = _page.title()
    except Exception:
        title = ""
    return {"ok": True, "url": _page.url, "title": title, "navigated": changed}


def _best(rows, text, kinds=None):
    """Index of the element whose name best matches `text` (exact > prefix > word > substring), preferring on-screen."""
    q = " ".join(text.lower().split())
    best, score = None, 0
    for r in rows:
        if kinds and r["role"] not in kinds:
            continue
        n = " ".join(r["name"].lower().split())
        s = 4 if n == q else 3 if n.startswith(q) else 2 if f" {q} " in f" {n} " else 1 if q in n else 0
        if s and not r.get("off"):
            s += 0.5
        if s > score:
            best, score = r["i"], s
    return best


def _handle(i):
    return _page.evaluate_handle("(i) => (window.__whisEls || [])[i] || null", i).as_element()


def _web_click(target) -> dict:
    before = _page.url
    el, name = None, str(target)
    if isinstance(target, int):
        el = _handle(target)
        if el is None:
            return {"ok": False, "msg": "that element is gone (the page changed); use the ids from the latest STATE"}
    else:
        snap = _page.evaluate(WEB_JS, 400)
        i = _best(snap["els"], name)
        if i is not None:
            el, name = _handle(i), snap["els"][i]["name"]
        else:
            loc = _page.get_by_text(name, exact=False)
            try:
                n = min(loc.count(), 8)
                el = next((loc.nth(k).element_handle() for k in range(n) if loc.nth(k).is_visible()), None)
            except Exception:
                el = None
            if el is None:
                return {"ok": False, "msg": f"no visible link/button/text matching '{name}' on the page"}
    try:
        el.click(timeout=2500)
    except Exception as e:                  # covered / animating / zero-size wrapper: a DOM click still follows the link
        try:
            el.evaluate("e => e.click()")
        except Exception:
            return {"ok": False, "msg": f"couldn't click it ({type(e).__name__}: {str(e).splitlines()[0][:120]})"}
    return dict(_settle(before), clicked=(None if isinstance(target, int) else name[:80]))


def _web_type(target, text, submit=False) -> dict:
    before = _page.url
    el = None
    if isinstance(target, int):
        el = _handle(target)
    else:
        snap = _page.evaluate(WEB_JS, 400)
        i = _best(snap["els"], str(target), kinds={"textbox", "searchbox", "combobox"})
        if i is None and not str(target).strip():
            i = next((r["i"] for r in snap["els"] if r["role"] in ("textbox", "searchbox", "combobox")), None)
        if i is not None:
            el = _handle(i)
        else:
            for loc in (_page.get_by_label(str(target)), _page.get_by_placeholder(str(target))):
                try:
                    if loc.count() and loc.first.is_visible():
                        el = loc.first.element_handle(); break
                except Exception:
                    pass
    if el is None:
        return {"ok": False, "msg": f"no text field matching '{target}'"}
    el.click(timeout=3000)
    try:
        el.fill(text, timeout=3000)
    except Exception:                       # not fillable (custom widget): select-all + type into whatever took focus
        _page.keyboard.press("Control+a"); _page.keyboard.type(text, delay=5)
    val = _page.evaluate("() => { let e = document.activeElement; while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;"
                         " return e ? String(e.value ?? e.innerText ?? '') : '' }")
    out = {"ok": True, "value": val[:200]}
    if submit:
        _page.keyboard.press("Enter")
        out.update(_settle(before))
    return out


def _yield_focus():
    """Brave's window shows up 1-3 s after launch and grabs the foreground - on top of whatever the user (or a
    fast-path 'open spotify') brought up meanwhile. Until whis actually asks for the browser, hand the foreground back
    and keep Brave minimized; _bring() restores it the first time it's needed."""
    import win32con
    global _hwnd
    t0, last_other = time.time(), 0
    while time.time() - t0 < 10 and not _brought and not bus.stop.is_set():
        fg = win32gui.GetForegroundWindow()
        if fg and _is_ours(fg):
            _hwnd = _hwnd or fg
            tree.skip_hwnds.add(fg)
            win32gui.ShowWindow(fg, win32con.SW_MINIMIZE)
            if last_other and win32gui.IsWindow(last_other):
                from .apps import focus_hwnd
                focus_hwnd(last_other)
            bus.log("events", kind="browser_yielded_focus", to=win32gui.GetWindowText(last_other)[:60] if last_other else "")
            return
        if fg:
            last_other = fg
        time.sleep(0.05)


def _bring():
    global _hwnd, _brought
    _brought = True
    try:
        _page.bring_to_front()
        if not _hwnd:
            _hwnd = _find_hwnd(_page.title())
            if _hwnd:
                tree.skip_hwnds.add(_hwnd)
        if _hwnd:
            from .apps import focus_hwnd
            focus_hwnd(_hwnd)
    except Exception:
        pass


def start():
    from . import watchdog
    watchdog.spawn("browser", _loop, restart_delay=2.0)
