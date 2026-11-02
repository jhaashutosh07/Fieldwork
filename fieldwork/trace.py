"""Run trace: every tool call, LLM call and decision, attributed to the account being worked on."""
from __future__ import annotations

import json
from contextlib import contextmanager


def _brief(obj, limit: int = 600):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return obj if len(s) <= limit else s[:limit] + "..."


class Trace:
    def __init__(self):
        self.events: list[dict] = []
        self.account: str | None = None
        self.cost_usd = 0.0
        self.llm_calls = 0
        self.tool_calls = 0

    @contextmanager
    def scope(self, account: str):
        prev, self.account = self.account, account
        try:
            yield
        finally:
            self.account = prev

    def tool(self, name: str, args: dict, result, ms: float):
        self.tool_calls += 1
        self.events.append({"kind": "tool", "account": self.account, "name": name, "args": args,
                            "result": _brief(result), "ms": ms})

    def llm(self, purpose: str, model: str, tokens_in: int, tokens_out: int, cost: float, ms: float, cached: bool,
            answer):
        self.llm_calls += 1
        self.cost_usd += cost
        self.events.append({"kind": "llm", "account": self.account, "name": purpose, "model": model,
                            "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost, "ms": ms,
                            "replayed": cached, "result": _brief(answer)})

    def decision(self, name: str, **detail):
        self.events.append({"kind": "decision", "account": self.account, "name": name, "result": _brief(detail)})
