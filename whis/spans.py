"""Candidate span extraction: select, don't generate. Ported from jev-voice-browser spans.js.
Also parse_pick() for overlay number picks (local, no Jev)."""
import re

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
             "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "to": 2, "too": 2, "for": 4, "won": 1}
_TEXT_TRIGGERS = r"(?:can you |could you |please )?(?:type|write|enter|say|search for|search|google|look up|find|go to|open|navigate to|visit|play|put on|run|execute)"
_URL = re.compile(r"\b((?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?)\b", re.I)


def text_spans(transcript: str, max_n: int = 3) -> list[str]:
    """Candidate free-text payloads: quoted text, everything after a trigger verb, last clause."""
    t = transcript.strip()
    out = []
    for q in re.findall(r"[\"“](.+?)[\"”]", t):
        out.append(q.strip())
    m = re.search(_TEXT_TRIGGERS + r"\s+(.+)$", t, re.I)
    if m:
        rest = re.sub(r"^(?:the words?|the text|this|that|this song|the song|some|in the box|into the box|for)\s+", "", m.group(1).strip(), flags=re.I)
        rest = re.sub(r"\s+(?:in|into|on|inside)\s+(?:it|there|here|the file|the note|the document|the box|the field)\s*[.?!]?$", "", rest, flags=re.I)
        rest = re.sub(r"\s+(?:in|on|inside)\s+(?:spotify|explorer|chrome|the browser|vs code|code|notepad|this page)\s*$", "", rest, flags=re.I)
        rest = re.sub(r"\s*(?:and (?:then|press|hit)\s+.*)$", "", rest, flags=re.I)
        if rest:
            out.append(rest)
    last = re.split(r",|\bthen\b|\band\b", t)[-1].strip()
    if last and last not in out:
        out.append(last)
    seen, uniq = set(), []
    for s in out:
        k = s.lower()
        if k and k not in seen:
            seen.add(k); uniq.append(s)
    return uniq[:max_n]


def url_spans(transcript: str) -> list[str]:
    t = transcript.lower().replace(" dot ", ".").replace(" slash ", "/")
    return [m.group(1) for m in _URL.finditer(t) if "." in m.group(1)][:3]


def numbers(transcript: str) -> list[int]:
    out = []
    for tok in re.findall(r"[a-z0-9]+", transcript.lower()):
        if tok.isdigit():
            out.append(int(tok))
        elif tok in NUM_WORDS:
            out.append(NUM_WORDS[tok])
    return out


def parse_pick(transcript: str, n_max: int) -> int | None:
    """When the overlay is showing, a bare number (or 'number four', 'the third one') picks an element."""
    t = transcript.lower().strip()
    ords = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
    for w, v in ords.items():
        if w in t and v <= n_max:
            return v
    words = re.findall(r"[a-z0-9]+", t)
    homo = ("to", "too", "for", "won")
    ns = [n for n in numbers(" ".join(w for w in words if len(words) == 1 or "number" in words or w not in homo)) if 1 <= n <= n_max]
    if ns and len(words) <= 4:
        return ns[-1]
    return None


def strip_wake(transcript: str, wake_words) -> tuple[bool, str]:
    """Returns (addressed_by_name, command_text_without_wake_word)."""
    t = transcript.strip()
    for w in wake_words:
        m = re.match(rf"^(?:(?:hey|ok|okay|yo),?\s+)?{re.escape(w)}\b[,.!]?\s*(.*)$", t, re.I)
        if m:
            return True, m.group(1).strip()
    return False, t


_VERBS = ("open", "go", "click", "press", "hit", "type", "write", "switch", "close", "quit", "scroll", "search", "google",
          "look", "play", "pause", "save", "turn", "volume", "navigate", "launch", "start", "show", "bring", "select", "tick",
          "undo", "paste", "copy", "find", "mute", "resume", "back", "stop", "cancel", "run", "execute", "check", "see", "tell", "read", "is", "are", "do", "what", "how", "put", "make")
_SEPS = ("and then", "after that", "and", "then")


def split_clauses(text: str) -> list[str]:
    """'open notepad and go to d2l' -> ['open notepad', 'go to d2l']. Only splits before a command verb."""
    words = text.split()
    parts, cur, i = [], [], 0
    while i < len(words):
        hit = None
        # comma or sentence end directly before a command verb: "open vs code, open the terminal",
        # "Go to D2L in Brave. Open Notepad" (the second sentence used to be dropped)
        if cur and cur[-1].endswith((",", ".", "?", "!")) and words[i].lower().strip(",.?!") in _VERBS:
            parts.append(" ".join(cur).strip(" ,."))
            cur = []
        for sep in _SEPS:
            n = len(sep.split())
            if [w.lower().strip(",.") for w in words[i:i + n]] == sep.split() and i + n < len(words)                     and words[i + n].lower().strip(",.") in _VERBS:
                hit = n
                break
        if hit and cur:
            parts.append(" ".join(cur).strip(" ,."))
            cur, i = [], i + hit
            continue
        cur.append(words[i]); i += 1
    if cur:
        parts.append(" ".join(cur).strip(" ,."))
    # a clause left over after an earlier partial was acted on may start with the separator: "and go to d2l"
    parts = [re.sub(r"^(?:and then|after that|and|then)\s+", "", p, flags=re.I).strip(" ,.?!") for p in parts]
    out = []
    for p in parts:      # STT echoes ("Pause. Pause", "Open notepad. Open notepad") must act once, not twice
        if p and not (out and out[-1].lower() == p.lower()):
            out.append(p)
    return out or [text]
