"""S3 smoke tests. Run: .venv\\Scripts\\python.exe scripts\\smoke.py"""
import os, sys, time, json
from dotenv import load_dotenv
load_dotenv()

def t(name, fn):
    t0 = time.perf_counter()
    try:
        out = fn()
        print(f"[ok]   {name}: {out}  ({(time.perf_counter()-t0)*1000:.0f} ms)")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")

def uia():
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        c = auto.GetForegroundControl()
        n = 0
        for ctrl, depth in auto.WalkControl(c, maxDepth=6):
            n += 1
            if n > 200: break
        return f"fg={c.Name[:40]!r} walked={n}"

def jev():
    import httpx
    key = os.environ["TYPESAFE_API_KEY"]
    body = {"state": "whis open notepad and", "model": "jev-1.13.0",
            "questions": {
                "is_command": {"type": "noul", "instructions": "The text is an instruction addressed to a voice assistant that controls this Windows computer, not chit-chat."},
                "complete": {"type": "noul", "instructions": "The user has finished saying the command; it can be executed now without waiting for more words."},
                "intent": {"type": "choice", "instructions": "What does the user want the computer to do",
                           "criteria": {"open_app": "launch or switch to an application", "type_text": "type words into the focused field", "none": "no clear action"}}}}
    c = httpx.Client(timeout=8)
    c.post(f"{os.environ.get('JEV_BASE_URL','https://api.typesafe.ai')}/v1/systemone", headers={"Authorization": f"Bearer {key}"}, json=body)  # warm
    t0 = time.perf_counter()
    r = c.post(f"{os.environ.get('JEV_BASE_URL','https://api.typesafe.ai')}/v1/systemone", headers={"Authorization": f"Bearer {key}"}, json=body)
    ms = (time.perf_counter()-t0)*1000
    r.raise_for_status()
    a = r.json()["answers"]
    return f"{ms:.0f}ms warm | is_command={a['is_command']['noul']:.2f} complete={a['complete']['noul']:.2f} intent={a['intent']['choice']}({a['intent']['confidence']:.2f})"

def tts():
    import pyttsx3
    e = pyttsx3.init(); e.setProperty("rate", 190)
    e.save_to_file("whis online", "logs/tts_test.wav"); e.runAndWait()
    return os.path.getsize("logs/tts_test.wav")

def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from whis import config    # same Brave as whis: Chrome on the Brave profile could re-key/upgrade it
        kw = {"executable_path": config.BROWSER_EXE} if config.BROWSER_EXE else {"channel": "chrome"}
        ctx = p.chromium.launch_persistent_context(config.BROWSER_PROFILE, headless=False, **kw,
                                                   args=["--force-renderer-accessibility"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.google.com", wait_until="domcontentloaded")
        title = page.title(); ctx.close()
        return title

def stt_model():
    from faster_whisper import WhisperModel
    m = WhisperModel("tiny.en", device="cuda", compute_type="float16")
    return "tiny.en loaded on cuda"

if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    for name, fn in [("uia", uia), ("jev", jev), ("tts", tts), ("browser", browser), ("stt_model", stt_model)]:
        t(name, fn)
