"""FastAPI service: start runs, browse traces and failure clusters, promote failures, view eval history."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import flywheel, store
from .agent import LATEST, VERSIONS
from .evals import clusters

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Fieldwork", version="0.5.0",
              description="Account-research agent with a trace-to-eval flywheel.")


class RunRequest(BaseModel):
    version: str = LATEST
    split: str = "test"
    llm: str = "replay"


@app.get("/api/versions")
def versions():
    return [{"version": k, "summary": p.summary} for k, p in VERSIONS.items()]


@app.get("/api/history")
def history():
    return json.loads(flywheel.HISTORY.read_text()) if flywheel.HISTORY.exists() else []


@app.get("/api/runs")
def runs():
    return store.list_runs()


@app.post("/api/runs")
def start_run(req: RunRequest):
    if req.version not in VERSIONS or req.split not in ("dev", "test", "all"):
        raise HTTPException(400, "unknown version or split")
    if req.llm not in ("none", "replay"):
        raise HTTPException(400, "the API only runs offline (none) or recorded (replay) LLM modes")
    res = flywheel.run(req.version, req.split, req.llm)
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
