# Changelog: how each version was earned

Every change below was motivated by failure clusters triaged on the **dev** split (`fieldwork triage <run>`).
The numbers that judge a change are always on the held-out **test** split (158 companies, 242 CRM rows), which no
fix was tuned on. Each fixed failure was promoted into `evals/regressions/` so it can never silently come back.

| Version | Test: verified accuracy | Test: silent error rate | Test: sent to review | Regression cases |
|---|---|---|---|---|
| v1 | 55.7% | 37.4% | 5.4% | 0/10 |
| v2 | 60.1% | 37.1% | 2.5% | 0/10 |
| v3 | 89.2% | 6.9% | 2.5% | 5/10 |
| v4 | 89.2% | **0.0%** | 8.7% | 10/10 |
| v5 (shipped) | **95.6%** | **0.0%** | 3.7% | 10/10 |
| v6 (rejected) | 91.1% | 0.7% | 6.2% | 10/10 |
| v7 (not shipped) | 95.6% | 0.0% | 3.7% | 10/10 |

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
Dev surfaced one silent error: `TELCO` (Tata Motors' old nickname) was resolved to Telefónica. The trace shows why:
shown a candidate list, the model reasoned "TELCO suggests a telecom company" and picked the telecom candidate. That
is a guess, not knowledge.

## v6: name it from knowledge (experiment, rejected)
Fix attempted for TELCO: no candidate list to anchor on. The model names the company from its own knowledge, must state
a basis (`former_name`, `abbreviation`, `same_name`; `guess` is refused) and a ticker, and the ticker and the name must
land on the same registry entry. On dev it removed TELCO and found ConocoPhillips, Uber and BioNTech, but traces also
showed the model confidently wrong when it claimed `subsidiary` (trivago → Expedia, AMI Semiconductor → Amkor), so
that basis was refused too.
On held-out test it lost **4.4 pts [−7.6, −1.3]** and added a silent error (Viacom → Paramount Global). The traces
explain why: the registry has shared tickers (two entities on `HPQ`, two on `AAL`), so "ticker must be unique" rejects
correct answers v5's candidate pick handled; and some "subsidiary" names really are old names of the parent
(First Data → Fiserv). **The CI gate fails it, so it never ships.** A fix tuned on dev that fails on held-out data is
exactly what the gate is for.

## v7: hybrid (experiment, not shipped)
v5's candidate pick first; v6's verified proposal only when the pick finds nothing. Dev +0.8 pts; held-out test
**+0.0 pts [0.0, 0.0]**, not significant, at 11% higher LLM cost. Not shipped: an experiment has to beat the shipped
version on held-out data, not just tie it.
