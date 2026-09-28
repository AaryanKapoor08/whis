"""One-command Retell setup for whis phone mode (P2-P5). Idempotent: re-run it whenever the tunnel URL changes.

  .venv\\Scripts\\python.exe scripts\\retell_setup.py --dry-run            # print payloads, no key needed
  .venv\\Scripts\\python.exe scripts\\retell_setup.py                      # create/update LLM + agent (URL from ngrok)
  .venv\\Scripts\\python.exe scripts\\retell_setup.py --url https://x.trycloudflare.com
  .venv\\Scripts\\python.exe scripts\\retell_setup.py --buy-number         # also buy a +1 506 number ($2/mo, needs KYC + card)

What it builds (Retell docs, Sep 2026):
  * Retell LLM (single prompt) with two custom tools -> POST <url>/retell (name/args/call body, signed) + end_call.
    Model gpt-4.1 on the fast tier (model_high_priority): non-reasoning, lowest time-to-first-token, reliable tool calls.
  * Voice agent: Retell platform voice (tuned for telecom audio, automatic TTS fallback), stt_mode=fast,
    responsiveness 1, interruption_sensitivity 0.8 (demo-hall noise), no backchannel (commands are short),
    boosted keywords for app names, call events -> <url>/retell/events (island pill on call start/end).
  * Phone number: reuses one nicknamed "whis" (or any number bound to the agent); otherwise buys CA/506 when asked.
State (ids) is cached in .retell.json at the repo root; lookups by agent name make it safe to delete that file.
Env: RETELL_API_KEY (required unless --dry-run), optional RETELL_MODEL, RETELL_VOICE_ID, RETELL_AGENT_NAME,
RETELL_AREA_CODE (506), RETELL_FAST_TIER (1), PHONE_PUBLIC_URL (skip ngrok detection)."""
import argparse, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass
import httpx

API = "https://api.retellai.com"
STATE = os.path.join(ROOT, ".retell.json")
PORT = 8000
PHONE_WAIT_S = 8.0
try:                                          # keep the tool timeout above the server's own wait
    from whis import config as _cfg           # noqa: E402  (imports only constants + dotenv)
    PORT, PHONE_WAIT_S = _cfg.SERVER_PORT, _cfg.PHONE_WAIT_S
except Exception:
    pass

AGENT_NAME = os.getenv("RETELL_AGENT_NAME", "whis")
MODEL = os.getenv("RETELL_MODEL", "gpt-4.1")
FAST_TIER = os.getenv("RETELL_FAST_TIER", "1") not in ("0", "false", "")
DEFAULT_VOICE = "retell-Cimo"                 # platform persona used in Retell's own examples; auto-picked if missing

PROMPT = """## Who you are
You are whis, a voice assistant living on the caller's Windows laptop. The caller is on the phone, away from the keyboard. You are their hands on that laptop.

## How you speak
- Like a quick, friendly person on the phone. One short sentence per turn, about ten words. No lists, no markdown, no emojis.
- Never describe how you work unless asked.

## Tools
- For ANY request to do something on the laptop (open, play, type, search, click, scroll, go to a site, run, close, save, submit, check D2L), call do_on_laptop with the caller's words as `command`. Keep their wording; only drop filler like "um" or "can you". Keep a multi-step request together in ONE call, e.g. "open spotify and play tame impala".
- If the caller asks what is on the screen or what is open, call whats_on_screen.
- After a tool returns, relay its result in a few words. "Done." means it worked: say something like "Done." or "Okay, it's open." Never invent details the tool did not report.

## Confirmations and choices
- The laptop asks before anything hard to undo (submit, close, delete, overwrite, send). When a result asks the caller to confirm, ask the caller that question, briefly, and wait.
- Never say yes for the caller. Only after the caller clearly says yes, call do_on_laptop with command "yes". If they say no or change their mind, call do_on_laptop with command "no".
- If the caller themselves asks for something destructive and vague (like "delete everything"), ask what exactly before calling the tool.
- If a result lists numbered options ("Which one? 1 ..., 2 ..."), read the first two or three and pass the caller's pick as the command, e.g. "two".
- If a result says it is still working, say "Still on it." and let the caller ask again.

## Other
- Small talk: answer in a few words, then ask what to do next.
- When the caller says bye or that's all, say a short goodbye and call end_call."""

BEGIN_MESSAGE = "Hey, whis here on your laptop. What should I do?"
BOOSTED = ["whis", "D2L", "Brightspace", "Spotify", "VS Code", "Claude Code", "Notepad", "Brave", "terminal", "Tame Impala"]


def tools(url: str) -> list[dict]:
    hook = url.rstrip("/") + "/retell"
    return [
        {"type": "custom", "name": "do_on_laptop", "url": hook, "method": "POST",
         "description": "Do something on the caller's laptop: open or focus apps, play music, type, click, scroll, "
                        "search, open websites like D2L, run terminal commands, and answer the laptop's yes/no or "
                        "numbered questions. Returns a short spoken result, a confirmation question, or a list of choices.",
         "parameters": {"type": "object", "required": ["command"], "properties": {
             "command": {"type": "string", "description": "The caller's request in their own words, e.g. 'open spotify and "
                                                          "play tame impala', or their answer: 'yes', 'no', 'two'."}}},
         "speak_during_execution": True, "execution_message_type": "static_text",
         "execution_message_description": "On it.",
         "speak_after_execution": True, "timeout_ms": int((PHONE_WAIT_S + 4) * 1000), "max_retry": 0},
        {"type": "custom", "name": "whats_on_screen", "url": hook, "method": "POST",
         "description": "Describe what is on the laptop screen right now: the app, window title and a few visible items.",
         "parameters": {"type": "object", "properties": {}},
         "speak_during_execution": False, "speak_after_execution": True, "timeout_ms": 5000, "max_retry": 0},
        {"type": "end_call", "name": "end_call", "description": "Hang up when the caller says bye or is done."},
    ]


def llm_payload(url: str) -> dict:
    return {"model": MODEL, "model_temperature": 0.2, "model_high_priority": FAST_TIER,
            "start_speaker": "agent", "begin_message": BEGIN_MESSAGE, "general_prompt": PROMPT, "general_tools": tools(url)}


def agent_payload(url: str, llm_id: str, voice_id: str) -> dict:
    return {"agent_name": AGENT_NAME, "response_engine": {"type": "retell-llm", "llm_id": llm_id},
            "voice_id": voice_id, "voice_speed": 1.05, "language": "en-US", "timezone": "America/Halifax",
            "responsiveness": 1.0, "interruption_sensitivity": 0.8, "enable_backchannel": False,
            "stt_mode": "fast", "denoising_mode": os.getenv("RETELL_DENOISE", "noise-cancellation"),
            "boosted_keywords": BOOSTED, "reminder_trigger_ms": 12000, "reminder_max_count": 1,
            "end_call_after_silence_ms": 60000, "max_call_duration_ms": 15 * 60 * 1000,
            "webhook_url": url.rstrip("/") + "/retell/events", "webhook_events": ["call_started", "call_ended"]}


# ---------------------------------------------------------------- tunnel
def detect_tunnel() -> str | None:
    """ngrok's local API (default :4040, falls through :4041/:4042 when several agents run)."""
    for port in (4040, 4041, 4042):
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/tunnels", timeout=1.5)
            for t in r.json().get("tunnels", []):
                pub, addr = t.get("public_url", ""), str(t.get("config", {}).get("addr", ""))
                if pub.startswith("https://") and addr.rstrip("/").endswith(str(PORT)):
                    return pub
        except Exception:
            continue
    return None


def tunnel_help():
    print(f"""No public URL. Start a tunnel to localhost:{PORT} in another terminal, then re-run:
  ngrok http {PORT}                      (winget install ngrok.ngrok; ngrok config add-authtoken <token> once)
  ngrok http --url=<your-free-static-domain>.ngrok-free.app {PORT}   (static domain: never re-run this script)
or, no account needed:
  cloudflared tunnel --url http://localhost:{PORT}    (winget install Cloudflare.cloudflared) then --url https://....trycloudflare.com""")


# ---------------------------------------------------------------- API
class Retell:
    def __init__(self, key: str):
        self.c = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {key}"}, timeout=30.0)

    def req(self, method: str, path: str, **kw):
        r = self.c.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else {}

    def find_agent(self, name: str) -> dict | None:
        body = {"filter_criteria": {"query": name, "channel": {"type": "string", "op": "eq", "value": "voice"}}}
        try:
            items = self.req("POST", "/v2/list-agents", json=body).get("items", [])
        except RuntimeError:
            items = self.req("POST", "/v2/list-agents", json={}).get("items", [])
        exact = [a for a in items if a.get("agent_name") == name]
        return exact[0] if exact else None

    def pick_voice(self) -> str:
        want = os.getenv("RETELL_VOICE_ID")
        voices = self.req("GET", "/list-voices")
        ids = {v["voice_id"] for v in voices}
        if want:
            if want not in ids:
                print(f"  ! RETELL_VOICE_ID={want} not in your voice list; using it anyway")
            return want
        if DEFAULT_VOICE in ids:
            return DEFAULT_VOICE
        plat = [v for v in voices if v.get("provider") == "platform"]
        plat.sort(key=lambda v: (v.get("accent") not in ("American", "Canadian"), v.get("voice_name", "")))
        if plat:
            print("  platform voices:", ", ".join(f"{v['voice_id']} ({v.get('gender')}, {v.get('accent')})" for v in plat[:8]))
            return plat[0]["voice_id"]
        return voices[0]["voice_id"]


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s: dict):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def bind_or_buy(rt: Retell, agent_id: str, st: dict, buy: bool, area: int, country: str) -> str | None:
    binding = [{"agent_id": agent_id, "agent_version": "latest", "weight": 1}]
    nums = rt.req("GET", "/v2/list-phone-numbers", params={"limit": 100}).get("items", [])
    mine = [n for n in nums if n.get("phone_number") == st.get("phone_number") or (n.get("nickname") or "").lower() == "whis"
            or any(a.get("agent_id") == agent_id for a in (n.get("inbound_agents") or []))]
    if mine:
        num = mine[0]["phone_number"]
        rt.req("PATCH", f"/update-phone-number/{num}", json={"inbound_agents": binding, "nickname": "whis"})
        print(f"  number {mine[0].get('phone_number_pretty', num)} bound to agent (inbound, latest version)")
        return num
    if not buy:
        print("  no 'whis' number yet. Re-run with --buy-number (CA/506, $2/mo; needs KYC + card), or buy one in the\n"
              "  dashboard (Phone Numbers > +, area code 506), nickname it 'whis', and re-run to bind it.")
        return None
    body = {"country_code": country, "number_provider": "twilio", "nickname": "whis", "inbound_agents": binding}
    if area:
        body["area_code"] = area
    try:
        out = rt.req("POST", "/create-phone-number", json=body)
    except RuntimeError as e:
        print(f"  ! purchase failed: {e}\n  (area-code search may be US-only in the API: try --area-code 0 for any CA number,\n"
              f"   or buy 506 from the dashboard, nickname it 'whis', and re-run to bind it. KYC must be complete.)")
        return None
    print(f"  bought {out.get('phone_number_pretty', out.get('phone_number'))}")
    return out.get("phone_number")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default=os.getenv("PHONE_PUBLIC_URL"), help="public https base URL (default: detect ngrok)")
    ap.add_argument("--dry-run", action="store_true", help="print the payloads, call nothing")
    ap.add_argument("--buy-number", action="store_true", help="buy a number if none is bound yet")
    ap.add_argument("--area-code", type=int, default=int(os.getenv("RETELL_AREA_CODE", "506")), help="0 = any")
    ap.add_argument("--country", default="CA", choices=["CA", "US"])
    ap.add_argument("--publish", action="store_true", help="also publish the agent draft (phone binds 'latest' anyway)")
    a = ap.parse_args()

    url = (a.url or detect_tunnel() or "").rstrip("/")
    if not url:
        if not a.dry_run:
            tunnel_help()
            sys.exit(2)
        url = "https://YOUR-TUNNEL.ngrok-free.app"
        print("(dry-run) no tunnel detected; using a placeholder URL\n")
    elif not url.startswith("https://"):
        print(f"! {url} is not https; Retell needs a public https URL")
    print(f"webhook: {url}/retell   events: {url}/retell/events")

    key = os.getenv("RETELL_API_KEY", "")
    if a.dry_run:
        print("\n== POST /create-retell-llm (or PATCH /update-retell-llm/{llm_id})")
        print(json.dumps(llm_payload(url), indent=2))
        print("\n== POST /create-agent (or PATCH /update-agent/{agent_id})")
        print(json.dumps(agent_payload(url, "<llm_id>", os.getenv("RETELL_VOICE_ID", DEFAULT_VOICE)), indent=2))
        print("\n== POST /create-phone-number (only with --buy-number, when no 'whis' number exists)")
        print(json.dumps({"country_code": a.country, "number_provider": "twilio", "nickname": "whis",
                          **({"area_code": a.area_code} if a.area_code else {}),
                          "inbound_agents": [{"agent_id": "<agent_id>", "agent_version": "latest", "weight": 1}]}, indent=2))
        if not key:
            print("\nRETELL_API_KEY is empty: put it in .env (dashboard > Settings > API Keys; use the key with the webhook badge).")
        return

    if not key:
        sys.exit("RETELL_API_KEY is empty. Put it in .env (the key with the webhook badge), or use --dry-run.")
    rt = Retell(key)
    st = load_state()

    agent = rt.find_agent(AGENT_NAME)
    if agent:
        full = rt.req("GET", f"/get-agent/{agent['agent_id']}")
        llm_id = (full.get("response_engine") or {}).get("llm_id") or st.get("llm_id")
    else:
        full, llm_id = None, st.get("llm_id")

    if llm_id:
        try:
            rt.req("PATCH", f"/update-retell-llm/{llm_id}", json=llm_payload(url))
            print(f"  LLM updated   {llm_id}  ({MODEL}{', fast tier' if FAST_TIER else ''})")
        except RuntimeError as e:
            print(f"  ! LLM update failed ({e}); creating a new one")
            llm_id = None
    if not llm_id:
        llm_id = rt.req("POST", "/create-retell-llm", json=llm_payload(url))["llm_id"]
        print(f"  LLM created   {llm_id}  ({MODEL}{', fast tier' if FAST_TIER else ''})")

    voice = (full or {}).get("voice_id") if (full and not os.getenv("RETELL_VOICE_ID")) else None
    voice = voice or rt.pick_voice()
    body = agent_payload(url, llm_id, voice)
    if full:
        out = rt.req("PATCH", f"/update-agent/{full['agent_id']}", json=body)
        print(f"  agent updated {out['agent_id']}  voice={voice}")
    else:
        out = rt.req("POST", "/create-agent", json=body)
        print(f"  agent created {out['agent_id']}  voice={voice}")
    agent_id = out["agent_id"]
    if a.publish and out.get("version") is not None:
        try:
            rt.req("POST", f"/publish-agent-version/{agent_id}", json={"version": out["version"]})
            print(f"  published v{out['version']}")
        except RuntimeError as e:
            print(f"  ! publish failed: {e}")

    st.update({"llm_id": llm_id, "agent_id": agent_id, "url": url, "voice_id": voice})
    num = bind_or_buy(rt, agent_id, st, a.buy_number, a.area_code, a.country)
    if num:
        st["phone_number"] = num
    save_state(st)
    print(f"\nready. Laptop: .venv\\Scripts\\python.exe -m whis --phone   then call {num or '<your number>'}.\n"
          f"Browser test (no phone): dashboard > Agents > {AGENT_NAME} > Test (web call).  Check: scripts\\phone_check.py --public")


if __name__ == "__main__":
    main()
