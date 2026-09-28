"""A1 check: the D2L (Brightspace) iCal parser on a synthetic feed, then (if D2L_ICAL_URL is set or --url given)
the real feed: prints the upcoming due items exactly as ambient mode will see them.

  .venv\\Scripts\\python.exe scripts\\ical_check.py              # synthetic test (+ real feed when D2L_ICAL_URL is set)
  .venv\\Scripts\\python.exe scripts\\ical_check.py --url "https://lms.unb.ca/d2l/le/calendar/feed/user/feed.ics?token=..."
"""
import argparse, os, sys
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from whis import config                       # noqa: E402
from whis.ambient import parse_ical, fetch_ical, LOCAL_TZ   # noqa: E402

# Brightspace-shaped feed: VTIMEZONE with a Windows TZID, UTC 'Z' times, IANA TZID, floating time, all-day date,
# folded long lines, escaped commas, "Available" start events that must be dropped, and a duplicate "Availability Ends".
SAMPLE = r"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Desire2Learn Inc.//D2L Calendar//EN
X-WR-CALNAME:UNB Calendar
BEGIN:VTIMEZONE
TZID:Atlantic Standard Time
BEGIN:STANDARD
DTSTART:16011104T020000
RRULE:FREQ=YEARLY;BYDAY=1SU;BYMONTH=11
TZOFFSETFROM:-0300
TZOFFSETTO:-0400
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010311T020000
RRULE:FREQ=YEARLY;BYDAY=2SU;BYMONTH=3
TZOFFSETFROM:-0400
TZOFFSETTO:-0300
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:6_1001@lms.unb.ca
DTSTAMP:20260920T120000Z
DTSTART:20260928T025900Z
DTEND:20260928T025900Z
SUMMARY:Assignment 3: Project Proposal - Due
LOCATION:CS3383 Algorithm Design and Analysis - FR01A (2026FA)
DESCRIPTION:Submit your proposal as a PDF\, one per group.\n\nView event - h
 ttps://lms.unb.ca/d2l/le/calendar/123456/event/1001/detailsview#1001
END:VEVENT
BEGIN:VEVENT
UID:6_1002@lms.unb.ca
DTSTAMP:20260920T120000Z
DTSTART:20260921T120000Z
DTEND:20260921T120000Z
SUMMARY:Assignment 3: Project Proposal - Available
LOCATION:CS3383 Algorithm Design and Analysis - FR01A (2026FA)
DESCRIPTION:View event - https://lms.unb.ca/d2l/le/calendar/123456/event/1002/detailsview#1002
END:VEVENT
BEGIN:VEVENT
UID:6_1003@lms.unb.ca
DTSTAMP:20260920T120000Z
DTSTART;TZID=Atlantic Standard Time:20260929T235900
DTEND;TZID=Atlantic Standard Time:20260929T235900
SUMMARY:Quiz 2 - Availability Ends
LOCATION:MATH 2213 Linear Algebra - FR01A (2026FA)
DESCRIPTION:View event - https://lms.unb.ca/d2l/le/calendar/654321/event/1003/detailsview#1003
END:VEVENT
BEGIN:VEVENT
UID:6_1004@lms.unb.ca
DTSTAMP:20260920T120000Z
DTSTART;TZID=America/Halifax:20260929T220000
SUMMARY:Quiz 2 - Due
LOCATION:MATH 2213 Linear Algebra - FR01A (2026FA)
END:VEVENT
BEGIN:VEVENT
UID:6_1005@lms.unb.ca
DTSTAMP:20260920T120000Z
DTSTART:20261002T170000
SUMMARY:Lab Report 1 - Due
LOCATION:PHYS 1061
END:VEVENT
BEGIN:VEVENT
UID:6_1006@lms.unb.ca
DTSTAMP:20260920T120000Z
DTSTART;VALUE=DATE:20261005
DTEND;VALUE=DATE:20261006
SUMMARY:Reading Response - Due
LOCATION:ENGL 1000 Intro to Literature
END:VEVENT
BEGIN:VEVENT
UID:6_1007@lms.unb.ca
DTSTAMP:20260920T120000Z
DTSTART:20260930T140000Z
DTEND:20260930T153000Z
SUMMARY:Lecture 12
LOCATION:CS3383 Algorithm Design and Analysis - FR01A (2026FA)
END:VEVENT
BEGIN:VEVENT
UID:6_1008@lms.unb.ca
DTSTAMP:20260920T120000Z
DTSTART:20261001T120000Z
SUMMARY:Midterm Quiz - Availability Starts
LOCATION:MATH 2213 Linear Algebra
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").encode()

fails = 0


def check(label, cond, got=""):
    global fails
    fails += 0 if cond else 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{'   got: ' + str(got) if not cond else ''}")


def local(e):
    return e["due"].astimezone(LOCAL_TZ).strftime("%a %b %d %H:%M %Z")


def synthetic():
    print("synthetic Brightspace feed")
    evs = parse_ical(SAMPLE, "https://lms.unb.ca/d2l/home")
    by = {e["name"]: e for e in evs}
    check("4 due items kept (Available/Starts/Lecture dropped, Quiz 2 deduped)", len(evs) == 4, [e["summary"] for e in evs])
    a3 = by.get("Assignment 3: Project Proposal", {})
    check("' - Due' stripped from name", bool(a3))
    check("UTC Z time -> 23:59 Halifax (ADT)", a3 and local(a3).startswith("Sun Sep 27 23:59"), a3 and local(a3))
    check("course shortened", a3.get("course") == "CS 3383", a3.get("course"))
    check("folded 'View event' link unfolded", a3.get("link") == "https://lms.unb.ca/d2l/le/calendar/123456/event/1001/detailsview#1001", a3.get("link"))
    q2 = by.get("Quiz 2", {})
    check("Due + Availability Ends -> earliest (22:00 IANA TZID)", q2 and local(q2).startswith("Tue Sep 29 22:00"), q2 and local(q2))
    lab = by.get("Lab Report 1", {})
    check("floating time -> D2L_TZ", lab and local(lab).startswith("Fri Oct 02 17:00"), lab and local(lab))
    check("no link -> default D2L link", lab.get("link") == "https://lms.unb.ca/d2l/home", lab.get("link"))
    rr = by.get("Reading Response", {})
    check("all-day date -> 23:59 local that day", rr and local(rr).startswith("Mon Oct 05 23:59"), rr and local(rr))
    check("sorted by due", [e["due"] for e in evs] == sorted(e["due"] for e in evs))
    # the Windows TZID path (no IANA name) on its own
    only_win = [e for e in parse_ical(SAMPLE.replace(b"Quiz 2 - Due", b"Quiz 2 - Opens")) if e["name"] == "Quiz 2"]
    check("Windows TZID 'Atlantic Standard Time' -> 23:59 ADT", only_win and local(only_win[0]).startswith("Tue Sep 29 23:59"),
          only_win and local(only_win[0]))


def real(url):
    print(f"\nreal feed: {url[:60]}...")
    try:
        evs = fetch_ical(url, config.BOOKMARKS["d2l"])
    except Exception as e:
        check("fetch + parse", False, repr(e)[:200])
        return
    now = datetime.now(timezone.utc)
    up = [e for e in evs if e["due"] > now]
    check("fetch + parse", True)
    print(f"  {len(evs)} due items, {len(up)} upcoming. Next ones (ambient horizon {config.AMBIENT['horizon_min']} min):")
    for e in up[:8]:
        mins = int((e["due"] - now).total_seconds() // 60)
        flag = "  <- would be nudged" if mins <= config.AMBIENT["horizon_min"] else ""
        print(f"    {local(e)}  ({mins} min)  {e['course']}: {e['name']}{flag}")
    if not up:
        print("    (nothing upcoming: use --demo-due-in for the pitch)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=config.D2L_ICAL_URL)
    a = ap.parse_args()
    synthetic()
    if a.url:
        real(a.url)
    print(f"\n{'ALL GOOD' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
