"""D2L / Brightspace tools for the Claude brain. Every function runs ON THE PLAYWRIGHT THREAD via
browser.call("run", fn, ...), so the Valence REST API calls use the logged-in session of whis's Brave profile
(context.request shares the browser's cookies; when the page is already on D2L, batches go through one parallel
fetch() in the page). READ-ONLY: only GET requests and page navigation, never a POST/PUT/DELETE or a submit.

courses(page)                         -> current-term courses [{name, code, orgUnitId, term}]
open_course(page, query)              -> navigate to /d2l/home/{ou} ("CS3873", "3873", "calculus", "random")
assignments(page, course, days, ...)  -> undone assignments (dropbox folders + my submissions) with due dates
open_assignment(page, name, course)   -> navigate to the assignment's page (nothing is submitted)
"""
import json, random, re, time, difflib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from . import config, bus

BASE = "{0.scheme}://{0.netloc}".format(urlparse(config.BOOKMARKS["d2l"]))
_HOST = urlparse(BASE).netloc
_TTL = 300.0
_c: dict = {}                    # cache: ver, courses (+t), folders[ou] (+t)


class D2LError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------- HTTP
def _get(page, path):
    """GET one API path -> (status, json or None)."""
    r = page.context.request.get(BASE + path, max_redirects=0, timeout=10000, fail_on_status_code=False)
    try:
        return r.status, (r.json() if r.status == 200 else None)
    except Exception:
        return r.status, None


def _get_many(page, paths):
    """GET several paths -> [(status, json|None)]. One parallel fetch() when the page is on D2L, else sequential."""
    if not paths:
        return []
    if urlparse(page.url).netloc == _HOST:
        try:
            raw = page.evaluate("""async (ps) => Promise.all(ps.map(async p => { try {
                const r = await fetch(p, {credentials: 'include', headers: {Accept: 'application/json'}});
                return [r.status, r.status === 200 ? await r.text() : null]; } catch (e) { return [0, null]; } }))""", paths)
            return [(s, json.loads(t) if t else None) for s, t in raw]
        except Exception as e:                       # navigation mid-call etc.: fall back to sequential
            bus.log("events", kind="d2l_fetch_many_error", err=repr(e)[:200])
    return [_get(page, p) for p in paths]


def _login(page):
    """Session cookie missing/expired: load D2L home in the browser (SSO bounces back when the IdP session is alive)."""
    from . import browser
    browser._do("goto", BASE + "/d2l/home")
    return urlparse(page.url).netloc == _HOST and "/login" not in page.url.lower()


def _versions(page):
    if "ver" not in _c:
        lp, le = "1.50", "1.80"
        s, d = _get(page, "/d2l/api/versions/")
        if s == 200 and isinstance(d, list):
            for v in d:
                if v.get("ProductCode") == "lp":
                    lp = v.get("LatestVersion") or lp
                if v.get("ProductCode") == "le":
                    le = v.get("LatestVersion") or le
        _c["ver"] = (lp, le)
    return _c["ver"]


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------------------------------------- courses
_TERM = re.compile(r"_(\d{4})(FA|WI|SM|SP|SU)_(?:UG|GR|[A-Z]{2})_([A-Z]{2,5})_(\d{4})", re.I)
_TERM_NAME = {"FA": "Fall", "WI": "Winter", "SM": "Summer", "SP": "Spring", "SU": "Summer"}


def _course_row(item):
    ou, acc = item["OrgUnit"], item.get("Access") or {}
    code, raw = ou.get("Code") or "", ou.get("Name") or ""
    name = re.sub(r"[^\w\s&/:,.()'-]", "", raw).strip()             # drop emoji
    m = _TERM.search(code)
    short, term = "", ""
    if m:
        short, term = f"{m.group(3).upper()} {m.group(4)}", f"{_TERM_NAME.get(m.group(2).upper(), m.group(2))} {m.group(1)}"
        t = re.search(r":\s*(.+?)(?:\s+-\s+[A-Z][\w.' -]+)?$", name)    # "(2026 FA) MATH 2513 Online: Multivariable Calculus... - Prof"
        title = t.group(1).strip() if t else ""
        disp = f"{short} {title}".strip() if title else short
    else:
        disp = name or code
    return {"name": disp, "code": code, "orgUnitId": ou["Id"], "term": term, "full_name": raw,
            "_start": _dt(acc.get("StartDate")), "_end": _dt(acc.get("EndDate")),
            "_ok": bool(acc.get("IsActive", True)) and bool(acc.get("CanAccess", True)), "_last": _dt(acc.get("LastAccessed"))}


def _all_courses(page):
    if _c.get("courses") and time.time() - _c["courses_t"] < _TTL:
        return _c["courses"]
    lp, _ = _versions(page)
    items, bm, relogged = [], None, False
    for _ in range(10):
        s, d = _get(page, f"/d2l/api/lp/{lp}/enrollments/myenrollments/?orgUnitTypeId=3" + (f"&bookmark={bm}" if bm else ""))
        if s in (401, 403) and not relogged:
            relogged = True
            if _login(page):
                continue
        if s != 200 or not isinstance(d, dict):
            raise D2LError(f"D2L enrollments API returned HTTP {s}" + (" (not logged in to D2L?)" if s in (401, 403) else ""))
        items += d.get("Items") or []
        pi = d.get("PagingInfo") or {}
        if not pi.get("HasMoreItems"):
            break
        bm = pi.get("Bookmark")
    rows = [_course_row(i) for i in items if i.get("OrgUnit")]
    _c["courses"], _c["courses_t"] = rows, time.time()
    return rows


def _current(rows):
    """Current-term courses: accessible, running now (or starting within 3 weeks), started in the last ~5 months, with a term code."""
    now = _now()
    cur = [r for r in rows if r["_ok"] and r["term"] and r["_start"] and now - timedelta(days=150) <= r["_start"] <= now + timedelta(days=21)
           and (r["_end"] is None or r["_end"] > now)]
    return cur or [r for r in rows if r["_ok"] and r["term"] and (r["_end"] is None or r["_end"] > now)]


def _pub(r):
    return {k: v for k, v in r.items() if not k.startswith("_") and k != "full_name"}


def courses(page, all_terms=False):
    try:
        rows = _all_courses(page)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    sel = rows if all_terms else _current(rows)
    return {"ok": True, "courses": [_pub(r) for r in sel]}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _match_course(rows, query):
    """Best course for 'CS3873' / 'cs 3873' / '3873' / 'calculus' / 'networks' (current term preferred on ties)."""
    q = query.lower().strip()
    qn = _norm(q)
    cur = {r["orgUnitId"] for r in _current(rows)}
    best, bs = None, 0.0
    for r in rows:
        short = _norm(r["name"].split(" ")[0] + (r["name"].split(" ")[1] if " " in r["name"] else ""))   # "cs3873"
        hay = (r["name"] + " " + r["full_name"] + " " + r["code"]).lower()
        s = 0.0
        if qn and qn == short:
            s = 10
        elif re.fullmatch(r"\d{4}", qn) and qn in short:
            s = 9
        elif qn and len(qn) >= 3 and qn in _norm(hay):
            s = 7
        else:
            words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 2 and w not in ("the", "course", "class", "open")]
            hit = sum(1 for w in words if w in hay)
            if words and hit:
                s = 4 + 2 * hit / len(words)
            else:
                s = 3 * difflib.SequenceMatcher(None, qn, _norm(r["name"])).ratio()
                s = s if s > 2 else 0
        if s and r["orgUnitId"] in cur:
            s += 0.5
        if s > bs:
            best, bs = r, s
    return best


SECTIONS = {   # standard Brightspace tool pages of a course
    "home": "/d2l/home/{ou}", "assignments": "/d2l/lms/dropbox/user/folders_list.d2l?ou={ou}&isprv=0",
    "content": "/d2l/le/content/{ou}/Home", "grades": "/d2l/lms/grades/my_grades/main.d2l?ou={ou}",
    "quizzes": "/d2l/lms/quizzing/user/quizzes_list.d2l?ou={ou}", "discussions": "/d2l/le/{ou}/discussions/List",
    "announcements": "/d2l/lms/news/main.d2l?ou={ou}", "classlist": "/d2l/lms/classlist/classlist.d2l?ou={ou}",
    "calendar": "/d2l/le/calendar/{ou}",
}
_SECTION_ALIASES = {"dropbox": "assignments", "assignment": "assignments", "homework": "assignments", "grade": "grades",
                    "marks": "grades", "quiz": "quizzes", "tests": "quizzes", "news": "announcements", "modules": "content",
                    "lectures": "content", "course home": "home", "discussion": "discussions", "forum": "discussions"}


def _current_ou(page):
    m = re.search(r"/d2l/home/(\d+)|[?&]ou=(\d+)|/d2l/le/(?:content/|calendar/)?(\d+)", page.url)
    return int(next(g for g in m.groups() if g)) if m else None


def open_course(page, query, section="home"):
    from . import browser
    try:
        rows = _all_courses(page)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "hint": "navigate to d2l and click the course card in STATE"}
    sec = (section or "home").lower().strip()
    sec = _SECTION_ALIASES.get(sec, sec)
    if sec not in SECTIONS:
        return {"ok": False, "error": f"unknown section '{section}'; one of {', '.join(SECTIONS)}"}
    q = (query or "").strip()
    if re.fullmatch(r"(?:this|current|the current|same|this one)(?: course| class)?", q.lower()):
        ou = _current_ou(page)
        r = next((x for x in rows if x["orgUnitId"] == ou), None)
        if r is None:
            return {"ok": False, "error": "the browser is not inside a D2L course; say which course"}
    elif not q or re.fullmatch(r"(?:a |any |some )?(?:random|any)(?: course| class| one)?", q.lower()):
        cur = _current(rows)
        if not cur:
            return {"ok": False, "error": "no current-term courses found"}
        r = random.choice(cur)
    else:
        r = _match_course(rows, q)
        if r is None:
            return {"ok": False, "error": f"no course matches '{q}'", "courses": [_pub(x)["name"] for x in _current(rows)]}
    browser._do("goto", BASE + SECTIONS[sec].format(ou=r["orgUnitId"]))
    ok = str(r["orgUnitId"]) in page.url and "/error" not in page.url and urlparse(page.url).netloc == _HOST
    return {"ok": ok, "course": r["name"], "section": sec, "url": page.url, "title": page.title()}


# ---------------------------------------------------------------------------------------------------------- assignments
def _folders(page, ous):
    """{ou: [folder dicts]} (cached 5 min)."""
    _, le = _versions(page)
    now = time.time()
    need = [ou for ou in ous if not (ou in _c.get("folders", {}) and now - _c["folders"][ou][0] < _TTL)]
    res = _get_many(page, [f"/d2l/api/le/{le}/{ou}/dropbox/folders/" for ou in need])
    fc = _c.setdefault("folders", {})
    errs = []
    for ou, (s, d) in zip(need, res):
        if s == 200 and isinstance(d, list):
            fc[ou] = (now, d)
        else:
            errs.append(s)
    return {ou: fc[ou][1] for ou in ous if ou in fc}, errs


def _spoken_due(d):
    if d is None:
        return "no due date"
    loc, now = d.astimezone(), datetime.now().astimezone()
    days = (loc.date() - now.date()).days
    tm = loc.strftime("%I:%M %p").lstrip("0")
    if days == 0:
        return f"today at {tm}"
    if days == 1:
        return f"tomorrow at {tm}"
    if days == -1:
        return f"yesterday at {tm}"
    if 1 < days < 7:
        return f"{loc.strftime('%A')} at {tm}"
    return f"{loc.strftime('%a %b')} {loc.day} at {tm}"


def assignments(page, course=None, days=None, include_submitted=False):
    """Undone assignments across current-term courses (or one course), soonest first."""
    try:
        rows = _all_courses(page)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "hint": "fall back to the UI: navigate d2l, open the course, click Assessments > Assignments"}
    if course:
        r = _match_course(rows, str(course))
        if r is None:
            return {"ok": False, "error": f"no course matches '{course}'"}
        sel = [r]
    else:
        sel = _current(rows)
    by_ou = {r["orgUnitId"]: r for r in sel}
    folders, errs = _folders(page, list(by_ou))
    now = _now()
    horizon = None
    if days is not None:                                  # through the END of that local day ("due tomorrow" = by tomorrow night)
        horizon = (datetime.now().astimezone() + timedelta(days=float(days))).replace(hour=23, minute=59, second=59)
    cands = []
    for ou, fl in folders.items():
        for f in fl:
            if f.get("IsHidden"):
                continue
            due = _dt(f.get("DueDate"))
            end = _dt((f.get("Availability") or {}).get("EndDate"))
            start = _dt((f.get("Availability") or {}).get("StartDate"))
            if start and start > now + timedelta(days=30):
                continue
            closed = end is not None and end < now
            if closed or (due and due < now - timedelta(days=7) and end is None):
                continue                                   # long past / no longer accepting
            if horizon and (due is None or due > horizon):
                continue
            cands.append((ou, f, due, end))
    _, le = _versions(page)
    # submission status only where it matters for the answer (whole-term question: overdue + next two weeks) -> 1 fetch each
    check_to = now + timedelta(days=14) if horizon is None else None
    chk = [c for c in cands if check_to is None or (c[2] is not None and c[2] <= check_to)]
    got = dict(zip([id(c) for c in chk], _get_many(page, [f"/d2l/api/le/{le}/{ou}/dropbox/folders/{f['Id']}/submissions/mysubmissions/"
                                                         for ou, f, _, _ in chk])))
    subs = [got.get(id(c), (0, None)) for c in cands]
    items = []
    for (ou, f, due, end), (s, d) in zip(cands, subs):
        submitted = None
        if s == 200 and isinstance(d, list):
            submitted = any(e.get("Submissions") for e in d if isinstance(e, dict))
        if submitted and not include_submitted:
            continue
        if due and due < now and not include_submitted and submitted is not False:
            continue                                        # past due + unknown status: don't nag
        items.append({"course": by_ou[ou]["name"], "name": f.get("Name", "?"), "due": _spoken_due(due),
                      "due_iso": due.astimezone().isoformat(timespec="minutes") if due else None,
                      "submitted": submitted, "overdue": bool(due and due < now),
                      "late_until": _spoken_due(end) if (due and due < now and end) else None,
                      "orgUnitId": ou, "folderId": f.get("Id"), "_d": due})
    items.sort(key=lambda x: (x["_d"] is None, x["_d"] or now))
    for x in items:
        x.pop("_d")
    todo = [x for x in items if not x["submitted"]]
    return {"ok": True, "now": datetime.now().astimezone().strftime("%A %b %d %I:%M %p"), "count_left": len(todo),
            "courses_checked": [by_ou[ou]["name"] for ou in folders], "api_errors": errs or None,
            "items": items[:25], "spoken": _summary(todo, days, course)}


def _summary(todo, days, course):
    """Spoken answer: the soonest few with due dates (whole term asked -> focus on the next two weeks)."""
    where = f" in {course}" if course else ""
    say = lambda xs: "; ".join(f"{x['course']} {x['name']}, " + (f"overdue, was due {x['due']}" if x["overdue"] else f"due {x['due']}")
                               for x in xs)
    if days is not None:
        d = float(days)
        when = " today" if d < 1 else " by tomorrow" if d <= 1.5 else " this week" if d <= 7 else f" in the next {int(d)} days"
        if not todo:
            return f"Nothing left to submit{where}{when}."
        n = len(todo)
        return (f"You have {n} assignment{'s' if n != 1 else ''} left{where}{when}: " + say(todo[:4])
                + (f"; and {n - 4} more" if n > 4 else "") + ".")
    soon_t = datetime.now().astimezone() + timedelta(days=14)
    soon = [x for x in todo if x["overdue"] or (x["due_iso"] and datetime.fromisoformat(x["due_iso"]) <= soon_t)]
    later = len(todo) - len(soon)
    if not todo:
        return f"No assignments left{where}. You're all caught up."
    if not soon:
        nxt = next((x for x in todo if x["due_iso"]), todo[0])
        return f"Nothing due in the next two weeks{where}. Next up: {say([nxt])}."
    n = len(soon)
    return (f"You have {n} due in the next two weeks{where}: " + say(soon[:4]) + (f"; and {n - 4} more" if n > 4 else "")
            + (f". Plus {later} later this term." if later else "."))


def open_assignment(page, name, course=None):
    from . import browser
    try:
        rows = _all_courses(page)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    sel = [_match_course(rows, str(course))] if course else _current(rows)
    sel = [r for r in sel if r]
    folders, _ = _folders(page, [r["orgUnitId"] for r in sel])
    by_ou = {r["orgUnitId"]: r for r in sel}
    q = str(name or "").lower().strip()
    qn = _norm(q)
    best, bs = None, 0.0
    now = _now()
    for ou, fl in folders.items():
        for f in fl:
            if f.get("IsHidden"):
                continue
            n = (f.get("Name") or "").lower()
            s = 10 if _norm(n) == qn else 8 if qn and qn in _norm(n) else 5 * difflib.SequenceMatcher(None, qn, _norm(n)).ratio()
            words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 1]
            s = max(s, 6 * sum(w in n for w in words) / max(1, len(words)))
            if course is None and any(w in by_ou[ou]["name"].lower() for w in q.split() if len(w) > 2):
                s += 1
            d = _dt(f.get("DueDate"))
            if d and d > now:
                s += 0.3                                     # upcoming beats past on ties
            if s > bs:
                best, bs = (ou, f), s
    if best is None or bs < 3:
        return {"ok": False, "error": f"no assignment matches '{name}'"}
    ou, f = best
    browser._do("goto", f"{BASE}/d2l/lms/dropbox/user/folder_submit_files.d2l?db={f['Id']}&grpid=0&isprv=0&bp=0&ou={ou}")
    ok = f"db={f['Id']}" in page.url and "/error" not in page.url
    return {"ok": ok, "course": by_ou[ou]["name"], "assignment": f.get("Name"), "due": _spoken_due(_dt(f.get("DueDate"))),
            "url": page.url, "note": "opened the assignment page only; nothing was submitted"}
