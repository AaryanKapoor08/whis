import os
import sys

# winrt (OCR in eyes.py) ships an old MSVCP140 14.29. If it loads first, torch/ctranslate2 built with
# newer MSVC crash in std::mutex (0xc0000005 in MSVCP140.dll). Pin the system copy before anything else.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.WinDLL(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "msvcp140.dll"))
    except OSError:
        pass
