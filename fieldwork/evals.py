"""Score an agent run against gold, and turn every miss into a typed failure.

The headline metric is *verified account accuracy*: a company counts only if exactly one asserted account covers
all of its CRM rows AND the entity, domain, headcount, country and ICP verdict are all correct. Output that merely
looks plausible earns nothing. Accounts sent to human review are not wrong, but they are not verified either.
The safety metric is the *silent error rate*: of the accounts the agent asserted without review, how many are wrong.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from .agent import Account
from .tools import host, name_score, world

DATA = Path(__file__).resolve().parents[1] / "data" / "dataset"
FIELDS = ("domain", "employees", "country")


@lru_cache(maxsize=1)
def gold() -> dict:
    return json.loads((DATA / "gold.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def crm_rows() -> tuple[dict, ...]:
    with (DATA / "crm_export.csv").open(encoding="utf-8") as f:
        return tuple(csv.DictReader(f))


def split_of(company_id: str) -> str:
    """60/40 dev/test split by company, stable across runs. Triage and fixes use dev; headline numbers use test."""
    return "dev" if int(hashlib.md5(company_id.encode()).hexdigest(), 16) % 10 < 6 else "test"


def rows_for(split: str) -> list[dict]:
    g = gold()["rows"]
    return [dict(r) for r in crm_rows() if split == "all" or split_of(g[r["crm_id"]]) == split]


def _field_ok(field: str, value, gc: dict) -> bool:
    if field == "domain":
        return host(value) in gc["domains"] if value else False
    if field == "employees":
        return value in gc["employees"]
    if field == "country":   # a few companies have no recorded country: "unknown" is then the right answer
        return value in gc["countries"] if gc["countries"] else value is None
    return False


def score(accounts: list[Account], rows: list[dict]) -> tuple[dict, list[dict]]:
    g = gold()
    gold_of = g["rows"]
    row_by_id = {r["crm_id"]: r for r in rows}
    companies = sorted({gold_of[r["crm_id"]] for r in rows})
    acct_of_row = {cid: a for a in accounts for cid in a.crm_ids}
    asserted = [a for a in accounts if not a.needs_review]
    failures: list[dict] = []

    def fail(company: str, kind: str, crm_ids, field=None, expected=None, got=None, detail=None):
        failures.append({"company_id": company, "company": g["companies"][company]["name"], "signature": kind,
                         "field": field, "expected": expected, "got": got, "crm_ids": sorted(crm_ids),
                         "rows": [row_by_id[i] for i in sorted(crm_ids)], "detail": detail or ""})

    verified = 0
    field_hits, field_total = Counter(), Counter()
    for co in companies:
        gc = g["companies"][co]
        crm_ids = [r["crm_id"] for r in rows if gold_of[r["crm_id"]] == co]
        accts = {id(acct_of_row[i]): acct_of_row[i] for i in crm_ids}.values()
        accts = list(accts)
        ok = True
        if any(a.needs_review for a in accts):
            ok = False
            reasons = {a.review_reason for a in accts if a.needs_review}
            fail(co, "sent_to_review", [i for i in crm_ids if acct_of_row[i].needs_review], detail="; ".join(reasons))
        live = [a for a in accts if not a.needs_review]
        wrong = [a for a in live if a.company_id != co]
        for a in wrong:
            ok = False
            mine = [i for i in a.crm_ids if i in crm_ids]
            other = g["companies"].get(a.company_id, {}).get("name") or world().registry.get(a.company_id, {}).get("name")
            names = [row_by_id[i]["account_name"] for i in mine]
            kind = ("wrong_entity:similar_name" if other and max(name_score(n, other) for n in names) >= 0.6
                    else "wrong_entity:alias_or_variant")
            fail(co, kind, mine, "entity", co, a.company_id, f"matched '{names[0]}' to {other}")
        right = [a for a in live if a.company_id == co]
        if len(right) > 1:
            ok = False
            fail(co, "split_duplicate", [i for a in right for i in a.crm_ids], detail=f"{len(right)} accounts")
        for a in right:
            strays = [i for i in a.crm_ids if gold_of[i] != co]
            if strays:
                ok = False
                fail(co, "over_merge", strays, detail="rows of another company merged in")
        if not right:
            continue
        a = right[0]
        for fld in FIELDS:
            v = a.fields[fld].value
            field_total[fld] += 1
            if _field_ok(fld, v, gc):
                field_hits[fld] += 1
                continue
            ok = False
            if fld == "employees" and v is not None and v in {e["value"] for e in world().profiles[co]["employees"]}:
                kind = "stale_headcount"
            elif fld == "domain" and v and host(v) in gc["old_domains"]:
                kind = "old_domain"
            else:
                kind = f"{fld}_mismatch"
            fail(co, kind, a.crm_ids, fld, gc["employees"] if fld == "employees" else
                 gc["domains"] if fld == "domain" else gc["countries"], v,
                 f"source={a.fields[fld].source} as_of={a.fields[fld].as_of}")
        field_total["icp"] += 1
        if a.icp_fit == gc["icp_fit"]:
            field_hits["icp"] += 1
        else:
            ok = False
            fields_ok = all(_field_ok(f, a.fields[f].value, gc) for f in FIELDS)
            kind = ("icp_false_positive" if a.icp_fit else "icp_false_negative") if fields_ok else "icp_from_bad_fields"
            fail(co, kind, a.crm_ids, "icp", gc["icp_fit"], a.icp_fit,
                 f"industries={gc['industries']} reason={a.icp_reason}")
        verified += ok

    # pairwise dedup quality over rows assigned to an asserted account
    same_pred, same_gold, both = 0, 0, 0
    ids = [r["crm_id"] for r in rows]
    for i, x in enumerate(ids):
        for y in ids[i + 1:]:
            p = acct_of_row[x] is acct_of_row[y] and not acct_of_row[x].needs_review
            t = gold_of[x] == gold_of[y]
            same_pred += p
            same_gold += t
            both += p and t
    dp = both / same_pred if same_pred else 1.0
    dr = both / same_gold if same_gold else 1.0

    tp = sum(1 for a in asserted if a.icp_fit and a.company_id in g["companies"] and g["companies"][a.company_id]["icp_fit"])
    pred_pos = sum(1 for a in asserted if a.icp_fit)
    gold_pos = sum(1 for co in companies if g["companies"][co]["icp_fit"])
    silent_wrong = 0
    for a in asserted:
        gc = g["companies"].get(a.company_id)
        if (gc is None or any(gold_of[i] != a.company_id for i in a.crm_ids)
                or not all(_field_ok(f, a.fields[f].value, gc) for f in FIELDS) or a.icp_fit != gc["icp_fit"]):
            silent_wrong += 1
    top = [a for a in asserted if a.icp_fit][:25]
    p_at_25 = (sum(1 for a in top if g["companies"].get(a.company_id, {}).get("icp_fit")) / len(top)) if top else 0.0
    entity_ok = sum(1 for r in rows if not acct_of_row[r["crm_id"]].needs_review
                    and acct_of_row[r["crm_id"]].company_id == gold_of[r["crm_id"]])
    metrics = {
        "companies": len(companies), "rows": len(rows), "accounts": len(accounts),
        "verified_accuracy": round(verified / len(companies), 4),
        "entity_accuracy": round(entity_ok / len(rows), 4),
        "dedup_f1": round(2 * dp * dr / (dp + dr), 4) if dp + dr else 0.0,
        **{f"{k}_accuracy": round(field_hits[k] / field_total[k], 4) if field_total[k] else 0.0
           for k in (*FIELDS, "icp")},
        "icp_precision": round(tp / pred_pos, 4) if pred_pos else 0.0,
        "icp_recall": round(tp / gold_pos, 4) if gold_pos else 0.0,
        "precision_at_25": round(p_at_25, 4),
        "silent_error_rate": round(silent_wrong / len(asserted), 4) if asserted else 0.0,
        "review_rate": round(sum(1 for r in rows if acct_of_row[r["crm_id"]].needs_review) / len(rows), 4),
    }
    return metrics, failures


def clusters(failures: list[dict]) -> list[dict]:
    by = defaultdict(list)
    for f in failures:
        by[f["signature"]].append(f)
    return sorted(({"signature": k, "count": len(v), "examples": [x["company"] for x in v[:4]]}
                   for k, v in by.items()), key=lambda c: -c["count"])
