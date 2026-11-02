"""Fetch a frozen snapshot of real US-listed company data from Wikidata.

Run once (needs network); the output under data/snapshot/ is committed so everything else runs offline.

    python -m fieldwork.data.build_snapshot
"""
from __future__ import annotations

import json
import time

import requests
from ..paths import ROOT

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "fieldwork/0.1 (open-source agent-eval portfolio project)"}
EXCHANGES = "wd:Q13677 wd:Q82059"  # NYSE, NASDAQ
OUT = ROOT / "data" / "snapshot"


def sparql(query: str) -> list[dict]:
    for attempt in range(4):
        r = requests.get(ENDPOINT, params={"query": query, "format": "json"}, headers=HEADERS, timeout=180)
        if r.status_code == 200:
            return r.json()["results"]["bindings"]
        time.sleep(5 * (attempt + 1))
    r.raise_for_status()
    return []


def qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def val(row: dict, key: str) -> str | None:
    return row[key]["value"] if key in row else None


def fetch_registry() -> list[dict]:
    """Every company currently listed on NYSE/NASDAQ with a ticker: the pool the registry tool searches."""
    rows = sparql(f"""
    SELECT ?c ?cLabel ?ticker ?exchLabel ?countryLabel WHERE {{
      VALUES ?exch {{ {EXCHANGES} }}
      ?c p:P414 ?st. ?st ps:P414 ?exch; pq:P249 ?ticker.
      FILTER NOT EXISTS {{ ?st pq:P582 ?end }}
      OPTIONAL {{ ?c wdt:P17 ?country. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}""")
    by_id: dict[str, dict] = {}
    for r in rows:
        cid = qid(val(r, "c"))
        name = val(r, "cLabel")
        if not name or name == cid:
            continue
        e = by_id.setdefault(cid, {"id": cid, "name": name, "tickers": [], "exchange": val(r, "exchLabel"),
                                   "country": val(r, "countryLabel")})
        t = val(r, "ticker")
        if t and t not in e["tickers"]:
            e["tickers"].append(t)
    return sorted(by_id.values(), key=lambda e: e["id"])


def fetch_profiles(ids: list[str]) -> dict[str, dict]:
    """Rich profile (dated headcounts, websites with rank/end, industries, aliases) for the study set."""
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 60):
        values = " ".join(f"wd:{x}" for x in ids[i:i + 60])
        emp = sparql(f"""
        SELECT ?c ?n ?t WHERE {{ VALUES ?c {{ {values} }}
          ?c p:P1128 ?s. ?s ps:P1128 ?n. OPTIONAL {{ ?s pq:P585 ?t. }} }}""")
        web = sparql(f"""
        SELECT ?c ?w ?rank ?end WHERE {{ VALUES ?c {{ {values} }}
          ?c p:P856 ?s. ?s ps:P856 ?w; wikibase:rank ?rank. OPTIONAL {{ ?s pq:P582 ?end. }} }}""")
        misc = sparql(f"""
        SELECT ?c ?indLabel ?countryLabel ?hqLabel ?inception WHERE {{ VALUES ?c {{ {values} }}
          OPTIONAL {{ ?c wdt:P452 ?ind. }} OPTIONAL {{ ?c wdt:P17 ?country. }}
          OPTIONAL {{ ?c wdt:P159 ?hq. }} OPTIONAL {{ ?c wdt:P571 ?inception. }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }} }}""")
        alias = sparql(f"""
        SELECT ?c ?a WHERE {{ VALUES ?c {{ {values} }}
          ?c skos:altLabel ?a. FILTER(LANG(?a) = "en") }}""")
        for r in emp:
            p = out.setdefault(qid(val(r, "c")), {})
            try:
                n = int(float(val(r, "n")))
            except (TypeError, ValueError):
                continue
            p.setdefault("employees", []).append({"value": n, "as_of": (val(r, "t") or "")[:10] or None})
        for r in web:
            p = out.setdefault(qid(val(r, "c")), {})
            p.setdefault("websites", []).append({"url": val(r, "w"), "rank": qid(val(r, "rank")),
                                                 "ended": (val(r, "end") or "")[:10] or None})
        for r in misc:
            p = out.setdefault(qid(val(r, "c")), {})
            for key, field in (("indLabel", "industries"), ("countryLabel", "countries"), ("hqLabel", "hq")):
                v = val(r, key)
                if v and not v.startswith("Q") and v not in p.setdefault(field, []):
                    p[field].append(v)
            inc = val(r, "inception")
            if inc and inc[:4].isdigit():
                p.setdefault("founded", set()).add(int(inc[:4]))
        for r in alias:
            p = out.setdefault(qid(val(r, "c")), {})
            a = val(r, "a")
            if a and a not in p.setdefault("aliases", []):
                p["aliases"].append(a)
        time.sleep(1)
    for p in out.values():
        if "founded" in p:
            p["founded"] = min(p["founded"])
        p.get("employees", []).sort(key=lambda e: e["as_of"] or "")
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    registry = fetch_registry()
    print(f"registry: {len(registry)} listed companies")
    study = sparql(f"""
    SELECT DISTINCT ?c WHERE {{
      VALUES ?exch {{ {EXCHANGES} }}
      ?c p:P414 ?st. ?st ps:P414 ?exch; pq:P249 ?ticker.
      FILTER NOT EXISTS {{ ?st pq:P582 ?end }}
      ?c p:P1128 ?es. ?es pq:P585 ?t.
      ?c wdt:P856 ?w. ?c wdt:P452 ?ind. }}""")
    ids = sorted({qid(val(r, "c")) for r in study})
    print(f"study set: {len(ids)} companies with dated headcount, website and industry")
    profiles = fetch_profiles(ids)
    reg_by_id = {e["id"]: e for e in registry}
    companies = []
    for cid in ids:
        if cid not in reg_by_id or cid not in profiles:
            continue
        companies.append({**reg_by_id[cid], **profiles[cid]})
    (OUT / "registry.json").write_text(json.dumps(registry, indent=1, ensure_ascii=False), encoding="utf-8")
    (OUT / "companies.json").write_text(json.dumps(companies, indent=1, ensure_ascii=False), encoding="utf-8")
    meta = {"source": "Wikidata Query Service (CC0)", "fetched": time.strftime("%Y-%m-%d"),
            "registry": len(registry), "companies": len(companies)}
    (OUT / "SNAPSHOT.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(meta)


if __name__ == "__main__":
    main()
