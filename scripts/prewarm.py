"""I4 pre-demo warm-up. Run before every demo:  .venv\\Scripts\\python.exe scripts\\prewarm.py
- creates C:\\whis-demo\\whis-demo.txt and opens it in Notepad (minimized) so "save it" is a plain Ctrl+S
- launches Spotify (minimized) so "open spotify" is a 50 ms focus, not a 1 s launch
- syncs the real Brave logins into the whis profile (scripts/sync_profile.py) unless --no-sync
- downloads/loads both Whisper models into the cache, warms Jev (prints p50 latency), pre-renders TTS phrases
- prints a checklist of keys / mic / GPU so nothing is discovered on stage
"""
import os, sys, time, subprocess, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from whis import config

OK, BAD = "[ok]  ", "[!!]  "


def demo_file():
    os.makedirs(os.path.dirname(config.DEMO_FILE), exist_ok=True)
    if not os.path.exists(config.DEMO_FILE):
        with open(config.DEMO_FILE, "w", encoding="utf-8") as f:
            f.write("whis demo\n")
    print(OK, "demo file", config.DEMO_FILE)


def open_minimized(app: str, target: str):
    from whis import apps
    import win32gui, win32con
    is_file = target == config.DEMO_FILE     # always open the demo file: an already-open Notepad may hold another file
    h = None if is_file else apps.find_window(app)
    if not h:
        try:
            if is_file:
                subprocess.Popen(["notepad.exe", target])   # not os.startfile: .txt may be associated with VS Code
            else:
                os.startfile(target)
        except Exception:
            subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=0x08000000)
        for _ in range(60):
            time.sleep(0.15)
            h = apps.find_window(app)
            if h:
                break
    if h:
        time.sleep(0.5)
        win32gui.ShowWindow(h, win32con.SW_MINIMIZE)
        print(OK, f"{app} running (minimized)")
    else:
        print(BAD, f"{app}: no window found")


def sync_profile():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "sync_profile.py")], capture_output=True, text=True)
    print(OK if r.returncode == 0 else BAD, "brave profile sync", (r.stdout.strip().splitlines() or ["?"])[-1])


def stt_models():
    try:
        from faster_whisper import WhisperModel
        for m in (config.STT["realtime_model_type"], config.STT["model"]):
            t0 = time.perf_counter()
            WhisperModel(m, device=config.STT.get("device", "cuda"), compute_type=config.STT.get("compute_type", "float16"))
            print(OK, f"whisper {m} loaded on {config.STT.get('device')} ({time.perf_counter() - t0:.1f}s)")
    except Exception as e:
        print(BAD, "whisper:", e)


def jev_warm():
    from whis import jev
    lat = []
    for _ in range(5):
        jev.client.warm()
        if jev.client.last_latency_ms:
            lat.append(jev.client.last_latency_ms)
    if lat:
        print(OK, f"jev {jev.client.provider['name']} p50={statistics.median(lat):.0f}ms max={max(lat):.0f}ms")
    else:
        print(BAD, "jev: no answer (key? network?)")


def tts():
    if not config.SPEAK:
        print(OK, "TTS off (one-way mode)"); return
    from whis import feedback
    feedback.warm(); print(OK, "TTS phrases cached")


def checklist():
    print(OK if config.JEV_PROVIDERS[0]["key"] else BAD, "TYPESAFE_API_KEY")
    print(OK if config.ANTHROPIC_API_KEY else BAD, "ANTHROPIC_API_KEY (planner / ask_screen)")
    print(OK if config.RETELL_API_KEY else "[--]  ", "RETELL_API_KEY (phone phase only)")
    print(OK if os.path.exists(config.BROWSER_EXE) else BAD, "browser exe", config.BROWSER_EXE)
    try:
        import sounddevice as sd
        d = sd.query_devices(kind="input")
        print(OK, "mic:", d["name"])
    except Exception as e:
        print(BAD, "mic:", e)
    try:
        import torch
        print(OK if torch.cuda.is_available() else BAD, "cuda:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "not available")
    except Exception as e:
        print(BAD, "torch:", e)


if __name__ == "__main__":
    no_sync = "--no-sync" in sys.argv
    checklist()
    demo_file()
    open_minimized("notepad", config.DEMO_FILE)
    open_minimized("spotify", "spotify:")
    if not no_sync:
        sync_profile()
    jev_warm()
    stt_models()
    tts()
    print("prewarm done. Now:  .venv\\Scripts\\python.exe -m whis")
