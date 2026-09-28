"""Shared dataclasses. Keep tiny; everything crosses threads via these."""
from __future__ import annotations
from dataclasses import dataclass, field
from concurrent.futures import Future
from typing import Any, Optional
import time


@dataclass
class Transcript:
    text: str
    final: bool
    utterance_id: int
    t: float = field(default_factory=time.perf_counter)
    source: str = "mic"            # mic | phone | fake
    future: Optional[Future] = None  # phone path: resolved with a spoken string for every decision


@dataclass
class Element:
    id: str                        # e01..e99
    role: str                      # button, link, edit, menuitem...
    name: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom (physical px)
    ref: Any = None                # uiautomation Control, or browser idx (int)
    source: str = "uia"            # uia | browser


@dataclass
class Snapshot:
    hwnd: int
    app: str
    title: str
    elements: list[Element]
    t: float = field(default_factory=time.perf_counter)
    source: str = "uia"

    def lines(self, limit: int = 25) -> list[str]:
        return [f'{e.id} {e.role} "{e.name[:40]}"' for e in self.elements[:limit]]

    def by_id(self, eid: str) -> Optional[Element]:
        return next((e for e in self.elements if e.id == eid), None)


@dataclass
class Action:
    intent: str
    args: dict = field(default_factory=dict)   # app, target(Element), text, key, url, query, direction
    said: str = ""


@dataclass
class Decision:
    kind: str                      # ignore | wait | act | confirm | disambiguate
    action: Optional[Action] = None
    say: str = ""                  # phrase key or text for feedback
    reason: str = ""


@dataclass
class Outcome:
    ok: bool
    msg: str = ""
