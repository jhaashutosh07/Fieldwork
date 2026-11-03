# Security

Fieldwork runs as a public demo: anyone can open the dashboard, try the agent on a company, and clean a small CSV.
This page is the threat model for that deployment and the controls that answer it.

## What is exposed

| Surface | Who | Notes |
|---|---|---|
| Dashboard (`/`, `/static/*`) | anyone | static files; no cookies, no accounts, no personal data |
| `POST /api/try`, `/api/try/stream`, `/api/batch` | anyone | run the shipped agent on visitor input; may call a paid LLM |
| `POST /api/runs` | anyone | re-run an eval on the frozen dataset (recorded LLM answers only) |
| Read APIs (`/api/history`, `/api/runs/...`, ...) | anyone | eval results over public Wikidata data |
| `POST /api/failures/{id}/promote` | operator only | writes a regression case to disk |

Secrets: one LLM API key (`GROQ_API_KEY`) and an admin token, both server-side environment variables. Neither is
ever sent to the browser, logged, or committed (`.env` is git-ignored; CI checks that no key is in the tree).

## Threats and controls

| Threat | Control | Where |
|---|---|---|
| **Cost abuse**: visitors burning the LLM budget | Recorded answers are free; new (paid) calls are capped per request (4 for a try, 25 for a CSV) and per day (`FIELDWORK_TRY_CAP`), then the agent falls back to rules and human review. Output tokens are capped per call. Public eval runs can't use a live model. | `api.BudgetedLLM`, `llm.MAX_COMPLETION_TOKENS` |
| **Denial of service** | Per-client token-bucket rate limits on every expensive endpoint; at most 4 agent runs at once (extra requests get `503`, not a queue); 64 concurrent connections max; request bodies capped at 128 KB, CSV at 60 KB / 50 rows; streams time out after 90 s; rate-limit state is pruned so memory stays bounded. | `security.py`, `cli.serve` |
| **Rate-limit bypass or lock-out behind the proxy** | Clients are identified by the *right-most* `X-Forwarded-For` entry (the one Render's proxy appends), not the spoofable left-most one, and not the proxy's own address. | `security.client_ip` |
| **Defacement / unauthorised writes** | The only write endpoint requires an admin token on the public demo, compared in constant time; Render generates the token. The UI hides the button in read-only mode. | `security.require_admin` |
| **XSS** | Every server value is HTML-escaped before rendering; no inline scripts or event handlers; strict Content-Security-Policy (`script-src 'self' https://cdnjs.cloudflare.com`, `object-src 'none'`, `base-uri 'none'`). | `static/app.js`, `security.CSP` |
| **Compromised CDN** | Chart.js is pinned to an exact version with a Subresource Integrity hash; the browser refuses a modified file. | `static/index.html` |
| **Clickjacking, MIME sniffing, referrer leaks** | `frame-ancestors 'none'` + `X-Frame-Options: DENY`, `nosniff`, strict referrer policy, `Permissions-Policy`, COOP/CORP, HSTS over HTTPS; API responses are `no-store`; no `Server` header. | `security.security_headers` |
| **Spreadsheet formula injection** in the downloaded CSV | Cells starting with `= + - @` (or tab/CR) are prefixed with `'`. | `static/app.js: csvCell` |
| **Prompt injection** via account names | Input is length-limited and JSON-encoded into the prompt. The model can only *suggest*: a match must be one of the registry's candidates (or resolve cleanly in the registry), an ICP verdict must be a boolean, and the guardrail sends weak evidence to review. Model text is never executed and is escaped when shown. | `agent.py` |
| **SSRF** | The agent never fetches visitor-supplied URLs; websites are only parsed for their hostname and looked up in a local index. | `tools.host` |
| **Injection into storage** | SQLAlchemy with bound parameters; IDs in URLs are validated against strict patterns. | `store.py`, `api.py` |
| **Malformed input** | Pydantic models with length limits; CSV parse errors return `400`, never a stack trace; internal errors are reported as `internal error`. | `api.py` |
| **Container escape impact** | Runs as an unprivileged user in a slim image. | `Dockerfile` |
| **Supply chain** | CI runs `pip-audit` (known CVEs in dependencies) and `bandit` (Python static analysis) on every push, with a least-privilege workflow token; Dependabot proposes weekly updates for pip, Actions and Docker. A failing scan blocks the deploy. | `.github/` |

## Known limits

- Rate limits are per process and in memory. That's fine for one instance; several instances would need a shared store
  (e.g. Redis) or the platform's edge limits.
- The demo has no user accounts by design. Anything a visitor submits is processed in memory and is not stored,
  apart from aggregate eval runs on the public dataset.

## Reporting

Please open a private security advisory on GitHub (Security → Report a vulnerability) rather than a public issue.
