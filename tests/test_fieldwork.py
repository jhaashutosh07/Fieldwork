import os
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
