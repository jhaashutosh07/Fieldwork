"""The eval flywheel: run -> triage failures into clusters -> promote a failure to a regression case -> gate.

A regression case is self-contained (the CRM rows plus the expected outcome), so it keeps guarding the fix even
after the dataset or the gold file changes.
"""
from __future__ import annotations

import json
import time
import uuid

from . import store
from .agent import LATEST, VERSIONS, Agent
from .evals import FIELDS, _field_ok, gold, rows_for, score
from .llm import LLM
from .tools import Tools, world
from .trace import Trace
from .paths import ROOT

CASES = ROOT / "evals" / "regressions"
BASELINE = ROOT / "evals" / "baseline.json"
HISTORY = ROOT / "evals" / "history.json"
GATE_TOLERANCE = {"verified_accuracy": -0.01, "silent_error_rate": 0.005}


def run(version: str = LATEST, split: str = "test", llm_mode: str | None = None, persist: bool = True) -> dict:
    policy = VERSIONS[version]
    llm = LLM(llm_mode)
    trace = Trace()
    rows = rows_for(split)
    t0 = time.perf_counter()
    accts = Agent(policy, Tools(world(), trace), llm, trace).run(rows)
    duration = (time.perf_counter() - t0) * 1000
    metrics, fails = score(accts, rows)
    metrics["cost_usd"] = round(trace.cost_usd, 5)
    metrics["llm_calls"] = trace.llm_calls
    record = {"id": uuid.uuid4().hex[:12], "version": version, "split": split, "llm_mode": llm.mode,
              "model": llm.model if llm.enabled else None, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "duration_ms": round(duration, 1), "cost_usd": metrics["cost_usd"], "llm_calls": trace.llm_calls,
              "tool_calls": trace.tool_calls, "metrics": metrics}
    if persist:
        store.save_run(record, trace, accts, fails)
    return {**record, "failures": fails, "accounts": accts}


def promote(failure_id: str) -> str:
    """Freeze a triaged failure into evals/regressions/<case>.json with the gold outcome for its company."""
    f = store.get_failure(failure_id)
    if f is None:
        raise KeyError(failure_id)
    if f.get("promoted_case"):
        return f["promoted_case"]
    gc = gold()["companies"][f["company_id"]]
    rows = f["rows"]
    case_id = f"{f['signature'].split(':')[0]}-{f['company_id']}"
    case = {"id": case_id, "created": time.strftime("%Y-%m-%d"), "from_run": f["run_id"], "signature": f["signature"],
            "company": gc["name"], "detail": f["detail"], "rows": rows,
            "expect": {"company_id": f["company_id"], "single_account": True,
                       "domain": gc["domains"], "employees": gc["employees"], "country": gc["countries"],
                       "icp_fit": gc["icp_fit"],
                       # abstaining is an acceptable fix for a mis-identified or unresolvable account
                       "allow_review": f["signature"].startswith(("sent_to_review", "wrong_entity"))}}
    CASES.mkdir(parents=True, exist_ok=True)
    (CASES / f"{case_id}.json").write_text(json.dumps(case, indent=1, ensure_ascii=False), encoding="utf-8")
    store.mark_promoted(failure_id, case_id)
    return case_id


def check_case(case: dict, version: str = LATEST, llm_mode: str | None = None) -> tuple[bool, str]:
    trace = Trace()
    accts = Agent(VERSIONS[version], Tools(world(), trace), LLM(llm_mode), trace).run([dict(r) for r in case["rows"]])
    exp = case["expect"]
    live = [a for a in accts if not a.needs_review]
    if any(a.needs_review for a in accts):
        if not exp.get("allow_review"):
            return False, "sent to review"
        if not live:
            return True, "sent to review (allowed)"
    if len(live) != 1:
        return False, f"{len(live)} accounts, expected 1"
    a = live[0]
    if a.company_id != exp["company_id"]:
        return False, f"resolved to {a.company_id}, expected {exp['company_id']}"
    gc = {"domains": exp["domain"], "employees": exp["employees"], "countries": exp["country"]}
    for fld in FIELDS:
        if not _field_ok(fld, a.fields[fld].value, gc):
            return False, f"{fld}={a.fields[fld].value!r}, expected one of {exp[fld]}"
    if a.icp_fit != exp["icp_fit"]:
        return False, f"icp_fit={a.icp_fit}, expected {exp['icp_fit']}"
    return True, "ok"


def load_cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES.glob("*.json"))]


def gate(version: str = LATEST, llm_mode: str | None = None, update_baseline: bool = False) -> tuple[bool, list[str]]:
    """CI gate: held-out metrics must not regress past tolerance and every regression case must pass."""
    result = run(version, "test", llm_mode, persist=False)
    m = result["metrics"]
    lines, ok = [], True
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else None
    if base and not update_baseline:
        for k, tol in GATE_TOLERANCE.items():
            delta = m[k] - base["metrics"][k]
            bad = delta < tol if tol < 0 else delta > tol
            ok &= not bad
            lines.append(f"{'FAIL' if bad else 'ok  '} {k}: {m[k]:.4f} (baseline {base['metrics'][k]:.4f}, "
                         f"delta {delta:+.4f})")
    for case in load_cases():
        passed, why = check_case(case, version, llm_mode)
        ok &= passed
        lines.append(f"{'ok  ' if passed else 'FAIL'} case {case['id']}: {why}")
    if update_baseline:
        BASELINE.write_text(json.dumps({"version": version, "llm_mode": result["llm_mode"], "metrics": m}, indent=1))
        lines.append(f"baseline updated from {version}")
    return ok, lines


def history(llm_mode: str | None = None) -> list[dict]:
    """Evaluate every released version on both splits and write evals/history.json (the README table)."""
    out = []
    for v, policy in VERSIONS.items():
        entry = {"version": v, "summary": policy.summary, "uses_llm": policy.icp == "llm" or policy.llm_adjudicate}
        for split in ("dev", "test"):
            r = run(v, split, llm_mode if entry["uses_llm"] else "none")
            entry[split] = r["metrics"]
            entry[f"{split}_run"] = r["id"]
            entry["llm_mode"] = r["llm_mode"]
            entry["model"] = r["model"]
            # replay with no recorded answers falls back to rules; say so instead of passing it off as an LLM result
            entry["llm_recorded"] = entry["uses_llm"] and r["metrics"]["llm_calls"] > 0
        cases = [check_case(c, v, llm_mode if entry["uses_llm"] else "none")[0] for c in load_cases()]
        entry["regression_cases"] = f"{sum(cases)}/{len(cases)}"
        out.append(entry)
    HISTORY.write_text(json.dumps(out, indent=1))
    return out
