"""Jev (TypeSafe System One) client. One persistent HTTP client, epoch-based staleness, provider failover.
Pattern from jev-voice brain.py (raw httpx) + jev-voice-browser jev.js (abort/epoch)."""
import time, threading
import httpx
from . import config, bus


class JevClient:
    def __init__(self):
        self._idx = 0
        self._timeouts = 0
        self._http = httpx.Client(timeout=config.JEV_TIMEOUT_S, limits=httpx.Limits(keepalive_expiry=config.JEV_KEEPALIVE_S))
        self._lock = threading.Lock()
        self.last_latency_ms = None

    @property
    def provider(self):
        return config.JEV_PROVIDERS[self._idx]

    def warm(self):
        try:
            self.ask("warm up", {"q": {"type": "noul", "instructions": "The text is a greeting."}})
        except Exception as e:
            bus.log("events", kind="jev_warm_fail", err=str(e))
        from . import planner
        planner.warm()                                           # Claude client import (~1.4 s) off the first escalation

    def _post(self, url, headers, body):
        try:
            return self._http.post(url, headers=headers, json=body)
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError):   # server closed the idle keep-alive socket: reconnect once
            return self._http.post(url, headers=headers, json=body)

    def ask(self, state, questions: dict, epoch: int = 0, current_epoch=lambda: 0):
        """Returns answers dict, or None if the answer is stale (epoch < current_epoch()) or on error."""
        p = self.provider
        body = {"state": state, "model": p["model"], "questions": questions}
        t0 = time.perf_counter()
        try:
            r = self._post(p["base_url"] + p["path"], {"Authorization": f"Bearer {p['key']}"}, body)
            if r.status_code == 429 or r.status_code >= 500:     # one quick retry on a server hiccup (Architecture: ≤1 retry)
                if epoch < current_epoch():
                    return None                                  # text moved on; don't spend the retry
                time.sleep(0.15)
                r = self._post(p["base_url"] + p["path"], {"Authorization": f"Bearer {p['key']}"}, body)
            r.raise_for_status()
            self._timeouts = 0
        except httpx.TimeoutException:
            with self._lock:
                self._timeouts += 1
                if self._timeouts >= config.JEV_FAILOVER_TIMEOUTS and len(config.JEV_PROVIDERS) > 1:
                    self._idx = (self._idx + 1) % len(config.JEV_PROVIDERS)
                    self._timeouts = 0
                    bus.log("events", kind="jev_failover", to=self.provider["name"])
            return None
        except Exception as e:
            bus.log("events", kind="jev_error", err=str(e)[:200])
            return None
        self.last_latency_ms = (time.perf_counter() - t0) * 1000
        data = r.json()
        bus.log("jev", ms=round(self.last_latency_ms), epoch=epoch, tokens=data.get("usage", {}).get("input_tokens"))
        if epoch < current_epoch():
            bus.log("events", kind="jev_stale", epoch=epoch, current=current_epoch())
            return None
        return data.get("answers", {})


client = JevClient()
