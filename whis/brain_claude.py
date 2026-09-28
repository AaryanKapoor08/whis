"""Claude brain (`--brain claude`): one gated FINAL utterance -> Claude tool-use loop over verified desktop tools.

Every action tool VERIFIES what happened (foreground app, read-back of typed text, page URL, Spotify now-playing) and
every tool result ends with the fresh STATE (foreground app/title, numbered interactive elements, browser URL + page
text, running apps), so Claude sees the screen after each step. `look()` adds a downscaled screenshot.
Destructive actions become `pending` and wait for the user's spoken "yes" (controller). Logs one line per turn to
logs/brain.jsonl. The Jev path (controller/policy/executor) is untouched; this reuses its executor/app/browser code."""
import base64, io, json, re, threading, time
from collections import deque
import win32gui
from . import config, bus, tree, apps
from .types import Action, Element, Snapshot

DEFAULT_MODEL = "claude-sonnet-5"
MODELS = ("claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-opus-5-5")
FALLBACK_MODEL = "claude-haiku-4-5-20251001"     # one retry of a failed step (API error / timeout)
MAX_STEPS, DEADLINE_S, CALL_TIMEOUT_S = 8, 25.0, 15.0
ELEMENT_CAP, WEB_CAP, PAGE_TEXT_CHARS, MEMORY_TURNS = 80, 70, 1500, 5
NO_TYPE_PROCS = {"code", "cursor", "windowsterminal", "powershell", "pwsh", "cmd", "conhost", "openconsole",
                 "wezterm-gui", "alacritty", "claude"}      # never paste free text into a terminal / the user's editors
HARMLESS_CMDS = re.compile(r"^(?:claude|dir|ls|pwd|cls|clear|whoami|date /t|time /t|echo\b.*|git status|git log --oneline.*|"
                           r"python --version|node --version|node -v|npm -v|code \.)$", re.I)
DESTRUCTIVE_NAMES = re.compile(r"\b(?:submit|delete|remove|send|post|publish|pay|buy|purchase|order|checkout|check out|"
                               r"discard|uninstall|sign out|log ?out|empty|format|overwrite|replace all|don't save|"
                               r"unsubscribe|transfer|close|exit|quit|end call|leave)\b", re.I)
KEYS = dict(config.KEYS, **{"ctrl_n": "{Ctrl}n", "ctrl_a": "{Ctrl}a", "ctrl_f": "{Ctrl}f", "space": "{Space}",
                            "up": "{Up}", "down": "{Down}", "left": "{Left}", "right": "{Right}", "f5": "{F5}",
                            "page_down": "{PageDown}", "page_up": "{PageUp}", "alt_left": "{Alt}{Left}", "home": "{Home}", "end": "{End}"})
CONFIRM_KEYS = {"alt_f4": "close the window", "ctrl_w": "close the tab/window", "ctrl_s": "save the file"}
MEDIA_VK = {"play_pause": 0xB3, "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD, "next": 0xB0, "previous": 0xB1}
# UIA control types kept for Claude's element list (native FindAll over the whole window: 25-100 ms even for Spotify's
# ~2100-node Chromium tree, where the shared 12-deep walk only saw 0-6 elements)
_CT = {50000: "button", 50002: "checkbox", 50003: "combobox", 50004: "edit", 50005: "link", 50007: "listitem",
       50011: "menuitem", 50013: "radio", 50019: "tab", 50024: "treeitem", 50031: "splitbutton", 50029: "row"}

_STOP_WORDS = re.compile(r"^(?:(?:ok(?:ay)?|alright|now|whis)[,.\s]+)*(?:stop|enough|that'?s enough|halt|pause|freeze)"
                         r"(?:[\s,]+(?:scrolling|it|now|there|please|here))*[.!\s]*$", re.I)
YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|confirm(?:ed)?|please do|yes please|affirmative|correct)\b", re.I)
NO = re.compile(r"^(?:no|nope|nah|cancel|don't|do not|stop|never ?mind|abort|forget it)\b", re.I)
_FILLER = re.compile(r"^(?:(?:hey|ok|okay|so|um+|uh+|now|and|then|alright|all right|also|next|right)[,.\s]+)*"
                     r"(?:(?:(?:can|could|would|will) you(?: please)?|please|i (?:want|need) you to|i'd like you to|"
                     r"go ahead and|let's|let us|i want to|i wanna|just)[,\s]+)*", re.I)
_VERBS = set("""open launch start close quit exit play pause resume stop skip next previous go navigate visit browse search google
look find type write draft compose click press hit tap select choose scroll save switch show bring focus run execute turn mute
unmute volume read summarize summarise check tell take create make new put copy paste undo redo reply maximize minimize add
set enter fill sign log download refresh reload zoom keep continue""".split())
_ASK_ANYWHERE = re.compile(r"\b(?:want you to|need you to|can you|could you|would you|please|go ahead and)\s+(?:please\s+|just\s+)?([a-z]+)", re.I)
_SCREEN_REF = re.compile(r"\b(?:screen|page|window|tab|this|it|here|app|assignments?|due|email|inbox|song|playing|site|website|"
                         r"article|d2l|spotify|notepad|browser|brave|vs code|terminal|open|courses?|class(?:es)?|homework|labs?|"
                         r"deadlines?)\b", re.I)
_QUESTION = re.compile(r"^(?:what|what's|whats|which|how|when|is|are|do|does|did|who|where|any|am i)\b", re.I)


def looks_like_command(text: str) -> bool:
    """Claude-free gate for unnamed speech: an imperative verb up front, or a question about the screen/apps."""
    t = _FILLER.sub("", text.strip()).strip(" ,.!?").lower()
    if not t:
        return False
    first = re.split(r"[\s,.!?]+", t)[0]
    if first in _VERBS:
        return True
    m = _ASK_ANYWHERE.search(t)            # "okay, now on D2L, I want you to open CS3873"
    if m and m.group(1).lower() in _VERBS:
        return True
    return bool(_QUESTION.match(t) and _SCREEN_REF.search(t))


def yes_no(text: str):
    t = text.strip().strip(" .!?,").lower()
    if YES.match(t):
        return True
    if NO.match(t):
        return False
    return None


# ---------------------------------------------------------------------------------------------------------- prompt
SYSTEM = f"""You are whis, a hands-free voice assistant that operates the user's Windows PC. Each user message is one spoken \
sentence (speech-to-text, so expect small recognition errors) plus MEMORY of the last turns and the current STATE of the screen. \
Do what was asked with the tools, as fast as possible, then finish.

How it works
- Every action result ends with a fresh STATE: the foreground app and window title, the running apps, and a numbered list of \
clickable/editable elements. Native apps: e01.. ids (click them with click). whis's browser in front: the URL, w01.. ids = the web \
page's links/buttons/tabs/fields (web components included; "(below)" = off-screen, still clickable) and some page text; use \
click_web / type_web with a w-id or the visible text. Trust STATE over assumptions. Ids change after every action: only use ids \
from the latest STATE.
- Call look() for a screenshot only when STATE doesn't show what you need (images, canvas apps, unlabeled icons). It is slower. \
click_at(x, y) on that screenshot is the last resort when no element id / visible text fits. Don't scroll around searching: \
STATE already lists off-screen web elements.
- Tools verify their effect. ok=false means it did NOT happen. Never tell the user something worked unless a result confirmed it. \
If an action failed, try one sensible alternative, otherwise finish and say briefly what failed.
- Speed: when an action call completes the whole request, give it finish="<short summary>" - the task ends when it succeeds, with no \
extra step. Or batch the predictable steps in ONE response ending with a done call (e.g. open_app, type_text, done). Calls run in \
order and stop at the first failure (you then see what failed). Use separate steps only when you must see the result first \
(e.g. navigate, then click_web on something you haven't seen yet).
- Finish every task by calling done or say (a real tool call, not text) unless finish= already ended it. Both are shown on a small \
status pill and may be spoken: plain text, at most ~12 words (answers to questions may be up to ~35 words).

Rules
- Speed matters: fewest steps, no exploring, no extra actions the user did not ask for.
- If the sentence is not a request for the computer (chit-chat, talking to someone else, noise, an unfinished thought), call \
done with an empty summary right away and do nothing else.
- Questions about the screen or apps ("what's on my screen", "what song is this"): answer from STATE, using look() only if \
STATE is not enough, with say(answer).
- D2L (UNB Brightspace): anything about assignments, homework, labs, what's due / left, deadlines, courses, classes or D2L means \
D2L - use the d2l_* tools (fast API, no screen needed), never answer from whatever unrelated window is in front (an editor, a \
markdown file, ...). "do I have an assignment left" / "what's due this week" -> d2l_assignments(days=7 for this week, speak=true). \
"open CS3873" / "open my calculus course" / "open a random course" -> d2l_open_course (with finish=); a course's assignments, \
grades, content, quizzes... page -> d2l_open_course(course or "this", section). "open assignment 2" -> d2l_open_assignment. \
"go to d2l" -> navigate("d2l", finish=...). Otherwise click_web on what STATE lists. Never guess D2L URLs. \
Never submit, post or change anything on D2L. Answers: short and spoken-style with the real assignment names and due dates.
- "it", "that", "there", "this", "the song" refer to the foreground app or to the recent turns in MEMORY.
- Writing: type_text pastes text into the focused field of the app you name. For new content in an editor (Notepad etc.) set \
new_document=true so it starts a fresh document instead of typing into the user's open file. \
When asked to compose (e.g. "write a haiku about the ocean"), write the text yourself: plain text, no quotes, no markdown.
- Music: play_song(query) searches Spotify and starts the best match (it opens Spotify itself). Pause/resume/skip/volume: media().
- Web: navigate(site) for bookmarks ({", ".join(config.BOOKMARKS)}) or a URL/domain, web_search(query) for anything else. \
The browser is whis's own Brave window. On a page, click_web / type_web (e.g. YouTube: type_web("Search", "lofi", submit=true), \
then click_web on the first video title link in STATE).
- YouTube's home page is empty when not signed in: then search (type_web on the search box with submit=true) for the topic the \
user named, or say briefly that YouTube shows no videos until they search or sign in. Never sign in/out, subscribe, like or \
change settings unless asked.
- Scrolling: scroll(direction) moves one page; "keep scrolling" / "scroll until I say stop" -> scroll(direction, continuous=true, \
finish="scrolling, say stop"); "stop" is handled locally.
- Terminals: open_terminal() (in_vscode=true for "the terminal in VS Code" / "in it" after VS Code), then run_command(command). \
"claude code" means the command `claude`. Never use type_text or press_key to type into terminals or code editors.
- Destructive actions (closing a window, saving over a file, clicking Submit/Send/Delete/Pay, non-trivial commands) are held for \
the user's spoken "yes": the tool reports needs_confirmation and the turn ends there. Don't try to get around that.
- Known apps: {", ".join(sorted(set(config.APPS)))}; other installed apps can be opened by name too."""

_S = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}
_STR = {"type": "string"}
_FINISH = {"type": "string", "description": "Set ONLY if this call completes the user's whole request: the <= 12-word summary to show. "
           "The task then ends as soon as this call succeeds (saves a step). Leave out if you still need to see the result."}
TOOLS = [
    {"name": "open_app", "description": "Open an app, or bring it to the front if it is already running. Verifies the app is in front. "
     "Use a browser name (brave/browser/chrome) for whis's browser.", "input_schema": _S({"name": _STR}, ["name"])},
    {"name": "focus_app", "description": "Bring an already-running app to the front (no launch). Verifies it is in front.",
     "input_schema": _S({"name": _STR}, ["name"])},
    {"name": "click", "description": "Click an element of the foreground window by its id from the latest STATE (e.g. 'e07'). "
     "Reports whether the screen changed. Clicks on Submit/Send/Delete/Close-like elements need the user's confirmation.",
     "input_schema": _S({"element_id": _STR}, ["element_id"])},
    {"name": "type_text", "description": "Paste text into the focused field of `app`. Refused unless `app` is the foreground app "
     "(focus it first; click a field first if needed). Reads the field back to verify. Not for terminals or code editors.",
     "input_schema": _S({"text": _STR, "app": {"type": "string", "description": "the app you expect in front, e.g. notepad, brave, spotify"},
                         "new_document": {"type": "boolean", "description": "press Ctrl+N first (fresh document in an editor)"}}, ["text", "app"])},
    {"name": "press_key", "description": "Press a key or shortcut in `app` (must be the foreground app).",
     "input_schema": _S({"key": {"type": "string", "enum": sorted(KEYS)}, "app": _STR}, ["key", "app"])},
    {"name": "navigate", "description": "Open a bookmark name, domain or URL in whis's browser. Returns the final URL.",
     "input_schema": _S({"url_or_site": _STR}, ["url_or_site"])},
    {"name": "web_search", "description": "Search the web (Bing) in whis's browser.", "input_schema": _S({"query": _STR}, ["query"])},
    {"name": "play_song", "description": "Search Spotify for a song/artist/album and start the best match. Verifies playback started.",
     "input_schema": _S({"query": {"type": "string", "description": "e.g. 'Loser by Tame Impala'"}}, ["query"])},
    {"name": "search_in_app", "description": "Use an app's own search box (Spotify, File Explorer, VS Code file search, Notepad find).",
     "input_schema": _S({"query": _STR, "app": _STR}, ["query", "app"])},
    {"name": "scroll", "description": "Scroll the foreground window one page. continuous=true starts smooth non-stop scrolling in the "
     "background (\"keep scrolling\", \"scroll until I say stop\"); the user's \"stop\" ends it. direction=stop stops it.",
     "input_schema": _S({"direction": {"type": "string", "enum": ["up", "down", "stop"]}, "continuous": {"type": "boolean"},
                         "speed": {"type": "string", "enum": ["slow", "normal", "fast"]}}, ["direction"])},
    {"name": "click_web", "description": "Click a link/button/tab on the web page in whis's browser, by its w-id from the latest STATE "
     "(e.g. 'w07', also off-screen ones) or by its visible text (e.g. 'Assignments', 'Sign in'). Pierces web components/shadow DOM. "
     "Waits for the page to load and returns the new URL/title.", "input_schema": _S({"target": _STR}, ["target"])},
    {"name": "type_web", "description": "Fill a text field on the web page: field = its w-id, label or placeholder (empty = first field). "
     "submit=true presses Enter afterwards (search boxes). Reads the field back to verify.",
     "input_schema": _S({"field": _STR, "text": _STR, "submit": {"type": "boolean"}}, ["field", "text"])},
    {"name": "click_at", "description": "Vision fallback: click at pixel (x, y) of the LATEST look() screenshot. Only when no element id "
     "or visible text in STATE fits.", "input_schema": _S({"x": {"type": "integer"}, "y": {"type": "integer"}}, ["x", "y"])},
    {"name": "d2l_courses", "description": "The user's current-term D2L (Brightspace, UNB) courses: name, code, orgUnitId. Fast API call, "
     "no screen needed.", "input_schema": _S({"all_terms": {"type": "boolean"}})},
    {"name": "d2l_open_course", "description": "Open a D2L course in the browser: its homepage or one of its pages (section). course = "
     "code or number or words from the title ('CS3873', '3873', 'calculus'), 'random' for a random current course, or 'this' for "
     "the course the browser is in.", "input_schema": _S({"course": _STR, "section": {"type": "string", "enum": [
         "home", "assignments", "content", "grades", "quizzes", "discussions", "announcements", "classlist", "calendar"]}}, ["course"])},
    {"name": "d2l_assignments", "description": "The user's D2L assignments that are NOT submitted yet, with due dates, soonest first "
     "(across current courses, or one course). days = only those due by the end of the day that many days ahead (0 today, 1 tomorrow, 7 this week). "
     "speak=true ends the task by telling the user the tool's own short summary (names + due dates) - use it when the user only "
     "asked what is due / left.", "input_schema": _S({"course": _STR, "days": {"type": "number"}, "speak": {"type": "boolean"},
                                                       "include_submitted": {"type": "boolean"}})},
    {"name": "d2l_open_assignment", "description": "Open one D2L assignment's page (read-only; never submits). name like 'Assignment 2', "
     "'lab 3'; course optional.", "input_schema": _S({"name": _STR, "course": _STR}, ["name"])},
    {"name": "go_back", "description": "Go back (browser history, or Alt+Left in other apps).", "input_schema": _S({})},
    {"name": "media", "description": "System media keys: play/pause, next/previous track, volume, mute.",
     "input_schema": _S({"action": {"type": "string", "enum": sorted(MEDIA_VK)}}, ["action"])},
    {"name": "open_terminal", "description": "Open a NEW terminal window, or with in_vscode=true focus VS Code and open its integrated terminal.",
     "input_schema": _S({"in_vscode": {"type": "boolean"}})},
    {"name": "run_command", "description": "Type a command + Enter into a terminal that whis opened (open_terminal first). "
     "Commands other than harmless ones (claude, dir, ls, git status, ...) need the user's confirmation.",
     "input_schema": _S({"command": _STR}, ["command"])},
    {"name": "close_window", "description": "Close an app's window (needs the user's confirmation; editors/terminals are never closed).",
     "input_schema": _S({"app": _STR}, ["app"])},
    {"name": "save", "description": "Save the current document in `app` with Ctrl+S (needs the user's confirmation).",
     "input_schema": _S({"app": _STR}, ["app"])},
    {"name": "look", "description": "Screenshot of the foreground window (downscaled) plus STATE. Use when STATE's element list is not enough.",
     "input_schema": _S({})},
    {"name": "say", "description": "Answer the user (shown on the status pill) and end the task. Use for questions.",
     "input_schema": _S({"text": _STR}, ["text"])},
    {"name": "done", "description": "End the task. summary (<= 12 words) is shown to the user; empty when nothing was asked.",
     "input_schema": _S({"summary": _STR}, ["summary"])},
]
_TERMINAL_TOOLS = {"say", "done"}
_FINISHABLE = {"open_app", "focus_app", "click", "type_text", "press_key", "navigate", "web_search", "play_song", "search_in_app",
               "scroll", "go_back", "media", "open_terminal", "run_command", "click_web", "type_web", "click_at", "d2l_open_course",
               "d2l_open_assignment"}
for _t in TOOLS:
    if _t["name"] in _FINISHABLE:
        _t["input_schema"]["properties"]["finish"] = _FINISH


# ---------------------------------------------------------------------------------------------------------- screen helpers
def _fg():
    h = win32gui.GetForegroundWindow()
    return h, tree._proc_name(h), win32gui.GetWindowText(h)


def _browser_fg(h=None) -> bool:
    """Strict: the foreground window is whis's own Playwright Brave (no post-action grace period)."""
    try:
        from . import browser
        h = h or win32gui.GetForegroundWindow()
        return bool(h) and ((browser._hwnd and h == browser._hwnd) or (browser._page is not None and browser._is_ours(h)))
    except Exception:
        return False


def _fg_is(app: str) -> bool:
    a = (app or "").lower().strip()
    if not a:
        return False
    h, proc, title = _fg()
    if a in config.BROWSER_NAMES or a in ("whis browser", "brave"):
        return _browser_fg(h)
    p = proc.lower()
    want = (config.APP_PROCS.get(a) or "").lower()
    if want:
        return p == want
    return bool(p) and (a == p or a in p or p in a.replace(" ", ""))


def _fg_desc() -> str:
    h, proc, title = _fg()
    return f"{'whis browser' if _browser_fg(h) else proc or '?'} \"{title[:60]}\""


def _walk(hwnd: int, cap: int) -> Snapshot:
    """Tree thread only (via tree.ui_call). Native FindAll of interactive, on-screen descendants; skips Spotify's library sidebar."""
    import uiautomation as auto
    from uiautomation.uiautomation import _AutomationClient
    ia = _AutomationClient.instance().IUIAutomation
    root = ia.ElementFromHandle(hwnd)
    cond = None
    for ct in _CT:
        c = ia.CreatePropertyCondition(30003, ct)
        cond = c if cond is None else ia.CreateOrCondition(cond, c)
    cond = ia.CreateAndCondition(cond, ia.CreatePropertyCondition(30022, False))      # IsOffscreen == False
    wr = root.CurrentBoundingRectangle
    excl = []
    for nm, ct in (("Your Library", 50028),):                                          # Spotify sidebar (DataGrid): noise
        e = root.FindFirst(4, ia.CreateAndCondition(ia.CreatePropertyCondition(30005, nm), ia.CreatePropertyCondition(30003, ct)))
        if e:
            r = e.CurrentBoundingRectangle; excl.append((r.left, r.top, r.right, r.bottom))
    arr = root.FindAll(4, cond)
    els, seen, t0 = [], set(), time.perf_counter()
    for i in range(arr.Length):
        if len(els) >= cap or time.perf_counter() - t0 > 1.2:
            break
        try:
            e = arr.GetElement(i)
            ct = e.CurrentControlType
            r = e.CurrentBoundingRectangle
            if r.right - r.left < 4 or r.bottom - r.top < 4:
                continue
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            if not (wr.left <= cx <= wr.right and wr.top <= cy <= wr.bottom):
                continue                                                               # scrolled out of the window
            if any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in excl):
                continue
            role = _CT.get(ct, "control")
            name = (e.CurrentName or "").strip()
            if ct == 50029:                                                            # Spotify rows: own name is stale
                try:
                    kid = ia.ControlViewWalker.GetFirstChildElement(e)
                    name = ((kid.CurrentName if kid else "") or name).strip()
                except Exception:
                    pass
            if not name and ct == 50004:
                name = "text field"
            if not name:
                continue
            key = (role, name.lower())
            if key in seen:
                continue
            seen.add(key)
            els.append(Element(f"e{len(els)+1:02d}", role, name[:70], (r.left, r.top, r.right, r.bottom),
                               auto.Control.CreateControlFromElement(e), "uia"))
        except Exception:
            continue
    return Snapshot(hwnd, tree._proc_name(hwnd), win32gui.GetWindowText(hwnd), els)


def _focused_text():
    """Tree thread: (name, text) of the focused control (Value or Text pattern); text None when unreadable."""
    import uiautomation as auto
    f = auto.GetFocusedControl()
    if f is None:
        return "", None
    txt = None
    try:
        vp = f.GetValuePattern()
        if vp:
            txt = vp.Value
    except Exception:
        pass
    if not txt:
        try:
            tp = f.GetTextPattern()
            if tp:
                txt = tp.DocumentRange.GetText(-1)
        except Exception:
            pass
    return (f.Name or ""), txt


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def _wid(s: str) -> str:
    """'w7' / 'W07' -> 'w07' (web element ids)."""
    m = re.fullmatch(r"\s*[wW]0*(\d+)\s*", s or "")
    return f"w{int(m.group(1)):02d}" if m else (s or "").strip().lower()


def _r(ok, result, **kw):
    return dict(ok=bool(ok), result=result, **kw)


# ---------------------------------------------------------------------------------------------------------- brain
class ClaudeBrain:
    def __init__(self, model=DEFAULT_MODEL, get_snapshot=None, get_apps=None, progress=None, dry_run=False, live=True,
                 use_browser=True, effort="low"):
        self.model, self.effort = model, effort
        self.get_snapshot, self.get_apps = get_snapshot, get_apps or (lambda: [])
        self.progress = progress or (lambda text, state: None)
        self.dry_run, self.live, self.use_browser = dry_run, live, use_browser
        self.memory: deque = deque(maxlen=MEMORY_TURNS)
        self.pending: dict | None = None
        self.cancel = threading.Event()
        self._busy = 0
        self._blk = threading.Lock()
        self._idle = threading.Event(); self._idle.set()
        self._els: dict[str, Element] = {}
        self._web: dict[str, dict] = {}             # w01.. -> web element (index into the page's window.__whisEls)
        self._shot: dict | None = None              # geometry of the latest look() image (click_at mapping)
        self._scrolling = None                      # "web" | "native" while a continuous scroll runs
        self._scroll_ev = threading.Event()
        self._term_hwnds: set[int] = set()          # terminals whis opened: the only places run_command types into
        self._client = None

    # ---- plumbing
    @property
    def busy(self):
        return self._busy > 0

    def wait_idle(self, timeout=60.0):
        return self._idle.wait(timeout)

    def _begin(self):
        with self._blk:
            self._busy += 1; self._idle.clear()

    def _end(self):
        with self._blk:
            self._busy -= 1
            if self._busy <= 0:
                self._busy = 0; self._idle.set()

    def _cli(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
        return self._client

    def warm(self):
        threading.Thread(target=self._cli, daemon=True, name="claude-brain-warm").start()

    def _create(self, model, messages):
        kw = dict(model=model, max_tokens=4096, tools=TOOLS, messages=messages,
                  system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],   # tools+system prefix
                  cache_control={"type": "ephemeral"})                                                # + the growing loop history
        if "haiku" not in model:
            kw["output_config"] = {"effort": self.effort}     # adaptive thinking stays on (can't be disabled on Opus 5.5)
        return self._cli().with_options(timeout=CALL_TIMEOUT_S, max_retries=1).messages.create(**kw)

    def _call(self, messages):
        try:
            return self._create(self.model, messages), self.model
        except Exception as e:
            bus.log("events", kind="claude_brain_error", model=self.model, err=repr(e)[:300])
            if self.model == FALLBACK_MODEL:
                raise
            return self._create(FALLBACK_MODEL, messages), FALLBACK_MODEL

    # ---- observation ("eyes")
    def observe(self, settle=0.25) -> str:
        if settle:
            time.sleep(settle)
        url = page = ""
        web = None
        if not self.live:
            snap = self.get_snapshot() if self.get_snapshot else None
            head = f"foreground: {snap.app} \"{snap.title}\"" if snap else "foreground: ?"
        else:
            h, proc, title = _fg()
            snap = None
            if self.use_browser and _browser_fg(h):
                from . import browser
                try:
                    web = browser.call("web_elements", WEB_CAP, timeout=5)
                    url = (web or {}).get("url") or browser.call("url", timeout=3) or ""
                    page = browser.call("page_text", timeout=5) or ""
                except Exception as e:
                    bus.log("events", kind="claude_observe_browser_error", err=repr(e)[:200])
                head = f"foreground: whis browser (Brave) \"{title[:80]}\""
            else:
                try:
                    snap = tree.ui_call(_walk, h, ELEMENT_CAP, timeout=3.0)
                except Exception as e:
                    bus.log("events", kind="claude_observe_error", err=repr(e)[:200])
                    snap = tree.get_snapshot()
                head = f"foreground: {proc or '?'} \"{title[:80]}\""
        els = snap.elements if snap else []
        self._els = {e.id: e for e in els}
        self._web = {}
        lines = [head]
        if url:
            lines.append(f"url: {url}")
        if isinstance(web, dict) and web.get("els"):
            for w in web["els"]:
                self._web[f"w{w['i'] + 1:02d}"] = w
            more = f", {web['n'] - len(web['els'])} more not listed" if web.get("n", 0) > len(web["els"]) else ""
            lines.append(f"web page elements ({len(web['els'])}{more}; click_web / type_web by id or text):")
            lines += [f'{k} {w["role"]} "{w["name"]}"' + (f' value="{w["val"]}"' if w.get("val") else "") + (" (below)" if w.get("off") else "")
                      for k, w in self._web.items()]
        elif els:
            lines.append(f"elements ({len(els)}, click by id):")
            lines += [f'{e.id} {e.role} "{e.name}"' for e in els[:ELEMENT_CAP]]
        else:
            lines.append("elements: none visible to accessibility (use look() if you need to see it)")
        if page:
            body = re.sub(r"\s+", " ", page.split("\n", 1)[-1]).strip()
            lines.append(f"page text: {body[:PAGE_TEXT_CHARS]}" + ("…" if len(body) > PAGE_TEXT_CHARS else ""))
        try:
            lines.append("running apps: " + ", ".join(self.get_apps()[:20]))
        except Exception:
            pass
        return "\n".join(lines)

    def _screenshot_block(self):
        from PIL import Image, ImageGrab
        h = win32gui.GetForegroundWindow()
        img = None
        if self.use_browser and _browser_fg(h):          # the page viewport in CSS px = Playwright mouse coordinates
            from . import browser
            try:
                jpg, vp = browser.call("screenshot", timeout=6)
                img = Image.open(io.BytesIO(jpg))
                self._shot = {"kind": "web", "hwnd": h, "sx": vp[0] / img.width, "sy": vp[1] / img.height}
            except Exception as e:
                bus.log("events", kind="claude_web_screenshot_error", err=repr(e)[:200])
                img = None
        if img is None:
            try:
                l, t, r, b = win32gui.GetWindowRect(h)        # physical px (whis is per-monitor DPI aware)
                if not (r - l > 50 and b - t > 50):
                    raise ValueError("tiny window")
                img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
            except Exception:
                img = ImageGrab.grab(all_screens=True)
                import win32api
                l, t = win32api.GetSystemMetrics(76), win32api.GetSystemMetrics(77)   # virtual screen origin
            self._shot = {"kind": "screen", "hwnd": h, "left": l, "top": t, "sx": 1.0, "sy": 1.0}
        if img.width > 1280:
            f = img.width / 1280
            img = img.resize((1280, max(1, int(img.height / f))))
            self._shot["sx"] *= f; self._shot["sy"] *= f
        self._shot["w"], self._shot["h"] = img.width, img.height
        buf = io.BytesIO(); img.convert("RGB").save(buf, "JPEG", quality=70)
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}}

    def _sig(self):
        h, _, title = _fg()
        return (h, title, tuple(e.name for e in list(self._els.values())[:40]))

    # ---- tools
    def _progress_text(self, name, a):
        el = self._els.get(str(a.get("element_id", "")))
        w = self._web.get(_wid(str(a.get("target", a.get("element_id", "")))))
        return {"open_app": f"opening {a.get('name', '')}…", "focus_app": f"switching to {a.get('name', '')}…",
                "click": f"clicking {el.name[:30] if el else a.get('element_id', '')}…", "type_text": "typing…",
                "press_key": f"pressing {a.get('key', '')}…", "navigate": f"going to {a.get('url_or_site', '')}…",
                "web_search": f"searching {a.get('query', '')}…", "play_song": f"playing {a.get('query', '')}…",
                "search_in_app": f"searching {a.get('query', '')}…", "scroll": f"scrolling {a.get('direction', '')}",
                "go_back": "going back", "media": a.get("action", "").replace("_", " "), "open_terminal": "opening terminal…",
                "run_command": f"running {a.get('command', '')}…", "close_window": f"close {a.get('app', '')}?",
                "save": "save?", "look": "looking…", "click_web": f"clicking {w['name'][:30] if w else a.get('target', '')}…",
                "type_web": "typing…", "click_at": "clicking…", "d2l_courses": "checking D2L…",
                "d2l_open_course": f"opening {a.get('course', '')}…", "d2l_assignments": "checking D2L assignments…",
                "d2l_open_assignment": f"opening {a.get('name', '')}…"}.get(name, name)

    def run_tool(self, name, a, confirmed=False) -> dict:
        fn = getattr(self, "_t_" + name, None)
        if fn is None:
            return _r(False, f"unknown tool {name}")
        try:
            return fn(a or {}, confirmed)
        except Exception as e:
            bus.log("events", kind="claude_tool_error", tool=name, err=repr(e)[:300])
            return _r(False, f"error: {e!r}"[:200])

    def _dry(self, what):
        return _r(True, f"DRY RUN: would {what}")

    def _wait(self, cond, timeout):
        t = time.perf_counter()
        while time.perf_counter() - t < timeout:
            if cond():
                return True
            time.sleep(0.05)
        return cond()

    def _t_open_app(self, a, confirmed=False):
        name = str(a.get("name", "")).strip()
        if not name:
            return _r(False, "no app name")
        if self.dry_run:
            return self._dry(f"open {name}")
        if name.lower() in config.BROWSER_NAMES or name.lower() in ("brave", "whis browser"):
            from . import browser
            browser.call("bring_to_front")
            ok = self._wait(_browser_fg, 3.0)
            return _r(ok, "whis browser is in front" if ok else f"browser didn't come to the front (foreground: {_fg_desc()})")
        existed = apps.find_window(name) is not None
        ok, msg = apps.open_or_focus(name)
        if not ok:
            return _r(False, msg)
        if self._wait(lambda: _fg_is(name), 6.0):
            return _r(True, f"{'switched to existing' if existed else 'launched new'} {name} window; it is in front")
        return _r(False, f"{msg}, but {name} is not in front (foreground: {_fg_desc()})")

    def _t_focus_app(self, a, confirmed=False):
        name = str(a.get("name", "")).strip()
        if name.lower() in config.BROWSER_NAMES:
            return self._t_open_app(a)
        h = apps.find_window(name)
        if not h:
            return _r(False, f"{name} is not running (use open_app)")
        if self.dry_run:
            return self._dry(f"focus {name}")
        apps.focus_hwnd(h)
        ok = self._wait(lambda: _fg_is(name), 3.0)
        return _r(ok, f"{name} is in front" if ok else f"{name} didn't come to the front (foreground: {_fg_desc()})")

    def _t_click(self, a, confirmed=False):
        eid = str(a.get("element_id", "")).strip()
        if eid[:1].lower() == "w" or (self._web and eid not in self._els):
            return self._t_click_web({"target": eid, **{k: v for k, v in a.items() if k.startswith("_")}}, confirmed)
        el = self._els.get(eid)
        if el is None:
            return _r(False, f"no element {eid} in the latest STATE")
        if not confirmed and DESTRUCTIVE_NAMES.search(el.name):
            return self._ask("click", {"element_id": eid, "_el": el, "_hwnd": _fg()[0]}, f"click {el.name[:40]}")
        if self.dry_run:
            return self._dry(f"click {el.name}")
        if confirmed and a.get("_hwnd") and _fg()[0] != a["_hwnd"]:
            apps.focus_hwnd(a["_hwnd"]); time.sleep(0.2)
            if _fg()[0] != a["_hwnd"]:
                return _r(False, "the window of that element is no longer in front")
        before = self._sig()
        from . import executor
        out = executor.run(Action("click_element", {"target": a.get("_el") or el}), None)
        if not out.ok:
            return _r(False, f"click failed: {out.msg}")
        changed = self._wait(lambda: self._sig()[:2] != before[:2], 0.6)
        return _r(True, f"clicked \"{el.name[:40]}\"; " + (f"window is now {_fg_desc()}" if changed else "title unchanged (check STATE)"),
                  **({} if changed else {"no_finish": True}))

    def _t_type_text(self, a, confirmed=False):
        text, app = str(a.get("text", "")), str(a.get("app", "")).strip()
        if not text:
            return _r(False, "no text")
        if self.dry_run:
            return self._dry(f"type {len(text)} chars into {app}")
        h, proc, title = _fg()
        if not _fg_is(app):
            return _r(False, f"refused: {app or 'the target app'} is not in front (foreground: {_fg_desc()}); focus it first")
        if proc.lower() in NO_TYPE_PROCS and not _browser_fg(h):
            return _r(False, f"refused: won't type into {proc} (terminal/code editor); use run_command in a whis terminal")
        from . import executor
        if a.get("new_document") and not _browser_fg(h):
            tree.ui_call(executor._send_keys, "{Ctrl}n")
            self._wait(lambda: _fg()[2] != title, 0.8)        # Notepad: the new tab retitles the window
            time.sleep(0.15)
            if not _fg_is(app):
                return _r(False, f"after Ctrl+N the foreground is {_fg_desc()}, not {app}; nothing typed")
            h = _fg()[0]
        if _browser_fg(h):
            from . import browser
            ok = browser.call("type_focused", text)
            back = browser.call("focused_value") if ok else None
        else:
            tree.ui_call(executor._type, text)
            time.sleep(0.25)
            try:
                _, back = tree.ui_call(_focused_text)
            except Exception:
                back = None
        if _fg()[0] != h:
            return _r(False, f"focus moved while typing (foreground now {_fg_desc()})")
        want = _norm(text)[:40]
        if isinstance(back, str) and back:
            if want and want in _norm(back):
                return _r(True, f"typed {len(text)} chars into {app}; verified in the field")
            return _r(False, f"pasted into {app} but the focused field doesn't contain the text (field starts: {back[:60]!r})")
        return _r(True, f"pasted {len(text)} chars into {app}; couldn't read the field back to verify")

    def _t_press_key(self, a, confirmed=False):
        key, app = str(a.get("key", "")), str(a.get("app", "")).strip()
        if key not in KEYS:
            return _r(False, f"unknown key {key}")
        h, proc, _ = _fg()
        if not confirmed and not self.dry_run and not _fg_is(app):
            return _r(False, f"refused: {app} is not in front (foreground: {_fg_desc()})")
        if not confirmed:
            if key in CONFIRM_KEYS:
                return self._ask("press_key", {"key": key, "app": app, "_hwnd": h}, f"{CONFIRM_KEYS[key]} in {app}")
            if proc.lower() in NO_TYPE_PROCS and key not in ("ctrl_grave", "escape"):
                return self._ask("press_key", {"key": key, "app": app, "_hwnd": h}, f"press {key} in {proc}")
            if proc.lower() in config.PROTECTED_APPS and key in ("alt_f4", "ctrl_w"):
                return _r(False, f"refused: never close {proc}")
        if self.dry_run:
            return self._dry(f"press {key}")
        if confirmed and a.get("_hwnd"):
            if _fg()[0] != a["_hwnd"]:
                apps.focus_hwnd(a["_hwnd"]); time.sleep(0.2)
            if _fg()[0] != a["_hwnd"]:
                return _r(False, "that window is no longer in front")
            if key in ("alt_f4", "ctrl_w") and _fg()[1].lower() in config.PROTECTED_APPS:
                return _r(False, f"refused: never close {_fg()[1]}")
        from . import executor
        before = self._sig()
        tree.ui_call(executor._send_keys, KEYS[key])
        changed = self._wait(lambda: self._sig()[:2] != before[:2], 0.5)
        return _r(True, f"pressed {key}; " + (f"window is now {_fg_desc()}" if changed else "window title unchanged"))

    def _t_navigate(self, a, confirmed=False):
        s = str(a.get("url_or_site", "")).strip()
        key = s.lower().removeprefix("the ").strip()
        if key in config.BOOKMARKS:
            url = config.BOOKMARKS[key]
        elif re.match(r"^https?://", s):
            url = s
        elif re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", s):
            url = "https://" + s
        else:
            return _r(False, f"'{s}' is not a bookmark, domain or URL; use web_search")
        if self.dry_run:
            return self._dry(f"open {url}")
        from . import browser
        ok = browser.call("goto", url, timeout=20)
        now = browser.call("url", timeout=3) or ""
        front = self._wait(_browser_fg, 2.0)
        if not ok:
            return _r(False, f"navigation to {url} failed (browser at {now or '?'})")
        return _r(True, f"browser at {now}" + ("" if front else f"; but the browser is not in front ({_fg_desc()})"))

    def _t_web_search(self, a, confirmed=False):
        q = str(a.get("query", "")).strip()
        if not q:
            return _r(False, "empty query")
        if self.dry_run:
            return self._dry(f"search {q}")
        from . import browser
        ok = browser.call("search", q, timeout=20)
        now = browser.call("url", timeout=3) or ""
        return _r(bool(ok) and "q=" in now, f"search results at {now}" if ok else "search failed")

    def _t_play_song(self, a, confirmed=False):
        q = str(a.get("query", "")).strip()
        if not q:
            return _r(False, "empty query")
        if self.dry_run:
            return self._dry(f"play {q} on Spotify")
        from . import executor
        out = executor._spotify_play(q)           # verified: waits for the Now playing bar to change
        tree.invalidate()
        return _r(out.ok, out.msg)

    def _t_search_in_app(self, a, confirmed=False):
        q, app = str(a.get("query", "")).strip(), str(a.get("app", "")).strip()
        if not q:
            return _r(False, "empty query")
        if app.lower() in config.BROWSER_NAMES:
            return self._t_web_search({"query": q})
        if self.dry_run:
            return self._dry(f"search {q} in {app}")
        if not _fg_is(app):
            apps.open_or_focus(app)
            if not self._wait(lambda: _fg_is(app), 4.0):
                return _r(False, f"{app} didn't come to the front (foreground: {_fg_desc()})")
        if _fg()[1].lower() in NO_TYPE_PROCS - {"code", "cursor"}:
            return _r(False, f"refused: won't type into {_fg()[1]}")
        from . import executor
        proc = _fg()[1].lower()
        keys = next((v for k, v in config.IN_APP_SEARCH_KEYS.items() if k in proc or k in app.lower()), config.IN_APP_SEARCH_KEYS["default"])
        h = _fg()[0]
        tree.ui_call(executor._send_keys, keys); time.sleep(0.35)
        if _fg()[0] != h:
            return _r(False, f"focus moved (foreground now {_fg_desc()})")
        tree.ui_call(executor._send_keys, "{Ctrl}a"); tree.ui_call(executor._type, q); time.sleep(0.15)
        tree.ui_call(executor._send_keys, "{Enter}")
        return _r(True, f"searched '{q}' in {app}; results not verified, check STATE")

    def _t_scroll(self, a, confirmed=False):
        if a.get("direction") == "stop":
            return _r(True, self.stop_scrolling() or "nothing was scrolling")
        d = "scroll_down" if a.get("direction") != "up" else "scroll_up"
        if self.dry_run:
            return self._dry(("continuous " if a.get("continuous") else "") + d)
        if a.get("continuous"):
            return self._scroll_start(a.get("direction") == "up", str(a.get("speed") or "normal"))
        from . import executor
        out = executor.run(Action(d, {}), None)
        return _r(out.ok, d.replace("_", " "))

    SCROLL_PX = {"slow": 220, "normal": 450, "fast": 900}      # px per second
    SCROLL_MAX_S = 180.0

    def _scroll_start(self, up: bool, speed: str):
        self.stop_scrolling()
        pxs = self.SCROLL_PX.get(speed, 450) * (-1 if up else 1)
        self._scroll_ev = ev = threading.Event()
        if self.use_browser and _browser_fg():
            from . import browser
            msg = browser.call("scroll_smooth", pxs, timeout=5)
            if not msg:
                return _r(False, "couldn't start scrolling the page")
            self._scrolling = "web"
            threading.Timer(self.SCROLL_MAX_S, lambda: ev.is_set() or self.stop_scrolling()).start()
            return _r(True, f"{msg} {'up' if up else 'down'} continuously ({speed}); the user says stop to end it")
        h = _fg()[0]
        try:
            import win32api
            l, t, r, b = win32gui.GetWindowRect(h)
            win32api.SetCursorPos(((l + r) // 2, (t + b) // 2))     # the wheel goes to the window under the cursor
        except Exception:
            pass
        self._scrolling = "native"

        def loop():
            import win32api, win32con
            step = max(8, int(abs(pxs) / 450 * 24)) * (1 if up else -1)     # wheel delta per 50 ms tick (120 = one notch)
            t0 = time.perf_counter()
            while not ev.is_set() and time.perf_counter() - t0 < self.SCROLL_MAX_S and _fg()[0] == h:
                win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, step, 0)
                ev.wait(0.05)
            if self._scroll_ev is ev:
                self._scrolling = None
        threading.Thread(target=loop, daemon=True, name="whis-scroll").start()
        return _r(True, f"scrolling {_fg_desc()} {'up' if up else 'down'} continuously ({speed}); the user says stop to end it")

    @property
    def scrolling(self) -> bool:
        return bool(self._scrolling)

    def stop_scrolling(self) -> str:
        """Stop a continuous scroll; returns what was stopped ('' if nothing ran)."""
        kind, self._scrolling = self._scrolling, None
        self._scroll_ev.set()
        if kind == "web":
            try:
                from . import browser
                browser.call("scroll_smooth", 0, timeout=3)
            except Exception:
                pass
        return "stopped scrolling" if kind else ""

    def _t_go_back(self, a, confirmed=False):
        if self.dry_run:
            return self._dry("go back")
        from . import executor
        before = self._sig()
        out = executor.run(Action("go_back", {}), None)
        changed = self._wait(lambda: self._sig()[:2] != before[:2], 0.8)
        return _r(out.ok, "went back; " + ("title changed" if changed else "title unchanged"))

    def _t_media(self, a, confirmed=False):
        act = str(a.get("action", ""))
        if act not in MEDIA_VK:
            return _r(False, f"unknown media action {act}")
        if self.dry_run:
            return self._dry(act)
        if act == "play_pause" and self.use_browser and _browser_fg():      # a video in whis's browser: toggle it directly, verified
            from . import browser
            try:
                st = browser.call("run", lambda page: page.evaluate("""() => { const v = [...document.querySelectorAll('video')]
                    .find(v => v.readyState > 0 && v.getBoundingClientRect().width > 50); if (!v) return null;
                    if (v.paused) v.play(); else v.pause(); return v.paused ? 'paused' : 'playing'; }"""), timeout=5)
            except Exception:
                st = None
            if st:
                return _r(True, f"the video in the browser is now {st}")
        from . import executor
        executor._vk(MEDIA_VK[act])
        return _r(True, f"sent the {act.replace('_', ' ')} media key (playback state not verified)")

    def _t_open_terminal(self, a, confirmed=False):
        if self.dry_run:
            return self._dry("open a terminal")
        from . import executor
        if a.get("in_vscode"):
            if not apps.is_demo_code(_fg()[0]):       # only the whis VS Code window (the user's own may run Claude Code)
                apps.open_or_focus("code")
                if not self._wait(lambda: apps.is_demo_code(_fg()[0]), 6.0):
                    return _r(False, f"the whis VS Code window didn't come to the front ({_fg_desc()})")
            h = _fg()[0]
            for _ in range(2):                    # Ctrl+` toggles: press again if it closed an open terminal
                tree.ui_call(executor._send_keys, "{Ctrl}`"); time.sleep(0.5)
                name, _ = tree.ui_call(_focused_text)
                if "terminal" in (name or "").lower() and apps.is_demo_code(h):
                    self._term_hwnds.add(h)
                    return _r(True, "VS Code terminal is open and focused")
            return _r(False, "couldn't focus VS Code's terminal")
        import subprocess
        before = {w for w, _, p in apps.running_windows() if p.lower() == "windowsterminal"}
        subprocess.Popen(["wt.exe", "-w", "new"], creationflags=0x08000000)
        new = []
        ok = self._wait(lambda: bool(new.extend(w for w, _, p in apps.running_windows()
                                                if p.lower() == "windowsterminal" and w not in before) or new), 6.0)
        if not ok:
            return _r(False, "no new terminal window appeared")
        apps.focus_hwnd(new[0])
        self._term_hwnds.add(new[0]); apps.WHIS_TERMINALS.add(new[0])
        ok = self._wait(lambda: _fg()[0] == new[0], 2.0)
        return _r(ok, "new terminal window is open and in front" if ok else "terminal opened but not in front")

    def _t_run_command(self, a, confirmed=False):
        cmd = str(a.get("command", "")).strip()
        if not cmd:
            return _r(False, "empty command")
        if cmd.lower() in ("claude code", "claude-code"):         # the spoken name; the CLI is `claude`
            cmd = "claude"
        h = _fg()[0]
        if not self.dry_run and (h not in self._term_hwnds or not apps.safe_terminal(h)):
            return _r(False, "refused: the foreground is not a terminal whis opened; call open_terminal first")
        if not confirmed and not HARMLESS_CMDS.match(cmd):
            return self._ask("run_command", {"command": cmd, "_hwnd": h}, f"run {cmd[:40]}")
        if self.dry_run:
            return self._dry(f"run {cmd}")
        if confirmed and a.get("_hwnd") and _fg()[0] != a["_hwnd"]:
            apps.focus_hwnd(a["_hwnd"]); time.sleep(0.2)
            if _fg()[0] != a["_hwnd"]:
                return _r(False, "that terminal is no longer in front")
        from . import executor
        tree.ui_call(executor._type, cmd); time.sleep(0.15)
        if _fg()[0] != h:
            return _r(False, "focus moved before Enter; command not run")
        tree.ui_call(executor._send_keys, "{Enter}")
        return _r(True, f"typed '{cmd}' + Enter in the terminal; output not verified (use look to check)")

    def _t_close_window(self, a, confirmed=False):
        app = str(a.get("app", "")).strip()
        h = a.get("_hwnd") or (_fg()[0] if _fg_is(app) else apps.find_window(app))
        if not h:
            return _r(False, f"no {app} window")
        proc = tree._proc_name(h).lower()
        if proc in config.PROTECTED_APPS or proc in NO_TYPE_PROCS:
            return _r(False, f"refused: never close {proc}")
        if not confirmed:
            return self._ask("close_window", {"app": app, "_hwnd": h}, f"close {app or proc}")
        if self.dry_run:
            return self._dry(f"close {app}")
        apps.focus_hwnd(h); time.sleep(0.2)
        if _fg()[0] != h:
            return _r(False, "that window is no longer in front; not closing")
        from . import executor
        tree.ui_call(executor._send_keys, "{Alt}{F4}")
        gone = self._wait(lambda: not win32gui.IsWindow(h) or not win32gui.IsWindowVisible(h), 1.5)
        return _r(gone, f"closed {app}" if gone else f"a dialog appeared instead: {_fg_desc()}")

    def _t_save(self, a, confirmed=False):
        app = str(a.get("app", "")).strip()
        if not confirmed:
            return self._ask("save", {"app": app, "_hwnd": _fg()[0]}, f"save in {app}")
        if self.dry_run:
            return self._dry(f"save in {app}")
        h = a.get("_hwnd")
        if h and _fg()[0] != h:
            apps.focus_hwnd(h); time.sleep(0.2)
        if not _fg_is(app) or _fg()[1].lower() in NO_TYPE_PROCS:
            return _r(False, f"{app} is not in front ({_fg_desc()}); not saving")
        from . import executor
        out = executor.run(Action("save", {}), None)
        time.sleep(0.3)
        return _r(out.ok, f"{out.msg}; window now {_fg_desc()}")

    def _t_look(self, a, confirmed=False):
        img = self._screenshot_block()
        what = "the web page viewport" if self._shot.get("kind") == "web" else "the foreground window"
        return _r(True, f"screenshot of {what} attached ({self._shot['w']}x{self._shot['h']} px; click_at uses these coordinates)", image=img)

    # ---- web page tools (whis's browser)
    def _need_browser(self):
        if not self.use_browser:
            return "the browser is disabled (--no-browser)"
        if not _browser_fg():            # Playwright only acts inside its own page, so just bring it up (saves a step)
            from . import browser
            try:
                browser.call("bring_to_front", timeout=5)
                self._wait(_browser_fg, 1.5)
            except Exception as e:
                return f"whis's browser didn't come to the front ({e!r:.80})"
        return None

    def _web_result(self, out, verb):
        if not isinstance(out, dict) or not out.get("ok"):
            return _r(False, f"{verb} failed: {(out or {}).get('msg', 'error') if isinstance(out, dict) else 'browser error'}")
        nav = f"page is now \"{out.get('title', '')[:60]}\" {out.get('url', '')}" if out.get("navigated") else "same URL (check STATE)"
        return _r(True, f"{verb}; {nav}")

    def _t_click_web(self, a, confirmed=False):
        tgt = str(a.get("target", a.get("element_id", ""))).strip()
        if not tgt:
            return _r(False, "no target")
        err = None if self.dry_run else self._need_browser()
        if err:
            return _r(False, err)
        w = a.get("_w") if confirmed and a.get("_w") else self._web.get(_wid(tgt))
        if re.fullmatch(r"[wW]\d+", tgt) and w is None:
            return _r(False, f"no element {tgt} in the latest STATE")
        name = w["name"] if w else tgt
        if not confirmed and DESTRUCTIVE_NAMES.search(name):
            return self._ask("click_web", {"target": tgt, "_w": w, "_url": self._url()}, f"click {name[:40]}")
        if self.dry_run:
            return self._dry(f"click {name}")
        from . import browser
        if confirmed and a.get("_url") and self._url() != a["_url"]:
            return _r(False, "the page changed since; not clicking")
        out = browser.call("web_click", w["i"] if w else tgt, timeout=15)
        res = self._web_result(out, f"clicked \"{((out or {}).get('clicked') or name)[:50]}\"" if isinstance(out, dict) else "click")
        if res["ok"] and not out.get("navigated"):
            res["no_finish"] = True                   # a menu/toggle: let Claude see the result before claiming success
        return res

    def _t_type_web(self, a, confirmed=False):
        field, text, submit = str(a.get("field", "")).strip(), str(a.get("text", "")), bool(a.get("submit"))
        if not text:
            return _r(False, "no text")
        err = None if self.dry_run else self._need_browser()
        if err:
            return _r(False, err)
        w = a.get("_w") if confirmed and a.get("_w") else self._web.get(_wid(field))
        label = (w["name"] if w else field) or "text field"
        if submit and not confirmed and not re.search(r"search|find|query|filter|url|address|go to", label + " " + self._url(), re.I) \
                and "d2l" in self._url().lower():
            return self._ask("type_web", {"field": field, "text": text, "submit": True, "_w": w}, f"type into {label[:30]} and press Enter")
        if self.dry_run:
            return self._dry(f"type {len(text)} chars into {label}")
        from . import browser
        out = browser.call("web_type", w["i"] if w else field, text, submit, timeout=15)
        if not isinstance(out, dict) or not out.get("ok"):
            return _r(False, f"typing failed: {(out or {}).get('msg', 'browser error') if isinstance(out, dict) else 'browser error'}")
        val = out.get("value") or ""
        verified = _norm(text)[:40] in _norm(val)
        msg = f"typed into {label[:40]}" + ("; verified in the field" if verified else f"; field reads {val[:60]!r}")
        if submit:
            msg += "; pressed Enter; " + (f"page is now \"{out.get('title', '')[:60]}\" {out.get('url', '')}" if out.get("navigated") else "same URL")
        return _r(verified or submit, msg)

    def _url(self):
        try:
            from . import browser
            return browser.call("url", timeout=3) or ""
        except Exception:
            return ""

    def _t_click_at(self, a, confirmed=False):
        s = self._shot
        if not s:
            return _r(False, "call look() first; click_at uses the latest screenshot's coordinates")
        try:
            x, y = float(a.get("x")), float(a.get("y"))
        except Exception:
            return _r(False, "x and y must be numbers")
        if not (0 <= x <= s["w"] and 0 <= y <= s["h"]):
            return _r(False, f"({x:.0f},{y:.0f}) is outside the {s['w']}x{s['h']} screenshot")
        if _fg()[0] != s["hwnd"] and not confirmed:
            return _r(False, f"the window changed since the screenshot (foreground: {_fg_desc()}); look() again")
        if s["kind"] == "web":
            from . import browser
            cx, cy = x * s["sx"], y * s["sy"]
            name = "" if self.dry_run else (browser.call("web_point", cx, cy, timeout=5) or "")
            if not confirmed and DESTRUCTIVE_NAMES.search(name):
                return self._ask("click_at", {"x": x, "y": y}, f"click {name[:40]}")
            if self.dry_run:
                return self._dry(f"click page at {cx:.0f},{cy:.0f} ({name[:30]})")
            out = browser.call("mouse_click", cx, cy, timeout=15)
            res = self._web_result(out, f"clicked page at ({cx:.0f},{cy:.0f})" + (f" on \"{name[:40]}\"" if name else ""))
            if res["ok"] and not out.get("navigated"):
                res["no_finish"] = True
            return res
        px, py = int(s["left"] + x * s["sx"]), int(s["top"] + y * s["sy"])
        try:
            import uiautomation as auto
            name = tree.ui_call(lambda: (auto.ControlFromPoint(px, py).Name or ""), timeout=2.0)
        except Exception:
            name = ""
        if not confirmed and DESTRUCTIVE_NAMES.search(name):
            return self._ask("click_at", {"x": x, "y": y}, f"click {name[:40]}")
        if self.dry_run:
            return self._dry(f"click screen at {px},{py} ({name[:30]})")
        import win32api, win32con
        before = self._sig()
        win32api.SetCursorPos((px, py))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0); time.sleep(0.03)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        changed = self._wait(lambda: self._sig()[:2] != before[:2], 0.6)
        return _r(True, f"clicked screen ({px},{py})" + (f" on \"{name[:40]}\"" if name else "") + ("; " + f"window is now {_fg_desc()}" if changed else "; title unchanged"),
                  **({} if changed else {"no_finish": True}))

    # ---- D2L (Brightspace) API tools: run on the Playwright thread with the logged-in session
    def _d2l(self, fn, *args, timeout=30, **kw):
        if not self.use_browser:
            return {"ok": False, "error": "the browser is disabled (--no-browser)"}
        from . import browser, d2l
        return browser.call("run", lambda page: getattr(d2l, fn)(page, *args, **kw), timeout=timeout)

    def _t_d2l_courses(self, a, confirmed=False):
        out = self._d2l("courses", all_terms=bool(a.get("all_terms")))
        if not out.get("ok"):
            return _r(False, f"D2L courses failed: {out.get('error')}")
        return _r(True, json.dumps(out["courses"], ensure_ascii=False))

    def _t_d2l_open_course(self, a, confirmed=False):
        if self.dry_run:
            return self._dry(f"open D2L course {a.get('course')}")
        out = self._d2l("open_course", str(a.get("course", "")), section=str(a.get("section") or "home"))
        if not out.get("ok"):
            return _r(False, f"couldn't open the course: {out.get('error', out.get('url', '?'))}" +
                      (f"; courses: {', '.join(out['courses'])}" if out.get("courses") else "") + (f"; {out['hint']}" if out.get("hint") else ""))
        self._wait(_browser_fg, 1.5)
        return _r(True, f"opened {out['course']} {out['section']} page (\"{out['title'][:60]}\") at {out['url']}")

    def _t_d2l_assignments(self, a, confirmed=False):
        kw = {"course": (str(a["course"]) if a.get("course") else None), "days": (a.get("days") if isinstance(a.get("days"), (int, float)) else None),
              "include_submitted": bool(a.get("include_submitted"))}
        out = self._d2l("assignments", **kw)
        if not out.get("ok"):
            return _r(False, f"D2L assignments failed: {out.get('error')}" + (f"; {out['hint']}" if out.get("hint") else ""))
        items = [{k: v for k, v in x.items() if k in ("course", "name", "due", "submitted", "overdue", "late_until")} for x in out["items"]]
        res = _r(True, json.dumps({"now": out["now"], "count_left": out["count_left"], "courses_checked": out["courses_checked"],
                                   "items": items, "summary": out["spoken"]}, ensure_ascii=False))
        if a.get("speak"):
            utt = getattr(self, "_cur_utt", "") or ""
            tail = re.split(r"assignments?|\bdue\b|\bleft\b", utt, flags=re.I)[-1]
            if re.search(r"\b(?:then|open|run|go|play|type|write|switch|launch|start)\b", tail, re.I):
                self.progress(out["spoken"], "acting")        # pitch: "...look if I have an assignment left, then open vs code..."
            else:                                             # show the answer and keep going with the rest of the sentence
                res["final"] = out["spoken"]
        return res

    def _t_d2l_open_assignment(self, a, confirmed=False):
        if self.dry_run:
            return self._dry(f"open D2L assignment {a.get('name')}")
        out = self._d2l("open_assignment", str(a.get("name", "")), course=(str(a["course"]) if a.get("course") else None))
        if not out.get("ok"):
            return _r(False, f"couldn't open the assignment: {out.get('error', out.get('url', '?'))}")
        return _r(True, f"opened {out['course']} {out['assignment']} (due {out['due']}); nothing submitted")

    def _ask(self, tool, args, desc):
        self.pending = {"tool": tool, "args": args, "desc": desc, "t": time.time()}
        return _r(False, f"needs_confirmation: asked the user to say yes to {desc}", pending=True)

    # ---- a turn
    def handle(self, utterance: str, t0: float | None = None) -> str:
        """Run the Claude loop for one final utterance. Returns the text shown to the user."""
        t0 = t0 or time.perf_counter()
        self._cur_utt, self._audited = utterance, False
        if self._scrolling:                              # any new command ends a continuous scroll; "stop" needs no Claude
            what = self.stop_scrolling()
            if _STOP_WORDS.match(utterance.strip()):
                return self._local_done(utterance, t0, "scroll", {"direction": "stop"}, what)
        self._begin()
        self.cancel.clear()
        steps, usage = [], {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
        first_action_ms, final, model_used, stop_reason = None, None, self.model, ""
        try:
            self.progress(utterance, "thinking")
            mem = "\n".join(f"- said \"{m['said']}\" -> {m['did'] or 'nothing'} -> \"{m['result']}\"" for m in self.memory) or "(none)"
            state = self.observe(settle=0)
            messages = [{"role": "user", "content": f"MEMORY (recent turns, oldest first):\n{mem}\n\nSTATE\n{state}\n\nUSER SAID: \"{utterance}\""}]
            for step in range(MAX_STEPS):
                if self.cancel.is_set():
                    final = final or "stopped for your next command"; stop_reason = "cancelled"; break
                if time.perf_counter() - t0 > DEADLINE_S:
                    final = final or "ran out of time"; stop_reason = "deadline"; break
                tc = time.perf_counter()
                resp, model_used = self._call(messages)
                u = resp.usage
                usage["in"] += u.input_tokens or 0; usage["out"] += u.output_tokens or 0
                usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
                usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
                api_ms = round((time.perf_counter() - tc) * 1000)
                messages.append({"role": "assistant", "content": resp.content})
                uses = [b for b in resp.content if b.type == "tool_use"]
                if not uses:
                    final = "".join(b.text for b in resp.content if b.type == "text").strip() or final or "done"
                    m = re.match(r"""^\s*(?:done|say)\(\s*(?:summary|text)?\s*=?\s*["'](.*)["']\s*\)\s*$""", final, re.S)
                    if m:                                   # the call was written as text instead of a tool_use block
                        final = m.group(1)
                    stop_reason = resp.stop_reason or "end_turn"; break
                results, failed, stop, ran_action = [], False, False, None
                for b in uses:
                    if failed or stop:
                        results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                        "content": "skipped: an earlier call in this turn " + ("failed" if failed else "ended the task")})
                        continue
                    if first_action_ms is None:
                        first_action_ms = round((time.perf_counter() - t0) * 1000)
                    args = dict(b.input or {})
                    if b.name in _TERMINAL_TOOLS:
                        final = str(args.get("text") if b.name == "say" else args.get("summary", "")).strip()
                        utt = getattr(self, "_cur_utt", "") or ""
                        did = [s["tool"] for s in steps if s.get("ok") and s["tool"] not in ("done", "say", "look")]
                        if (b.name == "done" and not getattr(self, "_audited", False)
                                and len(re.findall(r",|\bthen\b|\band\b", utt, re.I)) >= 2):   # long multi-part request
                            self._audited = True       # Haiku dropped the last clause of the one-breath pitch and claimed it
                            results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True, "content":
                                            f"Before finishing, check the request clause by clause against what actually ran: {did}. "
                                            "If any requested action was not executed, do it now; otherwise call done again."})
                            final = None
                            continue
                        if not final and re.match(r"^\s*(?:what|what's|whats|which|who|where|how|is|are|do|does|can you see|tell me)\b",
                                                  getattr(self, "_cur_utt", ""), re.I):
                            final = None           # Haiku sometimes ends a question with done(summary=""): make it answer
                            results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                            "content": "The user asked a question. Answer it now with say(text) in one or two short sentences."})
                            continue
                        stop = True
                        steps.append(dict(tool=b.name, args=args, ok=True, result=final[:200], ms=0, api_ms=api_ms))
                        results.append({"type": "tool_result", "tool_use_id": b.id, "content": "ok"})
                        continue
                    self.progress(self._progress_text(b.name, args), "acting")
                    ts = time.perf_counter()
                    r = self.run_tool(b.name, args)
                    ms = round((time.perf_counter() - ts) * 1000)
                    steps.append(dict(tool=b.name, args=args, ok=r["ok"], result=str(r["result"])[:200], ms=ms, api_ms=api_ms))
                    api_ms = 0
                    print(f"  [CLAUDE] {b.name} {json.dumps(args, ensure_ascii=False)[:120]} -> {'ok' if r['ok'] else 'FAIL'} {r['result'][:120]} ({ms} ms)", flush=True)
                    if r.get("pending"):
                        final = f"Say yes to {self.pending['desc']}"
                        stop = True
                        self.progress(final, "asking")
                        results.append({"type": "tool_result", "tool_use_id": b.id, "content": r["result"]})
                        continue
                    content = [{"type": "text", "text": ("ok: " if r["ok"] else "FAILED: ") + str(r["result"])}]
                    if r.get("image"):
                        content.append(r["image"])
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": content, **({} if r["ok"] else {"is_error": True})})
                    ran_action = results[-1]
                    if not r["ok"]:
                        failed = True
                    elif r.get("final") or (b.name in _FINISHABLE and str(args.get("finish") or "").strip() and not r.get("no_finish")):
                        final = str(r.get("final") or args["finish"]).strip()      # verified success ends the task: no extra API call
                        stop = True
                        steps[-1]["finished"] = True
                if stop and not failed:
                    stop_reason = "done"; break
                if stop and self.pending:
                    stop_reason = "confirm"; break
                if ran_action is not None:                     # fresh eyes after the batch, on its last action result
                    ran_action["content"].append({"type": "text", "text": "STATE\n" + self.observe()})
                messages.append({"role": "user", "content": results})
            else:
                final = final or "ran out of steps"; stop_reason = "max_steps"
        except Exception as e:
            bus.log("events", kind="claude_turn_error", err=repr(e)[:300])
            final = "Couldn't do that (Claude error)"; stop_reason = "error"
        finally:
            self._end()
        final = final if final is not None else ""
        total_ms = round((time.perf_counter() - t0) * 1000)
        did = "; ".join(f"{s['tool']}({_brief(s['args'])}) {'ok' if s['ok'] else 'FAILED'}" for s in steps if s["tool"] not in _TERMINAL_TOOLS)
        self.memory.append({"said": utterance[:120], "did": did[:300], "result": final[:120]})
        bus.log("brain", brain="claude", utterance=utterance, steps=steps, total_ms=total_ms, first_action_ms=first_action_ms,
                final_text=final, model=model_used, input_tokens=usage["in"] + usage["cache_read"] + usage["cache_write"],
                output_tokens=usage["out"], cache_read_tokens=usage["cache_read"], cache_write_tokens=usage["cache_write"],
                stop=stop_reason, ok=bool(steps) and all(s["ok"] for s in steps), pending=bool(self.pending))
        print(f"  [CLAUDE] {stop_reason}: {final!r}  ({total_ms} ms, first action {first_action_ms} ms, {len(steps)} steps, "
              f"in {usage['in']}+{usage['cache_read']}r+{usage['cache_write']}w / out {usage['out']} tok)", flush=True)
        if final and stop_reason != "confirm":
            self.progress(final, "asking" if steps and (steps[-1]["tool"] == "say" or steps[-1]["tool"] == "d2l_assignments") else ("acting" if stop_reason == "done" else "idle"))
        return final

    def confirm(self, yes: bool, utterance: str, t0: float | None = None) -> str:
        """The user's yes/no to a pending destructive action."""
        t0 = t0 or time.perf_counter()
        p, self.pending = self.pending, None
        if p is None:
            return ""
        self._begin()
        try:
            if not yes:
                final, steps = "Cancelled.", []
            else:
                self.progress(self._progress_text(p["tool"], p["args"]).rstrip("?") + "…", "acting")
                ts = time.perf_counter()
                r = self.run_tool(p["tool"], p["args"], confirmed=True)
                steps = [dict(tool=p["tool"], args={k: v for k, v in p["args"].items() if not k.startswith("_")}, ok=r["ok"],
                              result=str(r["result"])[:200], ms=round((time.perf_counter() - ts) * 1000))]
                final = (p["desc"] + ": done") if r["ok"] else f"{p['desc']} failed: {r['result']}"[:120]
                print(f"  [CLAUDE] confirmed {p['tool']} -> {'ok' if r['ok'] else 'FAIL'} {r['result'][:120]}", flush=True)
        finally:
            self._end()
        total_ms = round((time.perf_counter() - t0) * 1000)
        self.memory.append({"said": utterance[:60], "did": f"{'confirmed' if yes else 'cancelled'} {p['desc']}", "result": final[:120]})
        bus.log("brain", brain="claude", utterance=utterance, steps=steps, total_ms=total_ms, first_action_ms=(0 if steps else None),
                final_text=final, model="local-confirm", input_tokens=0, output_tokens=0, stop="confirmed" if yes else "cancelled",
                ok=bool(steps) and steps[0]["ok"])
        self.progress(final, "acting" if yes else "idle")
        return final

    def _local_done(self, utterance, t0, tool, args, final):
        self.memory.append({"said": utterance[:60], "did": f"{tool}({_brief(args)}) ok", "result": final})
        print(f"  [LOCAL] {final} <- '{utterance}'", flush=True)
        bus.log("brain", brain="claude", utterance=utterance, steps=[dict(tool=tool, args=args, ok=True, result=final, ms=0)],
                total_ms=round((time.perf_counter() - t0) * 1000), first_action_ms=0, final_text=final, model="local",
                input_tokens=0, output_tokens=0, stop="local", ok=True)
        self.progress(final, "acting")
        return final

    def local_media(self, intent: str, utterance: str, t0: float | None = None) -> str:
        """MEDIA_WORDS fast path ("pause", "volume up"): no Claude call; logged in the same format."""
        t0 = t0 or time.perf_counter()
        if self._scrolling and intent == "media_play_pause":        # "stop" / "pause" while scrolling = stop the scroll
            return self._local_done(utterance, t0, "scroll", {"direction": "stop"}, self.stop_scrolling() or "stopped")
        ts = time.perf_counter()
        ok = True
        if not self.dry_run and intent == "media_play_pause" and self.use_browser and _browser_fg():
            r = self._t_media({"action": "play_pause"})                  # a video in whis's browser: toggled directly
            return self._local_done(utterance, t0, "media", {"action": "play_pause"}, r["result"][:60])
        if not self.dry_run:
            from . import executor
            try:
                executor._vk(executor.VK[intent])
            except Exception:
                ok = False
        ms = round((time.perf_counter() - ts) * 1000)
        final = intent.replace("_", " ")
        self.memory.append({"said": utterance[:60], "did": f"media {intent}", "result": final})
        print(f"  [LOCAL] {intent} <- '{utterance}'", flush=True)
        bus.log("brain", brain="claude", utterance=utterance, steps=[dict(tool="media", args={"action": intent}, ok=ok, result="media key sent", ms=ms)],
                total_ms=round((time.perf_counter() - t0) * 1000), first_action_ms=round((ts - t0) * 1000), final_text=final,
                model="local", input_tokens=0, output_tokens=0, stop="local", ok=ok)
        self.progress(final, "acting")
        return final


def _brief(args: dict) -> str:
    v = next((str(x) for k, x in args.items() if not k.startswith("_")), "")
    return v[:40]
