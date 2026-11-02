"""Trace store: runs, events, accounts and failures. SQLite by default; set FIELDWORK_DB for Postgres."""
from __future__ import annotations

import json
import os

from sqlalchemy import (Column, Float, Integer, MetaData, String, Table, Text, create_engine, delete, func, insert,
                        select, update)
from .paths import ROOT

meta = MetaData()

runs = Table("runs", meta,
             Column("id", String(32), primary_key=True), Column("version", String(16)), Column("split", String(8)),
             Column("llm_mode", String(16)), Column("model", String(64)), Column("started_at", String(32)),
             Column("duration_ms", Float), Column("cost_usd", Float), Column("llm_calls", Integer),
             Column("tool_calls", Integer), Column("metrics", Text))
events = Table("events", meta,
               Column("id", Integer, primary_key=True, autoincrement=True), Column("run_id", String(32), index=True),
               Column("seq", Integer), Column("kind", String(16)), Column("account", String(64), index=True),
               Column("name", String(64)), Column("ms", Float), Column("payload", Text))
accounts = Table("accounts", meta,
                 Column("id", Integer, primary_key=True, autoincrement=True), Column("run_id", String(32), index=True),
                 Column("key", String(64)), Column("payload", Text))
failures = Table("failures", meta,
                 Column("id", String(48), primary_key=True), Column("run_id", String(32), index=True),
                 Column("signature", String(64), index=True), Column("company_id", String(32)),
                 Column("company", String(256)), Column("field", String(32)), Column("payload", Text),
                 Column("promoted_case", String(64)))

_engine = None


def engine():
    global _engine
    if _engine is None:
        url = os.environ.get("FIELDWORK_DB", f"sqlite:///{ROOT / 'fieldwork.db'}")
        _engine = create_engine(url, future=True)
        meta.create_all(_engine)
    return _engine


def save_run(run: dict, trace, accts: list, fails: list[dict]) -> None:
    with engine().begin() as c:
        c.execute(insert(runs).values(**{**run, "metrics": json.dumps(run["metrics"])}))
        if trace.events:
            c.execute(insert(events), [{"run_id": run["id"], "seq": i, "kind": e["kind"], "account": e["account"],
                                        "name": e["name"], "ms": e.get("ms"),
                                        "payload": json.dumps(e, ensure_ascii=False, default=str)}
                                       for i, e in enumerate(trace.events)])
        if accts:
            c.execute(insert(accounts), [{"run_id": run["id"], "key": a.key,
                                          "payload": json.dumps(a.to_dict(), ensure_ascii=False, default=str)}
                                         for a in accts])
        if fails:
            c.execute(insert(failures), [{"id": f"{run['id']}-{i:03d}", "run_id": run["id"],
                                          "signature": f["signature"], "company_id": f["company_id"],
                                          "company": f["company"], "field": f["field"],
                                          "payload": json.dumps(f, ensure_ascii=False, default=str),
                                          "promoted_case": None} for i, f in enumerate(fails)])


def list_runs(limit: int = 50) -> list[dict]:
    with engine().connect() as c:
        rows = c.execute(select(runs).order_by(runs.c.started_at.desc()).limit(limit)).mappings().all()
    return [{**r, "metrics": json.loads(r["metrics"])} for r in rows]


def get_run(run_id: str) -> dict | None:
    with engine().connect() as c:
        r = c.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
    return {**r, "metrics": json.loads(r["metrics"])} if r else None


def get_failures(run_id: str, signature: str | None = None) -> list[dict]:
    q = select(failures).where(failures.c.run_id == run_id)
    if signature:
        q = q.where(failures.c.signature == signature)
    with engine().connect() as c:
        rows = c.execute(q.order_by(failures.c.id)).mappings().all()
    return [{**json.loads(r["payload"]), "id": r["id"], "promoted_case": r["promoted_case"]} for r in rows]


def get_failure(failure_id: str) -> dict | None:
    with engine().connect() as c:
        r = c.execute(select(failures).where(failures.c.id == failure_id)).mappings().first()
    return {**json.loads(r["payload"]), "id": r["id"], "run_id": r["run_id"],
            "promoted_case": r["promoted_case"]} if r else None


def mark_promoted(failure_id: str, case_id: str) -> None:
    with engine().begin() as c:
        c.execute(update(failures).where(failures.c.id == failure_id).values(promoted_case=case_id))


def get_accounts(run_id: str) -> list[dict]:
    with engine().connect() as c:
        rows = c.execute(select(accounts.c.payload).where(accounts.c.run_id == run_id).order_by(accounts.c.id)).all()
    return [json.loads(r[0]) for r in rows]


def get_trace(run_id: str, account: str | None = None) -> list[dict]:
    q = select(events.c.payload).where(events.c.run_id == run_id)
    if account:
        q = q.where(events.c.account == account)
    with engine().connect() as c:
        rows = c.execute(q.order_by(events.c.seq)).all()
    return [json.loads(r[0]) for r in rows]


def count_runs() -> int:
    with engine().connect() as c:
        return c.execute(select(func.count()).select_from(runs)).scalar_one()


def prune_runs(keep: int, protect: set[str]) -> None:
    """Delete the oldest runs beyond `keep`, never the ones in `protect` (the history runs the dashboard links to)."""
    with engine().begin() as c:
        ids = [r[0] for r in c.execute(select(runs.c.id).order_by(runs.c.started_at.desc()))]
        doomed = [i for i in ids[keep:] if i not in protect]
        for t in (events, accounts, failures):
            c.execute(delete(t).where(t.c.run_id.in_(doomed)))
        c.execute(delete(runs).where(runs.c.id.in_(doomed)))
