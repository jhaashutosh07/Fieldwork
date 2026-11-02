"""Build the messy CRM export (agent input) and the gold answers (eval only) from the frozen snapshot.

Every "messy" value is real: CRM names are real Wikidata aliases or rep-style spellings of the real name, stale
headcounts are real older dated headcounts, and old domains are real ended/deprecated websites. Nothing is invented;
the generator only chooses which real value a sales rep would have typed, and when.

    python -m fieldwork.data.make_dataset
"""
from __future__ import annotations

import csv
import json
import random
import re
from urllib.parse import urlparse
from ..paths import ROOT

SNAP = ROOT / "data" / "snapshot"
OUT = ROOT / "data" / "dataset"
SEED = 7

# Gold ICP definition. Industry labels judged by hand against "B2B technology vendor" (see data/dataset/ICP.md).
ICP_INDUSTRIES = {
    "IT service management", "Internet of Things", "Prepackaged Software", "Software Publishers", "Travel technology",
    "analytics", "artificial intelligence", "business intelligence", "business software industry", "cloud computing",
    "computer and network surveillance", "computer hardware industry", "computer industry", "computer network",
    "computer security", "computer storage media", "computing platform", "customer relationship management",
    "data analytics software industry", "data center", "data storage medium", "data visualization",
    "development and operations", "email marketing", "enterprise software", "health information technology",
    "information and communications industry", "information and communications technology", "information security",
    "information technology", "information technology consulting", "information technology industry",
    "infrastructure platform", "intelligent agent", "networking hardware", "open-source intelligence", "semiconductor",
    "semiconductor industry", "software as a service", "software development", "software industry", "technology",
    "technology industry", "video conference", "web hosting service", "web service", "website builder",
}
ICP_COUNTRY = "United States"
ICP_MIN, ICP_MAX = 1_000, 100_000

LEGAL_SUFFIX = re.compile(r"[,.]?\s+(inc\.?|incorporated|corp\.?|corporation|co\.?|company|ltd\.?|limited|plc|"
                          r"llc|l\.p\.|lp|n\.v\.|nv|s\.a\.|sa|ag|se|holdings?|group|technologies)$", re.I)


def host(url: str | None) -> str | None:
    if not url:
        return None
    u = url if "//" in url else "http://" + url
    h = (urlparse(u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h or None


def current_and_old_hosts(websites: list[dict]) -> tuple[set[str], set[str]]:
    live = [w for w in websites if not w["ended"] and "Deprecated" not in w["rank"]]
    preferred = [w for w in live if "Preferred" in w["rank"]]
    current = {host(w["url"]) for w in (preferred or live)} - {None}
    old = {host(w["url"]) for w in websites} - current - {None}
    return current, old


def latest_headcount(employees: list[dict]) -> tuple[set[int], str | None]:
    dated = [e for e in employees if e["as_of"]]
    if not dated:
        return {e["value"] for e in employees}, None
    last = max(e["as_of"] for e in dated)
    return {e["value"] for e in dated if e["as_of"] == last}, last


def rep_spelling(name: str, rng: random.Random) -> str:
    """How a rep types a company name: drop the legal suffix, change case."""
    base = LEGAL_SUFFIX.sub("", name).strip() or name
    return rng.choice([base, base, base.upper(), base.lower(), name])


def main() -> None:
    rng = random.Random(SEED)
    companies = json.loads((SNAP / "companies.json").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)

    gold_companies, rows = {}, []
    for c in companies:
        current, old = current_and_old_hosts(c.get("websites", []))
        heads, as_of = latest_headcount(c.get("employees", []))
        if not current or not heads:
            continue
        industries = c.get("industries", [])
        countries = c.get("countries") or ([c["country"]] if c.get("country") else [])
        fit = (bool(ICP_INDUSTRIES & set(industries)) and ICP_COUNTRY in countries
               and any(ICP_MIN <= h <= ICP_MAX for h in heads))
        gold_companies[c["id"]] = {
            "id": c["id"], "name": c["name"], "tickers": c["tickers"], "domains": sorted(current),
            "old_domains": sorted(old), "employees": sorted(heads), "employees_as_of": as_of,
            "countries": countries, "industries": industries, "icp_fit": fit,
        }
        older = [e for e in c["employees"] if e["as_of"] and e["as_of"] != as_of and e["value"] not in heads]
        n_rows = rng.choices([1, 2, 3], weights=[62, 28, 10])[0]
        for k in range(n_rows):
            aliases = [a for a in c.get("aliases", []) if len(a) > 2]
            r = rng.random()
            name = rng.choice(aliases) if aliases and r < 0.30 else rep_spelling(c["name"], rng)
            r = rng.random()
            if old and r < 0.40:
                site = rng.choice(sorted(old))
            elif r < 0.55:
                site = ""
            else:
                d = rng.choice(sorted(current))
                site = rng.choice([d, "www." + d, "https://www." + d + "/", "http://" + d])
            r = rng.random()
            if older and r < 0.60:
                e = rng.choice(older)
                emp, updated = str(e["value"]), e["as_of"]
            elif r < 0.80:
                emp, updated = str(next(iter(heads))), as_of
            else:
                emp, updated = "", (as_of or "2020-01-01")
            country = rng.choice(countries) if countries and rng.random() < 0.8 else ""
            rows.append({"account_name": name, "website": site, "employee_count": emp, "hq_country": country,
                         "last_updated": updated or "", "_gold": c["id"]})

    rng.shuffle(rows)
    gold_rows = {}
    with (OUT / "crm_export.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["crm_id", "account_name", "website", "employee_count", "hq_country",
                                          "last_updated"])
        w.writeheader()
        for i, r in enumerate(rows, 1):
            crm_id = f"CRM-{i:04d}"
            gold_rows[crm_id] = r.pop("_gold")
            w.writerow({"crm_id": crm_id, **r})
    gold = {"rows": gold_rows, "companies": gold_companies,
            "icp": {"industries": sorted(ICP_INDUSTRIES), "country": ICP_COUNTRY, "min": ICP_MIN, "max": ICP_MAX}}
    (OUT / "gold.json").write_text(json.dumps(gold, indent=1, ensure_ascii=False), encoding="utf-8")
    fits = sum(g["icp_fit"] for g in gold_companies.values())
    print(f"{len(rows)} CRM rows -> {len(gold_companies)} companies ({fits} fit the ICP)")


if __name__ == "__main__":
    main()
