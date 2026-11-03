"""Insights beyond the headline number.

  - uncertainty:   95% bootstrap intervals (resampling companies); Wilson intervals for the silent error rate,
                   which stay honest at zero observed errors where a bootstrap collapses to [0, 0]
  - significance:  paired bootstrap of each version against the previous one, on the same companies
  - experiments:   versions after the shipped one, with a ship / don't-ship decision and the reason
  - automation:    risk-coverage curve: how many rows can run unattended at what error rate
  - cost:          LLM calls, dollars and time per 1,000 CRM rows, and dollars per verified account
  - robustness:    stress test that strips websites from the CRM and re-scores each version

    fieldwork insights [--llm replay|groq]      ->  evals/insights.json (read by the dashboard)
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
import time

from . import flywheel
from .agent import LATEST, VERSIONS
from .evals import FIELDS, _field_ok, gold
from .llm import LLM
from .paths import ROOT

INSIGHTS = ROOT / "evals" / "insights.json"
BOOT = 2000
SEED = 7
STRESS_RATES = (0.0, 0.25, 0.5, 0.75, 1.0)
STRESS_VERSIONS = ("v1", "v3", "v4", "v5")
CURVE_VERSIONS = ("v3", "v4", "v5")


# ---------- per-company outcomes ----------
def _account_wrong(a, g) -> bool:
    gc = g["companies"].get(a.company_id)
    return (gc is None or any(g["rows"][i] != a.company_id for i in a.crm_ids)
            or not all(_field_ok(f, a.fields[f].value, gc) for f in FIELDS) or a.icp_fit != gc["icp_fit"])


def outcomes(res: dict) -> dict[str, dict]:
    """Per company: verified (0/1), asserted accounts, wrong asserted accounts, rows sent to review, rows."""
    g = gold()
    failed = {f["company_id"] for f in res["failures"]}
    out = {}
    for r in res["rows"]:
        co = g["rows"][r["crm_id"]]
        o = out.setdefault(co, {"verified": 0, "asserted": 0, "wrong": 0, "review_rows": 0, "rows": 0})
        o["rows"] += 1
    for co, o in out.items():
        o["verified"] = int(co not in failed)
    for a in res["accounts"]:
        owner = g["rows"][a.crm_ids[0]]
        if owner not in out:
            continue
        if a.needs_review:
            out[owner]["review_rows"] += len(a.crm_ids)
        else:
            out[owner]["asserted"] += 1
            out[owner]["wrong"] += int(_account_wrong(a, g))
    return out


def _metric(outs: dict, ids: list[str], metric: str) -> float:
    if metric == "verified_accuracy":
        return sum(outs[i]["verified"] for i in ids) / len(ids)
    if metric == "silent_error_rate":
        asserted = sum(outs[i]["asserted"] for i in ids)
        return sum(outs[i]["wrong"] for i in ids) / asserted if asserted else 0.0
    if metric == "review_rate":
        return sum(outs[i]["review_rows"] for i in ids) / sum(outs[i]["rows"] for i in ids)
    raise KeyError(metric)


def bootstrap_ci(outs: dict, metric: str) -> list[float]:
    ids = sorted(outs)
    rng = random.Random(SEED)
    vals = sorted(_metric(outs, [rng.choice(ids) for _ in ids], metric) for _ in range(BOOT))
    return [round(vals[int(0.025 * BOOT)], 4), round(vals[int(0.975 * BOOT) - 1], 4)]


def wilson_ci(k: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 1.0]
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def ci(outs: dict, metric: str) -> list[float]:
    if metric == "silent_error_rate":
        return wilson_ci(sum(o["wrong"] for o in outs.values()), sum(o["asserted"] for o in outs.values()))
    return bootstrap_ci(outs, metric)


def paired_delta(a: dict, b: dict, metric: str) -> dict:
    """b minus a on the same companies, with a 95% interval and a two-sided bootstrap p-value."""
    ids = sorted(set(a) & set(b))
    rng = random.Random(SEED)
    point = _metric(b, ids, metric) - _metric(a, ids, metric)
    deltas = []
    for _ in range(BOOT):
        s = [rng.choice(ids) for _ in ids]
        deltas.append(_metric(b, s, metric) - _metric(a, s, metric))
    deltas.sort()
    le = sum(d <= 0 for d in deltas) / BOOT
    ge = sum(d >= 0 for d in deltas) / BOOT
    return {"delta": round(point, 4), "ci": [round(deltas[int(0.025 * BOOT)], 4), round(deltas[int(0.975 * BOOT) - 1], 4)],
            "p": round(min(1.0, 2 * min(le, ge)), 4)}


# ---------- automation vs risk ----------
def risk_coverage(res: dict) -> dict:
    """Sort accounts by how sure the agent is (asserted ones first, by confidence; then abstentions by their best
    candidate's score) and automate them in that order. Each point: share of rows automated vs error rate among
    automated accounts. Abstentions are judged on the entity of their best guess only."""
    g = gold()
    items = []
    for a in res["accounts"]:
        if a.needs_review:
            guess = a.company_id or a.guess_company_id
            wrong = guess is None or any(g["rows"][i] != guess for i in a.crm_ids)
            items.append((0, a.guess_score if guess else 0.0, len(a.crm_ids), wrong))
        else:
            items.append((1, a.confidence, len(a.crm_ids), _account_wrong(a, g)))
    items.sort(key=lambda x: (-x[0], -x[1]))
    total_rows = sum(i[2] for i in items)
    points, rows_done, wrong, n = [], 0, 0, 0
    operating = None
    for k, (asserted, _, nrows, w) in enumerate(items):
        rows_done += nrows
        wrong += w
        n += 1
        last_asserted = asserted and (k + 1 == len(items) or not items[k + 1][0])
        if last_asserted:
            operating = {"coverage": round(rows_done / total_rows, 4), "error": round(wrong / n, 4)}
        if last_asserted or k + 1 == len(items) or k % 5 == 4:
            points.append({"coverage": round(rows_done / total_rows, 4), "error": round(wrong / n, 4)})
    return {"points": points, "operating": operating, "full_automation_error": points[-1]["error"] if points else None}


# ---------- robustness ----------
def strip_websites(rows: list[dict], rate: float) -> list[dict]:
    """Blank the website on a stable `rate` share of rows (by hash of the CRM id): the CRM gets messier."""
    out = []
    for r in rows:
        h = int(hashlib.md5(r["crm_id"].encode(), usedforsecurity=False).hexdigest(), 16) % 10_000 / 10_000
        out.append({**r, "website": "" if h < rate else r["website"]})
    return out


def _cost(res: dict) -> dict:
    rows = len(res["rows"])
    m = res["metrics"]
    verified = round(m["verified_accuracy"] * m["companies"])
    return {"llm_calls_per_1k_rows": round(1000 * m["llm_calls"] / rows, 1),
            "usd_per_1k_rows": round(1000 * m["cost_usd"] / rows, 4),
            "usd_per_verified_account": round(m["cost_usd"] / verified, 6) if verified else None,
            "compute_ms_per_1k_rows": round(1000 * res["duration_ms"] / rows, 1)}


def build(llm_mode: str = "replay") -> dict:
    t0 = time.time()
    order = list(VERSIONS)
    shipped_idx = order.index(LATEST)
    results, outs = {}, {}
    for v in order:
        mode = llm_mode if (VERSIONS[v].icp == "llm" or VERSIONS[v].llm_adjudicate) else "none"
        results[v] = flywheel.run(v, "test", mode, persist=False)
        outs[v] = outcomes(results[v])

    versions = []
    for i, v in enumerate(order[:shipped_idx + 1]):
        m = results[v]["metrics"]
        e = {"version": v, "summary": VERSIONS[v].summary,
             **{k: m[k] for k in ("verified_accuracy", "silent_error_rate", "review_rate")},
             "ci": {k: ci(outs[v], k) for k in ("verified_accuracy", "silent_error_rate", "review_rate")},
             "cost": _cost(results[v])}
        if i:
            prev = order[i - 1]
            e["vs_prev"] = {k: paired_delta(outs[prev], outs[v], k) for k in ("verified_accuracy", "silent_error_rate")}
        versions.append(e)

    experiments = []
    base = results[LATEST]["metrics"]
    for v in order[shipped_idx + 1:]:
        m = results[v]["metrics"]
        d = {k: paired_delta(outs[LATEST], outs[v], k) for k in ("verified_accuracy", "silent_error_rate")}
        tol = flywheel.GATE_TOLERANCE
        gate_fail = (m["verified_accuracy"] - base["verified_accuracy"] < tol["verified_accuracy"]
                     or m["silent_error_rate"] - base["silent_error_rate"] > tol["silent_error_rate"])
        cost_ratio = (m["cost_usd"] / base["cost_usd"]) if base["cost_usd"] else None
        if gate_fail:
            decision, reason = "rejected", "fails the CI eval gate against the shipped version"
        elif d["verified_accuracy"]["delta"] > 0 and d["verified_accuracy"]["ci"][0] > 0:
            decision, reason = "ship", "significant gain on held-out test"
        else:
            decision = "not shipped"
            reason = "no significant gain on held-out test"
            if cost_ratio and cost_ratio > 1:
                reason += f" and {100 * (cost_ratio - 1):.0f}% higher LLM cost"
        experiments.append({"version": v, "summary": VERSIONS[v].summary,
                            **{k: m[k] for k in ("verified_accuracy", "silent_error_rate", "review_rate")},
                            "vs_shipped": d, "cost_ratio": round(cost_ratio, 3) if cost_ratio else None,
                            "decision": decision, "reason": reason})

    curves = {v: risk_coverage(results[v]) for v in CURVE_VERSIONS}

    stress = {"rates": list(STRESS_RATES), "series": {}}
    for v in STRESS_VERSIONS:
        mode = llm_mode if (VERSIONS[v].icp == "llm" or VERSIONS[v].llm_adjudicate) else "none"
        series = {"verified_accuracy": [], "silent_error_rate": [], "review_rate": [], "llm_calls": []}
        for rate in STRESS_RATES:
            res = flywheel.run(v, "test", mode, persist=False, rows=strip_websites(results[v]["rows"], rate))
            for k in series:
                series[k].append(res["metrics"][k])
        stress["series"][v] = series

    llm = LLM("replay")
    live_ms = [r["ms"] for r in llm._cache.values() if r.get("ms")]
    out = {"generated": time.strftime("%Y-%m-%d"), "split": "test", "shipped": LATEST,
           "companies": results[LATEST]["metrics"]["companies"], "rows": results[LATEST]["metrics"]["rows"],
           "bootstrap_resamples": BOOT, "versions": versions, "experiments": experiments,
           "risk_coverage": curves, "stress": stress,
           "llm": {"model": llm.model, "recorded_calls": len(llm._cache),
                   "median_live_ms": round(statistics.median(live_ms), 1) if live_ms else None,
                   "timed_calls": len(live_ms)},
           "build_seconds": round(time.time() - t0, 1)}
    INSIGHTS.write_text(json.dumps(out, indent=1))
    return out
