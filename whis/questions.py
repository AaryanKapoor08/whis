"""Builds the Jev state + question set for one decision. Texts ported from jev-voice-browser constants.js and
jev-voice brain.py, rewritten for the Windows desktop. All questions are speculative and run in parallel."""
from . import config, spans

INTENTS = {
    "open_app": {"what": "Launch an application that is not running, or bring it to the front if it is",
                 "not_for": "clicking things inside an app, opening a website", "examples": ["open notepad", "launch spotify", "start the calculator"]},
    "focus_app": {"what": "Switch to an application or window that is already open",
                  "not_for": "launching new apps", "examples": ["switch to chrome", "go to spotify", "show notepad", "bring up the browser"]},
    "close_window": {"what": "Close the current window or app", "not_for": "closing a browser tab (use press_key ctrl_w)", "examples": ["close this", "close the window", "quit notepad"]},
    "click_element": {"what": "Click, press, select or open a named on-screen element (button, link, menu item, tab, checkbox)",
                      "not_for": "typing, opening apps, navigating to sites", "examples": ["click submit", "press the save button", "open assignment 3", "select the first link", "tick the box"]},
    "type_text": {"what": "Type words into the focused text field", "not_for": "searching the web, pressing single keys",
                  "examples": ["type hello world", "write dear professor", "enter my name"]},
    "press_key": {"what": "Press a keyboard key or shortcut", "not_for": "typing sentences, saving a file",
                  "examples": ["press enter", "hit escape", "undo that", "paste", "new tab", "close tab"]},
    "save": {"what": "Save the current file or document", "not_for": "anything else", "examples": ["save it", "save the file", "save this"]},
    "scroll_up": {"what": "Scroll the current view up", "not_for": None, "examples": ["scroll up", "go up a bit"]},
    "scroll_down": {"what": "Scroll the current view down", "not_for": None, "examples": ["scroll down", "keep going", "page down"]},
    "go_back": {"what": "Go back to the previous page or screen", "not_for": None, "examples": ["go back", "back", "previous page"]},
    "navigate_url": {"what": "Open a website or bookmarked site in the browser", "not_for": "searching for something, opening desktop apps",
                     "examples": ["go to d2l", "open youtube", "navigate to github dot com", "open gmail"]},
    "search_in_app": {"what": "Search for something INSIDE the current or named application (Spotify, Explorer, VS Code, the current page)",
                      "not_for": "web search, opening apps", "examples": ["in spotify search for tame impala", "search for invoices in explorer", "find the word budget", "search this page for login"]},
    "play_song": {"what": "Play a named song, artist or playlist in Spotify", "not_for": "plain play/pause, web search",
                  "examples": ["play tame impala", "play this song blinding lights", "put on some daft punk", "play let it happen on spotify"]},
    "ask_screen": {"what": "Answer a question about what is currently on screen or on the page (read, summarise, check, look for)",
                   "not_for": "doing actions", "examples": ["do I have any assignments left", "what's on this page", "is there anything due this week", "read me the first result"]},
    "open_terminal": {"what": "Open a terminal: the integrated terminal inside the current editor (VS Code / Cursor) when the user says 'in it' or is in an editor, otherwise Windows Terminal",
                      "not_for": "running a command", "examples": ["open the terminal in it", "open a terminal", "show the terminal"]},
    "run_command": {"what": "Type a command into the terminal and press enter", "not_for": "typing prose",
                    "examples": ["run claude code", "run npm install", "execute python main.py", "run claude in it"]},
    "search_web": {"what": "Search the web for a phrase", "not_for": "opening a known site", "examples": ["search for weather in fredericton", "google python tutorials", "look up hack atlantic"]},
    "media_play_pause": {"what": "Play or pause music or video", "not_for": None, "examples": ["play", "pause", "pause the music", "resume"]},
    "volume_up": {"what": "Make the volume louder", "not_for": None, "examples": ["volume up", "louder", "turn it up"]},
    "volume_down": {"what": "Make the volume quieter", "not_for": None, "examples": ["volume down", "quieter", "turn it down"]},
    "confirm": {"what": "The user agrees to a pending confirmation", "not_for": "anything when nothing is pending", "examples": ["yes", "yeah do it", "confirm", "go ahead"]},
    "cancel": {"what": "The user declines or cancels a pending confirmation or the current action", "not_for": None, "examples": ["no", "cancel", "never mind", "stop"]},
    "none": {"what": "No clear instruction for the computer, or chit-chat, or an incomplete fragment", "not_for": None, "examples": ["so anyway", "I think we should", "um"]},
}

Q_IS_COMMAND = ("Is `transcript` an instruction addressed to a voice assistant that controls this Windows computer "
                "(open or switch apps, click things, type, press keys, scroll, browse the web, play media, confirm or cancel)? "
                "Chit-chat, narration, talking to another person, or reading aloud is not a command. A correction of the last action IS a command.")
Q_ADDRESSED = ("Is `transcript` spoken TO the computer assistant, rather than conversation with another person, a phone call, "
               "presenting to an audience, reading aloud, or thinking out loud?")
Q_COMPLETE = ("Has the user finished saying the command in `transcript`, so it can be executed now without waiting for more words? "
              "Speech arrives word by word. A command is complete when its verb and any required object are present "
              "(e.g. 'open notepad' is complete; 'open' or 'click the' is not). Trailing 'and' or 'then' means more is coming.")
Q_DESTRUCTIVE = ("Would carrying out the action in `transcript` on the current screen submit a form, place an order, pay, delete, send a message, "
                 "post publicly, log out, overwrite or save over a file, or otherwise do something hard to undo? "
                 "Opening apps, navigating, scrolling, clicking links or tabs, and typing into a field are NOT destructive.")
Q_CORRECTION = ("Is `transcript` correcting or undoing the most recent action in `context.recent_actions` (e.g. 'no, the other one', 'not that', 'undo')?")


def build(ctx) -> tuple[dict, dict]:
    """ctx: controller context with .transcript .final .silent_ms .snapshot .recent .pending .named .apps_running .overlay_n
    Returns (state, questions)."""
    snap = ctx.snapshot
    elems = snap.lines(config.STATE_MAX_ELEMENTS) if snap else []
    cand_text = spans.text_spans(ctx.transcript)
    cand_url = spans.url_spans(ctx.transcript)
    state = {
        "transcript": ctx.transcript, "final": ctx.final, "silent_ms": int(ctx.silent_ms),
        "app": snap.app if snap else "", "title": (snap.title[:80] if snap else ""),
        "elements": elems,
        "apps_running": ctx.apps_running[:12],
        "candidates": {"text": cand_text, "url": cand_url},
        "context": {"recent_actions": ctx.recent[-3:], "pending_confirmation": ctx.pending_desc},
    }
    q = {
        "is_command": {"type": "noul", "instructions": Q_IS_COMMAND},
        "complete": {"type": "noul", "instructions": Q_COMPLETE},
        "intent": {"type": "choice", "instructions": "Which single action does the user want the computer to take right now", "criteria": INTENTS},
        "app": {"type": "choice", "instructions": "Which application does the user refer to, if any",
                "criteria": {**{a: None for a in sorted(set(list(config.APPS) + ["chrome", "brave"] + ctx.apps_running[:12]))}, "none": "no app named"}},
        "key": {"type": "choice", "instructions": "Which key or shortcut does the user want pressed, if any",
                "criteria": {**{k: None for k in config.KEYS}, "none": "no key named"}},
        "site": {"type": "choice", "instructions": "Which known site does the user want to open, if any",
                 "criteria": {**{s: None for s in config.BOOKMARKS}, "none": "no known site named"}},
        "destructive": {"type": "noul", "instructions": Q_DESTRUCTIVE},
    }
    if not ctx.named:
        q["addressed"] = {"type": "noul", "instructions": Q_ADDRESSED}
    if elems:
        q["target"] = {"type": "choice", "instructions": "Which on-screen element in `elements` does the user mean, if any",
                       "criteria": {**{ln.split()[0]: ln for ln in elems}, "none": "no element referred to"}}
    if cand_text:
        q["text_span"] = {"type": "choice", "instructions": "Which candidate in `candidates.text` is the exact text the user wants typed or searched",
                          "criteria": {**{c: None for c in cand_text}, "none": "none of them"}}
    if ctx.recent:
        q["is_correction"] = {"type": "noul", "instructions": Q_CORRECTION}
    return state, q
