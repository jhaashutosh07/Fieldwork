import os
from pathlib import Path
import tempfile

os.environ.setdefault("FIELDWORK_DB", f"sqlite:///{tempfile.mkdtemp()}/test.db")
os.environ["FIELDWORK_LLM"] = "replay"

from fieldwork import flywheel  # noqa: E402
from fieldwork.agent import LATEST, Account, FieldValue  # noqa: E402
from fieldwork.evals import gold, rows_for, score, split_of  # noqa: E402
from fieldwork.tools import name_score, normalize, world  # noqa: E402


def test_normalize_strips_legal_forms():
    assert normalize("Embraer S.A.") == normalize("Embraer")
    assert normalize("CureVac GmbH") == "curevac"
    assert normalize("AT&T Inc.") == "at and t"


def test_name_score_handles_dropped_words_but_not_generic_ones():
    assert name_score("Marvell", "Marvell Technology") >= 0.9
    assert name_score("Conoco Phillips", "ConocoPhillips") >= 0.95
    # "International" alone must not identify "CACI International"
    assert name_score("International", "CACI International") < 0.9


def _oracle(rows):
    """Perfect output built from gold: the eval must score it 100% with no silent errors."""
    g = gold()
    by_co = {}
    for r in rows:
        by_co.setdefault(g["rows"][r["crm_id"]], []).append(r["crm_id"])
    out = []
    for co, ids in by_co.items():
        gc = g["companies"][co]
        a = Account(key=f"co:{co}", crm_ids=ids, company_id=co, entity_confidence=1.0, confidence=1.0,
                    icp_fit=gc["icp_fit"])
        a.fields = {"domain": FieldValue(gc["domains"][0], "gold", None, 1.0),
                    "employees": FieldValue(gc["employees"][0], "gold", None, 1.0),
                    "country": FieldValue((gc["countries"] or [None])[0], "gold", None, 1.0)}
        out.append(a)
    return out


def test_eval_scores_oracle_perfectly():
    rows = rows_for("test")
    m, fails = score(_oracle(rows), rows)
    assert m["verified_accuracy"] == 1.0
    assert m["silent_error_rate"] == 0.0
    assert m["dedup_f1"] == 1.0
    assert fails == []


def test_eval_catches_plausible_but_stale_headcount():
    rows = rows_for("test")
    accts = _oracle(rows)
    for victim in accts:
        current = gold()["companies"][victim.company_id]["employees"]
        older = [e["value"] for e in world().profiles[victim.company_id]["employees"] if e["value"] not in current]
        if older:
            break
    victim.fields["employees"] = FieldValue(older[0], "crm", None, 0.9)
    m, fails = score(accts, rows)
    assert m["silent_error_rate"] > 0
    assert any(f["signature"] == "stale_headcount" and f["company_id"] == victim.company_id for f in fails)


def test_split_is_disjoint_and_stable():
    ids = list(gold()["companies"])
    assert {split_of(i) for i in ids} == {"dev", "test"}
    assert all(split_of(i) == split_of(i) for i in ids)


def test_every_regression_case_passes_on_latest():
    for case in flywheel.load_cases():
        ok, why = flywheel.check_case(case, LATEST)
        assert ok, f"{case['id']}: {why}"


def test_api_run_roundtrip():
    from fastapi.testclient import TestClient
    from fieldwork.api import app

    c = TestClient(app)
    assert c.get("/api/versions").status_code == 200
    r = c.post("/api/runs", json={"version": "v4", "split": "dev", "llm": "none"})
    assert r.status_code == 200
    run_id = r.json()["id"]
    detail = c.get(f"/api/runs/{run_id}").json()
    assert detail["metrics"]["verified_accuracy"] > 0.8
    assert c.get(f"/api/runs/{run_id}/trace").json()


def test_wilson_interval_is_honest_at_zero_errors():
    from fieldwork.insights import wilson_ci
    lo, hi = wilson_ci(0, 150)
    assert lo == 0.0 and 0.015 < hi < 0.03


def test_paired_delta_detects_a_real_gain_and_ignores_noise():
    from fieldwork.insights import paired_delta
    a = {f"c{i}": {"verified": int(i % 2 == 0), "asserted": 1, "wrong": 0, "review_rows": 0, "rows": 1} for i in range(200)}
    better = {k: {**v, "verified": 1} for k, v in a.items()}
    assert paired_delta(a, better, "verified_accuracy")["ci"][0] > 0
    assert paired_delta(a, a, "verified_accuracy")["p"] == 1.0


def test_stress_strip_rate_is_stable():
    from fieldwork.insights import strip_websites
    rows = rows_for("test")
    half = strip_websites(rows, 0.5)
    assert strip_websites(rows, 0.5) == half
    assert all(r["website"] == "" for r in strip_websites(rows, 1.0))
    with_site = sum(1 for r in rows if r["website"])
    assert 0.3 * with_site < sum(1 for r in half if r["website"]) < 0.7 * with_site


def test_try_endpoint_resolves_an_old_website_and_refuses_gibberish():
    from fastapi.testclient import TestClient
    from fieldwork.api import app
    from fieldwork.security import limiter

    c = TestClient(app)
    r = c.post("/api/try", json={"account_name": "adobe", "website": "business.adobe.com", "hq_country": "United States"})
    assert r.status_code == 200
    acct = r.json()["account"]
    assert acct["fields"]["domain"]["value"] == "adobe.com" and not acct["needs_review"]
    assert r.json()["steps"]
    limiter.reset()
    r = c.post("/api/try", json={"account_name": "qzxwvplmk"})
    assert r.json()["account"]["needs_review"]
    limiter.reset()
    assert c.post("/api/try", json={"account_name": ""}).status_code == 422


def test_try_stream_sends_steps_then_the_account():
    from fastapi.testclient import TestClient
    from fieldwork.api import app
    from fieldwork.security import limiter

    limiter.reset()
    with TestClient(app).stream("POST", "/api/try/stream", json={"account_name": "adobe", "website": "business.adobe.com"}) as r:
        body = "".join(r.iter_text())
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert body.count("event: step") >= 3
    assert body.rstrip().splitlines()[-1].startswith("data:") and "event: done" in body
    assert body.index("event: step") < body.index("event: done")


def test_batch_merges_duplicates_and_flags_what_it_cannot_verify():
    from fastapi.testclient import TestClient
    from fieldwork.api import app
    from fieldwork.security import limiter

    limiter.reset()
    csv_text = ("name,website,country\n"
                "adobe,business.adobe.com,United States\n"
                "ADBE,business.adobe.com,United States\n"
                "qzxwvplmk,,\n")
    d = TestClient(app).post("/api/batch", json={"csv": csv_text}).json()
    assert d["summary"] == {**d["summary"], "rows": 3, "accounts": 2, "merged_rows": 1, "needs_review": 1}
    adobe = next(a for a in d["accounts"] if not a["needs_review"])
    assert adobe["domain"]["value"] == "adobe.com" and len(adobe["rows"]) == 2


def test_batch_rejects_bad_input():
    from fastapi.testclient import TestClient
    from fieldwork.api import app
    from fieldwork.security import limiter

    c = TestClient(app)
    limiter.reset()
    assert c.post("/api/batch", json={"csv": "foo,bar\n1,2"}).status_code == 400
    limiter.reset()
    too_many = "name\n" + "\n".join(f"Company {i}" for i in range(60))
    assert c.post("/api/batch", json={"csv": too_many}).status_code == 400


def test_security_headers_on_pages_and_api():
    from fastapi.testclient import TestClient
    from fieldwork.api import app

    c = TestClient(app)
    for path in ("/", "/api/versions", "/static/app.js"):
        h = c.get(path).headers
        assert "script-src 'self' https://cdnjs.cloudflare.com" in h["content-security-policy"]
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert "'unsafe-inline'" not in c.get("/").headers["content-security-policy"].split("script-src")[1].split(";")[0]
    assert c.get("/api/history").headers["cache-control"] == "no-store"


def test_page_has_no_inline_script_and_pins_cdn_integrity():
    from fieldwork.api import STATIC

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "onclick=" not in html
    assert 'integrity="sha512-' in html and 'crossorigin="anonymous"' in html


def test_oversized_body_is_rejected():
    from fastapi.testclient import TestClient
    from fieldwork.api import app

    r = TestClient(app).post("/api/batch", content=b"x" * (200 * 1024), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_promote_is_admin_only_on_the_public_demo(monkeypatch):
    from fastapi.testclient import TestClient
    from fieldwork import api, security

    monkeypatch.setattr(api, "PUBLIC", True)
    monkeypatch.setattr(security, "ADMIN_TOKEN", "s3cret-token")
    c = TestClient(api.app)
    assert c.post("/api/failures/0123456789ab-000/promote").status_code == 403
    assert c.post("/api/failures/0123456789ab-000/promote", headers={"x-admin-token": "wrong"}).status_code == 403
    assert c.post("/api/failures/0123456789ab-000/promote", headers={"x-admin-token": "s3cret-token"}).status_code == 404
    assert c.post("/api/failures/../../etc/promote").status_code in (404, 422)


def test_rate_limit_uses_rightmost_forwarded_ip(monkeypatch):
    from fastapi.testclient import TestClient
    from fieldwork import security
    from fieldwork.api import app

    monkeypatch.setattr(security, "TRUST_PROXY", True)
    limiter = security.limiter
    limiter.reset()
    c = TestClient(app)
    body = {"csv": "name\nadobe"}
    # a client spoofing the left-most X-Forwarded-For value is still identified by the proxy-appended address
    codes = [c.post("/api/batch", json=body, headers={"x-forwarded-for": f"1.1.1.{i}, 9.9.9.9"}).status_code for i in range(3)]
    assert codes[:2] == [200, 200] and codes[2] == 429
    assert c.post("/api/batch", json=body, headers={"x-forwarded-for": "9.9.9.10"}).status_code == 200
    limiter.reset()


def test_live_llm_budget_stops_paid_calls():
    from fieldwork.api import BudgetedLLM

    class Fake:
        mode, model, enabled = "groq", "m", True
        def is_recorded(self, s, u): return u == "known"
        def json(self, s, u, trace=None, purpose=""): return {"ok": u}

    b = BudgetedLLM(Fake(), budget=1)
    assert b.json("s", "new-1") == {"ok": "new-1"}
    assert b.json("s", "new-2") is None            # budget spent: no more paid calls
    assert b.json("s", "known") == {"ok": "known"}  # recorded answers stay free


def test_cleaned_csv_cells_are_formula_safe():
    js = (Path(__file__).resolve().parents[1] / "fieldwork" / "static" / "app.js").read_text(encoding="utf-8")
    assert r"/^[=+\-@\t\r]/" in js


def test_langgraph_pipeline_matches_the_shipped_pipeline_exactly():
    from fieldwork.agent import VERSIONS, Agent
    from fieldwork.graph_agent import PipelineGraph
    from fieldwork.llm import LLM
    from fieldwork.tools import Tools
    from fieldwork.trace import Trace

    def sig(accts):
        return sorted((a.key, tuple(a.crm_ids), a.company_id, a.needs_review, a.icp_fit,
                       tuple((k, str(v.value)) for k, v in sorted(a.fields.items()))) for a in accts)

    rows, llm = rows_for("test"), LLM("replay")
    t1, t2 = Trace(), Trace()
    plain = Agent(VERSIONS[LATEST], Tools(world(), t1), llm, t1).run(rows)
    graph = PipelineGraph(VERSIONS[LATEST], Tools(world(), t2), llm, t2).run(rows)
    assert sig(plain) == sig(graph) and t1.llm_calls == t2.llm_calls


def test_review_memory_is_private_per_session_and_teaches_the_agent():
    import uuid
    from fastapi.testclient import TestClient
    from fieldwork.api import app
    from fieldwork.security import limiter

    limiter.reset()
    c = TestClient(app)
    me, other = {"x-fieldwork-session": str(uuid.uuid4())}, {"x-fieldwork-session": str(uuid.uuid4())}
    r = c.post("/api/try", json={"account_name": "Totsuko", "hq_country": "Japan"}, headers=me).json()
    assert r["account"]["needs_review"] and r["queued_for_review"] == 1
    item = next(i for i in c.get("/api/review", headers=me).json()["items"] if i["row"]["account_name"] == "Totsuko")
    sony = next(h["id"] for h in c.get("/api/companies?q=Sony", headers=me).json() if h["name"].startswith("Sony"))
    assert c.post(f"/api/review/{item['id']}/decide", json={"company_id": sony}, headers=other).status_code == 404
    assert c.post(f"/api/review/{item['id']}/decide", json={"company_id": "Q0"}, headers=me).status_code == 400
    assert c.post(f"/api/review/{item['id']}/decide", json={"company_id": sony}, headers=me).status_code == 200
    limiter.reset()
    mine = c.post("/api/try", json={"account_name": "Totsuko", "hq_country": "Japan"}, headers=me).json()["account"]
    assert mine["company_id"] == sony and mine["resolved_via"] == "review_memory"
    limiter.reset()
    theirs = c.post("/api/try", json={"account_name": "Totsuko", "hq_country": "Japan"}, headers=other).json()["account"]
    assert theirs["needs_review"]   # another visitor's agent did not learn from my decision
    assert c.get("/api/review", headers={"x-fieldwork-session": "not-a-uuid"}).status_code == 400


def test_website_evidence_beats_review_memory():
    from fieldwork.agent import VERSIONS, Agent, memory_key
    from fieldwork.llm import LLM
    from fieldwork.tools import Tools
    from fieldwork.trace import Trace

    row = {"crm_id": "X", "account_name": "adobe", "website": "adobe.com", "hq_country": "United States",
           "employee_count": "", "last_updated": ""}
    t = Trace()
    wrong = {memory_key(row): "Q312"}   # a bad decision in memory must not override the website match
    cid, _, via = Agent(VERSIONS[LATEST], Tools(world(), t), LLM("replay"), t, memory=wrong).resolve(row)
    assert via.startswith("domain") and cid != "Q312"


def test_learning_from_review_reduces_repeat_questions():
    from fieldwork.learning import experiment

    r = experiment("test", "replay")
    r.pop("_outcomes")
    a, b = r["month2_without_memory"], r["month2_with_memory"]
    assert b["repeat_questions"] < a["repeat_questions"] and b["review_rows"] < a["review_rows"]
    assert b["verified_accuracy"] >= a["verified_accuracy"] and b["silent_error_rate"] <= a["silent_error_rate"]


def test_metrics_endpoints_report_traffic():
    from fastapi.testclient import TestClient
    from fieldwork.api import app
    from fieldwork.security import limiter

    limiter.reset()
    c = TestClient(app)
    c.post("/api/batch", json={"csv": "name\nadobe\nqzxwvplmk"})
    m = c.get("/api/metrics?minutes=5").json()
    assert m["window"]["rows"] >= 2 and m["window"]["review_rows"] >= 1 and m["window"]["requests"] >= 1
    assert "fieldwork_rows_total" in c.get("/metrics").text


def test_tool_agent_answers_are_scored_like_the_pipeline():
    from fieldwork.graph_agent import _parse_final, accounts_from_answers
    from fieldwork.tools import Tools
    from fieldwork.trace import Trace

    rows = rows_for("test")[:2]
    g = gold()
    good = {"company_id": g["rows"][rows[0]["crm_id"]], "domain": None, "employees": None, "country": None,
            "icp_fit": False, "needs_review": False, "reason": ""}
    accts = accounts_from_answers(rows, [good, {"company_id": "nonsense", "needs_review": False}], Tools(world(), Trace()))
    assert sum(a.needs_review for a in accts) == 1   # an unknown company id is never asserted
    assert _parse_final('noise {"company_id": null, "needs_review": true} tail') == {"company_id": None, "needs_review": True}
