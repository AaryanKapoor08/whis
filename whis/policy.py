"""Pure decision function. Order is fixed (Architecture.md §Policy order). No I/O here.
Ported from jev-voice-browser policy.js evaluatePolicy."""
from . import config, spans
from .types import Decision, Action

T = config.T


def _c(ans, k):   # choice helper -> (choice, confidence, top_prob)
    a = ans.get(k)
    if not a or a.get("type") != "choice":
        return None, 0.0, 0.0
    probs = a.get("probabilities") or {}
    return a.get("choice"), float(a.get("confidence", 0)), float(probs.get(a.get("choice"), 0))


def _n(ans, k, default=0.0):
    a = ans.get(k)
    return float(a["noul"]) if a and "noul" in a else default


def _song(t: str) -> str:
    """Strip the words around a song/artist name: articles, 'song for me', 'on spotify', 'please'."""
    import re
    t = t.strip(" .!?,")
    t = re.sub(r"^(?:a|an|some|me|us)\s+", "", t, flags=re.I)      # keep "the": The Weeknd, The Beatles
    t = re.sub(r"^(?:(?:the|a)\s+)?(?:song|track|tune)?\s*(?:called|named|titled)\s+", "", t, flags=re.I)   # "the song called Loser"
    t = re.sub(r"\s+(?:on|in|from)\s+spotify\s*$", "", t, flags=re.I)   # before "song": "a Tame Impala song in Spotify"
    t = re.sub(r"\s+(?:song|songs|track|tracks|music|album|playlist)(?:\s+(?:for me|for us|please|now))?\s*$", "", t, flags=re.I)
    t = re.sub(r"\s+(?:for me|for us|please|now)\s*$", "", t, flags=re.I)
    t = re.sub(r"\s+(?:on|in|from)\s+spotify\s*$", "", t, flags=re.I)
    return t.strip()


_PRONOUN_SONG = r"^(?:that|this|it|that one|this one|that song|this song|the song|the first one|the top one|it again)$"
_FILLER = r"(?:okay|ok|so|now|and|then|um|uh|please|hey|alright|all right|actually|also)\b[\s,.]*"
_ASK = r"(?:(?:can|could|would|will) you\s+(?:please\s+)?|i (?:want|need) you to\s+|i want to\s+|let's\s+)"
_LEAD_VERB = {"play_song": r"(?:play|put on|queue(?: up)?|listen to)", "run_command": r"(?:run|execute|launch|start|type)",
              "search_in_app": r"(?:search(?: for)?|find|look up|look for)", "search_web": r"(?:search(?: the web)?(?: for)?|google|look up)",
              "type_text": r"(?:type|write|enter|say)"}


def _clean(text: str, intent: str) -> str:
    """Strip what surrounds the payload of a free-text intent: leading filler/politeness/verb ('okay, now play a',
    'can you write'), trailing punctuation (not for type_text: dictation may end with '?'), a trailing 'in it'."""
    import re
    t = text.strip()
    if intent == "type_text":       # dictation: only strip a spoken request wrapper ("can you write X in it?")
        m = re.match(r"^" + _ASK + _LEAD_VERB["type_text"] + r"\b[\s,:]*(.+)$", t, re.I)
        if m:
            t = re.sub(r"\s+(?:in|into|inside)\s+(?:it|there|here)\s*[.?!]?$", "", m.group(1), flags=re.I)
        return t.strip()
    for _ in range(4):
        t2 = re.sub(r"^" + _FILLER, "", t, flags=re.I)
        t2 = re.sub(r"^" + _ASK, "", t2, flags=re.I)
        if intent in _LEAD_VERB:
            t2 = re.sub(r"^" + _LEAD_VERB[intent] + r"\b[\s,:]*", "", t2, flags=re.I)
        if t2 == t:
            break
        t = t2.strip()
    t = t.strip(" .!?,;:")
    t = re.sub(r"\s+(?:please|for me|for us|now|right now)$", "", t, flags=re.I)
    return t.strip(" .!?,;:")


_CLAUDE = r"^(?:claude|cloud|clod|claw|clawed|clot|plot|clode)(?:\s+(?:code|cold|coat|coach|cod))?$"
_SPEECHY = r"\b(?:you|your|me|my|i|we|us|our|can|could|would|please|thing|something|this|that|there|it)\b"


def _command(text: str) -> str:
    """run_command payload: 'claude code and terminal' -> 'claude'. Returns '' for text that reads like speech,
    not a command line (it would be typed into a terminal and executed)."""
    import re
    t = re.sub(r"\s+(?:and|in|on|inside|into|in the|on the)\s+(?:the\s+|a\s+|my\s+)?(?:terminal|console|shell|it|there|vs code|code)$", "", text, flags=re.I)
    t = re.sub(r"^(?:the\s+)?command\s+", "", t, flags=re.I).strip(" .!?,")
    if re.match(_CLAUDE, t, re.I):
        return "claude"
    words = t.split()
    if not words or len(words) > 6 or re.search(_SPEECHY, t, re.I) or re.search(r"\b(?:and|then)\b", t, re.I):
        return ""
    return t


def app_mentioned(app: str, tr: str) -> bool:
    """The chosen app must actually be named in the transcript ('Camera. Open camera' -> chrome@0.56 must not act).
    Browser names are interchangeable; a running-process name like 'WindowsTerminal' matches 'terminal'."""
    import re
    a, t = (app or "").lower(), tr.lower()
    if not a or a == "none":
        return False
    if a in config.BROWSER_NAMES or a in ("msedge", "edge"):
        return bool(re.search(r"\b(?:browser|chrome|brave|edge|internet|web)\b", t))
    words = re.findall(r"[a-z0-9]+", t)
    if any(len(w) >= 3 and w in t for w in re.findall(r"[a-z0-9]+", a)):
        return True
    return any(len(w) >= 4 and w in a for w in words)


def _free_text(ans, tr: str) -> str:
    """Payload for free-text intents. Jev selects among candidate spans; if it picks the whole sentence while a
    shorter after-the-verb candidate exists ("play tame impala" -> "tame impala"), take the shorter one, so the
    controller's 'span == whole sentence' escalation only fires when Jev truly did not understand."""
    cands = spans.text_spans(tr)
    span, sconf, _ = _c(ans, "text_span")
    text = span if (span and span != "none" and sconf >= T["spanConfidence"]) else (cands[0] if cands else "")
    if cands and text.strip().lower() == tr.strip().lower() and cands[0].strip().lower() != tr.strip().lower():
        text = cands[0]
    return text.strip()


_VERB_RESCUE = {"play_song": r"\bplay\b", "run_command": r"\b(?:run|execute)\b",
                "open_app": r"\b(?:open|launch|start|switch to|bring up)\b", "focus_app": r"\b(?:open|switch to|go to|bring up)\b",
                "search_in_app": r"\bsearch\b", "search_web": r"\b(?:search|google)\b",
                "ask_screen": r"^(?:(?:okay|ok|so|now|and|then|please)[\s,]+)*(?:check|look|see|tell me|read|do i have|is there|are there)\b"}


def _rescued(intent, iconf, tr) -> bool:
    """Final 'play a team and parlour song for me' -> play_song 0.54 used to go to the planner (3 s, then a double
    act). A top intent just under the bar whose own verb is spoken is taken locally; args are still checked."""
    import re
    return (intent in _VERB_RESCUE and iconf >= T.get("verbRescue", 0.4)
            and re.search(_VERB_RESCUE[intent], tr, re.I) is not None)


def decide(ans: dict, ctx) -> Decision:
    tr = ctx.transcript
    snap = ctx.snapshot
    intent, iconf, _ = _c(ans, "intent")

    # 1. pending confirmation
    if ctx.pending is not None:
        if intent == "confirm" and iconf >= T["intentConfidence"]:
            return Decision("act", ctx.pending, say="", reason="confirmed")
        if intent == "cancel" and iconf >= T["intentConfidence"]:
            return Decision("cancel", None, say="cancelled", reason="cancelled")
        if intent not in (None, "none") and iconf >= T["intentConfidence"] and _n(ans, "is_command") >= T["isCommand"]:
            ctx.pending = None            # a new confident command supersedes the pending one
        elif ctx.final:
            return Decision("ignore", reason="pending: not yes/no")
        else:
            return Decision("wait", reason="pending: waiting")

    # 2. overlay pick
    if ctx.overlay_n:
        pick = spans.parse_pick(tr, ctx.overlay_n)
        if pick is not None:
            el = snap.elements[pick - 1] if snap and pick <= len(snap.elements) else None
            if el:
                return Decision("act", Action("click_element", {"target": el}, said=tr), reason="overlay pick")

    # 3. is it a command / addressed to us
    if _n(ans, "is_command") < T["isCommand"]:
        # "look if I have an assignment left" (pitch): is_command 0.24 but ask_screen 0.94 - a question about the
        # screen, spoken inside the follow-up window, with a check/look verb up front, is a command
        import re
        if not (intent == "ask_screen" and iconf >= 0.85 and ctx.named and re.search(_VERB_RESCUE["ask_screen"], tr.strip(), re.I)):
            return Decision("ignore", reason="not a command")
    if not ctx.named and _n(ans, "addressed", 1.0) < T["addressed"]:
        return Decision("ignore", reason="not addressed")

    # correction shortcut
    if ctx.recent and _n(ans, "is_correction") >= T["correction"] and ctx.recent[-1].get("action") == "click_element" and snap:
        return Decision("disambiguate", None, say="which", reason="correction")

    # 4. intent
    if intent in ("confirm", "cancel"):
        return Decision("ignore", reason="confirm/cancel with nothing pending")
    if intent in (None, "none") or iconf < T["intentConfidence"]:
        # rescue: "open google" -> navigate_url 0.41 (or open_app 0.53) but site=google 0.97. A sure site choice that is
        # literally spoken carries it.
        site_r, sconf_r, _ = _c(ans, "site")
        if (intent in ("navigate_url", "open_app", "focus_app") and iconf >= T["intentRescue"] and site_r not in (None, "none")
                and sconf_r >= T["siteRescue"] and (intent == "navigate_url" or site_r in tr.lower())):
            intent = "navigate_url"
        elif not (ctx.final and _rescued(intent, iconf, tr)):
            return Decision("ignore" if ctx.final else "wait", reason=f"intent {intent} conf {iconf:.2f}")

    # 5. completeness
    complete = _n(ans, "complete")
    if intent in config.FREE_TEXT_INTENTS:
        if not ctx.final and ctx.silent_ms < config.SILENCE_FREE_MS:
            return Decision("wait", reason="free text: wait for final/silence")
    elif complete < T["complete"] and not ctx.final and ctx.silent_ms < config.SILENCE_CLOSED_MS:
        return Decision("wait", reason=f"incomplete {complete:.2f}")

    # 6. build action
    args = {}
    if intent in ("open_app", "focus_app"):
        app, aconf, _ = _c(ans, "app")
        site, sconf, _ = _c(ans, "site")
        if (site not in (None, "none") and sconf >= T["siteRescue"] and site in tr.lower()
                and (app in (None, "none") or app.lower() in config.BROWSER_NAMES or app.lower() in config.BOOKMARKS)):
            intent, args = "navigate_url", {"url": config.BOOKMARKS[site]}       # "open google" = go there, not just raise the browser
        elif app in (None, "none") or aconf < T["appConfidence"]:
            return Decision("ignore" if ctx.final else "wait", reason="no app")
        elif not app_mentioned(app, tr):
            return Decision("ignore" if ctx.final else "wait", reason=f"app {app} not said")
        else:
            args["app"] = app
    elif intent == "click_element":
        tgt, tconf, ttop = _c(ans, "target")
        el = snap.by_id(tgt) if (snap and tgt and tgt != "none") else None
        if el is None and snap:
            el = _ordinal_target(tr, snap)          # "the first link", "second button"
            if el is not None:
                tconf = ttop = 1.0
        if el is None or tconf < T["targetConfidence"] or ttop < T["targetTopProb"]:
            if snap and snap.elements:
                return Decision("disambiguate", None, say="which", reason=f"target {tgt} {tconf:.2f}/{ttop:.2f}")
            return Decision("ignore", reason="no elements")
        args["target"] = el
    elif intent == "press_key":
        key, kconf, _ = _c(ans, "key")
        if key in (None, "none") or kconf < T["keyConfidence"]:
            return Decision("ignore" if ctx.final else "wait", reason="no key")
        args["key"] = key
    elif intent == "run_command":
        text = _command(_clean(_free_text(ans, tr), intent))    # 'cloud code and terminal' -> 'claude'
        if not text:
            return Decision("ignore", reason="no command")      # speech-like text is never typed into a shell
        args["text"] = text
    elif intent == "ask_screen":
        args["question"] = tr
    elif intent in ("search_in_app", "play_song"):
        text = _clean(_free_text(ans, tr), intent)   # "Tame Impala." -> "Tame Impala"
        if intent == "play_song":
            text = _song(text)                  # "a Tame Impala song for me." -> "Tame Impala"
            import re
            if re.match(_PRONOUN_SONG, text, re.I):    # "play that" -> the last thing searched/played
                text = next((r.get("text") for r in reversed(ctx.recent or []) if r.get("action") in ("play_song", "search_in_app") and r.get("text")), "")
        if not text:
            return Decision("ignore", reason="no text")
        app, aconf, _ = _c(ans, "app")
        args["text"] = text
        args["app"] = app if (app and app != "none" and aconf >= T["appConfidence"]) else None
    elif intent in ("type_text", "search_web"):
        text = _clean(_free_text(ans, tr), intent)
        if not text:
            return Decision("ignore", reason="no text")
        args["text"] = text
    elif intent == "navigate_url":
        site, sconf, _ = _c(ans, "site")
        urls = spans.url_spans(tr)
        if site and site != "none" and sconf >= T["siteConfidence"]:
            args["url"] = config.BOOKMARKS[site]
        elif urls:
            args["url"] = urls[0] if urls[0].startswith("http") else "https://" + urls[0]
        else:
            cands = spans.text_spans(tr)
            if not cands:
                return Decision("ignore", reason="no url")
            intent, args = "search_web", {"text": cands[0]}
    action = Action(intent, args, said=tr)

    # 7. destructive gate
    if intent in config.ALWAYS_CONFIRM:
        return Decision("confirm", action, say="confirm_save", reason="always confirm")
    if intent in config.GATED_INTENTS:
        if _n(ans, "destructive") >= T["destructive"] or iconf < T["destructiveIntentConfidence"] and _n(ans, "destructive") >= 0.3:
            return Decision("confirm", action, say="confirm_generic", reason="destructive")

    # 8. act
    return Decision("act", action, reason="ok")


_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1, "top": 1}
_ROLE_WORDS = {"link": "link", "result": "link", "button": "button", "tab": "tabitem", "field": "edit", "box": "edit", "item": None, "one": None}


def _ordinal_target(tr: str, snap):
    import re
    m = re.search(r"(first|second|third|fourth|fifth|last|top)\s+(?:search\s+)?(link|result|button|tab|field|box|item|one)", tr.lower())
    if not m:
        return None
    n, role = _ORD[m.group(1)], _ROLE_WORDS[m.group(2)]
    pool = [e for e in snap.elements if role is None or e.role.startswith(role)]
    if role == "link":   # skip nav chrome: prefer links with longer names (results) over short menu links
        pool = [e for e in pool if len(e.name) > 12] or pool
    if not pool:
        return None
    return pool[n - 1] if 0 < n <= len(pool) else (pool[-1] if n == -1 else None)
