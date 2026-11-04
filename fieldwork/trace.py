"""Run trace: every tool call, LLM call and decision, attributed to the account being worked on."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Callable


def _brief(obj, limit: int = 600):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return obj if len(s) <= limit else s[:limit] + "..."


class Trace:
    def __init__(self, on_event: Callable[[dict], None] | None = None):
        """on_event, if given, is called with each event as it happens (used to stream a run to the browser)."""
        self.events: list[dict] = []
        self.account: str | None = None
        self.cost_usd = 0.0
        self.llm_calls = 0
        self.tool_calls = 0
        self._on_event = on_event

    def _add(self, event: dict) -> None:
        event["t_ns"] = time.time_ns()   # end time; with "ms" it gives the span's start for OpenTelemetry export
        self.events.append(event)
        if self._on_event:
            self._on_event(event)

    @contextmanager
    def scope(self, account: str):
        prev, self.account = self.account, account
        try:
            yield
        finally:
            self.account = prev

    def tool(self, name: str, args: dict, result, ms: float):
        self.tool_calls += 1
        self._add({"kind": "tool", "account": self.account, "name": name, "args": args, "result": _brief(result),
                   "ms": ms})

    def llm(self, purpose: str, model: str, tokens_in: int, tokens_out: int, cost: float, ms: float, cached: bool,
            answer):
        self.llm_calls += 1
        self.cost_usd += cost
        self._add({"kind": "llm", "account": self.account, "name": purpose, "model": model, "tokens_in": tokens_in,
                   "tokens_out": tokens_out, "cost_usd": cost, "ms": ms, "replayed": cached,
                   "result": _brief(answer)})

    def decision(self, name: str, **detail):
        self._add({"kind": "decision", "account": self.account, "name": name, "result": _brief(detail)})
