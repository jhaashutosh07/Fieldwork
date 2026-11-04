"""Framework comparison on the same held-out companies: the shipped pipeline, the same pipeline as a LangGraph graph,
and an autonomous tool-calling agent (LangGraph model <-> tools loop).

The autonomous agent costs real LLM calls, so it runs on a seeded random sample of held-out companies; every system
is scored on exactly that sample. Live answers are recorded, so `fieldwork frameworks` replays for free in CI.

    fieldwork frameworks [--llm groq|replay]   ->  evals/frameworks.json
"""
from __future__ import annotations

import json
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from .agent import LATEST, VERSIONS, Agent
from .evals import gold, rows_for, score, split_of
from .graph_agent import PipelineGraph, ToolAgent, accounts_from_answers
from .insights import outcomes, paired_delta, wilson_ci
from .llm import LLM
from .paths import ROOT
from .tools import Tools, world
from .trace import Trace

OUT = ROOT / "evals" / "frameworks.json"
SAMPLE_COMPANIES = 60
SEED = 7


def sample_rows() -> list[dict]:
    g = gold()
    test_cos = sorted({g["rows"][r["crm_id"]] for r in rows_for("test")})
    picked = set(random.Random(SEED).sample(test_cos, SAMPLE_COMPANIES))
    return [r for r in rows_for("test") if g["rows"][r["crm_id"]] in picked]


def _summary(name: str, accts: list, rows: list[dict], trace_events: list[dict], wall_ms: float) -> dict:
    m, fails = score(accts, rows)
    llm = [e for e in trace_events if e["kind"] == "llm"]
    res = {"accounts": accts, "failures": fails, "rows": rows, "metrics": m}
    o = outcomes(res)
    asserted = sum(x["asserted"] for x in o.values())
    wrong = sum(x["wrong"] for x in o.values())
    return {"system": name, "verified_accuracy": m["verified_accuracy"], "silent_error_rate": m["silent_error_rate"],
            "silent_error_ci": wilson_ci(wrong, asserted), "review_rate": m["review_rate"],
            "entity_accuracy": m["entity_accuracy"], "icp_precision": m["icp_precision"], "icp_recall": m["icp_recall"],
            "llm_calls_per_row": round(len(llm) / len(rows), 2),
            "tokens_per_row": round(sum(e["tokens_in"] + e["tokens_out"] for e in llm) / len(rows)),
            "usd_per_1k_rows": round(1000 * sum(e["cost_usd"] for e in llm) / len(rows), 4),
            "compute_ms_per_row": round(wall_ms / len(rows), 1), "_outcomes": o}


def run(llm_mode: str = "replay", workers: int = 4) -> dict:
    rows = sample_rows()
    llm = LLM(llm_mode)
    results = []

    t = Trace()
    t0 = time.perf_counter()
    accts = Agent(VERSIONS[LATEST], Tools(world(), t), llm, t).run(rows)
    results.append(_summary(f"Pipeline ({LATEST})", accts, rows, t.events, (time.perf_counter() - t0) * 1000))

    t = Trace()
    t0 = time.perf_counter()
    accts = PipelineGraph(VERSIONS[LATEST], Tools(world(), t), llm, t).run(rows)
    results.append(_summary(f"Pipeline on LangGraph ({LATEST})", accts, rows, t.events, (time.perf_counter() - t0) * 1000))

    def episode(row):
        tr = Trace()
        return ToolAgent(Tools(world(), tr), llm, tr).answer(row), tr.events

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        done = list(pool.map(episode, rows))
    answers, events = [d[0] for d in done], [e for d in done for e in d[1]]
    accts = accounts_from_answers(rows, answers, Tools(world(), Trace()))
    results.append(_summary("Autonomous tool-calling agent", accts, rows, events, (time.perf_counter() - t0) * 1000))

    live_ms = [r["ms"] for r in llm._cache.values() if r.get("ms") and str(r.get("key", "")).startswith("chat:")]
    base = results[0]["_outcomes"]
    for r in results[1:]:
        r["vs_pipeline"] = {k: paired_delta(base, r["_outcomes"], k) for k in ("verified_accuracy", "silent_error_rate")}
    tool = results[2]
    # where the autonomous agent went wrong, by failure type (the same signatures the eval uses)
    tool_fails = score(accts, rows)[1]
    by_sig: dict[str, int] = {}
    for f in tool_fails:
        by_sig[f["signature"]] = by_sig.get(f["signature"], 0) + 1
    tool["failure_types"] = dict(sorted(by_sig.items(), key=lambda kv: -kv[1]))
    tool["steps_per_row"] = tool["llm_calls_per_row"]
    tool["median_live_ms_per_call"] = round(statistics.median(live_ms), 1) if live_ms else None
    examples = [{"company": f["company"], "signature": f["signature"], "rows": [r["account_name"] for r in f["rows"]],
                 "detail": f["detail"][:160]} for f in tool_fails if f["signature"] != "sent_to_review"][:8]
    out = {"generated": time.strftime("%Y-%m-%d"), "sample_companies": SAMPLE_COMPANIES, "sample_rows": len(rows),
           "model": llm.model, "systems": [{k: v for k, v in r.items() if not k.startswith("_")} for r in results],
           "tool_agent_wrong_examples": examples}
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return out
