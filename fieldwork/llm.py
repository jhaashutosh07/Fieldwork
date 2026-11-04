"""Thin LLM client with a record/replay cache.

FIELDWORK_LLM selects the mode:
  none    - no model; LLM-backed steps fall back to heuristics (default)
  groq    - live calls to Groq (GROQ_API_KEY; FIELDWORK_MODEL overrides the model), recorded to data/llm_cache.jsonl
  openai  - live calls to OpenAI (OPENAI_API_KEY), recorded likewise
  replay  - answer only from the recorded cache; CI uses this, so evals are deterministic and free
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from .paths import ROOT

CACHE = ROOT / "data" / "llm_cache.jsonl"
MAX_COMPLETION_TOKENS = 1536   # bounds cost and latency per call; reasoning models need headroom for their thinking
PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY", "model": "openai/gpt-oss-120b",
             "usd_per_mtok": (0.15, 0.75)},
    "openai": {"base_url": None, "key": "OPENAI_API_KEY", "model": "gpt-4o-mini", "usd_per_mtok": (0.15, 0.60)},
}


class LLM:
    def __init__(self, mode: str | None = None, record: bool = True, retries: int = 6):
        """record=False keeps new answers in memory only (the public try-it endpoint must not grow the eval cache)."""
        self.mode = (mode or os.environ.get("FIELDWORK_LLM", "none")).lower()
        self.record, self.retries = record, retries
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        if CACHE.exists():
            for line in CACHE.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._cache[rec["key"]] = rec
        self._client = None
        if self.mode in PROVIDERS:
            from openai import OpenAI
            cfg = PROVIDERS[self.mode]
            self._client = OpenAI(api_key=os.environ[cfg["key"]], base_url=cfg["base_url"])
            self.model = os.environ.get("FIELDWORK_MODEL", cfg["model"])
        else:
            recorded = {r["model"] for r in self._cache.values()}
            self.model = next(iter(recorded)) if len(recorded) == 1 else "replay"

    @property
    def enabled(self) -> bool:
        return self.mode != "none"

    @staticmethod
    def _key(system: str, user: str) -> str:
        return hashlib.sha256(f"{system}\n---\n{user}".encode()).hexdigest()[:24]

    def is_recorded(self, system: str, user: str) -> bool:
        return self._key(system, user) in self._cache

    def json(self, system: str, user: str, trace=None, purpose: str = "") -> dict | None:
        """Return the model's JSON answer, or None when no model or recording is available."""
        if not self.enabled:
            return None
        key = self._key(system, user)
        rec = self._cache.get(key)
        t0 = time.perf_counter()
        if rec is None:
            if self._client is None:
                return None
            rec = self._complete(key, {"response_format": {"type": "json_object"},
                                       "messages": [{"role": "system", "content": system},
                                                    {"role": "user", "content": user}]}, _parse_json)
        return self._answer(rec, trace, purpose, t0)

    def chat(self, messages: list[dict], tools: list[dict], trace=None, purpose: str = "") -> dict | None:
        """One turn of a tool-calling conversation: returns {"content", "tool_calls": [{id, name, arguments}]}.
        Recorded like json(), keyed by the full conversation so far, so a whole agent episode replays exactly."""
        if not self.enabled:
            return None
        key = "chat:" + self._key(json.dumps(tools, sort_keys=True),
                                  json.dumps(messages, sort_keys=True, ensure_ascii=False))
        rec = self._cache.get(key)
        t0 = time.perf_counter()
        if rec is None:
            if self._client is None:
                return None
            rec = self._complete(key, {"messages": messages, "tools": tools}, _parse_chat)
        return self._answer(rec, trace, purpose, t0)

    def _answer(self, rec: dict, trace, purpose: str, t0: float):
        if trace is not None:
            trace.llm(purpose, rec["model"], rec["tokens_in"], rec["tokens_out"], rec["cost_usd"],
                      round((time.perf_counter() - t0) * 1000, 2), cached=rec.get("_fresh") is None, answer=rec["answer"])
        rec.pop("_fresh", None)
        return rec["answer"]

    def _complete(self, key: str, request: dict, parse) -> dict:
        t0 = time.perf_counter()
        for attempt in range(self.retries):
            try:
                resp = self._client.chat.completions.create(model=self.model, temperature=0,
                                                            max_completion_tokens=MAX_COMPLETION_TOKENS, **request)
                break
            except Exception as exc:  # retry rate limits, server errors and network failures; not bad requests
                status = getattr(exc, "status_code", None)
                if attempt == self.retries - 1 or (status is not None and status not in (408, 409, 429) and status < 500):
                    raise
                time.sleep(min(60, 4 * 2 ** attempt))
        usage = resp.usage
        pin, pout = PROVIDERS[self.mode]["usd_per_mtok"]
        answer = parse(resp.choices[0].message)
        rec = {"key": key, "model": self.model, "answer": answer, "tokens_in": usage.prompt_tokens,
               "tokens_out": usage.completion_tokens,
               "cost_usd": round((usage.prompt_tokens * pin + usage.completion_tokens * pout) / 1e6, 6),
               "ms": round((time.perf_counter() - t0) * 1000, 1)}
        with self._lock:
            self._cache[key] = rec
            if self.record:
                with CACHE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {**rec, "_fresh": True}


def _parse_json(message) -> dict:
    try:
        return json.loads(message.content)
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_chat(message) -> dict:
    return {"content": message.content or "",
            "tool_calls": [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                           for tc in (message.tool_calls or [])]}
