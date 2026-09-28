"""System-2 fallback: when Jev is unsure about a FINAL utterance, ask Claude for a plan (list of actions).
Router pattern from jev-codex-router / jev-voice escalate.py. Only runs on finals Jev already judged to be commands."""
import json, time
from . import config, bus
from .types import Action

# Measured 2026-09-25 (time to full response, real prompts): planner Sonnet 5 low p50 1.5 s / max 2.2 s vs Haiku 1.1 s
# (Sonnet returns [] on nonsense where Haiku emits junk acts; Opus 5.5 low 3.6 s = too slow). ask: Haiku 0.9 s vs
# Sonnet 5 1.4 s with no accuracy gain on page Q&A. System prompts are below the cache minimum; streaming doesn't cut full-response time.
PLANNER_MODEL = "claude-sonnet-5"
ASK_MODEL = "claude-haiku-4-5-20251001"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"      # on error/timeout of the primary
_MODEL_KW = {"claude-sonnet-5": {"output_config": {"effort": "low"}}}   # adaptive thinking is on by default; low keeps it short
PLANNER_TIMEOUT_S, ASK_TIMEOUT_S = 3.0, 3.0
_client = None

SYSTEM = """You turn one spoken instruction into a short list of actions for a Windows voice assistant. Reply with JSON only:
{"actions":[{"intent":"...","args":{...}}], "say":"<=6 words status"}
Intents and args:
open_app{app} focus_app{app} close_window{} click_element{target:"e07" from elements} type_text{text} press_key{key: enter|escape|tab|backspace|ctrl_s|ctrl_z|ctrl_c|ctrl_v|ctrl_t|ctrl_w|alt_f4|win_d}
save{} scroll_up{} scroll_down{} go_back{} navigate_url{url} search_web{text} search_in_app{text, app?} play_song{text} ask_screen{question} open_terminal{} run_command{text} media_play_pause{} volume_up{} volume_down{}
Keys also: ctrl_grave (toggle VS Code terminal), ctrl_l. 'open the terminal in it and run claude code' = open_terminal, run_command{text:'claude'}.
Rules: use the screen elements when the user refers to something visible; prefer navigate_url with a bookmark from `bookmarks` over search_web;
"it/there/that" refers to the current app or the last action; never invent text the user did not say; if the request is not something these actions can do, return {"actions":[],"say":"can't do that"}."""


def _cli():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def warm():
    """`import anthropic` + client build costs ~1.4 s; pay it off the voice path at startup."""
    import threading
    threading.Thread(target=_cli, daemon=True, name="claude-warm").start()


def _create(model: str, timeout: float, **kw) -> str:
    """One Claude call, no SDK retries (voice latency); falls back to Haiku once. Returns the text blocks joined."""
    for m in dict.fromkeys((model, FALLBACK_MODEL)):
        try:
            r = _cli().with_options(timeout=timeout, max_retries=0).messages.create(model=m, **_MODEL_KW.get(m, {}), **kw)
            return "".join(b.text for b in r.content if b.type == "text").strip()   # skip thinking blocks
        except Exception as e:
            if m == FALLBACK_MODEL:
                raise
            bus.log("events", kind="claude_fallback", model=m, err=repr(e)[:200])


def plan(state: dict) -> tuple[list[Action], str]:
    if not config.ANTHROPIC_API_KEY:
        return [], ""
    ctx = {k: state.get(k) for k in ("transcript", "app", "title", "elements", "apps_running", "context")}
    ctx["bookmarks"] = list(config.BOOKMARKS)
    ctx["apps_known"] = list(config.APPS) + ["chrome"]
    t0 = time.perf_counter()
    try:
        txt = _create(PLANNER_MODEL, PLANNER_TIMEOUT_S, max_tokens=1024, system=SYSTEM,   # headroom for low-effort thinking
                      messages=[{"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}])
        txt = txt[txt.find("{"): txt.rfind("}") + 1]
        data = json.loads(txt)
    except Exception as e:
        bus.log("events", kind="planner_error", err=repr(e)[:200])
        return [], ""
    acts = []
    for a in data.get("actions", [])[:5]:
        intent, args = a.get("intent"), dict(a.get("args") or {})
        if intent == "navigate_url" and args.get("url") in config.BOOKMARKS:
            args["url"] = config.BOOKMARKS[args["url"]]
        acts.append(Action(intent, args, said=state.get("transcript", "")))
    bus.log("planner", ms=round((time.perf_counter() - t0) * 1000), n=len(acts), transcript=state.get("transcript", "")[:80])
    return acts, data.get("say", "")


def answer(question: str, screen_text: str) -> str:
    """Short spoken-style answer about the screen. Shown in the island (one-way UI)."""
    if not config.ANTHROPIC_API_KEY:
        return ""
    t0 = time.perf_counter()
    try:
        out = _create(ASK_MODEL, ASK_TIMEOUT_S, max_tokens=80,
                      system="Answer the user's question about their screen in one short sentence (max 18 words). Be concrete: names, dates, counts. If unknown, say what you see.",
                      messages=[{"role": "user", "content": f"QUESTION: {question}" + chr(10)*2 + "SCREEN:" + chr(10) + screen_text[:7000]}])
    except Exception as e:
        bus.log("events", kind="planner_answer_error", err=repr(e)[:200]); return ""
    bus.log("planner", ms=round((time.perf_counter() - t0) * 1000), answer=out[:100])
    return out
