"""FastAPI service: start runs, browse traces and failure clusters, promote failures, view eval history."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import flywheel, store
from .agent import LATEST, VERSIONS, Agent
from .evals import clusters
from .llm import LLM
from .tools import Tools, world
from .trace import Trace

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("uvicorn.error")
PUBLIC = os.environ.get("FIELDWORK_PUBLIC") == "1"   # hosted demo: one run at a time, bounded storage
_run_lock = threading.Lock()
_last_run: dict[str, float] = {}
# Try-it: live LLM calls only when the operator opts in and provides a key; capped per day; never recorded.
TRY_LIVE = os.environ.get("FIELDWORK_TRY_LIVE") == "1" and bool(os.environ.get("GROQ_API_KEY"))
TRY_DAILY_CAP = int(os.environ.get("FIELDWORK_TRY_CAP", "300"))
_try = {"day": "", "llm_calls": 0, "last": {}, "llm": {}}
app = FastAPI(title="Fieldwork", version="0.5.0",
              description="Account-research agent with a trace-to-eval flywheel.")


class TryRequest(BaseModel):
    account_name: str = Field(min_length=1, max_length=120)
    website: str = Field("", max_length=200)
    hq_country: str = Field("", max_length=60)


def _try_llm(live: bool) -> LLM:
    mode = "groq" if live else "replay"
    if mode not in _try["llm"]:
        _try["llm"][mode] = LLM(mode, record=False, retries=2)
    return _try["llm"][mode]


class RunRequest(BaseModel):
    version: str = LATEST
    split: str = "test"
    llm: str = "replay"


@app.get("/healthz")
def healthz():
    return {"ok": True, "runs": store.count_runs()}


def _client_ok(request: Request, bucket: str, gap: float) -> None:
    ip = request.client.host if request.client else "?"
    key = f"{bucket}:{ip}"
    if time.time() - _try["last"].get(key, 0) < gap:
        raise HTTPException(429, f"one request every {gap:g} seconds, please")
    _try["last"][key] = time.time()


def _run_rows(rows: list[dict], on_event=None) -> dict:
    """Run the shipped agent; live LLM if enabled and under the daily cap, else (or on failure) recorded answers."""
    day = time.strftime("%Y-%m-%d")
    if _try["day"] != day:
        _try["day"], _try["llm_calls"] = day, 0
    live = TRY_LIVE and _try["llm_calls"] < TRY_DAILY_CAP
    note = ""
    for attempt_live in ([True, False] if live else [False]):
        trace = Trace(on_event)
        try:
            accts = Agent(VERSIONS[LATEST], Tools(world(), trace), _try_llm(attempt_live), trace).run(
                [dict(r) for r in rows])
            break
        except Exception as exc:   # live model unavailable or rate limited: fall back to recorded answers
            log.warning("live LLM failed: %s: %s", type(exc).__name__, str(exc)[:300])
            note = f"live model unavailable ({type(exc).__name__}); used recorded answers only"
            if on_event:
                on_event({"kind": "reset", "note": note})
    if attempt_live:
        _try["llm_calls"] += trace.llm_calls
    return {"accounts": accts, "trace": trace, "llm": "live" if attempt_live else "recorded only", "note": note}


def _try_row(req: TryRequest) -> dict:
    return {"crm_id": "TRY-1", "account_name": req.account_name.strip(), "website": req.website.strip(),
            "hq_country": req.hq_country.strip(), "employee_count": "", "last_updated": ""}


def _try_payload(res: dict) -> dict:
    t = res["trace"]
    return {"version": LATEST, "llm": res["llm"], "note": res["note"], "account": res["accounts"][0].to_dict(),
            "steps": t.events, "llm_calls": t.llm_calls, "tool_calls": t.tool_calls}


@app.post("/api/try")
def try_it(req: TryRequest, request: Request):
    """Run the shipped agent on one CRM row typed by the visitor and return the account plus every step it took."""
    _client_ok(request, "try", 2)
    return _try_payload(_run_rows([_try_row(req)]))


@app.post("/api/try/stream")
def try_stream(req: TryRequest, request: Request):
    """Same as /api/try, streamed as server-sent events: one `step` event per tool call, LLM call or decision as it
    happens, then a `done` event with the account."""
    _client_ok(request, "try", 2)
    q: queue.Queue = queue.Queue()
    row = _try_row(req)

    def work():
        try:
            q.put(("done", _try_payload(_run_rows([row], on_event=lambda e: q.put(("step", e))))))
        except Exception as exc:   # surfaced to the client as an error event
            q.put(("error", {"error": type(exc).__name__}))

    threading.Thread(target=work, daemon=True).start()

    def events():
        while True:
            try:
                kind, data = q.get(timeout=120)
            except queue.Empty:
                yield 'event: error\ndata: {"error": "timeout"}\n\n'
                return
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            if kind in ("done", "error"):
                return

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


BATCH_MAX_ROWS = 50
_HEADER_ALIASES = {"account_name": ("account_name", "account", "name", "company", "company_name"),
                   "website": ("website", "domain", "url", "site"),
                   "hq_country": ("hq_country", "country", "hq"),
                   "employee_count": ("employee_count", "employees", "headcount"),
                   "last_updated": ("last_updated", "updated", "as_of", "date")}


class BatchRequest(BaseModel):
    csv: str = Field(min_length=1, max_length=60_000)


def _parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.strip()))
    if not reader.fieldnames:
        raise HTTPException(400, "empty CSV")
    cols = {h.strip().lower(): h for h in reader.fieldnames if h}
    pick = {k: next((cols[a] for a in aliases if a in cols), None) for k, aliases in _HEADER_ALIASES.items()}
    if not pick["account_name"]:
        raise HTTPException(400, "the CSV needs a column named account_name, name or company")

    def cell(r: dict, key: str, limit: int) -> str:
        return (r.get(pick[key]) or "").strip()[:limit] if pick[key] else ""

    rows = []
    for i, r in enumerate(reader, 1):
        name = cell(r, "account_name", 120)
        if not name:
            continue
        emp = cell(r, "employee_count", 20).replace(",", "")
        rows.append({"crm_id": f"ROW-{i:03d}", "account_name": name, "website": cell(r, "website", 200),
                     "hq_country": cell(r, "hq_country", 60), "employee_count": emp if emp.isdigit() else "",
                     "last_updated": cell(r, "last_updated", 10)})
    if not rows:
        raise HTTPException(400, "no rows with an account name")
    if len(rows) > BATCH_MAX_ROWS:
        raise HTTPException(400, f"at most {BATCH_MAX_ROWS} rows per request in the demo")
    return rows


@app.post("/api/batch")
def batch(req: BatchRequest, request: Request):
    """Clean a small CRM export: merge duplicates, resolve each account, fill current facts with provenance, judge
    fit, and flag what needs a human. Returns one entry per account with the CRM rows it came from."""
    _client_ok(request, "batch", 8 if PUBLIC else 0)
    rows = _parse_csv(req.csv)
    t0 = time.perf_counter()
    res = _run_rows(rows)
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
    if PUBLIC:
        ip = request.client.host if request.client else "?"
        if time.time() - _last_run.get(ip, 0) < 5:
            raise HTTPException(429, "one run every 5 seconds, please")
        _last_run[ip] = time.time()
    if not _run_lock.acquire(timeout=30):
        raise HTTPException(429, "busy, try again")
    try:
        res = flywheel.run(req.version, req.split, req.llm)
        if PUBLIC:
            protect = {e[k] for e in history() for k in ("dev_run", "test_run")}
            store.prune_runs(keep=100, protect=protect)
    finally:
        _run_lock.release()
    return {"id": res["id"], "metrics": res["metrics"]}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return {**run, "clusters": clusters(store.get_failures(run_id))}


@app.get("/api/runs/{run_id}/failures")
def run_failures(run_id: str, signature: str | None = None):
    return store.get_failures(run_id, signature)


@app.get("/api/runs/{run_id}/accounts")
def run_accounts(run_id: str):
    return store.get_accounts(run_id)


@app.get("/api/runs/{run_id}/trace")
def run_trace(run_id: str, account: str | None = None):
    return store.get_trace(run_id, account)


@app.post("/api/failures/{failure_id}/promote")
def promote(failure_id: str):
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
