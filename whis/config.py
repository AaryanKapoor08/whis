"""All tunables. Thresholds ported from jev-voice-browser constants.js (T) and jev-voice config.py."""
import os, sys, glob

# CUDA DLLs for CTranslate2 (faster-whisper): must be on PATH before the STT child process spawns.
_sp = os.path.join(sys.prefix, "Lib", "site-packages")
for d in [os.path.join(_sp, "nvidia", "cublas", "bin"), os.path.join(_sp, "nvidia", "cudnn", "bin"), os.path.join(_sp, "torch", "lib")]:
    if os.path.isdir(d) and d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

# --- Jev providers (first is primary; failover after JEV_FAILOVER_TIMEOUTS consecutive timeouts)
JEV_PROVIDERS = [
    {"name": "typesafe", "base_url": os.getenv("JEV_BASE_URL", "https://api.typesafe.ai"),
     "path": "/v1/systemone", "model": os.getenv("JEV_MODEL", "jev-1.13.0"),   # jev-latest/jev-preview both = 1.13.0 (GET /v1/models, 2026-09-25)
     "key": os.getenv("TYPESAFE_API_KEY", "")},
]
JEV_TIMEOUT_S = 4.0
JEV_KEEPALIVE_S = 300.0         # httpx default 5 s dropped the socket between utterances: +190 ms TLS handshake each time
JEV_FAILOVER_TIMEOUTS = 2
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Policy thresholds (verbatim names from constants.js T)
T = {
    "intentConfidence": 0.55,
    "complete": 0.6,
    "isCommand": 0.5,
    "addressed": 0.85,             # unnamed speech must be clearly aimed at the computer
    "destructive": 0.5,
    "destructiveIntentConfidence": 0.9,
    "targetConfidence": 0.45,
    "targetTopProb": 0.35,
    "spanConfidence": 0.35,
    "correction": 0.6,
    "appConfidence": 0.4,
    "keyConfidence": 0.45,
    "siteConfidence": 0.45,
    "siteRescue": 0.6,             # navigate_url accepted below intentConfidence when the site choice is this sure
    "intentRescue": 0.35,          # ...but never below this intent confidence
    "verbRescue": 0.4,             # final whose top intent is below intentConfidence but its own verb is spoken ("play ...")
    "escalateIntent": 0.4,       # planner only when Jev's top intent is at least this sure (vague speech -> ignore)
}

# --- Controller timing (ms)
DEBOUNCE_MS = 150
SILENCE_CLOSED_MS = 900     # closed-set intents may fire on silence even if complete < T
SILENCE_FREE_MS = 600       # free-text intents wait for final or this much silence
MAX_INFLIGHT = 2
FOLLOWUP_S = 15.0           # after a wake word or an act, unnamed commands are accepted this long ("pause" after play_song)
MEDIA_WORDS = {"pause": "media_play_pause", "play": "media_play_pause", "resume": "media_play_pause", "stop": "media_play_pause",
               "pause it": "media_play_pause", "pause this": "media_play_pause", "pause the music": "media_play_pause",
               "volume up": "volume_up", "louder": "volume_up", "turn it up": "volume_up",
               "volume down": "volume_down", "quieter": "volume_down", "turn it down": "volume_down"}   # local fast path, no Jev
WAKE_WORDS = ["whis", "wiss", "whiz", "wis", "this", "please", "peace", "piece"]   # how Whisper hears "whis" at sentence start (logs: 20+ "Please ...", "Peace.")

# --- Intents
CLOSED_INTENTS = {"open_terminal", "open_app", "focus_app", "close_window", "click_element", "press_key", "save",
                  "scroll_up", "scroll_down", "go_back", "media_play_pause", "volume_up", "volume_down",
                  "confirm", "cancel"}
FREE_TEXT_INTENTS = {"type_text", "search_web", "navigate_url", "search_in_app", "play_song", "ask_screen", "run_command"}
GATED_INTENTS = {"click_element", "press_key", "save", "close_window"}   # destructive question asked
ALWAYS_CONFIRM = {"save", "close_window"}
PROTECTED_APPS = {"cursor", "code", "windowsterminal", "python", "powershell", "cmd", "conhost"}   # never Alt+F4 these

# --- Keys / sites / apps
KEYS = {"enter": "{Enter}", "escape": "{Esc}", "tab": "{Tab}", "backspace": "{Back}",
        "ctrl_s": "{Ctrl}s", "ctrl_z": "{Ctrl}z", "ctrl_c": "{Ctrl}c", "ctrl_v": "{Ctrl}v",
        "ctrl_t": "{Ctrl}t", "ctrl_w": "{Ctrl}w", "alt_f4": "{Alt}{F4}", "win_d": "{Win}d", "ctrl_grave": "{Ctrl}`", "ctrl_l": "{Ctrl}l"}
BOOKMARKS = {
    "d2l": os.getenv("D2L_URL", "https://d2l.example.edu/d2l/home"),
    "gmail": "https://mail.google.com", "youtube": "https://www.youtube.com",
    "github": "https://github.com", "google": "https://www.google.com",
}
_VSCODE = next((p for p in [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "Code.exe"),
                            r"C:\Program Files\Microsoft VS Code\Code.exe"] if os.path.exists(p)), "code")   # exe beats the bin/code script
APPS = {   # spoken name -> launch target (os.startfile / start). "chrome"/"browser" handled by browser.py
    "notepad": "notepad.exe", "spotify": "spotify", "explorer": "explorer.exe", "files": "explorer.exe",
    "settings": "ms-settings:", "terminal": "wt.exe", "calculator": "calc.exe",
    "vs code": _VSCODE, "code": _VSCODE, "cursor": "cursor", "edge": "msedge.exe",
}
APP_PROCS = {   # spoken name -> process name (exe without .exe); used to find running windows reliably
    "notepad": "Notepad", "spotify": "Spotify", "explorer": "explorer", "files": "explorer", "terminal": "WindowsTerminal",
    "calculator": "CalculatorApp", "settings": "SystemSettings", "vs code": "Code", "code": "Code", "cursor": "Cursor", "edge": "msedge",
}
IN_APP_SEARCH_KEYS = {"spotify": "{Ctrl}l", "explorer": "{Ctrl}e", "code": "{Ctrl}p", "cursor": "{Ctrl}p", "notepad": "{Ctrl}f", "default": "{Ctrl}f"}
BROWSER_NAMES = {"chrome", "browser", "google chrome", "the browser", "brave", "brave browser"}
BROWSER_PROFILE = r"C:\whis-profile"
# The user's real Brave profile; scripts/sync_profile.py copies its logins/extensions into BROWSER_PROFILE.
REAL_BROWSER_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Users\Jaska\AppData\Local"),
                                    "BraveSoftware", "Brave-Browser", "User Data")
BROWSER_KEEP_EXTENSIONS = True     # drop Playwright's --disable-extensions so the synced extensions load
BROWSER_EXE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"   # None -> Playwright Chrome channel
BROWSER_TITLE_SUFFIX = "Brave"
DEMO_FILE = r"C:\whis-demo\whis-demo.txt"
DEMO_WORKSPACE = r"C:\whis-demo"   # whis only ever drives THIS VS Code window (the user's own VS Code/terminals are off-limits)

# --- Tree
TREE_POLL_MS = 250
TREE_MAX_DEPTH = 12
TREE_MAX_ELEMENTS = 60
STATE_MAX_ELEMENTS = 25
INTERACTIVE_TYPES = {"ButtonControl", "HyperlinkControl", "MenuItemControl", "ListItemControl", "TabItemControl",
                     "CheckBoxControl", "RadioButtonControl", "ComboBoxControl", "EditControl", "TreeItemControl",
                     "SplitButtonControl", "DocumentControl"}

# --- STT
# Final lane: large-v3-turbo int8_float16. Bench 2026-09-25 (scripts/stt_compare.py --bench, 121 clips clean+noisy+mic):
#   turbo int8 WER 0.2% after fix_vocab, terms 115/116, p50 283 ms (7 s: 328 ms), +1.2 GB | turbo fp16 same words, 312 ms, +2.1 GB
#   large-v3 int8 0.2%, 116/116 but p50 439 / p90 524 ms (7 s: 629 ms), +2.0 GB | distil-large-v3.5 0.7-0.9%, "clock code" misses.
# Realtime lane: small.en (WER 0.5%, p50 126 ms, +0.4 GB) vs base.en 1.3% / 56 ms vs distil-small.en 25% (unusable).
STT_PROMPT = ("Whis, open Spotify and search for Tame Impala. Whis, open the terminal and run Claude Code, then open VS Code. "
              "Whis, go to D2L in Brave. Whis, open Notepad. Pause, volume up, volume down, close the window. Hey whis.")   # no "thanks": it feeds the silence hallucination
STT = dict(model="large-v3-turbo", realtime_model_type="small.en", language="en", device="cuda", compute_type="int8_float16",   # int8 weights: ~half the VRAM/RAM, same words
           initial_prompt=STT_PROMPT, initial_prompt_realtime=STT_PROMPT, beam_size=5, beam_size_realtime=3,
           enable_realtime_transcription=True, realtime_processing_pause=0.08, init_realtime_after_seconds=0.1,
           post_speech_silence_duration=0.5, silero_sensitivity=0.4, webrtc_sensitivity=3,
           spinner=False, print_transcription_time=False)
# Deterministic post-STT corrections (whis/stt.py correct()): regex (case-insensitive, whole words) -> replacement.
STT_FIXES = {
    r"tim mimbala|tim impala|team impala|tame impaler|team and parlour|team and parlor|tame and parlour": "Tame Impala",
    r"cloud code|clod code|clodcode|claw code|clawed code|plot code|claude cold|cloudcode": "Claude Code",
    r"vscode|v\.?s\.? code|vs\. code|bs code": "VS Code",
    r"d to l|d two l|d-2-l|d 2 l|dtl": "D2L",
    r"note pad": "Notepad",
    r"spot if i|spotty fi|spotty fy|spatifa|spatifia|spatiffy": "Spotify",   # turbo on noisy audio
    r"^(?:swiss|weiss|wiss|wis|whiz|vis)": "whis",   # wake word spelled variously at sentence start
}
# Fuzzy vocabulary: windows of n-1..n+1 words scoring >= threshold become the term (score = mean of the plain
# difflib ratio and a phonetic-key ratio). Far-off misrecognitions ("plot code", "team and parlour") live in STT_FIXES.
STT_VOCAB = ["Tame Impala", "Claude Code", "VS Code", "D2L", "Notepad", "Spotify", "Brave"]
STT_FUZZY_RATIO = 0.80        # multi-word terms ("close code" scores 0.78 and must stay)
STT_FUZZY_RATIO_SHORT = 0.85  # single-word terms ("the brave" scores 0.79)

# --- Feedback (one-way: user speaks, whis listens; no TTS)
SPEAK = False          # TTS phrases off
TONE = True            # short ack beep on act
PHRASES = {
    "ack": None,   # tone
    "which": "Which one?",
    "confirm_save": "That will overwrite the file. Say yes to confirm.",
    "confirm_generic": "That's hard to undo. Say yes to confirm.",
    "cancelled": "Cancelled.",
    "fail": "Couldn't do that.",
    "notcmd": "I didn't catch a command.",
    "ready": "Whis online.",
}
LOG_DIR = "logs"

# --- Phone (P2): Retell custom functions -> POST /retell (through ngrok). Keys in .env.
RETELL_API_KEY = os.getenv("RETELL_API_KEY", "")
SERVER_HOST, SERVER_PORT = "0.0.0.0", 8000
PHONE_WAIT_S = 8.0          # max time a phone command waits for the laptop before answering

# --- Ambient (P3): proactive nudges. High-confidence only, rate-limited, never on the voice fast path.
D2L_ICAL_URL = os.getenv("D2L_ICAL_URL", "")
AMBIENT = dict(poll_s=2.0, ical_poll_s=60.0, interrupt_noul=0.75, hits_needed=2, cooldown_s=120.0,
               dismiss_cooldown_s=300.0, horizon_min=180, stale_after_s=5.0, heartbeat_s=12.0, call_budget_per_hour=400)

# --- Cloud STT (Deepgram streaming, keyterm prompting). Used by `--stt deepgram`, or automatically when the key is set;
# falls back to local Whisper if the key is missing or the socket does not open within DG_CONNECT_TIMEOUT_S.
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
DG_MODEL = "flux-general-en"  # Deepgram's recommended real-time/agent model (Sep 2026): native end-of-turn + keyterms. "nova-3" = v1 path
DG_EOT_THRESHOLD = 0.7        # Flux: end-of-turn confidence (0.5-1.0, default 0.7); higher = fewer cut-offs, more latency
DG_EOT_TIMEOUT_MS = 1500      # Flux: force EndOfTurn after this much silence (default 5000; commands are short)
DG_ENDPOINTING_MS = 300       # nova-3 only: silence that ends an utterance (final); partials stream continuously
DG_CONNECT_TIMEOUT_S = 5.0
STT_KEYTERMS = ["whis", "Tame Impala", "Claude Code", "VS Code", "D2L", "Brave", "Spotify", "Notepad", "Cursor",
                "terminal", "Fredericton", "Hack Atlantic", "Daft Punk", "The Weeknd"]
