"""I3 failure hygiene: every worker thread runs under spawn(); an uncaught exception is logged and the loop is
restarted after a (doubling, capped) delay instead of silently dying — a dead tree/browser thread would hang
every ui_call/browser.call and with it the whole voice loop."""
import threading, time
from . import bus

_threads: dict[str, threading.Thread] = {}


def spawn(name: str, fn, restart_delay: float = 1.0) -> threading.Thread:
    def runner():
        delay = restart_delay
        while not bus.stop.is_set():
            try:
                fn()
                return                                    # normal exit (bus.stop)
            except Exception as e:
                bus.log("events", kind="thread_crash", thread=name, err=repr(e)[:300], restart_in_s=delay)
                time.sleep(delay)
                delay = min(delay * 2, 10.0)
    t = threading.Thread(target=runner, daemon=True, name=name)
    _threads[name] = t
    t.start()
    return t


def alive() -> dict[str, bool]:
    return {n: t.is_alive() for n, t in _threads.items()}
