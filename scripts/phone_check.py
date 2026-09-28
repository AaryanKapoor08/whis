"""Pre-call check for the phone path: fires Retell-shaped, correctly signed requests at the running whis server
(locally and, with --public, through the tunnel exactly as Retell will) and times every reply.

  (terminal 1)  .venv\\Scripts\\python.exe -m whis --phone            # or add --fake-stt --dry-run --fake-snapshot
  (terminal 2)  .venv\\Scripts\\python.exe scripts\\phone_check.py [--public] [--command "open notepad"]

Default command is chit-chat ("hello there"), which whis ignores, so the check never touches the laptop.
Signs with RETELL_API_KEY from .env when set (so signature verification is exercised end to end)."""
import argparse, json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import httpx
from whis import config                      # noqa: E402  (constants + .env)
from whis.server import sign                  # noqa: E402

OK, BAD = "PASS", "FAIL"
fails = 0


def check(label: str, cond: bool, detail: str = ""):
    global fails
    fails += 0 if cond else 1
    print(f"  [{OK if cond else BAD}] {label}{'  ' + detail if detail else ''}")


def post(base: str, path: str, payload: dict, key: str, signed: bool = True, timeout: float = 20.0):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signed and key:
        headers["X-Retell-Signature"] = sign(body, key)
    t0 = time.perf_counter()
    r = httpx.post(base + path, content=body, headers=headers, timeout=timeout)
    return r, (time.perf_counter() - t0) * 1000


def tool(name: str, args: dict) -> dict:
    return {"name": name, "args": args,
            "call": {"call_id": "check_" + str(int(time.time())), "call_type": "phone_call", "call_status": "ongoing",
                     "from_number": "+15065550100", "to_number": "+15065550199", "transcript": "User: test\n"}}


def run(base: str, key: str, command: str, where: str):
    print(f"\n{where}: {base}")
    try:
        h = httpx.get(base + "/health", timeout=8).json()
    except Exception as e:
        check("health", False, repr(e)[:120])
        return
    check("health", h.get("ok") is True, f"verify={h.get('verify')} calls={h.get('calls')} jev={h.get('provider')}")
    budget = (config.PHONE_WAIT_S + 1) * 1000

    r, ms = post(base, "/retell", tool("whats_on_screen", {}), key)
    check("whats_on_screen", r.status_code == 200 and len(r.text) > 5, f"{ms:.0f} ms  -> {r.text[:90]!r}")

    r, ms = post(base, "/retell", tool("do_on_laptop", {"command": command}), key)
    check("do_on_laptop", r.status_code == 200 and ms < budget, f"{ms:.0f} ms (budget {budget:.0f})  -> {r.text[:90]!r}")

    r, ms = post(base, "/retell/whats_on_screen", {}, key)
    check("args-only route", r.status_code == 200, f"{ms:.0f} ms")

    r, ms = post(base, "/retell/events", {"event": "call_started", "call": {"call_id": "check", "from_number": "+15065550100"}}, key)
    check("call event webhook", r.status_code in (200, 204), f"{r.status_code}")
    active = httpx.get(base + "/health", timeout=8).json().get("call_active")
    r, ms = post(base, "/retell/events", {"event": "call_ended", "call": {"call_id": "check"}}, key)
    check("call_active on during call", active is True, f"call_ended -> {r.status_code}")

    if key and h.get("verify"):
        r, _ = post(base, "/retell", tool("whats_on_screen", {}), key, signed=False)
        check("unsigned request rejected", r.status_code == 401, f"{r.status_code}")
        body = json.dumps(tool("whats_on_screen", {})).encode()
        stale = sign(body, key, int(time.time() * 1000) - 10 * 60 * 1000)
        r = httpx.post(base + "/retell", content=body, headers={"X-Retell-Signature": stale}, timeout=8)
        check("stale signature rejected", r.status_code == 401, f"{r.status_code}")
    else:
        print("  [info] signature verification is off (no RETELL_API_KEY or --no-verify): skipped reject checks")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", default=f"http://127.0.0.1:{config.SERVER_PORT}")
    ap.add_argument("--public", action="store_true", help="also go through the tunnel (ngrok detected or --url)")
    ap.add_argument("--url", default=os.getenv("PHONE_PUBLIC_URL"))
    ap.add_argument("--command", default="hello there", help="do_on_laptop command (default is ignored chit-chat)")
    a = ap.parse_args()
    key = config.RETELL_API_KEY
    run(a.local, key, a.command, "local")
    if a.public:
        url = a.url
        if not url:
            from retell_setup import detect_tunnel  # noqa: E402
            url = detect_tunnel()
        if not url:
            check("tunnel detected", False, "start ngrok/cloudflared or pass --url")
        else:
            run(url.rstrip("/"), key, a.command, "public")
    print(f"\n{'ALL GOOD' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
