"""fieldwork CLI.

  fieldwork run [--version v5] [--split test] [--llm none|replay|groq|openai]
  fieldwork triage <run_id>                 failure clusters for a run
  fieldwork promote <failure_id>            freeze a failure as a regression case
  fieldwork gate [--update-baseline]        CI gate: held-out metrics + every regression case
  fieldwork history [--llm replay]          evaluate every version on dev and test -> evals/history.json
  fieldwork serve [--port 8000]             API + dashboard
"""
from __future__ import annotations

import argparse
import sys

from . import flywheel, store
from .agent import LATEST, VERSIONS
from .evals import clusters

HEADLINE = ("verified_accuracy", "silent_error_rate", "review_rate", "entity_accuracy", "dedup_f1", "icp_precision",
            "icp_recall", "llm_calls", "cost_usd")


def _print_metrics(m: dict) -> None:
    for k in HEADLINE:
        v = m.get(k)
        print(f"  {k:<20} {v:.4f}" if isinstance(v, float) else f"  {k:<20} {v}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fieldwork")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--version", default=LATEST, choices=list(VERSIONS))
    r.add_argument("--split", default="test", choices=["dev", "test", "all"])
    r.add_argument("--llm", default=None)
    t = sub.add_parser("triage")
    t.add_argument("run_id")
    t.add_argument("--signature")
    p = sub.add_parser("promote")
    p.add_argument("failure_id")
    g = sub.add_parser("gate")
    g.add_argument("--version", default=LATEST, choices=list(VERSIONS))
    g.add_argument("--llm", default=None)
    g.add_argument("--update-baseline", action="store_true")
    h = sub.add_parser("history")
    h.add_argument("--llm", default=None)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        res = flywheel.run(a.version, a.split, a.llm)
        print(f"run {res['id']}  {a.version} on {a.split}  ({res['duration_ms']:.0f} ms, llm={res['llm_mode']})")
        _print_metrics(res["metrics"])
        print("failure clusters:")
        for c in clusters(res["failures"]):
            print(f"  {c['count']:>3}  {c['signature']:<30} e.g. {', '.join(c['examples'][:3])}")
        print(f"next: fieldwork triage {res['id']}")
    elif a.cmd == "triage":
        fails = store.get_failures(a.run_id, a.signature)
        if not fails:
            print("no failures (or unknown run id)")
        for c in clusters(fails):
            print(f"\n{c['signature']}  ({c['count']})")
            for f in [x for x in fails if x["signature"] == c["signature"]][:8]:
                tag = f"  -> {f['promoted_case']}" if f["promoted_case"] else ""
                names = ", ".join(r["account_name"] for r in f["rows"][:2])
                print(f"  {f['id']}  {f['company']:<34} rows: {names[:40]:<40} {f['detail'][:70]}{tag}")
    elif a.cmd == "promote":
        print("regression case:", flywheel.promote(a.failure_id))
    elif a.cmd == "gate":
        ok, lines = flywheel.gate(a.version, a.llm, a.update_baseline)
        print("\n".join(lines))
        print("GATE PASSED" if ok else "GATE FAILED")
        return 0 if ok else 1
    elif a.cmd == "history":
        for e in flywheel.history(a.llm):
            m = e["test"]
            print(f"{e['version']}  verified {m['verified_accuracy']:.1%}  silent errors {m['silent_error_rate']:.1%}  "
                  f"review {m['review_rate']:.1%}  cases {e['regression_cases']}  ({e['llm_mode']})  {e['summary']}")
    elif a.cmd == "serve":
        import uvicorn
        uvicorn.run("fieldwork.api:app", host=a.host, port=a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
