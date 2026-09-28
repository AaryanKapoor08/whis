"""A1/A2 ambient mode: "Clippy had the right idea, it just didn't know when to shut up."
Pattern from jev-drone (notes/group-d §7): code decides WHEN a judgment is worth paying for (decision_needed),
a coarse fingerprint skips unchanged scenes, a dedicated Noul gates the interrupt (>= .8 twice in a row),
cooldown after speaking (longer if dismissed), hard call budget, stale judgments dropped, fail-open on errors.

Inputs: D2L iCal feed (config.D2L_ICAL_URL, polled every 60 s) or a synthetic item (--demo-due-in N minutes).
Output: island pill "Assignment X due in N min — open it? (say yes)" + pop sound, and controller.pending =
navigate_url(link) so the next "yes" opens it and "no" is silent for dismiss_cooldown_s. Never on the voice fast path.
Never nudges while a phone call is active (server.call_active()).

Demo cue: with --demo-due-in the synthetic item waits for arm() (POST localhost:8000/ambient/arm) when the phone
server is running, so it cannot pop during the phone act. WHIS_DEMO_ARM=auto|manual|<seconds> overrides.

D2L (Brightspace) iCal: Calendar > Subscribe gives .../d2l/le/calendar/feed/user/feed.ics?token=... (webcal:// ok).
Events look like SUMMARY "Assignment 3 - Due" / "Quiz 2 - Availability Ends", LOCATION = course offering, DESCRIPTION
ending in "View event - <url>". Times are UTC (Z), TZID (IANA or Windows names like "Atlantic Standard Time"), floating
(-> D2L_TZ, default America/Halifax) or all-day (-> 23:59 local). Only due/end events are kept; "Available"/start dropped."""
import os, re, sys, time, threading, hashlib
from datetime import datetime, date, time as dtime, timezone, timedelta
from zoneinfo import ZoneInfo
import win32gui
from . import config, bus, jev
from .types import Action

C = config.AMBIENT
Q_INTERRUPT = ("Would the user want to be interrupted RIGHT NOW with a short spoken reminder about `next_due`, considering "
               "how soon it is due, whether they are speaking or busy, and how recently they were nudged or dismissed a nudge? "
               "Interrupting is costly: only yes when it clearly helps. `derived.nudge_window_open=true` and no recent dismissal is a clear yes; "
               "`user_speaking=true`, a nudge in the last few minutes, or `assistant.user_dismissed_last_nudge=true` is a clear no.")


LOCAL_TZ = ZoneInfo(os.getenv("D2L_TZ", "America/Halifax"))
_START = re.compile(r"\b(available|availability\s+(starts?|begins?)|opens?|start(s|\s+date)?|begins?)\s*$", re.I)
_DUE = re.compile(r"\b(due|deadline|ends?|end\s+date|closes?|close\s+date|submission)\b", re.I)
_SUFFIX = re.compile(r"\s*[-\u2013\u2014:]\s*(due(\s+date)?|availability\s+ends?|ends?|end\s+date|closes?|deadline)\s*$", re.I)
_COURSE = re.compile(r"\b([A-Z]{2,5})\s?\*?(\d{3,4}[A-Z]?)\b")
_URL = re.compile(r"https?://[^\s\"'<>]+")


def _short_course(s: str) -> str:
    m = _COURSE.search(s or "")
    return f"{m.group(1)} {m.group(2)}" if m else (s or "").split(" - ")[0].strip()[:30]


def _as_utc(v) -> datetime | None:
    """icalendar value -> aware UTC datetime. Floating times are D2L_TZ; all-day dates mean 23:59 local that day."""
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=LOCAL_TZ)).astimezone(timezone.utc)
    if isinstance(v, date):
        return datetime.combine(v, dtime(23, 59), LOCAL_TZ).astimezone(timezone.utc)
    return None


def parse_ical(raw: bytes, default_link: str = "") -> list[dict]:
    """D2L/Brightspace iCal bytes -> [{name, course, due (UTC), link, summary}], due/end events only, one per item."""
    from icalendar import Calendar
    best: dict = {}
    for ev in Calendar.from_ical(raw).walk("VEVENT"):
        summary = re.sub(r"\s+", " ", str(ev.get("SUMMARY", "") or "")).strip()
        if not summary or _START.search(summary) or not _DUE.search(summary):
            continue
        p = ev.get("DTSTART") or ev.get("DTEND") or ev.get("DUE")
        due = _as_utc(p.dt) if p is not None else None
        if due is None:
            continue
        desc = str(ev.get("DESCRIPTION", "") or "")
        m = _URL.search(str(ev.get("URL", "") or "")) or _URL.search(desc.split("View event")[-1]) or _URL.search(desc)
        name = _SUFFIX.sub("", summary).strip() or summary
        course = _short_course(str(ev.get("LOCATION", "") or ""))
        e = {"name": name[:80], "course": course, "due": due, "summary": summary[:120],
             "link": m.group(0).rstrip(").,;>]") if m else default_link}
        key = (name.lower(), course)
        if key not in best or due < best[key]["due"]:     # "Due" and "Availability Ends" for one item: keep the earlier
            best[key] = e
    return sorted(best.values(), key=lambda x: x["due"])


def fetch_ical(url: str, default_link: str = "") -> list[dict]:
    import httpx
    url = re.sub(r"^webcals?://", "https://", url.strip())
    r = httpx.get(url, timeout=8.0, follow_redirects=True)
    if b"BEGIN:VCALENDAR" not in r.content[:2000]:
        raise ValueError(f"not an iCal feed (HTTP {r.status_code}, {r.headers.get('content-type', '?')}); token expired?")
    return parse_ical(r.content, default_link)


_instance = None


def arm(minutes: float | None = None) -> bool:
    """Cue the demo nudge: the synthetic item becomes due `minutes` (default --demo-due-in) from NOW."""
    return _instance.arm(minutes) if _instance is not None else False


def _phone_busy() -> bool:
    srv = sys.modules.get("whis.server")
    try:
        return bool(srv and srv.call_active())
    except Exception:
        return False


class Ambient:
    def __init__(self, controller, get_snapshot, demo_due_in: float | None = None):
        self.ctrl = controller
        self.get_snapshot = get_snapshot
        self.demo = None
        self.demo_minutes = demo_due_in
        self.arm_at = None                                     # perf_counter time of a delayed auto-arm
        self.events: list[dict] = []
        self.last_ical_t = 0.0
        self.last_key = None
        self.last_ask_t = 0.0
        self.hits = 0
        self.last_nudge_t = 0.0
        self.dismissed = False
        self.nudge_action: Action | None = None
        self.calls: list[float] = []
        if demo_due_in is not None:
            mode = os.getenv("WHIS_DEMO_ARM") or ("manual" if "whis.server" in sys.modules else "auto")
            if mode == "auto":
                self.arm()
            elif mode != "manual":
                try:
                    self.arm_at = time.perf_counter() + float(mode)
                except ValueError:
                    pass
            if self.demo is None:
                print("  [AMBIENT] demo nudge waiting for its cue: curl -X POST localhost:8000/ambient/arm"
                      + (f"  (auto in {mode}s)" if self.arm_at else ""))

    def arm(self, minutes: float | None = None) -> bool:
        mins = float(minutes) if minutes is not None else (self.demo_minutes if self.demo_minutes is not None else 110.0)
        self.demo = {"course": "CS 3383", "name": "Assignment 3: Project Proposal",
                     "due": datetime.now(timezone.utc) + timedelta(minutes=mins), "link": config.BOOKMARKS["d2l"]}
        self.arm_at = None
        self.last_nudge_t, self.dismissed, self.hits, self.last_key = 0.0, False, 0, None
        bus.log("ambient", kind="armed", minutes=mins)
        print(f"  [AMBIENT] demo armed: due in {mins:g} min")
        return True

    # ---- inputs
    def _poll_ical(self):
        if not config.D2L_ICAL_URL or time.time() - self.last_ical_t < C["ical_poll_s"]:
            return
        self.last_ical_t = time.time()
        try:
            self.events = fetch_ical(config.D2L_ICAL_URL, config.BOOKMARKS["d2l"])   # last good list kept on error
            bus.log("ambient", kind="ical", n=len(self.events), next=(self.events[0]["name"] if self.events else None))
        except Exception as e:
            bus.log("events", kind="ambient_ical_error", err=repr(e)[:200])

    def next_due(self) -> dict | None:
        now = datetime.now(timezone.utc)
        cands = [e for e in ([self.demo] if self.demo else []) + self.events if e["due"] > now]
        if not cands:
            return None
        e = min(cands, key=lambda x: x["due"])
        mins = int((e["due"] - now).total_seconds() // 60)
        return {**e, "minutes": mins} if mins <= C["horizon_min"] else None

    # ---- gate (code first, Jev second)
    def _observe(self):
        h = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(h)
        snap = self.get_snapshot()
        speaking = (time.perf_counter() - self.ctrl.last_transcript_t) < 3.0
        since = time.time() - self.last_nudge_t if self.last_nudge_t else None
        return {"app": (snap.app if snap else ""), "title": title[:80], "user_speaking": speaking,
                "last_nudge_s_ago": (int(since) if since is not None else None), "next_due": self.next_due()}

    def _in_cooldown(self, obs) -> bool:
        s = obs["last_nudge_s_ago"]
        return s is not None and s < (C["dismiss_cooldown_s"] if self.dismissed else C["cooldown_s"])

    def _decision_needed(self, obs) -> bool:
        return (obs["next_due"] is not None and not obs["user_speaking"] and not self._in_cooldown(obs) and not _phone_busy()
                and self.ctrl.pending is None and self._budget_ok())

    def _key(self, obs):
        nd = obs["next_due"]
        return (nd and nd["name"], nd and min(nd["minutes"] // 10, 30), obs["app"],
                hashlib.md5(obs["title"].encode()).hexdigest()[:6], self.dismissed)

    def _budget_ok(self) -> bool:
        now = time.time()
        self.calls = [t for t in self.calls if now - t < 3600]
        return len(self.calls) < C["call_budget_per_hour"]

    def _ask(self, obs) -> float | None:
        nd = obs["next_due"]
        state = {"mission": "Help the user hands-free. Interrupting is costly: only speak when it clearly helps.",
                 "next_due": {"course": nd["course"], "name": nd["name"], "minutes": nd["minutes"]},
                 "observed": {"app": obs["app"], "title": obs["title"], "user_speaking": obs["user_speaking"]},
                 "assistant": {"can_speak": True, "last_nudge_s_ago": obs["last_nudge_s_ago"], "user_dismissed_last_nudge": self.dismissed},
                 "derived": {"nudge_window_open": nd["minutes"] <= 120 and not obs["user_speaking"]}}
        self.calls.append(time.time())
        ans = jev.client.ask(state, {"interrupt": {"type": "noul", "instructions": Q_INTERRUPT}})
        if not ans or "interrupt" not in ans:
            return None
        p = float(ans["interrupt"].get("noul", 0))
        bus.log("ambient", kind="judgment", p=round(p, 3), minutes=nd["minutes"], app=obs["app"], speaking=obs["user_speaking"])
        return p

    # ---- output
    def _nudge(self, obs):
        nd = obs["next_due"]
        text = f"{nd['name']} ({nd['course']}) is due in {nd['minutes']} min - open it? say yes"
        self.nudge_action = Action("navigate_url", {"url": nd["link"]}, said="ambient nudge")
        self.ctrl.set_pending(self.nudge_action)
        self.last_nudge_t = time.time()
        self.dismissed = False
        self.hits = 0
        from . import feedback
        feedback.pop()
        feedback.say(f"{nd['name']} is due in {nd['minutes']} minutes. Want it open?")
        try:
            from . import overlay
            overlay.pill(text, "asking")
        except Exception:
            pass
        bus.log("ambient", kind="nudge", item=nd["name"], minutes=nd["minutes"])
        print(f"  [NUDGE] {text}")

    def _track_answer(self):
        """After a nudge: pending cleared by an act of our action -> accepted; cleared otherwise -> dismissed."""
        if self.nudge_action is None or self.ctrl.pending is self.nudge_action:
            return
        last = self.ctrl.recent[-1] if self.ctrl.recent else None
        accepted = bool(last and last.get("action") == "navigate_url" and last.get("target") == self.nudge_action.args.get("url")
                        and time.time() - last["t"] < 60)
        self.dismissed = not accepted
        if self.demo and accepted:
            self.demo = None                                   # the demo item is done once opened
        bus.log("ambient", kind="outcome", outcome=("accepted" if accepted else "dismissed"))
        self.nudge_action = None

    # ---- loop
    def run(self):
        bus.log("events", kind="ambient_started", demo=bool(self.demo), ical=bool(config.D2L_ICAL_URL))
        while not bus.stop.is_set():
            try:
                if self.arm_at is not None and time.perf_counter() >= self.arm_at:
                    self.arm()
                self._poll_ical()
                self._track_answer()
                obs = self._observe()
                if self._decision_needed(obs):
                    key = self._key(obs)
                    # re-ask on a new scene, to confirm a first hit, or on a slow heartbeat (the Noul hovers near
                    # the threshold; an unchanged scene must not freeze the nudge forever)
                    if key != self.last_key or self.hits > 0 or time.time() - self.last_ask_t >= C["heartbeat_s"]:
                        self.last_key = key
                        self.last_ask_t = time.time()
                        t0 = time.perf_counter()
                        p = self._ask(obs)
                        fresh = (time.perf_counter() - t0) < C["stale_after_s"]
                        if p is not None and fresh and p >= C["interrupt_noul"]:
                            self.hits += 1
                            if self.hits >= C["hits_needed"] and self._decision_needed(self._observe()):
                                self._nudge(obs)
                        else:
                            self.hits = 0
                else:
                    self.hits = 0
            except Exception as e:
                bus.log("events", kind="ambient_error", err=repr(e)[:200])
            time.sleep(C["poll_s"])


def start(controller, get_snapshot, demo_due_in: float | None = None):
    from . import watchdog
    global _instance
    amb = _instance = Ambient(controller, get_snapshot, demo_due_in)
    watchdog.spawn("ambient", amb.run)
    return amb
