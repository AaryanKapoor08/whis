r"""Transcribe one utterance with the configured STT models (+ large-v3-turbo) side by side, and self-test the
vocabulary corrector.
  .venv\Scripts\python.exe scripts\stt_compare.py                          # record 7 s from the mic
  .venv\Scripts\python.exe scripts\stt_compare.py --file logs/mic_sample.npy  # float32 16 kHz mono .npy (or .wav)
  .venv\Scripts\python.exe scripts\stt_compare.py --test                    # corrector tests only (no GPU)
  .venv\Scripts\python.exe scripts\stt_compare.py --bench                   # model matrix on logs/stt_bench (SAPI voices
                                                                             # clean + noisy, + mic_sample): WER, vocab hits, ms, VRAM"""
import sys, os, time, argparse, json, re, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from whis import config  # FIRST: sets the CUDA DLL PATH
import numpy as np

SR, SECS = 16000, 7

FIX_CASES = [   # (heard, must contain) ; corrected term compared case-insensitively
    ("open Spotify and then search for Tim Mimbala", "tame impala"),
    ("search for team impala in it", "tame impala"),
    ("in Spotify search for team and parlour", "tame impala"),
    ("open plot code in the terminal", "claude code"),
    ("open cloud code", "claude code"),
    ("WIS, open Clodcode in the terminal.", "claude code"),
    ("open VSCode", "vs code"),
    ("go to d to l", "d2l"),
    ("open note pad", "notepad"),
    ("search for tame impalla", "tame impala"),      # fuzzy-only (not in STT_FIXES)
    ("open claude coat in the terminal", "claude code"),
    ("open spotifi", "spotify"),
    ("Swiss, pause the music", "whis"),
]
KEEP_CASES = ["open notepad", "type hello from me", "search for weather in fredericton", "go to google",
              "close the window", "volume down", "play some tame impala", "Play some Tame Impala on Spotify.",
              "close code", "open the terminal", "click the first result", "the brave new world", "open claude",
              "Please, can you open Spotify and then search for Tame Impala in it?"]


def run_tests() -> bool:
    from whis.stt import fix_vocab
    ok = True
    for heard, want in FIX_CASES:
        out = fix_vocab(heard)
        good = want in out.lower()
        ok &= good
        print(f"{'PASS' if good else 'FAIL'} fix   {heard!r} -> {out!r}")
    for s in KEEP_CASES:
        out = fix_vocab(s)
        ok &= out == s
        print(f"{'PASS' if out == s else 'FAIL'} keep  {s!r}" + ("" if out == s else f" -> {out!r}"))
    s = "Please, can you open Spotify and then search for team impala in it?"
    t0 = time.perf_counter()
    for _ in range(200):
        fix_vocab(s)
    print(f"fix_vocab: {(time.perf_counter() - t0) / 200 * 1000:.3f} ms per call ({len(s.split())} words)")
    print("ALL PASS" if ok else "SOME FAILED")
    return ok


def load_audio(path: str) -> np.ndarray:
    if path.endswith(".npy"):
        return np.load(path).astype(np.float32).ravel()
    import wave
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


# ---- bench: realistic model matrix on the command vocabulary
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH_DIR = os.path.join(ROOT, "logs", "stt_bench")
BENCH_PHRASES = [
    "whis, open Spotify and play Tame Impala.", "Open Spotify.", "Play Tame Impala.", "In Spotify search for Tame Impala.",
    "Then open the browser.", "Open D2L.", "Look if I have an assignment left.", "Then open VS Code.",
    "Open the terminal in it and run Claude Code.", "Run Claude Code.", "Open Notepad and type hello from me.",
    "Open Brave and go to D2L.", "whis, pause the music.", "Volume up.", "Close the window.", "Save it.",
    "Click the first result.", "whis, what's on my screen?", "Switch to VS Code.", "Search for Tame Impala on Spotify."]
BENCH_VOICES = [("Microsoft David Desktop", 0), ("Microsoft Zira Desktop", 0), ("Microsoft David Desktop", 3)]   # (voice, rate): 3 = fast/casual
MIC_REF = "Please, can you open Spotify and then search for Tame Impala in it?"   # logs/mic_sample.npy (real mic)
TERMS = ["whis", "tame impala", "claude code", "vs code", "d2l", "spotify", "brave", "notepad"]
FINAL_CANDS = [("large-v3-turbo", "int8_float16"), ("large-v3-turbo", "float16"), ("distil-large-v3.5", "float16"),
               ("distil-large-v3.5", "int8_float16"), ("large-v3", "int8_float16"), ("large-v3", "float16")]
RT_CANDS = [("small.en", "int8_float16"), ("small.en", "float16"), ("base.en", "float16"), ("distil-small.en", "float16")]


def make_bench():
    """Synthesize BENCH_PHRASES with the SAPI voices (16 kHz mono wav) into logs/stt_bench + refs.json."""
    os.makedirs(BENCH_DIR, exist_ok=True)
    refs = {}
    lines = ["Add-Type -AssemblyName System.Speech", "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
             "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)"]
    for vi, (v, rate) in enumerate(BENCH_VOICES):
        for pi, p in enumerate(BENCH_PHRASES):
            name = f"v{vi}_p{pi:02d}.wav"
            refs[name] = p
            spoken = p.replace("whis", "wiss").replace("D2L", "D 2 L").replace("'", "''")   # SAPI reads 'whis' as 'whiz'
            lines += [f"$s.SelectVoice('{v}')", f"$s.Rate = {rate}", f"$s.SetOutputToWaveFile('{os.path.join(BENCH_DIR, name)}', $f)",
                      f"$s.Speak('{spoken}')", "$s.SetOutputToNull()"]
    subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(lines)], check=True)
    json.dump(refs, open(os.path.join(BENCH_DIR, "refs.json"), "w"), indent=1)
    print(f"{len(refs)} clips -> {BENCH_DIR}")


def _norm_words(s: str) -> list[str]:
    s = s.lower().replace("vscode", "vs code").replace("v.s.", "vs")
    return re.sub(r"[^a-z0-9' ]", " ", s).split()


def _wer(ref: str, hyp: str) -> tuple[int, int]:
    r, h = _norm_words(ref), _norm_words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)], len(r)


def _terms(ref: str, hyp: str) -> tuple[int, int]:
    r, h = " ".join(_norm_words(ref)), " " + " ".join(_norm_words(hyp)) + " "
    want = [t for t in TERMS if f" {t} " in f" {r} "]
    return sum(f" {t} " in h for t in want), len(want)


def _vram() -> int:
    try:
        return int(subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]).split()[0])
    except Exception:
        return -1


def _clips(noisy: bool) -> list[tuple[str, np.ndarray, str]]:
    refs = json.load(open(os.path.join(BENCH_DIR, "refs.json")))
    rng = np.random.default_rng(0)
    out = []
    for name, ref in refs.items():
        a = load_audio(os.path.join(BENCH_DIR, name))
        a = np.concatenate([np.zeros(SR // 5, np.float32), a, np.zeros(SR // 5, np.float32)])
        if noisy:                                  # ~12 dB SNR room noise (pink-ish) + mild gain drop
            n = np.cumsum(rng.standard_normal(len(a))).astype(np.float32); n -= np.convolve(n, np.ones(400) / 400, "same")
            n *= np.sqrt(np.mean(a ** 2) / (np.mean(n ** 2) + 1e-9) / 10 ** 1.2)
            a = 0.6 * (a + n)
        out.append((name + ("~noisy" if noisy else ""), a.astype(np.float32), ref))
    mic = os.path.join(ROOT, "logs", "mic_sample.npy")
    if os.path.exists(mic):
        out.append(("mic_sample", load_audio(mic), MIC_REF))
    return out


def bench(which: str):
    from faster_whisper import WhisperModel
    from whis.stt import fix_vocab
    clips = [c for c in _clips(False) if c[0] != "mic_sample"] + _clips(True)
    cands = (FINAL_CANDS if which in ("final", "all") else []) + (RT_CANDS if which in ("rt", "all") else [])
    print(f"{len(clips)} clips; idle VRAM {_vram()} MiB", flush=True)
    rows = []
    for name, ct in cands:
        beam = config.STT.get("beam_size_realtime", 3) if (name, ct) in RT_CANDS else config.STT.get("beam_size", 5)
        try:
            v0 = _vram()
            m = WhisperModel(name, device="cuda", compute_type=ct)
            m.transcribe(clips[0][1], language="en", beam_size=1)            # warm-up
            e = n = th = tn = fe = fth = 0; ms = []; misses = []
            for cname, a, ref in clips:
                t0 = time.perf_counter()
                segs, _ = m.transcribe(a, language="en", beam_size=beam, initial_prompt=config.STT_PROMPT,
                                       condition_on_previous_text=False, vad_filter=False)
                hyp = " ".join(s.text.strip() for s in segs)
                ms.append((time.perf_counter() - t0) * 1000)
                fixed = fix_vocab(hyp)
                de, dn = _wer(ref, hyp); e += de; n += dn
                fe += _wer(ref, fixed)[0]
                a1, b1 = _terms(ref, hyp); th += a1; tn += b1
                fth += _terms(ref, fixed)[0]
                if _terms(ref, fixed)[0] < b1 or cname == "mic_sample":
                    misses.append(f"{cname}: {hyp!r}")
            vr = _vram() - v0
            ms.sort()
            row = (f"{name}/{ct}/b{beam}", 100 * e / n, 100 * fe / n, f"{th}/{tn}", f"{fth}/{tn}", ms[len(ms) // 2], ms[int(len(ms) * .9)], vr)
            rows.append(row)
            print("%-34s WER %5.1f%%  fixed %5.1f%%  terms raw %s fixed %s  p50 %4.0f ms  p90 %4.0f ms  VRAM +%d MiB" % row, flush=True)
            for mm in misses[:6]:
                print("      ", mm, flush=True)
            del m
        except Exception as ex:
            print(f"{name}/{ct} FAIL {ex!r}"[:300], flush=True)
    json.dump(rows, open(os.path.join(BENCH_DIR, f"results_{which}.json"), "w"), indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="audio file (.npy float32 16 kHz mono, or 16 kHz 16-bit .wav) instead of the mic")
    ap.add_argument("--test", action="store_true", help="only run the vocabulary-corrector tests")
    ap.add_argument("--beam", type=int, default=config.STT.get("beam_size", 5))
    ap.add_argument("--make-bench", action="store_true", help="synthesize the bench clips (SAPI) into logs/stt_bench")
    ap.add_argument("--bench", nargs="?", const="all", choices=["all", "final", "rt"], help="run the model matrix")
    args = ap.parse_args()
    if args.test:
        sys.exit(0 if run_tests() else 1)
    if args.make_bench:
        return make_bench()
    if args.bench:
        return bench(args.bench)
    if args.file:
        a = load_audio(args.file)
    else:
        import sounddevice as sd
        print("default input device:", sd.query_devices(kind="input")["name"], flush=True)
        print(f"RECORDING {SECS}s — speak now", flush=True)
        audio = sd.rec(int(SR * SECS), samplerate=SR, channels=1, dtype="float32"); sd.wait()
        a = audio[:, 0]
    print(f"{len(a) / SR:.1f} s, peak level {np.abs(a).max():.3f} (should be > 0.05)", flush=True)
    from faster_whisper import WhisperModel
    from whis.stt import fix_vocab
    names = list(dict.fromkeys([config.STT["realtime_model_type"], config.STT["model"], "large-v3-turbo"]))
    for name in names:
        try:
            m = WhisperModel(name, device="cuda", compute_type="float16")
            m.transcribe(a[:SR], language="en", beam_size=1)      # warm-up (CUDA kernels)
            t0 = time.perf_counter()
            segs, _ = m.transcribe(a, language="en", beam_size=args.beam, initial_prompt=config.STT_PROMPT)
            text = " ".join(s.text.strip() for s in segs)
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"{name:16s} {ms:5d} ms  -> {text}", flush=True)
            fixed = fix_vocab(text)
            if fixed != text:
                print(f"{'':16s} {'fixed':>8s}  -> {fixed}", flush=True)
            del m
        except Exception as e:
            print(f"{name:16s} FAIL {e}", flush=True)


if __name__ == "__main__":
    main()
