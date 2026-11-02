# Changelog: how each version was earned

Every change below was motivated by failure clusters triaged on the **dev** split (`fieldwork triage <run>`).
The numbers that judge a change are always on the held-out **test** split (158 companies, 242 CRM rows), which no
fix was tuned on. Each fixed failure was promoted into `evals/regressions/` so it can never silently come back.

| Version | Test: verified accuracy | Test: silent error rate | Test: sent to review | Regression cases |
|---|---|---|---|---|
| v1 | 55.7% | 37.4% | 5.4% | 0/9 |
| v2 | 60.1% | 37.1% | 2.5% | 0/9 |
| v3 | 89.2% | 6.9% | 2.5% | 4/9 |
| v4 | 89.2% | **0.0%** | 8.7% | 9/9 |
| v5 | **95.6%** | **0.0%** | 3.7% | 9/9 |

## v1: baseline
Match each CRM row to the registry on name alone, merge rows with identical normalized names, trust the values the
rep typed, and decide ICP fit with substring keywords.

Dev triage (v1): `stale_headcount` 62, `old_domain` 29, `sent_to_review` 22, `split_duplicate` 13,
`icp_false_positive` 5, `wrong_entity:similar_name` 5, `icp_false_negative` 4.

## v2: resolve by website, merge by entity
`split_duplicate` (13 on dev): "adobe" and "ADBE" became two accounts. Rows are now resolved through a domain index
first (current *and* former websites), and merged on the resolved company rather than on the spelling.
Dev: `split_duplicate` 13 → 0, `wrong_entity:similar_name` 5 → 1.
Promoted: `split_duplicate-Q11463` (Adobe), `split_duplicate-Q1347782` (Marvell).

## v3: reconcile by freshness, with provenance
`stale_headcount` (64) and `old_domain` (31) were the two largest clusters: the CRM held real but outdated values
(Consolidated Edison's 2010 headcount, `business.adobe.com`). Every candidate value now carries its source and date;
the newest dated value wins and the current website comes from the profile service.
Dev: `stale_headcount` 64 → 0, `old_domain` 31 → 0.
Promoted: `stale_headcount-Q1127536` (Consolidated Edison), `old_domain-Q11463` (Adobe).

## v4: guardrail + ICP rules from triage
Remaining silent errors were confident wrong matches (`Cliffs Natural Resources` → *Alpha* Natural Resources,
`Nuclear Corporation of America` → *Reinsurance Group of America*) and ICP misjudgments (`medical technology`
matched the keyword "technology"; `video conference` and `development and operations` matched nothing;
GlobalFoundries was judged by the Cayman Islands row instead of its listed US presence).
- Name-only matches must be strong and unambiguous (or corroborated by country); otherwise the row goes to human
  review instead of being guessed.
- ICP keywords rebuilt from the triaged dev failures, with an exclusion list; country checked against every listed
  country.
Test: silent error rate 6.9% → **0.0%**, at the cost of review rate 2.5% → 8.7%.
Promoted: `wrong_entity-Q5133094` (Cleveland-Cliffs), `wrong_entity-Q1645081` (Nucor),
`icp_false_positive-Q2357515` (Stryker), `icp_false_negative-Q17460900` (Zoom), `icp_false_negative-Q691592`
(GlobalFoundries).

## v5: LLM where rules run out
v4's review queue is now the biggest gap, and most of it is **former or alternate names with no website**
("Applied Molecular Genetics" = Amgen, "UberCab" = Uber, "Continental Oil Company" = ConocoPhillips). No string
matcher can resolve these. v5 asks an LLM to pick a candidate or name the current company, and then **verifies the
suggestion against the registry**: the model never asserts an entity on its own. Industry labels that no rule covers
also go to the model.
Model: `openai/gpt-oss-120b` on Groq, temperature 0, answers recorded to `data/llm_cache.jsonl` and replayed in CI.
Test: verified accuracy 89.2% → **95.6%**, review rate 8.7% → **3.7%**, silent errors still **0.0%**; 71 LLM calls,
about $0.01.
Dev surfaced one new silent error worth watching: `TELCO` (Tata Motors' old nickname) was resolved to Telefónica,
because the model's suggestion matched a real listed company. Registry verification checks that the suggestion
exists, not that it is the right company, so the next step is to require a second signal (country or ticker)
before accepting a model-suggested name.
