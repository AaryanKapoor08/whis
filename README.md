# whis

### Talk to your PC like it's a person. Or call it from your phone.

> "Clippy had the right idea, it just didn't know when to shut up."

**whis** turns a Windows 11 PC into something you can run with plain speech. There are no commands to memorize
and no rigid grammar. You say what you'd say to a friend sitting at your keyboard, and it does it: opens and switches
apps, clicks anything on screen, types, saves, plays music, browses, searches, and tells you what's on the page.

It starts deciding **while you're still talking**, so actions land about as fast as you finish the sentence. It
**asks before anything hard to undo**. It **ignores chatter that isn't meant for it**. And when you're away from
the desk, you can **call it on a real phone number**: the laptop does what you say and answers back on the call.

Built **solo in 36 hours** for **HackAtlantic 2026**.

---

## Why this matters

About **1 in 5 people** can't comfortably use a mouse and keyboard. Existing tools like Talon and Windows Voice
Access work, but they make you learn a fixed command language first. whis takes the sentence you'd actually say
and handles the rest.

## Highlights

- **⚡ Fast.** One decision call per transcript, with a **p50 of ~170 ms** and a p95 of ~410 ms, measured over
  **1,350 logged calls**. Simple commands fire the moment the sentence is complete.
- **🔗 Chained commands.** *"Open Spotify, play Tame Impala, then open Brave and go to D2L"* is split at the verbs
  and run in order, from one breath.
- **🛡️ Safe.** Saving, closing windows, and destructive clicks like *Submit* wait for a spoken "yes". A new
  command cancels a pending confirm, and stray yes/no answers are ignored. Editors, terminals and Python are
  never closed.
- **🤫 Knows when you're not talking to it.** Saying the wake word *"whis"* opens an 8-second follow-up window.
  Without it, speech only triggers an action when the model is at least 85% sure it was addressed.
- **📞 Phone-callable.** A real number, backed by a Retell voice agent and a signed webhook. Every outcome is
  read back as a sentence, including confirms ("Say yes to confirm") and numbered choices ("Which one? 1 Submit,
  2 Assignment 3…").
- **🔔 Ambient mode.** Stays quiet until it's confident a nudge actually helps: *"Assignment 3 is due in 110
  min, open it?"* It uses a two-check confidence gate, never interrupts while you're talking, backs off after a
  "no", and has an hourly budget.
- **👀 Sees the screen.** It reads live Windows UI Automation trees and a numbered browser DOM snapshot, so
  "click the second one" and "do I have an assignment left?" both work.
- **💪 Self-healing.** A watchdog restarts the UI tree, browser, feedback, controller and ambient threads if
  any of them crash.
- **✨ Polished feedback.** A click-through Dynamic-Island-style overlay with numbered badges and an
  acknowledgement tone.

## Status

| part | state |
|---|---|
| Voice control (native apps + browser), chaining, confirm gate, numbered picks, island overlay | built, live-tested with the mic |
| Phone (`--phone`, Retell → ngrok → `POST /retell`) | built, verified locally in fake mode; live call tested at the demo |
| Ambient nudges (`--ambient`, `--demo-due-in`) | built, verified in fake mode; real D2L iCal feed untested |

## How it works

```
mic ─► STT (faster-whisper on GPU via RealtimeSTT, or Deepgram Nova-3) ─► partial + final transcripts
                                             │
   UIA tree / Playwright DOM snapshot ───────┤  one Jev "System One" request per transcript:
   (numbered elements, kept warm off-path)   │  is_command, addressed, complete, intent, target, app, key,
                                             ▼  site, text_span, destructive ... answered in parallel
                                        policy.decide ─► ignore | wait | act | confirm | disambiguate
                                             │
          executor: uiautomation (Invoke / SendKeys) · os.startfile · media keys · Playwright (Brave)
          island overlay (tkinter, click-through) + ack tone
```

- **Fast path:** transcript → one Jev call → act. Everything the decision needs (is this a command, is it for
  me, is it finished, what's the intent and target, is it destructive) is asked in a single parallel request.
- **System 2:** when a sentence is too open-ended for the fast path, a larger LLM writes a short step-by-step
  plan (`planner.py`), with a smaller model as fallback. Screen questions are answered from the page text.
- **Phone:** Retell custom functions `do_on_laptop(command)` and `whats_on_screen()` POST to one endpoint,
  verified with `X-Retell-Signature`.
- **Ambient:** reads the D2L iCal feed (or a synthetic item for demos). Cheap code checks decide when a
  judgment is worth making. A dedicated gate has to clear 0.75 on two checks in a row, with a 300 s cooldown
  after a "no" and an hourly call budget.

## Setup (Windows 11, Python 3.12, NVIDIA GPU recommended)

```powershell
uv venv --python 3.12 .venv
uv pip install uiautomation pywin32 playwright "RealtimeSTT[faster-whisper]" fastapi uvicorn httpx icalendar `
  sounddevice soundfile numpy pyttsx3 python-dotenv anthropic pyperclip pyautogui pillow pycaw
uv pip install torch --index-url https://download.pytorch.org/whl/cu121
uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
copy .env.example .env   # TYPESAFE_API_KEY (Jev), ANTHROPIC_API_KEY, D2L_URL
                         # optional: DEEPGRAM_API_KEY, RETELL_API_KEY, D2L_ICAL_URL, OPENROUTER_API_KEY
```

## Run

```powershell
.venv\Scripts\python.exe -m whis                             # mic + island overlay
.venv\Scripts\python.exe -m whis --phone                     # + Retell webhook on :8000
.venv\Scripts\python.exe -m whis --ambient                   # + D2L due-date nudges (D2L_ICAL_URL)
.venv\Scripts\python.exe -m whis --phone --demo-due-in 110   # demo: synthetic assignment due in 110 min
```

| flag | effect |
|---|---|
| `--stt auto\|whisper\|deepgram` | speech backend. `auto` (default) picks Deepgram when `DEEPGRAM_API_KEY` is set, otherwise local faster-whisper (large-v3-turbo finals, small.en partials) |
| `--phone` / `--no-verify` | start the Retell webhook on :8000 / skip signature verification |
| `--ambient` | proactive nudges from `D2L_ICAL_URL` |
| `--demo-due-in MIN` | inject a synthetic assignment due in MIN minutes (turns on ambient) |
| `--fake-stt` / `--dry-run` / `--fake-snapshot` | read transcripts from stdin / print decisions without running them / use a canned D2L screen |
| `--no-overlay` / `--no-browser` | headless / skip Playwright |

**Try it without a mic:**

```powershell
printf 'whis open notepad\nwhis open spotify and play tame impala\n' | .venv\Scripts\python.exe -u -m whis --fake-stt --dry-run --fake-snapshot
```

**Phone setup:** run `ngrok http --domain=<your-dev-domain> 8000` and point both Retell custom functions at
`https://<domain>/retell`. Check it locally with:

```powershell
curl -X POST localhost:8000/retell -H "Content-Type: application/json" -d "{\"name\":\"do_on_laptop\",\"args\":{\"command\":\"open notepad\"}}"
```

## Scripts

| script | job |
|---|---|
| `scripts\smoke.py` | checks each library in isolation |
| `scripts\sync_profile.py` | copies your Brave logins, cookies and extensions into `C:\whis-profile` (Brave can stay open) |
| `scripts\prewarm.py` | pre-demo checklist: keys, mic, CUDA, demo apps, profile sync (`--no-sync` skips it), warm models |
| `scripts\rehearse.py` | runs utterances through the real decision pipeline without executing anything |
| `scripts\ab_test.py`, `scripts\scenarios.py` | A/B harness and scenario suite for comparing decision backends |
| `scripts\stt_compare.py` | STT benchmark (`--bench`: 121 clips, WER / latency / VRAM) and vocab-corrector tests |
| `scripts\phone_check.py`, `scripts\retell_setup.py` | phone webhook check and Retell agent setup |
| `scripts\ical_check.py` | D2L iCal feed check |

## Layout

| file | job |
|---|---|
| `whis/config.py` | every threshold, key, app map, STT/ambient setting |
| `whis/stt.py`, `whis/stt_deepgram.py` | RealtimeSTT / Deepgram → partial and final transcripts, vocabulary fixes |
| `whis/controller.py` | ≤2 requests in flight, epoch invalidation, wake word + follow-up window, chaining, pending confirm |
| `whis/questions.py`, `whis/spans.py`, `whis/policy.py` | Jev question set, candidate spans, the fixed decision order |
| `whis/router.py`, `whis/jev.py` | intent routing and the Jev client |
| `whis/tree.py`, `whis/eyes.py`, `whis/browser.py` | UIA snapshot thread, screen reading, Playwright thread (persistent Brave profile) |
| `whis/executor.py`, `whis/apps.py` | one handler per intent; app launch and focus |
| `whis/overlay.py`, `whis/feedback.py` | Dynamic-Island pill + numbered badges; tones |
| `whis/planner.py`, `whis/brain_claude.py` | System 2 planning and screen Q&A |
| `whis/server.py`, `whis/ambient.py`, `whis/d2l.py`, `whis/watchdog.py` | phone webhook, proactive nudges, D2L, thread supervision |

## Credits

| project | licence | what we took |
|---|---|---|
| [jev-voice-browser](https://github.com/moritzkremb/jev-voice-browser) | MIT | streaming-transcript policy layer (`T` thresholds, act-on-partials, destructive gate), span extraction |
| [jev-voice](https://github.com/kevinbadi/jev-voice) | MIT | one-request speculative question set, wake word + follow-up window, escalation shape |
| [jev-ultrafast](https://github.com/browser-use/jev-ultrafast) | MIT | `snapshot.js` indexed DOM snapshot with occlusion check |
| [Windows-MCP](https://github.com/CursorTouch/Windows-MCP), [Windows-Use](https://github.com/CursorTouch/Windows-Use) | MIT | UIA walker design, interactive control-type sets, single-COM-thread rule |
| [jev-drone](https://github.com/RomanSlack/jev-drone) | MIT | ambient gate: code trigger, scene fingerprint, gated interrupt, hysteresis, cooldown, call budget |
| [macbrow](https://github.com/timpratim/macbrow), [agent-desktop](https://github.com/lahfir/agent-desktop) | MIT / Apache-2.0 | router + span selection, risk bars |
| [jev-windows-voice](https://github.com/mstf-svndk/jev-windows-voice) | MIT | clipboard typing, snapshot invalidation ideas |
| [jev-codex-router](https://github.com/0xNatoshi/jev-codex-router), [typesafe-mario](https://github.com/fhshaik/typesafe-mario), [OneVOneJev](https://github.com/emrickgarrett/OneVOneJev), [jev-trader](https://github.com/buberlo/jev-trader) | MIT / — | escalation dossier, epoch invalidation, in-flight guard, fallback ladder |
| [TypeSafe Jev](https://typesafe.ai) | — | the System One decision model behind every decision |
| [Anthropic](https://www.anthropic.com) | — | System 2 planner and screen Q&A models |
| [RealtimeSTT](https://github.com/KoljaB/RealtimeSTT), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [Deepgram](https://deepgram.com) (optional) | MIT / — | speech recognition |
| [uiautomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) | Apache-2.0 | Windows UI Automation bindings |
| [Retell AI](https://retellai.com), [ngrok](https://ngrok.com), [Playwright](https://playwright.dev), [FastAPI](https://fastapi.tiangolo.com) | — | phone agent, tunnel, browser, webhook |
