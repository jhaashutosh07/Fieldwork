"""The agent's tools: a listed-company registry, a domain index and a company-profile service.

All three read the frozen Wikidata snapshot, so runs are reproducible and need no network. Each call is
recorded on the active trace with its latency.
"""
from __future__ import annotations

import difflib
import json
import re
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

SNAP = Path(__file__).resolve().parents[1] / "data" / "snapshot"

_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|nv|bv|sa|ag|se|gmbh|"
                     r"ab|asa|spa|oyj|kk|holdings?|group|the)\b")
# Words too common to identify a company on their own ("International" must not match "CACI International").
GENERIC = {"international", "american", "america", "national", "global", "general", "united", "first", "new",
           "communications", "technologies", "technology", "systems", "industries", "energy", "resources",
           "financial", "bank", "bancorp", "services", "solutions", "networks", "software", "pharmaceuticals",
           "therapeutics", "motors", "airlines", "and", "of"}


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.']", "", s.replace("&", " and "))
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", _SUFFIX.sub(" ", s)).strip()


def host(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip()
    u = u if "//" in u else "http://" + u
    h = (urlparse(u).hostname or "").lower()
    return (h[4:] if h.startswith("www.") else h) or None


def name_score(query: str, candidate: str) -> float:
    a, b = normalize(query), normalize(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a.replace(" ", "") == b.replace(" ", ""):
        return 0.97
    ta, tb = set(a.split()), set(b.split())
    if ta < tb and ta - GENERIC:        # rep dropped words from the official name: "Marvell" -> "Marvell Technology"
        return 0.9
    if tb < ta and tb - GENERIC:        # rep added a division or legal tail: "Medtronic - MITG" -> "Medtronic"
        return 0.85
    jac = len(ta & tb) / len(ta | tb)
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    prefix = 0.1 if b.startswith(a) or a.startswith(b) else 0.0
    return round(min(0.99, 0.5 * seq + 0.4 * jac + prefix), 3)


class World:
    """Read-only view over the snapshot; one instance is shared by every run."""

    def __init__(self, snapshot: Path = SNAP):
        self.registry = {e["id"]: e for e in json.loads((snapshot / "registry.json").read_text(encoding="utf-8"))}
        self.profiles = {c["id"]: c for c in json.loads((snapshot / "companies.json").read_text(encoding="utf-8"))}
        self._tokens: dict[str, set[str]] = {}
        for cid, e in self.registry.items():
            for t in normalize(e["name"]).split():
                self._tokens.setdefault(t, set()).add(cid)
            for t in e["tickers"]:
                self._tokens.setdefault(t.lower(), set()).add(cid)
        self._domains: dict[str, list[tuple[str, str]]] = {}
        for cid, p in self.profiles.items():
            for w in p.get("websites", []):
                h = host(w["url"])
                status = "former" if (w["ended"] or "Deprecated" in w["rank"]) else "current"
                if h and (cid, status) not in self._domains.get(h, []):
                    self._domains.setdefault(h, []).append((cid, status))


@lru_cache(maxsize=1)
def world() -> World:
    return World()


class Tools:
    """Tool surface handed to the agent. `trace` receives one event per call."""

    def __init__(self, w: World, trace):
        self.w, self.trace = w, trace

    def _log(self, tool: str, args: dict, result, t0: float):
        self.trace.tool(tool, args, result, round((time.perf_counter() - t0) * 1000, 2))
        return result

    def registry_search(self, query: str, limit: int = 5) -> list[dict]:
        t0 = time.perf_counter()
        q = normalize(query)
        pool: set[str] = set()
        for t in q.split():
            pool |= self.w._tokens.get(t, set())
        if not pool and q:
            pool = {cid for cid, e in self.w.registry.items() if normalize(e["name"])[:3] == q[:3]}
        hits = []
        for cid in pool:
            e = self.w.registry[cid]
            s = max(name_score(query, e["name"]), 1.0 if query.strip().upper() in e["tickers"] else 0.0)
            hits.append({"id": cid, "name": e["name"], "tickers": e["tickers"], "country": e["country"], "score": s})
        hits.sort(key=lambda h: (-h["score"], h["id"]))
        return self._log("registry_search", {"query": query}, hits[:limit], t0)

    def domain_lookup(self, domain: str) -> list[dict]:
        t0 = time.perf_counter()
        h = host(domain)
        out = [{"id": cid, "name": self.w.registry[cid]["name"], "status": st}
               for cid, st in self.w._domains.get(h or "", []) if cid in self.w.registry]
        return self._log("domain_lookup", {"domain": h}, out, t0)

    def company_profile(self, company_id: str) -> dict:
        t0 = time.perf_counter()
        e = self.w.registry.get(company_id, {})
        p = self.w.profiles.get(company_id)
        if p is None:
            out = {"id": company_id, "name": e.get("name"), "tickers": e.get("tickers", []),
                   "countries": [e["country"]] if e.get("country") else [], "websites": [], "employees": [],
                   "industries": []}
        else:
            out = {"id": company_id, "name": p["name"], "tickers": p["tickers"], "countries": p.get("countries", []),
                   "industries": p.get("industries", []), "employees": p.get("employees", []),
                   "websites": [{"domain": host(w["url"]), "preferred": "Preferred" in w["rank"],
                                 "status": "former" if (w["ended"] or "Deprecated" in w["rank"]) else "current"}
                                for w in p.get("websites", [])]}
        return self._log("company_profile", {"id": company_id}, out, t0)
