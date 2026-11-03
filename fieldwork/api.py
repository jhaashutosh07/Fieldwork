"""FastAPI service: try the agent, clean a CSV, browse eval runs, traces and failure clusters, view insights.

Public-demo hardening (rate limits per real client, bounded concurrency and LLM spend, request-size limits,
security headers, read-only writes) lives in security.py; SECURITY.md describes the threat model.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path as FsPath

from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import flywheel, security, store
from .agent import LATEST, VERSIONS, Agent
from .evals import clusters
from .llm import LLM
from .security import AgentSlot, limiter
from .tools import Tools, world
from .trace import Trace

STATIC = FsPath(__file__).parent / "static"
log = logging.getLogger("uvicorn.error")
PUBLIC = os.environ.get("FIELDWORK_PUBLIC") == "1"   # hosted demo: read-only writes, bounded storage
# Live LLM for visitor requests only when the operator opts in and provides a key; capped per request and per day;
# answers are never written to the eval cache.
TRY_LIVE = os.environ.get("FIELDWORK_TRY_LIVE") == "1" and bool(os.environ.get("GROQ_API_KEY"))
TRY_DAILY_CAP = int(os.environ.get("FIELDWORK_TRY_CAP", "300"))
LLM_CALLS_PER_REQUEST = {"try": 4, "batch": 25}
_llm_day = {"day": "", "fresh_calls": 0}
_llm_lock = threading.Lock()
_llms: dict[str, LLM] = {}
_run_lock = threading.Lock()

RUN_ID = Path(pattern=r"^[0-9a-f]{12}$")
FAILURE_ID = Path(pattern=r"^[0-9a-f]{12}-[0-9]{3}$")

app = FastAPI(title="Fieldwork", version="0.6.0", description="Account-research agent with a trace-to-eval flywheel.")
app.middleware("http")(security.security_headers)
app.add_middleware(security.BodyLimitMiddleware)


# ---------- LLM budget ----------
def _shared_llm(live: bool) -> LLM:
    mode = "groq" if live else "replay"
    if mode not in _llms:
        _llms[mode] = LLM(mode, record=False, retries=2)
    return _llms[mode]


class BudgetedLLM:
    """Per-request view of the shared client: recorded answers are free; new (paid) calls stop at the request's
    budget and at the day's remaining cap, after which the agent falls back to rules and human review."""

    def __init__(self, llm: LLM, budget: int):
        self._llm, self.budget, self.fresh = llm, budget, 0
        self.mode, self.model = llm.mode, llm.model

    @property
    def enabled(self) -> bool:
        return self._llm.enabled

    def json(self, system: str, user: str, trace=None, purpose: str = ""):
        if not self._llm.is_recorded(system, user):
            with _llm_lock:
                if self.fresh >= self.budget or _llm_day["fresh_calls"] >= TRY_DAILY_CAP:
                    return None
                self.fresh += 1
                _llm_day["fresh_calls"] += 1
        return self._llm.json(system, user, trace, purpose)


def _run_rows(rows: list[dict], kind: str, on_event=None) -> dict:
    """Run the shipped agent on visitor rows inside a concurrency slot; live LLM if enabled, else (or on failure)
    recorded answers only."""
    day = time.strftime("%Y-%m-%d")
    with _llm_lock:
        if _llm_day["day"] != day:
            _llm_day.update(day=day, fresh_calls=0)
        live = TRY_LIVE and _llm_day["fresh_calls"] < TRY_DAILY_CAP
    note = ""
    with AgentSlot():
        for attempt_live in ([True, False] if live else [False]):
            trace = Trace(on_event)
            llm = BudgetedLLM(_shared_llm(attempt_live), LLM_CALLS_PER_REQUEST[kind] if attempt_live else 0)
            try:
                accts = Agent(VERSIONS[LATEST], Tools(world(), trace), llm, trace).run([dict(r) for r in rows])
                break
            except Exception as exc:   # live model unavailable or rate limited: fall back to recorded answers
                log.warning("live LLM failed: %s: %s", type(exc).__name__, str(exc)[:300])
                note = f"live model unavailable ({type(exc).__name__}); used recorded answers only"
                if on_event:
                    on_event({"kind": "reset", "note": note})
    if attempt_live and llm.fresh >= llm.budget:
        note = note or f"live LLM budget for this request reached ({llm.budget} calls); the rest used rules"
    return {"accounts": accts, "trace": trace, "llm": "live" if attempt_live else "recorded only", "note": note}


# ---------- try the agent ----------
class TryRequest(BaseModel):
    account_name: str = Field(min_length=1, max_length=120)
    website: str = Field("", max_length=200)
    hq_country: str = Field("", max_length=60)


def _try_row(req: TryRequest) -> dict:
    return {"crm_id": "TRY-1", "account_name": req.account_name.strip(), "website": req.website.strip(),
            "hq_country": req.hq_country.strip(), "employee_count": "", "last_updated": ""}


def _try_payload(res: dict) -> dict:
    t = res["trace"]
    return {"version": LATEST, "llm": res["llm"], "note": res["note"], "account": res["accounts"][0].to_dict(),
            "steps": t.events, "llm_calls": t.llm_calls, "tool_calls": t.tool_calls}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/config")
def config():
    """What the browser may offer: write actions are hidden on the read-only public demo."""
    return {"read_only": PUBLIC, "live_llm": TRY_LIVE, "version": LATEST, "batch_max_rows": BATCH_MAX_ROWS}


@app.post("/api/try")
def try_it(req: TryRequest, request: Request):
    """Run the shipped agent on one CRM row typed by the visitor and return the account plus every step it took."""
    limiter.check(request, "try", per_minute=20, burst=5)
    return _try_payload(_run_rows([_try_row(req)], "try"))


@app.post("/api/try/stream")
def try_stream(req: TryRequest, request: Request):
    """Same as /api/try, streamed as server-sent events: one `step` event per tool call, LLM call or decision as it
    happens, then a `done` event with the account."""
    limiter.check(request, "try", per_minute=20, burst=5)
    q: queue.Queue = queue.Queue(maxsize=1000)
    row = _try_row(req)

    def work():
        try:
            q.put(("done", _try_payload(_run_rows([row], "try", on_event=lambda e: q.put(("step", e))))))
        except HTTPException as exc:
            q.put(("error", {"error": exc.detail}))
        except Exception as exc:   # never leak internals to the client
            log.warning("stream failed: %s", type(exc).__name__)
            q.put(("error", {"error": "internal error"}))

    threading.Thread(target=work, daemon=True).start()

    def events():
        deadline = time.monotonic() + 90
        while True:
            try:
                kind, data = q.get(timeout=max(1.0, deadline - time.monotonic()))
            except queue.Empty:
                yield 'event: error\ndata: {"error": "timeout"}\n\n'
                return
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            if kind in ("done", "error"):
                return

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- clean a CSV ----------
BATCH_MAX_ROWS = 50
_HEADER_ALIASES = {"account_name": ("account_name", "account", "name", "company", "company_name"),
                   "website": ("website", "domain", "url", "site"),
                   "hq_country": ("hq_country", "country", "hq"),
                   "employee_count": ("employee_count", "employees", "headcount"),
                   "last_updated": ("last_updated", "updated", "as_of", "date")}


class BatchRequest(BaseModel):
    csv: str = Field(min_length=1, max_length=60_000)


def _parse_csv(text: str) -> list[dict]:
    try:
        reader = csv.DictReader(io.StringIO(text.replace("\x00", "").strip()))
        fieldnames = reader.fieldnames
        if not fieldnames:
            raise HTTPException(400, "empty CSV")
        cols = {h.strip().lower(): h for h in fieldnames if h}
        pick = {k: next((cols[a] for a in aliases if a in cols), None) for k, aliases in _HEADER_ALIASES.items()}
        if not pick["account_name"]:
            raise HTTPException(400, "the CSV needs a column named account_name, name or company")

        def cell(r: dict, key: str, limit: int) -> str:
            v = r.get(pick[key]) if pick[key] else ""
            return (v if isinstance(v, str) else "").strip()[:limit]

        rows = []
        for i, r in enumerate(reader, 1):
            if i > BATCH_MAX_ROWS + 1:
                break
            name = cell(r, "account_name", 120)
            if not name:
                continue
            emp = cell(r, "employee_count", 20).replace(",", "")
            rows.append({"crm_id": f"ROW-{i:03d}", "account_name": name, "website": cell(r, "website", 200),
                         "hq_country": cell(r, "hq_country", 60), "employee_count": emp if emp.isdigit() else "",
                         "last_updated": cell(r, "last_updated", 10)})
    except csv.Error:
        raise HTTPException(400, "could not parse the CSV")
    if not rows:
        raise HTTPException(400, "no rows with an account name")
    if len(rows) > BATCH_MAX_ROWS:
        raise HTTPException(400, f"at most {BATCH_MAX_ROWS} rows per request in the demo")
    return rows


@app.post("/api/batch")
def batch(req: BatchRequest, request: Request):
    """Clean a small CRM export: merge duplicates, resolve each account, fill current facts with provenance, judge
    fit, and flag what needs a human. Returns one entry per account with the CRM rows it came from."""
    limiter.check(request, "batch", per_minute=4, burst=2)
    rows = _parse_csv(req.csv)
    t0 = time.perf_counter()
    res = _run_rows(rows, "batch")
    by_id = {r["crm_id"]: r for r in rows}
    out = []
    for a in res["accounts"]:
        f = a.to_dict()["fields"]
        out.append({"key": a.key, "rows": [by_id[i] for i in a.crm_ids], "company_id": a.company_id,
                    "name": f.get("name", {}).get("value"), "ticker": f.get("ticker", {}).get("value"),
                    "domain": f.get("domain"), "employees": f.get("employees"), "country": f.get("country"),
                    "industries": a.industries, "icp_fit": a.icp_fit, "icp_reason": a.icp_reason,
                    "confidence": a.confidence, "needs_review": a.needs_review, "review_reason": a.review_reason,
                    "resolved_via": a.resolved_via})
    t = res["trace"]
    return {"version": LATEST, "llm": res["llm"], "note": res["note"], "accounts": out,
            "summary": {"rows": len(rows), "accounts": len(out),
                        "merged_rows": sum(len(x["rows"]) - 1 for x in out if len(x["rows"]) > 1),
                        "needs_review": sum(1 for x in out if x["needs_review"]),
                        "target_customers": sum(1 for x in out if x["icp_fit"] and not x["needs_review"]),
                        "llm_calls": t.llm_calls, "tool_calls": t.tool_calls,
                        "seconds": round(time.perf_counter() - t0, 2)}}


# ---------- evals ----------
class RunRequest(BaseModel):
    version: str = Field(LATEST, max_length=8)
    split: str = Field("test", max_length=8)
    llm: str = Field("replay", max_length=8)


@app.get("/api/versions")
def versions():
    return [{"version": k, "summary": p.summary} for k, p in VERSIONS.items()]


@app.get("/api/history")
def history():
    return json.loads(flywheel.HISTORY.read_text()) if flywheel.HISTORY.exists() else []


@app.get("/api/insights")
def insights():
    from .insights import INSIGHTS
    return json.loads(INSIGHTS.read_text()) if INSIGHTS.exists() else {}


@app.get("/api/runs")
def runs():
    return store.list_runs()


@app.post("/api/runs")
def start_run(req: RunRequest, request: Request):
    if req.version not in VERSIONS or req.split not in ("dev", "test", "all"):
        raise HTTPException(400, "unknown version or split")
    if req.llm not in ("none", "replay"):
        raise HTTPException(400, "the API only runs offline (none) or recorded (replay) LLM modes")
    limiter.check(request, "runs", per_minute=6, burst=2)
    if not _run_lock.acquire(timeout=20):
        raise HTTPException(503, "busy, try again", headers={"Retry-After": "5"})
    try:
        with AgentSlot():
            res = flywheel.run(req.version, req.split, req.llm)
        if PUBLIC:
            protect = {e[k] for e in history() for k in ("dev_run", "test_run")}
            store.prune_runs(keep=100, protect=protect)
    finally:
        _run_lock.release()
    return {"id": res["id"], "metrics": res["metrics"]}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str = RUN_ID):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return {**run, "clusters": clusters(store.get_failures(run_id))}


@app.get("/api/runs/{run_id}/failures")
def run_failures(run_id: str = RUN_ID, signature: str | None = None):
    return store.get_failures(run_id, signature[:64] if signature else None)


@app.get("/api/runs/{run_id}/accounts")
def run_accounts(run_id: str = RUN_ID):
    return store.get_accounts(run_id)


@app.get("/api/runs/{run_id}/trace")
def run_trace(run_id: str = RUN_ID, account: str | None = None):
    return store.get_trace(run_id, account[:64] if account else None)


@app.post("/api/failures/{failure_id}/promote")
def promote(request: Request, failure_id: str = FAILURE_ID):
    if PUBLIC:
        security.require_admin(request)
    try:
        return {"case": flywheel.promote(failure_id)}
    except KeyError:
        raise HTTPException(404, "failure not found")


@app.get("/api/regressions")
def regressions():
    return [{"id": c["id"], "signature": c["signature"], "company": c["company"], "created": c["created"],
             "detail": c["detail"]} for c in flywheel.load_cases()]


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
