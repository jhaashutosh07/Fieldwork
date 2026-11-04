"""Two framework versions of the agent, for an honest comparison on the same eval.

1. PipelineGraph: the shipped pipeline as a LangGraph StateGraph (resolve_rules -> [resolve_llm] -> merge -> enrich
   -> judge -> guardrail). Each node calls the same stage method as Agent.run, so the results must be identical;
   tests/test_fieldwork.py checks that on the held-out split.
2. ToolAgent: an autonomous tool-calling agent. The LLM decides which tools to call (registry search, website lookup,
   company profile) in a LangGraph model <-> tools loop and writes the final answer itself; no rules, no guardrail.
"""
from __future__ import annotations

import json
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .agent import ICP_TEXT, Account, Agent, FieldValue, Policy
from .llm import LLM
from .tools import Tools, host
from .trace import Trace


# ---------- 1. the pipeline as a LangGraph graph ----------
class PipelineState(TypedDict, total=False):
    rows: list[dict]
    decided: dict
    pending: dict
    groups: dict
    rows_by_key: dict
    enriched: list
    accounts: list


class PipelineGraph:
    def __init__(self, policy: Policy, tools: Tools, llm: LLM, trace: Trace, memory: dict | None = None):
        assert policy.dedupe == "entity", "the graph version implements entity-level merging (v2 onwards)"
        self.a, self.trace = Agent(policy, tools, llm, trace, memory=memory), trace
        g = StateGraph(PipelineState)
        for name in ("resolve_rules", "resolve_llm", "merge", "enrich", "judge", "guardrail"):
            g.add_node(name, getattr(self, "_" + name))
        g.add_edge(START, "resolve_rules")
        g.add_conditional_edges("resolve_rules", lambda s: "resolve_llm" if s["pending"] else "merge",
                                ["resolve_llm", "merge"])
        g.add_edge("resolve_llm", "merge")
        g.add_edge("merge", "enrich")
        g.add_edge("enrich", "judge")
        g.add_edge("judge", "guardrail")
        g.add_edge("guardrail", END)
        self.graph = g.compile()

    def run(self, rows: list[dict]) -> list[Account]:
        return self.graph.invoke({"rows": rows})["accounts"]

    def mermaid(self) -> str:
        return self.graph.get_graph().draw_mermaid()

    def _resolve_rules(self, s: PipelineState) -> dict:
        decided, pending = {}, {}
        for row in s["rows"]:
            with self.trace.scope(row["crm_id"]):
                res, ctx = self.a.resolve_rules(row)
                if res is None:
                    pending[row["crm_id"]] = ctx
                else:
                    decided[row["crm_id"]] = (*res, self.a._guess)
        return {"decided": decided, "pending": pending}

    def _resolve_llm(self, s: PipelineState) -> dict:
        decided = dict(s["decided"])
        by_id = {r["crm_id"]: r for r in s["rows"]}
        for crm_id, ctx in s["pending"].items():
            with self.trace.scope(crm_id):
                decided[crm_id] = (*self.a.resolve_with_llm(by_id[crm_id], ctx), self.a._guess)
        return {"decided": decided, "pending": {}}

    def _merge(self, s: PipelineState) -> dict:
        groups: dict[str, Account] = {}
        rows_by_key: dict[str, list[dict]] = {}
        for row in s["rows"]:
            cid, conf, via, guess = s["decided"][row["crm_id"]]
            key = f"co:{cid}" if cid else f"review:{row['crm_id']}"
            with self.trace.scope(row["crm_id"]):
                if key not in groups:
                    groups[key] = Account(key, [], cid, via, conf, guess_company_id=guess[0], guess_score=guess[1])
                else:
                    groups[key].entity_confidence = max(groups[key].entity_confidence, conf)
                self.trace.decision("resolve", company_id=groups[key].company_id, via=groups[key].resolved_via,
                                    confidence=groups[key].entity_confidence, account=key)
            groups[key].crm_ids.append(row["crm_id"])
            rows_by_key.setdefault(key, []).append(row)
        return {"groups": groups, "rows_by_key": rows_by_key}

    def _enrich(self, s: PipelineState) -> dict:
        enriched = []
        for key, acct in s["groups"].items():
            with self.trace.scope(key):
                if self.a.enrich(acct, s["rows_by_key"][key]):
                    enriched.append(key)
        return {"enriched": enriched}

    def _judge(self, s: PipelineState) -> dict:
        for key in s["enriched"]:
            with self.trace.scope(key):
                self.a.judge(s["groups"][key])
        return {}

    def _guardrail(self, s: PipelineState) -> dict:
        for key in s["enriched"]:
            with self.trace.scope(key):
                self.a.guardrail(s["groups"][key])
        return {"accounts": Agent.rank(s["groups"])}


# ---------- 2. an autonomous tool-calling agent ----------
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "registry_search", "description": "Search the registry of NYSE/NASDAQ-listed companies by name or ticker. "
        "Returns up to 5 candidates with id, name, tickers, country and a 0-1 name-match score.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "domain_lookup", "description": "Find which company uses a website domain (current or former).",
        "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}, "required": ["domain"]}}},
    {"type": "function", "function": {
        "name": "company_profile", "description": "Profile of a company by registry id: websites (current/former), "
        "dated headcounts, countries and industry labels.",
        "parameters": {"type": "object", "properties": {"company_id": {"type": "string"}}, "required": ["company_id"]}}},
    {"type": "function", "function": {
        "name": "submit_answer", "description": "Submit the final answer for this CRM row. Call exactly once, last.",
        "parameters": {"type": "object", "properties": {
            "company_id": {"type": ["string", "null"]}, "domain": {"type": ["string", "null"]},
            "employees": {"type": ["integer", "null"]}, "country": {"type": ["string", "null"]},
            "icp_fit": {"type": "boolean"}, "needs_review": {"type": "boolean"}, "reason": {"type": "string"}},
            "required": ["company_id", "icp_fit", "needs_review", "reason"]}}},
]
TOOL_SYSTEM = (
    "You are a CRM research agent. For one CRM account row, identify the listed company it refers to and report its "
    "current facts, using the tools. Rules: resolve the company with the tools (never from memory alone); take the "
    "website from the profile's current preferred website, the headcount from the most recent dated value, and a "
    "country the profile lists. Ideal customer profile: " + ICP_TEXT + " If you cannot identify the company with "
    "confidence, set needs_review to true instead of guessing. Finish by calling submit_answer exactly once.")
MAX_STEPS = 6


class ToolState(TypedDict, total=False):
    messages: list[dict]
    steps: int
    final: dict | None


class ToolAgent:
    """LangGraph loop: model -> (tool calls? -> tools -> model) -> final JSON. One episode per CRM row."""

    def __init__(self, tools: Tools, llm: LLM, trace: Trace):
        self.tools, self.llm, self.trace = tools, llm, trace
        g = StateGraph(ToolState)
        g.add_node("model", self._model)
        g.add_node("tools", self._tools)
        g.add_edge(START, "model")
        g.add_conditional_edges("model", lambda s: "tools" if s["final"] is None and s["steps"] < MAX_STEPS else END,
                                ["tools", END])
        g.add_edge("tools", "model")
        self.graph = g.compile()

    def answer(self, row: dict) -> dict:
        user = json.dumps({k: row.get(k, "") for k in ("account_name", "website", "hq_country")})
        with self.trace.scope(row["crm_id"]):
            out = self.graph.invoke({"messages": [{"role": "system", "content": TOOL_SYSTEM},
                                                  {"role": "user", "content": user}], "steps": 0, "final": None},
                                    {"recursion_limit": 2 * MAX_STEPS + 4})
        return out["final"] or {"company_id": None, "needs_review": True, "reason": "no final answer within step budget"}

    def _model(self, s: ToolState) -> dict:
        try:
            msg = self.llm.chat(s["messages"], TOOL_SPECS, self.trace, "tool_agent") or {"content": "", "tool_calls": []}
        except Exception as exc:   # e.g. the model emits a malformed tool call the API rejects: the episode fails
            return {"steps": MAX_STEPS, "final": {"company_id": None, "needs_review": True,
                                                  "reason": f"model error: {type(exc).__name__}"}}
        submit = next((tc for tc in msg["tool_calls"] if tc["name"] == "submit_answer"), None)
        if submit:
            try:
                return {"steps": s["steps"] + 1, "final": json.loads(submit["arguments"] or "{}")}
            except json.JSONDecodeError:
                return {"steps": s["steps"] + 1, "final": None}
        assistant = {"role": "assistant", "content": msg["content"] or ""}
        if msg["tool_calls"]:
            assistant["tool_calls"] = [{"id": tc["id"], "type": "function",
                                        "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                                       for tc in msg["tool_calls"]]
            return {"messages": s["messages"] + [assistant], "steps": s["steps"] + 1, "final": None}
        return {"messages": s["messages"] + [assistant], "steps": s["steps"] + 1,
                "final": _parse_final(msg["content"])}

    def _tools(self, s: ToolState) -> dict:
        out = []
        for tc in s["messages"][-1].get("tool_calls", []):
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                result = self._call(tc["function"]["name"], args)
            except Exception as exc:   # malformed arguments or unknown tool: tell the model, don't crash
                result = {"error": type(exc).__name__}
            out.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, ensure_ascii=False)})
        return {"messages": s["messages"] + out}

    def _call(self, name: str, args: dict):
        if name == "registry_search":
            return [{k: h[k] for k in ("id", "name", "tickers", "country", "score")}
                    for h in self.tools.registry_search(str(args.get("query", ""))[:120])]
        if name == "domain_lookup":
            return self.tools.domain_lookup(str(args.get("domain", ""))[:200])
        if name == "company_profile":
            p = self.tools.company_profile(str(args.get("company_id", ""))[:16])
            return {**p, "employees": p.get("employees", [])[-6:]}
        return {"error": f"unknown tool {name}"}


def _parse_final(content: str) -> dict | None:
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


def tool_agent_accounts(rows: list[dict], tools: Tools, llm: LLM, trace: Trace) -> list[Account]:
    """Run one episode per row, then merge rows the agent mapped to the same company so the output is scored by the
    same eval as the pipeline."""
    agent = ToolAgent(tools, llm, trace)
    return accounts_from_answers(rows, [agent.answer(r) for r in rows], tools)


def accounts_from_answers(rows: list[dict], answers: list[dict], tools: Tools) -> list[Account]:
    groups: dict[str, Account] = {}
    for row, a in zip(rows, answers):
        cid = a.get("company_id") if isinstance(a.get("company_id"), str) else None
        review = bool(a.get("needs_review")) or cid is None or cid not in tools.w.registry
        key = f"review:{row['crm_id']}" if review else f"co:{cid}"
        if key not in groups:
            acct = Account(key, [], None if review else cid, "tool_agent", 0.0 if review else 0.9)
            if review:
                acct.needs_review, acct.review_reason = True, str(a.get("reason", ""))[:200] or "agent unsure"
            else:
                emp = a.get("employees")
                acct.fields = {"name": FieldValue(tools.w.registry[cid]["name"], "llm", None, 0.9),
                               "domain": FieldValue(host(a.get("domain")) if a.get("domain") else None, "llm", None, 0.9),
                               "employees": FieldValue(int(emp) if isinstance(emp, (int, float)) else None, "llm", None, 0.9),
                               "country": FieldValue(a.get("country") or None, "llm", None, 0.9)}
                acct.icp_fit, acct.icp_reason, acct.confidence = bool(a.get("icp_fit")), str(a.get("reason", ""))[:200], 0.9
            groups[key] = acct
        groups[key].crm_ids.append(row["crm_id"])
    return Agent.rank(groups)
