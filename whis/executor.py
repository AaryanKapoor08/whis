"""Runs one Action. Every UIA call goes through tree.ui_call (single COM thread). Hard tools before clicks."""
import time, ctypes, threading
from . import config, bus, tree, apps
from .types import Action, Outcome, Snapshot

VK = {"volume_down": 0xAE, "volume_up": 0xAF, "media_play_pause": 0xB3, "mute": 0xAD}
TERMINAL_PROCS = {"code", "cursor", "windowsterminal", "powershell", "pwsh", "cmd", "conhost", "openconsole", "wezterm-gui", "alacritty"}


def _vk(code):
    ctypes.windll.user32.keybd_event(code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(code, 0, 2, 0)


def _send_keys(keys: str):
    import uiautomation as auto
    auto.SendKeys(keys, interval=0.01, waitTime=0)


def _click(el) -> str:
    """Invoke pattern → Toggle/Select → real click at centre. Runs on tree thread."""
    c = el.ref
    try:
        p = c.GetInvokePattern()
        if p:
            p.Invoke(); return "invoked"
    except Exception:
        pass
    for getter, meth in (("GetTogglePattern", "Toggle"), ("GetSelectionItemPattern", "Select"), ("GetExpandCollapsePattern", "Expand")):
        try:
            p = getattr(c, getter)()
            if p:
                getattr(p, meth)(); return meth.lower()
        except Exception:
            pass
    try:
        c.Click(simulateMove=False, waitTime=0); return "clicked"
    except Exception:
        pass
    l, t, r, b = el.rect
    import pyautogui
    pyautogui.click((l + r) // 2, (t + b) // 2); return "clicked_xy"


def _spotify_play(query: str) -> Outcome:
    """Type the query into Spotify's search (only while Spotify is the foreground window - keys never go anywhere else),
    wait until a 'Search results' row matches the query, Invoke that row's Play button, and confirm playback started.
    Rows' own Name can be stale after a re-render; the first child's name is current. The sidebar's
    'Play <playlist>' buttons are never considered. Own COM init: the polling must not stall the tree thread."""
    import re, psutil, uiautomation as auto
    from difflib import SequenceMatcher
    from uiautomation.uiautomation import _AutomationClient
    target = re.split(r"\s+by\s+", query.strip(), maxsplit=1, flags=re.I)[0].lower()   # "Loser by Tame Impala" -> the song
    with auto.UIAutomationInitializerInThread():
        ia = _AutomationClient.instance().IUIAutomation
        prop = ia.CreatePropertyCondition

        def window():
            for w in auto.GetRootControl().GetChildren():
                try:
                    if w.ClassName == "Chrome_WidgetWin_1" and psutil.Process(w.ProcessId).name().lower() == "spotify.exe":
                        return w
                except Exception:
                    pass

        def rows(w):
            g = w.Element.FindFirst(4, prop(30005, "Search results"))       # NameProperty
            if not g:
                return []
            out = []
            for r in auto.Control.CreateControlFromElement(g).GetChildren():
                if r.ControlTypeName == "DataItemControl":
                    kids = r.GetChildren()
                    out.append((r, ((kids[0].Name if kids else "") or r.Name or "").strip()))
            return out

        def now_playing(w):
            bar = w.Element.FindFirst(4, prop(30005, "Now playing bar"))
            if not bar:
                return None, ""
            bar = auto.Control.CreateControlFromElement(bar)
            paused = bar.Element.FindFirst(4, prop(30005, "Pause")) is not None
            links = bar.Element.FindAll(4, prop(30003, 50005))                # HyperlinkControl: title / artist
            return paused, " ".join(links.GetElement(i).CurrentName for i in range(links.Length))

        if _fg_proc() != "spotify":
            apps.open_or_focus("spotify")
            t = time.perf_counter()
            while _fg_proc() != "spotify" and time.perf_counter() - t < 4.0:     # restoring a minimized Spotify took 3.2 s
                time.sleep(0.05)
        w = window()
        if _fg_proc() != "spotify" or w is None:
            return Outcome(False, "Spotify didn't come to the front")
        was_playing, np_before = now_playing(w)
        before = [lbl for _, lbl in rows(w)][:3]
        _send_keys("{Ctrl}l")
        box, t = None, time.perf_counter()
        while time.perf_counter() - t < 1.0:          # keys go in only once focus is Spotify's own search box
            f = auto.GetFocusedControl()
            if f and f.ControlTypeName in ("ComboBoxControl", "EditControl") and "play" in (f.Name or "").lower() and _fg_proc() == "spotify":
                box = f; break
            time.sleep(0.05)
        if box is None:
            return Outcome(False, "couldn't reach Spotify's search box")
        _send_keys("{Ctrl}a"); _type(query); time.sleep(0.15)
        _send_keys("{Enter}")                         # typing only shows suggestions; Enter loads the results page
        best, t0 = None, time.perf_counter()
        while time.perf_counter() - t0 < 4.0:
            time.sleep(0.15)
            rs = rows(w)
            scored = sorted(((SequenceMatcher(None, target, lbl.lower()).ratio(), i, r, lbl) for i, (r, lbl) in enumerate(rs[:8])),
                            key=lambda x: (-x[0], x[1]))
            if scored and scored[0][0] >= 0.8:
                best = scored[0]; break
            if scored and [lbl for _, lbl in rs][:3] != before and time.perf_counter() - t0 > 2.5:
                best = min(scored, key=lambda x: x[1]); break                  # results changed but no close name: top result
        if best is None:
            return Outcome(False, f"no Spotify results for {query}")
        _, _, row, label = best
        btn = auto.ButtonControl(searchFromControl=row, searchDepth=6, RegexName=r"^Play")
        if not btn.Exists(0, 0):
            return Outcome(False, f"no play button for {label}")
        try:
            btn.GetInvokePattern().Invoke()
        except Exception:
            btn.Click(simulateMove=False, waitTime=0)
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < 2.0:
            playing, np_now = now_playing(w)
            if playing and (not was_playing or np_now != np_before or label.lower() in np_now.lower()):   # already on it = fine
                return Outcome(True, f"playing {label}")
            time.sleep(0.1)
        return Outcome(False, f"pressed play on {label}, but nothing started")


def _type(text: str) -> str:
    """Clipboard paste: instant and never reorders characters (SendKeys raced in the new Notepad)."""
    import uiautomation as auto, pyperclip
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    pyperclip.copy(text)
    auto.SendKeys("{Ctrl}v", waitTime=0.05)
    if old is not None:
        threading.Timer(1.0, lambda: pyperclip.copy(old)).start()
    return "pasted"


def run(a: Action, snap: Snapshot | None) -> Outcome:
    t0 = time.perf_counter()
    try:
        i, g = a.intent, a.args
        if i in ("open_app", "focus_app"):
            name = g["app"]
            if name.lower() in config.BROWSER_NAMES:
                from . import browser
                ok = browser.call("bring_to_front"); out = Outcome(bool(ok), "browser up")
            else:
                ok, msg = apps.open_or_focus(name); out = Outcome(ok, msg)
        elif i == "close_window":
            fgp = _fg_proc()                        # real foreground, not a possibly stale snapshot
            if fgp in config.PROTECTED_APPS or (snap and snap.app.lower() in config.PROTECTED_APPS):
                out = Outcome(False, f"won't close {fgp or snap.app}")
            else:
                tree.ui_call(_send_keys, "{Alt}{F4}"); out = Outcome(True, "closed")
        elif i == "click_element":
            el = g["target"]
            if el.source == "browser":
                from . import browser
                out = Outcome(bool(browser.call("click", el.ref)), f"clicked {el.name}")
            else:
                how = tree.ui_call(_click, el); out = Outcome(True, f"{how} {el.name}")
        elif i == "type_text":
            if _browser_foreground():
                from . import browser
                ok = browser.call("type_focused", g["text"]); out = Outcome(bool(ok), "typed")
            else:
                how = tree.ui_call(_type, g["text"]); out = Outcome(True, how)
        elif i == "press_key":
            tree.ui_call(_send_keys, config.KEYS[g["key"]]); out = Outcome(True, f"pressed {g['key']}")
        elif i == "save":
            tree.ui_call(_send_keys, "{Ctrl}s")
            out = Outcome(True, "saved")
            for _ in range(3):                      # a Save As dialog (new file) is its own window: give the tree 3 polls
                time.sleep(0.3)
                import win32gui                     # title of the real foreground (the tree walk of the dialog can take >1 s)
                if "save as" in win32gui.GetWindowText(win32gui.GetForegroundWindow()).lower():
                    tree.ui_call(_send_keys, config.DEMO_FILE + "{Enter}")
                    out = Outcome(True, f"saved as {config.DEMO_FILE.rsplit(chr(92), 1)[-1]}")
                    break
        elif i in ("scroll_up", "scroll_down"):
            if _browser_foreground():
                from . import browser
                browser.call("scroll", 1 if i == "scroll_down" else -1); out = Outcome(True, i)
            else:
                import uiautomation as auto
                tree.ui_call(auto.WheelDown if i == "scroll_down" else auto.WheelUp, 4); out = Outcome(True, i)
        elif i == "go_back":
            if _browser_foreground():
                from . import browser
                browser.call("back")
            else:
                tree.ui_call(_send_keys, "{Alt}{Left}")
            out = Outcome(True, "back")
        elif i == "navigate_url":
            from . import browser
            url = g.get("url") or ""
            if not url:
                out = Outcome(False, "no url configured")
            else:
                ok = browser.call("goto", url); out = Outcome(bool(ok), f"opened {url.split('//')[-1][:40]}")
        elif i == "search_in_app":
            app = g.get("app")
            if app and app.lower() not in config.BROWSER_NAMES:
                apps.open_or_focus(app); time.sleep(0.6)
            if _browser_foreground() and not app:
                tree.ui_call(_send_keys, "{Ctrl}f"); time.sleep(0.2)
                tree.ui_call(_type, g["text"]); out = Outcome(True, f"find {g['text']}")
            else:
                snap2 = tree.get_snapshot()
                proc = (app or (snap2.app if snap2 else "") or "default").lower()
                keys = next((v for k, v in config.IN_APP_SEARCH_KEYS.items() if k in proc), config.IN_APP_SEARCH_KEYS["default"])
                tree.ui_call(_send_keys, keys); time.sleep(0.35)
                tree.ui_call(_send_keys, "{Ctrl}a"); tree.ui_call(_type, g["text"]); time.sleep(0.15)
                tree.ui_call(_send_keys, "{Enter}")
                out = Outcome(True, f"searched {g['text']}")
        elif i == "open_terminal":
            import win32gui
            fg = _fg_proc() or (snap.app.lower() if snap else "")
            if fg in ("code", "cursor") or " in it" in a.said.lower() or "vs code" in a.said.lower():
                if not (fg == "code" and apps.is_demo_code(win32gui.GetForegroundWindow())):
                    apps.open_or_focus("code")                 # the whis VS Code window only (never the user's own / Cursor)
                    for _ in range(40):
                        if apps.is_demo_code(win32gui.GetForegroundWindow()):
                            break
                        time.sleep(0.1)
                if apps.is_demo_code(win32gui.GetForegroundWindow()):
                    tree.ui_call(_send_keys, "{Ctrl}`"); out = Outcome(True, "terminal opened")
                else:
                    out = Outcome(False, "the whis VS Code window didn't come to the front")
            else:
                ok, msg = apps.open_or_focus("terminal"); out = Outcome(ok, msg)
        elif i == "run_command":
            time.sleep(0.3)
            import win32gui
            fg, h = _fg_proc(), win32gui.GetForegroundWindow()
            if not apps.safe_terminal(h):     # only whis's own terminals / the whis VS Code window: never the user's
                out = Outcome(False, f"refused: {fg or 'this window'} isn't a terminal whis opened")   # (their Claude Code runs there)
            else:
                tree.ui_call(_type, g["text"]); time.sleep(0.15); tree.ui_call(_send_keys, "{Enter}")
                out = Outcome(True, f"ran {g['text']}")
        elif i == "play_song":
            out = _spotify_play(g["text"])
            tree.invalidate()
        elif i == "ask_screen":
            from . import planner
            text = ""
            if _browser_foreground():
                from . import browser
                text = browser.call("page_text") or ""
            if not text:
                snap2 = tree.get_snapshot()
                text = chr(10).join(f"{e.role}: {e.name}" for e in (snap2.elements if snap2 else []))
                if snap2: text = f"{snap2.app} - {snap2.title}" + chr(10) + text
            ans = planner.answer(g["question"], text)
            out = Outcome(bool(ans), ans or "couldn't read the screen")
        elif i == "search_web":
            from . import browser
            ok = browser.call("search", g["text"]); out = Outcome(bool(ok), f"searching {g['text']}")
        elif i in VK:
            _vk(VK[i]); out = Outcome(True, i.replace("_", " "))
        else:
            out = Outcome(False, f"unknown intent {i}")
    except Exception as e:
        bus.log("events", kind="exec_error", intent=a.intent, err=repr(e)[:200])
        out = Outcome(False, "fail")
    tree.invalidate()
    bus.log("exec", intent=a.intent, ok=out.ok, msg=out.msg, ms=round((time.perf_counter() - t0) * 1000))
    return out


def _fg_proc() -> str:
    try:
        import win32gui
        return tree._proc_name(win32gui.GetForegroundWindow()).lower()
    except Exception:
        return ""


def _browser_foreground() -> bool:
    try:
        from . import browser
        return browser.is_foreground()
    except Exception:
        return False
