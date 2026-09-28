"""Queues and shared state between threads. Import this, never create queues elsewhere."""
import queue, threading, time, json, os
from . import config

transcript_q: "queue.Queue" = queue.Queue()
executor_q: "queue.Queue" = queue.Queue()
feedback_q: "queue.Queue" = queue.Queue()
overlay_q: "queue.Queue" = queue.Queue()

stop = threading.Event()

_log_lock = threading.Lock()


def log(name: str, **fields):
    """One JSON line per event to logs/<kind>.jsonl (latency, misses, events)."""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    fields["t"] = round(time.time(), 3)
    with _log_lock, open(os.path.join(config.LOG_DIR, f"{name}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(fields, ensure_ascii=False, default=str) + "\n")
