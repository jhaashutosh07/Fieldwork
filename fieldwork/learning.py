"""Learning from human review: does a reviewer's decision this month stop the same question coming back next month?

Month 1: the shipped agent runs on the CRM export; everything it refuses to guess goes to a review queue. A reviewer
resolves each item (simulated here with the gold answer, i.e. a careful human), and every decision becomes agent
memory keyed by (account name, HQ country). Month 2: next month's export (every month-1 row comes back, plus new rows
reps added) is run twice on the same companies, without and with that memory.
"""
from __future__ import annotations

from .agent import LATEST, VERSIONS, Agent, memory_key
from .evals import gold, rows_for, score
from .llm import LLM
from .tools import Tools, world
from .trace import Trace


def _run(rows: list[dict], memory: dict | None, llm: LLM) -> tuple[list, dict, Trace]:
    trace = Trace()
    accts = Agent(VERSIONS[LATEST], Tools(world(), trace), llm, trace, memory=memory).run([dict(r) for r in rows])
    return accts, score(accts, rows)[0], trace


def simulate_reviewer(accounts: list, rows: list[dict]) -> dict[str, str]:
    """A careful reviewer resolves every item the agent sent to review. Returns the agent memory they create."""
    g, by_id = gold(), {r["crm_id"]: r for r in rows}
    memory = {}
    for a in accounts:
        if a.needs_review:
            for cid in a.crm_ids:
                memory[memory_key(by_id[cid])] = g["rows"][cid]
    return memory


def experiment(split: str = "test", llm_mode: str = "replay") -> dict:
    llm = LLM(llm_mode)
    m1 = rows_for(split, "m1")
    m1_accts, m1_metrics, _ = _run(m1, None, llm)
    memory = simulate_reviewer(m1_accts, m1)
    reviewed_rows = sum(len(a.crm_ids) for a in m1_accts if a.needs_review)

    m2 = rows_for(split, "m2")
    base_accts, base, _ = _run(m2, None, llm)
    mem_accts, with_mem, trace = _run(m2, memory, llm)

    def review_rows(accts):
        return [cid for a in accts if a.needs_review for cid in a.crm_ids]

    by_id = {r["crm_id"]: r for r in m2}
    repeat = lambda ids: sum(1 for i in ids if memory_key(by_id[i]) in memory)   # noqa: E731
    base_review, mem_review = review_rows(base_accts), review_rows(mem_accts)
    hits = sum(1 for e in trace.events if e["kind"] == "decision" and e["name"] == "review_memory_hit")
    keep = ("verified_accuracy", "silent_error_rate", "review_rate", "entity_accuracy")
    return {
        "split": split, "month1_rows": len(m1), "month2_rows": len(m2), "reviewed_rows": reviewed_rows,
        "memory_entries": len(memory), "memory_hits": hits,
        "month1": {k: m1_metrics[k] for k in keep},
        "month2_without_memory": {**{k: base[k] for k in keep}, "review_rows": len(base_review),
                                  "repeat_questions": repeat(base_review)},
        "month2_with_memory": {**{k: with_mem[k] for k in keep}, "review_rows": len(mem_review),
                               "repeat_questions": repeat(mem_review)},
        "_outcomes": (base_accts, mem_accts, m2),
    }
