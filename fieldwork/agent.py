"""The account-research agent: CRM rows in, a deduplicated, verified, ICP-ranked account list out.

Pipeline per run:  resolve each row to a company  ->  merge rows into accounts  ->  enrich from the profile
service  ->  reconcile conflicting fields with provenance  ->  judge ICP fit  ->  guardrail  ->  rank.

Each released version is a `Policy`; the eval history compares them on the same held-out split.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .llm import LLM
from .tools import Tools, host, name_score, normalize
from .trace import Trace

ICP_TEXT = ("B2B technology vendors - companies that sell software, cloud, IT services, cybersecurity, data/analytics, "
            "semiconductors, computer or networking hardware to businesses - headquartered in the United States with "
            "1,000-100,000 employees. Consumer internet, e-commerce, media, telecom carriers, fintech/financial "
            "services, healthcare, retail and industrial manufacturing are out of scope.")
ICP_COUNTRY, ICP_MIN, ICP_MAX = "United States", 1_000, 100_000


@dataclass(frozen=True)
class Policy:
    name: str
    summary: str
    resolve_by_domain: bool = False   # look the website up before trusting the name
    dedupe: str = "name"              # "name": rows with the same normalized name; "entity": same resolved company
    fields: str = "crm"               # "crm": trust the rep's values; "freshest": newest dated value wins
    guardrail: bool = False           # abstain to human review instead of asserting a weak match
    icp: str = "keywords_v1"          # "keywords_v1" | "keywords_v2" | "llm"
    llm_adjudicate: bool = False      # ask the model to pick a candidate, or name the current company, before abstaining
    country_rule: str = "field"       # "field": the account's country field; "listed": any country the profile lists
    llm_resolve: str = "pick"         # "pick" among search candidates | "propose" from knowledge, verified |
                                      # "pick_then_propose": propose only when the pick finds nothing


VERSIONS = {
    "v1": Policy("v1", "Baseline: match on name, trust CRM values, keyword ICP"),
    "v2": Policy("v2", "Resolve by website first; merge rows on the resolved company",
                 resolve_by_domain=True, dedupe="entity"),
    "v3": Policy("v3", "Reconcile fields by freshness with provenance instead of trusting the CRM",
                 resolve_by_domain=True, dedupe="entity", fields="freshest"),
    "v4": Policy("v4", "Guardrail: weak name-only matches go to human review; ICP rules fixed from triage",
                 resolve_by_domain=True, dedupe="entity", fields="freshest", guardrail=True, icp="keywords_v2",
                 country_rule="listed"),
    "v5": Policy("v5", "LLM resolves former names and ambiguous matches (registry-verified) and judges ICP industry",
                 resolve_by_domain=True, dedupe="entity", fields="freshest", guardrail=True, icp="llm",
                 llm_adjudicate=True, country_rule="listed"),
    "v6": Policy("v6", "LLM names the company from knowledge with a stated basis; ticker and name must agree in the registry",
                 resolve_by_domain=True, dedupe="entity", fields="freshest", guardrail=True, icp="llm",
                 llm_adjudicate=True, country_rule="listed", llm_resolve="propose"),
    "v7": Policy("v7", "Hybrid: v5's candidate pick first; v6's verified knowledge proposal only when the pick finds nothing",
                 resolve_by_domain=True, dedupe="entity", fields="freshest", guardrail=True, icp="llm",
                 llm_adjudicate=True, country_rule="listed", llm_resolve="pick_then_propose"),
}
LATEST = "v5"   # shipped version; later entries are experiments that must beat it on held-out test to ship

# v1 ICP heuristic: substring match on industry labels.
KEYWORDS_V1 = ("software", "technology", "computer", "data", "semiconductor", "cloud", "internet", "network", "digital")
# v2: rebuilt from triaged ICP failures on the dev split (see docs/CHANGELOG.md).
KEYWORDS_V2_IN = ("software", "computer", "semiconductor", "cloud", "information technology", "information security",
                  "data", "analytics", "saas", "network", "it service", "web hosting", "web service",
                  "information and communications", "artificial intelligence", "business intelligence",
                  "customer relationship", "video conference", "development and operations", "website builder")
KEYWORDS_V2_OUT = ("medical", "financial", "telecommunications", "mobile phone", "consumer", "media", "e-commerce",
                   "game", "social", "streaming")


def _acronym(name: str) -> str:
    return "".join(w[0] for w in normalize(name).split())


@dataclass
class FieldValue:
    value: object
    source: str
    as_of: str | None
    confidence: float


@dataclass
class Account:
    key: str
    crm_ids: list[str]
    company_id: str | None = None
    resolved_via: str | None = None
    entity_confidence: float = 0.0
    fields: dict[str, FieldValue] = field(default_factory=dict)
    industries: list[str] = field(default_factory=list)
    icp_fit: bool = False
    icp_reason: str = ""
    confidence: float = 0.0
    needs_review: bool = False
    review_reason: str = ""
    guess_company_id: str | None = None   # best candidate behind an abstention (for risk-coverage analysis only)
    guess_score: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fields"] = {k: asdict(v) for k, v in self.fields.items()}
        return d


class Agent:
    def __init__(self, policy: Policy, tools: Tools, llm: LLM, trace: Trace):
        self.p, self.tools, self.llm, self.trace = policy, tools, llm, trace

    # ---------- entity resolution ----------
    def resolve(self, row: dict) -> tuple[str | None, float, str]:
        p, t = self.p, self.tools
        self._guess = (None, 0.0)
        if p.resolve_by_domain and row.get("website"):
            hits = t.domain_lookup(row["website"])
            ids = {h["id"] for h in hits}
            if len(ids) == 1:
                h = hits[0]
                return h["id"], (0.95 if h["status"] == "current" else 0.85), f"domain:{h['status']}"
        hits = t.registry_search(row["account_name"])
        if not hits:
            return None, 0.0, "no registry match"
        top = hits[0]
        self._guess = (top["id"], top["score"])
        margin = top["score"] - (hits[1]["score"] if len(hits) > 1 else 0.0)
        if not p.guardrail:
            return (top["id"], top["score"], "name") if top["score"] >= 0.5 else (None, 0.0, "weak name match")
        corroborated = bool(row.get("hq_country")) and row["hq_country"] == top["country"]
        if (top["score"] >= 0.95 and margin >= 0.05) or (top["score"] >= 0.8 and margin >= 0.15 and corroborated):
            return top["id"], 0.9, "name"
        if p.llm_adjudicate and self.llm.enabled:
            if p.llm_resolve == "propose":
                pick, via = self._propose(row)
            else:
                pick, via = self._adjudicate(row, hits)
                if not pick and p.llm_resolve == "pick_then_propose":
                    pick, via = self._propose(row)
            if pick:
                return pick, 0.85, via
        return None, top["score"], f"ambiguous name match (top {top['score']:.2f}, margin {margin:.2f})"

    def _adjudicate(self, row: dict, hits: list[dict]) -> tuple[str | None, str]:
        """Ask the model, then verify: a candidate pick must be one we offered, and a suggested current name must
        itself resolve cleanly in the registry. The model never asserts an entity on its own."""
        system = ("You match a sales CRM account to the publicly listed company it refers to. The CRM name may be a "
                  "former name, a subsidiary, an abbreviation or a ticker. Answer JSON {\"id\": <id of the matching "
                  "candidate or null>, \"current_name\": <if no candidate matches: the listed company's current name, "
                  "else null>, \"ticker\": <its ticker or null>, \"reason\": <short>}. Only pick a candidate that is "
                  "the same company, not merely a similar name. Use null when unsure.")
        user = json.dumps({"crm_row": {k: row[k] for k in ("account_name", "website", "hq_country")},
                           "candidates": [{k: h[k] for k in ("id", "name", "tickers", "country")} for h in hits]})
        ans = self.llm.json(system, user, self.trace, "adjudicate_match") or {}
        if ans.get("id") in {h["id"] for h in hits}:
            return ans["id"], "name+llm_pick"
        for q in (ans.get("ticker"), ans.get("current_name")):
            if not q or not isinstance(q, str):
                continue
            found = self.tools.registry_search(q)
            if found and found[0]["score"] >= 0.9 and (len(found) == 1 or found[0]["score"] - found[1]["score"] >= 0.05):
                self.trace.decision("llm_rewrite_verified", suggested=q, company_id=found[0]["id"])
                return found[0]["id"], "llm_rewrite+registry"
        return None, ""

    def _propose(self, row: dict) -> tuple[str | None, str]:
        """v6: the model names the company from its own knowledge (no candidate list to anchor on) and states how it
        knows. Accepted only for a factual basis, and only when its ticker and its name identify the same registry
        entry: two identifiers that must agree. "subsidiary" is not accepted: on dev it mapped trivago to Expedia and
        AMI Semiconductor to Amkor, and a separately listed subsidiary is a different account anyway."""
        system = ("You identify which publicly listed company a sales-CRM account refers to. The CRM name may be a "
                  "former name, an abbreviation, a nickname or a subsidiary. Use what you know about company renames, "
                  "abbreviations and acquisitions. Answer JSON {\"company\": <current listed company name or null>, "
                  "\"ticker\": <its primary US ticker or null>, \"basis\": one of \"former_name\", "
                  "\"abbreviation\", \"subsidiary\", \"same_name\", \"guess\", \"confidence\": <0..1>}. Use "
                  "\"guess\" if you are inferring from the words in the name (e.g. an industry) rather than knowing.")
        user = json.dumps({k: row[k] for k in ("account_name", "website", "hq_country")})
        ans = self.llm.json(system, user, self.trace, "propose_company") or {}
        basis, conf = ans.get("basis"), ans.get("confidence") or 0
        ticker, company = ans.get("ticker"), ans.get("company")
        if (basis not in ("former_name", "abbreviation", "same_name") or not isinstance(conf, (int, float))
                or conf < 0.8 or not isinstance(ticker, str) or not isinstance(company, str)):
            self.trace.decision("llm_proposal_rejected", reason="no factual basis or low confidence", answer=ans)
            return None, ""
        by_ticker = [h for h in self.tools.registry_search(ticker) if ticker.upper() in h["tickers"]]
        by_name = self.tools.registry_search(company)
        name_agrees = bool(by_ticker) and (
            (bool(by_name) and by_name[0]["id"] == by_ticker[0]["id"] and by_name[0]["score"] >= 0.85)
            or name_score(company, by_ticker[0]["name"]) >= 0.85
            or _acronym(company) == normalize(by_ticker[0]["name"]).replace(" ", ""))   # "Advanced Micro Devices" = AMD
        if len(by_ticker) == 1 and name_agrees:
            self.trace.decision("llm_proposal_verified", company=company, ticker=ticker, company_id=by_ticker[0]["id"])
            return by_ticker[0]["id"], f"llm_{basis}+registry"
        self.trace.decision("llm_proposal_rejected", reason="ticker and name disagree in registry", answer=ans,
                            ticker_hits=[h["id"] for h in by_ticker], name_top=by_name[0]["id"] if by_name else None)
        return None, ""

    # ---------- field reconciliation ----------
    def reconcile(self, acct: Account, rows: list[dict], prof: dict) -> None:
        current = [w["domain"] for w in prof["websites"] if w["status"] == "current" and w["domain"]]
        current.sort(key=lambda d: not any(w["preferred"] for w in prof["websites"] if w["domain"] == d))
        dated = [e for e in prof["employees"] if e.get("as_of")]
        latest = max(dated, key=lambda e: e["as_of"]) if dated else (prof["employees"][-1] if prof["employees"]
                                                                     else None)
        crm_site = next((r["website"] for r in rows if r.get("website")), None)
        crm_emp = next((r for r in rows if r.get("employee_count")), None)
        crm_country = next((r["hq_country"] for r in rows if r.get("hq_country")), None)
        f = acct.fields
        f["name"] = FieldValue(prof["name"], "registry", None, 0.95)
        f["ticker"] = FieldValue((prof["tickers"] or [None])[0], "registry", None, 0.95)

        if self.p.fields == "crm":
            f["domain"] = (FieldValue(host(crm_site), "crm", None, 0.6) if crm_site
                           else FieldValue(current[0] if current else None, "profile", None, 0.9))
            f["employees"] = (FieldValue(int(crm_emp["employee_count"]), "crm", crm_emp["last_updated"] or None, 0.6)
                              if crm_emp else FieldValue(latest and latest["value"], "profile",
                                                         latest and latest.get("as_of"), 0.9))
            f["country"] = (FieldValue(crm_country, "crm", None, 0.6) if crm_country
                            else FieldValue((prof["countries"] or [None])[0], "profile", None, 0.9))
            return

        # freshest: every candidate value carries a source and a date; the newest dated value wins.
        options = [(e["as_of"], "profile", e["value"]) for e in dated]
        options += [(r["last_updated"], "crm", int(r["employee_count"])) for r in rows
                    if r.get("employee_count") and r.get("last_updated")]
        if options:
            as_of, src, val = max(options, key=lambda o: (o[0], o[1] == "profile"))
            f["employees"] = FieldValue(val, src, as_of, 0.9 if src == "profile" else 0.7)
        else:
            f["employees"] = FieldValue(latest and latest["value"], "profile", None, 0.5)
        if current:
            f["domain"] = FieldValue(current[0], "profile", None, 0.95)
        else:
            f["domain"] = FieldValue(host(crm_site), "crm", None, 0.5)
        if prof["countries"]:
            pick = crm_country if crm_country in prof["countries"] else prof["countries"][0]
            f["country"] = FieldValue(pick, "profile", None, 0.9)
        else:
            f["country"] = FieldValue(crm_country, "crm", None, 0.5)
        self.trace.decision("reconcile", employees=asdict(f["employees"]), domain=asdict(f["domain"]),
                            crm_employees=crm_emp and crm_emp["employee_count"], crm_website=crm_site)

    # ---------- ICP ----------
    def judge_icp(self, industries: list[str]) -> tuple[bool, str]:
        labels = [i.lower() for i in industries]
        if self.p.icp in ("keywords_v2", "llm"):
            hits = [l for l in labels if any(k in l for k in KEYWORDS_V2_IN) and not any(k in l for k in KEYWORDS_V2_OUT)]
            unknown = [l for l in labels if not any(k in l for k in KEYWORDS_V2_IN + KEYWORDS_V2_OUT)]
            # Rules decide the labels they know; only labels no rule covers go to the model.
            if not hits and unknown and self.p.icp == "llm" and self.llm.enabled:
                system = ("You qualify accounts for a B2B sales team. Ideal customer profile: " + ICP_TEXT +
                          " Judge ONLY whether the company's industry is in scope, from its industry labels. Answer "
                          "JSON {\"in_scope\": true|false, \"reason\": <short>}.")
                ans = self.llm.json(system, json.dumps({"industries": industries}), self.trace, "judge_icp_industry")
                if ans and isinstance(ans.get("in_scope"), bool):
                    return ans["in_scope"], "llm: " + str(ans.get("reason", ""))[:120]
            return bool(hits), f"keywords_v2: {hits[:3]}"
        hits = [l for l in labels if any(k in l for k in KEYWORDS_V1)]
        return bool(hits), f"keywords_v1: {hits[:3]}"

    # ---------- run ----------
    def run(self, rows: list[dict]) -> list[Account]:
        groups: dict[str, Account] = {}
        rows_by_key: dict[str, list[dict]] = {}
        for row in rows:
            with self.trace.scope(row["crm_id"]):
                if self.p.dedupe == "name":
                    key = "name:" + normalize(row["account_name"])
                    if key not in groups:
                        cid, conf, via = self.resolve(row)
                        groups[key] = Account(key, [], cid, via, conf, guess_company_id=self._guess[0],
                                              guess_score=self._guess[1])
                else:
                    cid, conf, via = self.resolve(row)
                    key = f"co:{cid}" if cid else f"review:{row['crm_id']}"
                    if key not in groups:
                        groups[key] = Account(key, [], cid, via, conf, guess_company_id=self._guess[0],
                                              guess_score=self._guess[1])
                    else:
                        groups[key].entity_confidence = max(groups[key].entity_confidence, conf)
                self.trace.decision("resolve", company_id=groups[key].company_id, via=groups[key].resolved_via,
                                    confidence=groups[key].entity_confidence, account=key)
                groups[key].crm_ids.append(row["crm_id"])
                rows_by_key.setdefault(key, []).append(row)

        for key, acct in groups.items():
            with self.trace.scope(key):
                if acct.company_id is None:
                    acct.needs_review, acct.review_reason = True, acct.resolved_via or "unresolved"
                    continue
                prof = self.tools.company_profile(acct.company_id)
                self.reconcile(acct, rows_by_key[key], prof)
                acct.industries = prof["industries"]
                emp = acct.fields["employees"].value
                in_country = (ICP_COUNTRY in prof["countries"] if self.p.country_rule == "listed"
                              else acct.fields["country"].value == ICP_COUNTRY)
                if in_country and emp and ICP_MIN <= emp <= ICP_MAX:   # cheap checks first; judge industry last
                    ind_fit, reason = self.judge_icp(prof["industries"])
                else:
                    ind_fit, reason = False, "outside ICP country or headcount band"
                acct.icp_fit = bool(ind_fit)
                acct.icp_reason = reason
                missing = [k for k, v in acct.fields.items() if v.value is None]
                acct.confidence = round(min([acct.entity_confidence] + [v.confidence for v in acct.fields.values()
                                                                        if v.value is not None]), 3)
                if self.p.guardrail and (missing or acct.confidence < 0.8):
                    acct.needs_review = True
                    acct.review_reason = f"low confidence {acct.confidence}" + (f"; missing {missing}" if missing else "")
                self.trace.decision("account", icp_fit=acct.icp_fit, icp_reason=reason, confidence=acct.confidence,
                                    needs_review=acct.needs_review)
        out = list(groups.values())
        out.sort(key=lambda a: (a.needs_review, not a.icp_fit, -a.confidence,
                                -(a.fields["employees"].value or 0) if "employees" in a.fields else 0))
        return out
