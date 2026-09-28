"""Copy login state (cookies, passwords, local storage, extensions, prefs) from the user's REAL Brave profile
into the whis Playwright profile, so whis drives a browser that is already signed in everywhere.

Why copy instead of pointing Playwright at the real profile: Chromium >= 136 (Brave 1.96 = Chromium 154) refuses
remote debugging on the default user-data-dir, and Brave must be closed for that anyway. A copy works while the
user's own Brave stays open (Chromium opens its SQLite files with shared read access).

Same Windows user + same brave.exe => DPAPI / app-bound cookie keys decrypt in the copy (`Local State` is copied too).

Run:  .venv\\Scripts\\python.exe scripts\\sync_profile.py            (defaults from whis/config.py)
      .venv\\Scripts\\python.exe scripts\\sync_profile.py --full     (also History / IndexedDB / Service Worker)
"""
import os, sys, json, shutil, argparse, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from whis import config

# Relative to "User Data". Files or directories. Order does not matter.
CORE = [
    "Local State",
    "Default/Preferences", "Default/Secure Preferences",
    "Default/Network/Cookies", "Default/Network/Cookies-journal", "Default/Cookies",
    "Default/Network/TransportSecurity", "Default/Network/Network Persistent State",
    "Default/Login Data", "Default/Login Data For Account", "Default/Web Data", "Default/Account Web Data",
    "Default/Local Storage", "Default/Session Storage",
    "Default/Extensions", "Default/Local Extension Settings", "Default/Extension State", "Default/Extension Rules",
    "Default/Extension Scripts", "Default/Extension Cookies", "Default/Sync Extension Settings", "Default/Managed Extension Settings",
    "Default/Bookmarks", "Default/Favicons", "Default/Shortcuts", "Default/Top Sites",
]
FULL_EXTRA = ["Default/History", "Default/IndexedDB", "Default/Service Worker", "Default/File System"]
SKIP_NAMES = {"LOCK", "lockfile", "LOG", "LOG.old"}


def _copy(src, dst):
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            out = os.path.join(dst, rel) if rel != "." else dst
            os.makedirs(out, exist_ok=True)
            for f in files:
                if f in SKIP_NAMES:
                    continue
                try:
                    shutil.copy2(os.path.join(root, f), os.path.join(out, f))
                except OSError:
                    pass                      # locked by the running browser; best effort
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        shutil.copy2(src, dst)
        return True
    except OSError as e:
        print(f"   ! {os.path.basename(src)}: {e.strerror}")
        return False


def _fix_prefs(path):
    """No 'Brave didn't shut down correctly' bar, no session restore prompt."""
    try:
        with open(path, encoding="utf-8") as f:
            p = json.load(f)
        p.setdefault("profile", {})["exit_type"] = "Normal"
        p["profile"]["exited_cleanly"] = True
        p.setdefault("session", {})["restore_on_startup"] = 5           # open new tab page
        with open(path, "w", encoding="utf-8") as f:
            json.dump(p, f)
    except Exception as e:
        print(f"   ! prefs fix: {e}")


def sync(real: str, dst: str, full=False) -> int:
    if not os.path.isdir(os.path.join(real, "Default")):
        print(f"real profile not found: {real}"); return 1
    t0 = time.perf_counter(); n = 0
    for rel in CORE + (FULL_EXTRA if full else []):
        s = os.path.join(real, rel)
        if not os.path.exists(s):
            continue
        d = os.path.join(dst, rel)
        if os.path.isdir(s):
            shutil.rmtree(d, ignore_errors=True)
        if _copy(s, d):
            n += 1
            print(f"   {rel}")
    _fix_prefs(os.path.join(dst, "Default", "Preferences"))
    for stale in ("lockfile", os.path.join("Default", "LOCK")):
        try:
            os.remove(os.path.join(dst, stale))
        except OSError:
            pass
    print(f"synced {n} items -> {dst} in {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", default=config.REAL_BROWSER_PROFILE)
    ap.add_argument("--dst", default=config.BROWSER_PROFILE)
    ap.add_argument("--full", action="store_true", help="also History, IndexedDB, Service Worker (slower, bigger)")
    a = ap.parse_args()
    sys.exit(sync(a.real, a.dst, a.full))
