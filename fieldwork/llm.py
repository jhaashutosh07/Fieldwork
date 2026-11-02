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

    def json(self, system: str, user: str, trace=None, purpose: str = "") -> dict | None:
        """Return the model's JSON answer, or None when no model or recording is available."""
        if not self.enabled:
            return None
        key = hashlib.sha256(f"{system}\n---\n{user}".encode()).hexdigest()[:24]
        rec = self._cache.get(key)
        t0 = time.perf_counter()
        if rec is None:
            if self._client is None:
                return None
            rec = self._call(key, system, user)
        if trace is not None:
            trace.llm(purpose, rec["model"], rec["tokens_in"], rec["tokens_out"], rec["cost_usd"],
                      round((time.perf_counter() - t0) * 1000, 2), cached=rec.get("_fresh") is None, answer=rec["answer"])
        rec.pop("_fresh", None)
        return rec["answer"]

    def _call(self, key: str, system: str, user: str) -> dict:
        t0 = time.perf_counter()
        for attempt in range(self.retries):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model, temperature=0, response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
                break
            except Exception:  # rate limits on free tiers: back off and retry
                if attempt == self.retries - 1:
                    raise
                time.sleep(min(60, 4 * 2 ** attempt))
        usage = resp.usage
        pin, pout = PROVIDERS[self.mode]["usd_per_mtok"]
        try:
            answer = json.loads(resp.choices[0].message.content)
        except (json.JSONDecodeError, TypeError):
            answer = {}
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
