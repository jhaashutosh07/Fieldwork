"use strict";
// Fieldwork dashboard. Every value from the server is escaped before it touches innerHTML (see esc()).

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null || isNaN(x) ? "-" : (100 * x).toFixed(1) + "%");
const pp = (x) => (x == null ? "-" : (x >= 0 ? "+" : "") + (100 * x).toFixed(1));
const num = (v) => Number(v).toLocaleString("en-US");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const KINDS = new Set(["tool", "llm", "decision"]);

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `${r.status}`;
    try { const j = await r.json(); msg = j.detail || msg; } catch { /* not JSON */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.json();
}
// A random id per browser keeps this visitor's review queue and agent memory private (never shared with others).
const SESSION = (() => {
  const make = () => (crypto.randomUUID ? crypto.randomUUID() : "10000000-1000-4000-8000-100000000000".replace(/[018]/g,
    (c) => (c ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16)));
  try { let id = localStorage.getItem("fieldwork.session"); if (!id) { id = make(); localStorage.setItem("fieldwork.session", id); } return id; }
  catch { return make(); }
})();
const HDRS = { "Content-Type": "application/json", "X-Fieldwork-Session": SESSION };
const postJSON = (path, body) => api(path, { method: "POST", headers: HDRS, body: JSON.stringify(body) });
const getS = (path) => api(path, { headers: { "X-Fieldwork-Session": SESSION } });

const state = { config: { read_only: true }, history: [], insights: null, run: null, failures: [], trace: null, accounts: null, chartsDone: {} };

// ---------- tabs ----------
const TABS = ["overview", "clean", "review", "results", "insights", "monitoring", "explorer"];
const LEGACY = { try: "overview", batch: "clean", tests: "explorer" };
function currentTab() {
  const h = location.hash.replace("#", "");
  return TABS.includes(h) ? h : LEGACY[h] || "overview";
}
function showTab(tab, effects = true) {
  for (const t of TABS) {
    $(`view-${t}`).hidden = t !== tab;
    const a = document.querySelector(`.tabs a[data-tab="${t}"]`);
    a.setAttribute("aria-selected", String(t === tab));
  }
  if (!effects) return;   // first paint happens before the data has loaded
  if (tab === "results") drawResultsCharts();
  if (tab === "insights") drawInsightsCharts();
  if (tab === "review") loadReview();
  if (tab === "monitoring") startMonitoring(); else stopMonitoring();
  if (tab === "explorer" && !state.runsRequested) {
    state.runsRequested = true;
    loadRuns(new URLSearchParams(location.search).get("run")).catch((e) => { $("clusters").innerHTML = `<p class="empty">${esc(e.message)}</p>`; });
  }
}
window.addEventListener("hashchange", () => { showTab(currentTab()); window.scrollTo({ top: 0 }); });

// ---------- shared rendering ----------
const chip = (text, cls = "") => `<span class="chip ${cls}">${esc(text)}</span>`;
function lineChart(canvas, datasets, xLabel, yMin, yMax, yName, labels) {
  if (!window.Chart) return;
  const grid = { color: css("--line") }, ticks = { color: css("--muted"), callback: (v) => (labels ? labels[v] ?? v : v + "%") };
  new Chart(canvas, {
    type: "line",
    data: labels ? { labels, datasets } : { datasets },
    options: {
      responsive: true, maintainAspectRatio: false, interaction: { mode: labels ? "index" : "nearest", intersect: false },
      scales: {
        x: labels ? { ticks: { color: css("--muted") }, grid: { display: false } }
          : { type: "linear", min: 0, max: 100, title: { display: true, text: xLabel, color: css("--muted") }, ticks: { color: css("--muted"), callback: (v) => v + "%" }, grid },
        y: { min: yMin, max: yMax, ticks: { color: css("--muted"), callback: (v) => v + "%" }, grid },
      },
      plugins: {
        legend: { labels: { color: css("--ink"), boxWidth: 12, boxHeight: 12 } },
        tooltip: { callbacks: { label: (c) => labels ? `${c.dataset.label}: ${c.parsed.y.toFixed(1)}%` : `${c.dataset.label}: ${yName} ${c.parsed.y.toFixed(1)}% at ${c.parsed.x.toFixed(0)}%` } },
      },
    },
  });
}
const released = () => state.history.filter((h) => h.status !== "experiment");
const shippedEntry = () => { const r = released(); return [...r].reverse().find((h) => !h.uses_llm || h.llm_recorded) || r[r.length - 1]; };

// ---------- overview: big numbers ----------
function renderBig() {
  const r = released(), first = r[0], last = shippedEntry(), ins = state.insights;
  if (!first || !last) return;
  const cov = ins && ins.risk_coverage && ins.risk_coverage[last.version];
  const cost = ins && ins.versions ? ins.versions.find((v) => v.version === last.version) : null;
  const big = (from, to, label) => `<div class="bignum"><div class="n">${from ? `<span class="from">${esc(from)} → </span>` : ""}<span class="good">${esc(to)}</span></div><div class="l">${esc(label)}</div></div>`;
  $("bignums").innerHTML = [
    big(pct(first.test.verified_accuracy), pct(last.test.verified_accuracy), "companies fully verified"),
    big(pct(first.test.silent_error_rate), pct(last.test.silent_error_rate), "silent errors (wrong, unflagged)"),
    cov ? big("", pct(cov.operating.coverage), "of rows handled with no human") : "",
    cost ? big("", "$" + cost.cost.usd_per_1k_rows.toFixed(2), "LLM cost per 1,000 rows") : "",
  ].join("");
}

// ---------- overview: try the agent ----------
const EXAMPLES = [
  { n: "UberCab", c: "Netherlands", why: "former name" },
  { n: "adobe", w: "business.adobe.com", c: "United States", why: "old website" },
  { n: "Facebook", why: "renamed" },
  { n: "Zoom Communications", w: "http://zoom.com", c: "United States", why: "target customer?" },
  { n: "Cliffs Natural Resources", c: "United States", why: "look-alike trap" },
  { n: "Totsuko", c: "Japan", why: "watch it refuse to guess" },
];
const STAGES = ["Resolve", "Merge", "Reconcile", "Judge", "Guardrail"];
function stageOf(e) {
  if (e.kind === "tool" && (e.name === "domain_lookup" || e.name === "registry_search")) return 0;
  if (e.kind === "llm" && (e.name === "adjudicate_match" || e.name === "propose_company")) return 0;
  if (e.kind === "decision" && String(e.name).startsWith("llm_")) return 0;
  if (e.kind === "decision" && e.name === "resolve") return 1;
  if ((e.kind === "tool" && e.name === "company_profile") || (e.kind === "decision" && e.name === "reconcile")) return 2;
  if (e.kind === "llm" && e.name === "judge_icp_industry") return 3;
  if (e.kind === "decision" && e.name === "account") return 3;
  return null;
}
function drawPipe(active = -1, finalState = null) {
  $("pipe").innerHTML = STAGES.map((name, i) => {
    const cls = finalState ? (i === 4 ? finalState : "done") : i < active ? "done" : i === active ? "active" : "";
    const label = finalState && i === 4 ? (finalState === "review" ? "Guardrail: human review" : "Guardrail: verified ✓") : name;
    return `<span class="node ${cls}">${esc(label)}</span>`;
  }).join('<span class="arrow" aria-hidden="true">→</span>');
}
function stepLabel(e) {
  const a = e.args || {};
  if (e.kind === "tool" && e.name === "domain_lookup") return `Looked up website <b>${esc(a.domain)}</b>`;
  if (e.kind === "tool" && e.name === "registry_search") return `Searched the registry for <b>“${esc(a.query)}”</b>`;
  if (e.kind === "tool" && e.name === "company_profile") return "Fetched the company profile";
  if (e.kind === "llm") return `Asked the LLM: <b>${esc(String(e.name).replace(/_/g, " "))}</b> <span class="muted">(${e.replayed ? "recorded answer" : "live"})</span>`;
  return `Decision: <b>${esc(String(e.name).replace(/_/g, " "))}</b>`;
}
function briefResult(e) {
  const r = e.result;
  if (Array.isArray(r) && e.name === "registry_search") return r.slice(0, 3).map((h) => `${h.name} (${h.score})`).join(" · ") || "no matches";
  if (Array.isArray(r) && e.name === "domain_lookup") return r.map((h) => `${h.name} [${h.status} site]`).join(" · ") || "no company uses this website";
  if (e.name === "company_profile" && r && typeof r === "object") return `${r.name}; ${(r.employees || []).length} dated headcounts; ${(r.websites || []).length} websites on record`;
  return typeof r === "string" ? r : JSON.stringify(r);
}
function stepHtml(e) {
  const kind = KINDS.has(e.kind) ? e.kind : "";
  const ms = typeof e.ms === "number" ? ` <span class="muted">· ${e.ms} ms</span>` : "";
  return `<div class="ev ${kind}"><div class="head">${stepLabel(e)}${ms}</div><pre>${esc(briefResult(e))}</pre></div>`;
}
function resultCard(res) {
  const a = res.account, f = a.fields || {};
  const fv = (k, fmt = (v) => v) => f[k] && f[k].value != null
    ? `${esc(fmt(f[k].value))} <span class="src">${esc(f[k].source)}${f[k].as_of ? " · " + esc(f[k].as_of) : ""}</span>` : `<span class="muted">unknown</span>`;
  if (a.needs_review) {
    return `<div class="card"><h3>Sent to human review</h3><div class="verdict">${chip("not sure, so it did not guess", "meh")}</div>
      <div class="kv"><span class="k">Why</span><span>${esc(a.review_reason)}</span></div></div>`;
  }
  return `<div class="card"><h3>${esc(f.name && f.name.value)}</h3>
    <div class="verdict">${a.icp_fit ? chip("target customer", "ok") : chip("not a target customer", "no")} ${chip("confidence " + a.confidence)} ${chip("via " + a.resolved_via)}</div>
    <div class="kv"><span class="k">Ticker</span><span>${fv("ticker")}</span><span class="k">Website</span><span>${fv("domain")}</span>
      <span class="k">Employees</span><span>${fv("employees", num)}</span><span class="k">Country</span><span>${fv("country")}</span>
      <span class="k">Industries</span><span>${esc((a.industries || []).join(", ") || "-")}</span>
      <span class="k">Fit reason</span><span class="muted">${esc(a.icp_reason)}</span></div></div>`;
}
function renderTry(res) {
  const steps = (res.steps || []).map(stepHtml).join("");
  $("tryOut").innerHTML = `<div class="result">${resultCard(res)}<div><div class="meta">${res.steps.length} steps · ${res.tool_calls} tool calls · ${res.llm_calls} LLM calls (${esc(res.llm)})${res.note ? " · " + esc(res.note) : ""}</div><div class="trace tablewrap">${steps}</div></div></div>`;
}
async function runTry(ev) {
  if (ev) ev.preventDefault();
  const body = { account_name: $("tName").value.trim(), website: $("tSite").value.trim(), hq_country: $("tCountry").value.trim() };
  if (!body.account_name) return;
  $("tBtn").disabled = true; $("tBtn").textContent = "Running…";
  drawPipe(0);
  $("tryOut").innerHTML = `<div class="result"><div class="card"><div class="working"><span class="spinner"></span>Working on “${esc(body.account_name)}”…</div></div>
    <div><div class="meta" id="liveMeta">Streaming each step as it happens</div><div class="trace tablewrap" id="liveSteps"></div></div></div>`;
  let stage = 0, final = null, failed = null;
  try {
    const res = await fetch("/api/try/stream", { method: "POST", headers: HDRS, body: JSON.stringify(body) });
    if (!res.ok || !res.body) {
      let msg = `${res.status}`; try { msg = (await res.json()).detail || msg; } catch { /* not JSON */ }
      throw Object.assign(new Error(msg), { status: res.status });
    }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "", chain = Promise.resolve();
    const handle = async (kind, data) => {
      if (kind === "step") {
        if (data.kind === "reset") { $("liveSteps").innerHTML = ""; $("liveMeta").textContent = data.note || ""; stage = 0; drawPipe(0); return; }
        const st = stageOf(data);
        if (st != null && st >= stage) { stage = st; drawPipe(stage); }
        $("liveSteps").insertAdjacentHTML("beforeend", stepHtml(data));
        $("liveSteps").scrollTop = $("liveSteps").scrollHeight;
        await sleep(160);   // pace the replay so each step is readable; the real timing is shown on each step
      } else if (kind === "done") final = data;
      else if (kind === "error") failed = data.error || "error";
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        const kind = (block.match(/^event: (.*)$/m) || [])[1], data = (block.match(/^data: (.*)$/m) || [])[1];
        if (kind && data) { const parsed = JSON.parse(data); chain = chain.then(() => handle(kind, parsed)); }
      }
    }
    await chain;
    if (failed) throw new Error(failed);
    if (final) {
      await sleep(120); drawPipe(4, final.account.needs_review ? "review" : "done"); renderTry(final);
      if (final.queued_for_review) noteQueued(final.queued_for_review);
    }
  } catch (e) {
    drawPipe(-1);
    $("tryOut").innerHTML = `<p class="empty">${esc(String(e.message).slice(0, 200))}</p>`;
  } finally { $("tBtn").disabled = false; $("tBtn").textContent = "Run agent"; }
}
function setupTry() {
  $("tryForm").addEventListener("submit", runTry);
  $("examples").insertAdjacentHTML("beforeend", EXAMPLES.map((x, i) => `<button type="button" data-i="${i}">${esc(x.n)} <span class="why">· ${esc(x.why)}</span></button>`).join(""));
  $("examples").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-i]"); if (!b) return;
    const x = EXAMPLES[Number(b.dataset.i)];
    $("tName").value = x.n; $("tSite").value = x.w || ""; $("tCountry").value = x.c || ""; runTry();
  });
  drawPipe(-1);
}

// ---------- clean a CSV ----------
const SAMPLE_CSV = `account_name,website,hq_country,employee_count,last_updated
adobe,business.adobe.com,United States,11144,2012-01-01
ADBE,business.adobe.com,United States,11847,2013-11-01
Marvell,,United States,7000,2025-01-01
MARVELL TECHNOLOGY,,United States,,2026-01-01
GLOBALFOUNDRIES,http://gf.com,Cayman Islands,14600,2021-12-31
globalfoundries,,United States,14600,2021-12-31
UberCab,,Netherlands,12000,2017-12-01
"Consolidated Edison, Inc.",,United States,15541,2010-01-01
Zoom Communications,http://zoom.com,United States,2000,2019-01-01
Cliffs Natural Resources,,United States,30000,2024-01-01
Target Store,,,400000,2025-01-01
Totsuko,,Japan,113000,2023-03-31`;
let batchResult = null;
// Spreadsheet formula injection: cells starting with = + - @ (or tab / CR) are prefixed with ' so Excel won't execute them.
const csvCell = (v) => {
  let s = String(v ?? "");
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};
const fieldCell = (f, fmt = (v) => v) => !f || f.value == null ? `<span class="muted">-</span>`
  : `${esc(fmt(f.value))}<span class="sub">${esc(f.source)}${f.as_of ? " · " + esc(f.as_of) : ""}</span>`;
function renderBatch(d) {
  const s = d.summary;
  const stat = (n, label) => `<span class="stat"><b>${esc(n)}</b>${esc(label)}</span>`;
  $("bSummary").innerHTML = [stat(s.rows, "CRM rows"), stat(s.accounts, "accounts"), stat(s.merged_rows, "duplicates merged"),
    stat(s.target_customers, "target customers"), stat(s.needs_review, "sent to review"),
    `<span class="stat">${esc(s.llm_calls)} LLM calls (${esc(d.llm)}) · ${esc(s.seconds)}s</span>`].join("")
    + (d.note ? `<span class="stat bad">${esc(d.note)}</span>` : "");
  $("bOut").innerHTML = "<thead><tr><th>From CRM rows</th><th>Company</th><th>Website</th><th>Employees</th><th>Country</th><th>Status</th></tr></thead><tbody>" +
    d.accounts.map((a) => `<tr><td>${a.rows.map((r) => esc(r.account_name)).join("<br>")}${a.rows.length > 1 ? `<span class="sub">${chip(a.rows.length + " merged")}</span>` : ""}</td>
      <td>${a.needs_review ? `<span class="muted">?</span>` : `<b>${esc(a.name)}</b>${a.ticker ? `<span class="sub">${esc(a.ticker)}</span>` : ""}`}</td>
      <td>${a.needs_review ? "" : fieldCell(a.domain)}</td><td>${a.needs_review ? "" : fieldCell(a.employees, num)}</td>
      <td>${a.needs_review ? "" : fieldCell(a.country)}</td>
      <td>${a.needs_review ? `${chip("needs review", "meh")}<span class="sub">${esc(a.review_reason)}</span>` : a.icp_fit ? chip("target customer", "ok") : chip("verified")}</td></tr>`).join("") + "</tbody>";
  $("bPanel").hidden = false;
}
function downloadBatch() {
  if (!batchResult) return;
  const head = ["crm_rows", "status", "company", "ticker", "website", "employees", "employees_as_of", "employees_source", "country", "target_customer", "review_reason"];
  const lines = batchResult.accounts.map((a) => [a.rows.map((r) => r.account_name).join(" | "), a.needs_review ? "needs_review" : "verified",
    a.name, a.ticker, a.domain && a.domain.value, a.employees && a.employees.value, a.employees && a.employees.as_of, a.employees && a.employees.source,
    a.country && a.country.value, a.needs_review ? "" : a.icp_fit, a.review_reason].map(csvCell).join(","));
  const url = URL.createObjectURL(new Blob([[head.join(","), ...lines].join("\n")], { type: "text/csv" }));
  const link = Object.assign(document.createElement("a"), { href: url, download: "fieldwork_cleaned.csv" });
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function runBatch() {
  if (!$("bCsv").value.trim()) $("bCsv").value = SAMPLE_CSV;
  $("bRun").disabled = true; $("bRun").textContent = "Cleaning…";
  $("bSummary").innerHTML = `<span class="stat working"><span class="spinner"></span>Resolving, merging and verifying…</span>`;
  try {
    batchResult = await postJSON("/api/batch", { csv: $("bCsv").value });
    renderBatch(batchResult); $("bDownload").disabled = false;
    if (batchResult.queued_for_review) noteQueued(batchResult.queued_for_review);
  } catch (e) {
    $("bSummary").innerHTML = `<span class="stat bad">${esc(String(e.message).slice(0, 200))}</span>`;
    $("bPanel").hidden = true;
  } finally { $("bRun").disabled = false; $("bRun").textContent = "Clean it"; }
}
function setupBatch() {
  $("bCsv").value = SAMPLE_CSV;
  $("bSample").addEventListener("click", () => { $("bCsv").value = SAMPLE_CSV; });
  $("bFile").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    if (f.size > 60000) { $("bSummary").innerHTML = `<span class="stat bad">File too large for the demo (60 KB max)</span>`; return; }
    $("bCsv").value = await f.text();
  });
  $("bDownload").addEventListener("click", downloadBatch);
  $("bRun").addEventListener("click", runBatch);
}

// ---------- review queue ----------
function noteQueued(n) {
  const b = $("reviewBadge"); b.hidden = false; b.textContent = String((Number(b.textContent) || 0) + n);
  state.reviewStale = true;
}
function renderLearning() {
  const L = state.insights && state.insights.learning; if (!L) return;
  const a = L.month2_without_memory, b = L.month2_with_memory;
  const kpi = (label, from, to, sub) => `<div class="kpi"><div class="label">${esc(label)}</div><div class="value good"><span class="from">${esc(from)} → </span>${esc(to)}</div><span class="sub">${esc(sub)}</span></div>`;
  $("learnKpis").innerHTML = [
    kpi("Repeat questions to reviewers", a.repeat_questions, b.repeat_questions, "rows a human already answered last month"),
    kpi("Rows sent to review", a.review_rows, b.review_rows, `of ${L.month2_rows} rows next month`),
    kpi("Verified accuracy", pct(a.verified_accuracy), pct(b.verified_accuracy), L.vs_without_memory ? `${pp(L.vs_without_memory.verified_accuracy.delta)} pts [${pp(L.vs_without_memory.verified_accuracy.ci[0])}, ${pp(L.vs_without_memory.verified_accuracy.ci[1])}]` : ""),
    kpi("Silent errors", pct(a.silent_error_rate), pct(b.silent_error_rate), "memory never overrides a website match"),
  ].join("");
  $("learnNote").innerHTML = `A reviewer resolved the ${esc(L.reviewed_rows)} rows the agent sent to review this month; those decisions became ${esc(L.memory_entries)} memory entries. Next month's export (${esc(L.month2_rows)} rows: last month's rows plus new ones) hit memory ${esc(L.memory_hits)} times. The ${esc(b.repeat_questions)} questions that still come back are companies with no country or website on record: memory fixes <i>which</i> company it is, not missing data.`;
}
async function loadReview(force = false) {
  if (state.reviewLoading || (state.reviewLoaded && !force && !state.reviewStale)) return;
  state.reviewLoading = true;
  try {
    const q = await getS("/api/review");
    state.review = q; state.reviewLoaded = true; state.reviewStale = false;
    $("reviewBadge").hidden = true; $("reviewBadge").textContent = "";
    renderQueue();
  } catch (e) { $("queue").innerHTML = `<p class="empty">${esc(e.message)}</p>`; }
  finally { state.reviewLoading = false; }
}
function renderQueue() {
  const items = state.review.items;
  const pending = items.filter((i) => i.status === "pending"), decided = items.filter((i) => i.status === "decided");
  $("reviewCounts").innerHTML = `${chip(pending.length + " to review", pending.length ? "meh" : "ok")} ${chip(decided.length + " decided")} ${chip(state.review.memory_entries + " in agent memory", "ok")}`;
  $("rerunBtn").disabled = !decided.length;
  if (!items.length) { $("queue").innerHTML = `<p class="empty">Nothing to review. Try a company the agent can't place, e.g. “Totsuko”.</p>`; return; }
  $("queue").innerHTML = items.map((i) => {
    const r = i.row;
    const head = `<div class="qhead"><b>${esc(r.account_name)}</b><span class="qrow">${r.website ? esc(r.website) + " · " : ""}${r.hq_country ? esc(r.hq_country) : "no country"}</span>${chip(i.source === "eval" ? "from the eval" : "from your " + i.source, "")}</div>`;
    if (i.status !== "pending") {
      return `<div class="qitem done">${head}<div>${i.status === "decided" ? `${chip("resolved", "ok")} ${esc(i.company && i.company.name)}` : chip("skipped")}</div></div>`;
    }
    const cands = (i.candidates || []).map((c) => `<button type="button" class="cand" data-item="${esc(i.id)}" data-company="${esc(c.id)}">${esc(c.name)} <span class="muted">${esc([c.ticker, c.country].filter(Boolean).join(" · "))}</span></button>`).join("");
    return `<div class="qitem">${head}<div class="qrow">${chip("agent unsure", "meh")} ${esc(i.reason)}</div>
      <div class="cands">${cands || `<span class="muted">No candidates; search below.</span>`}</div>
      <div class="qsearch"><input type="search" placeholder="Search the registry, e.g. Sony" data-search="${esc(i.id)}" aria-label="Search companies"><button type="button" class="btn small" data-skip="${esc(i.id)}">Skip</button></div>
      <div class="cands" data-results="${esc(i.id)}"></div></div>`;
  }).join("");
}
async function decide(itemId, companyId) {
  try { await postJSON(`/api/review/${encodeURIComponent(itemId)}/decide`, { company_id: companyId }); }
  catch (e) { alert(e.message); return; }
  state.reviewStale = true; await loadReview(true);
}
function setupReview() {
  $("queue").addEventListener("click", (e) => {
    const c = e.target.closest(".cand[data-company]"); if (c) return decide(c.dataset.item, c.dataset.company);
    const sk = e.target.closest("button[data-skip]"); if (sk) return decide(sk.dataset.skip, null);
  });
  let timer = null;
  $("queue").addEventListener("input", (e) => {
    const inp = e.target.closest("input[data-search]"); if (!inp) return;
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const box = document.querySelector(`[data-results="${CSS.escape(inp.dataset.search)}"]`);
      if (inp.value.trim().length < 2) { box.innerHTML = ""; return; }
      try {
        const hits = await getS(`/api/companies?q=${encodeURIComponent(inp.value.trim())}`);
        box.innerHTML = hits.map((c) => `<button type="button" class="cand" data-item="${esc(inp.dataset.search)}" data-company="${esc(c.id)}">${esc(c.name)} <span class="muted">${esc([c.ticker, c.country].filter(Boolean).join(" · "))}</span></button>`).join("") || `<span class="muted">No match.</span>`;
      } catch (err) { box.innerHTML = `<span class="muted">${esc(err.message)}</span>`; }
    }, 250);
  });
  $("rerunBtn").addEventListener("click", async () => {
    $("rerunBtn").disabled = true; $("rerunBtn").textContent = "Re-running…";
    try {
      const r = await postJSON("/api/review/rerun", {});
      $("rerunOut").innerHTML = `<div class="tablewrap short"><table><thead><tr><th>CRM row</th><th>Now resolves to</th><th>How</th></tr></thead><tbody>` +
        r.results.map((x) => `<tr><td>${esc(x.row.account_name)}</td><td>${x.company_id ? `<b>${esc(x.name)}</b>` : `<span class="muted">-</span>`}${x.needs_review ? `<span class="sub">${chip("still needs review", "meh")} ${esc(x.review_reason)}</span>` : ""}</td>
          <td>${x.via === "review_memory" ? chip("learned from your review", "ok") : chip(x.via || "-")}</td></tr>`).join("") + "</tbody></table></div>";
    } catch (e) { $("rerunOut").innerHTML = `<p class="empty">${esc(e.message)}</p>`; }
    finally { $("rerunBtn").disabled = false; $("rerunBtn").textContent = "Re-run decided rows"; }
  });
}

// ---------- monitoring ----------
const monCharts = {};
function countChart(id, labels, datasets) {
  if (!window.Chart) return;
  if (monCharts[id]) { const c = monCharts[id]; c.data.labels = labels; datasets.forEach((d, i) => { c.data.datasets[i].data = d.data; }); c.update("none"); return; }
  const grid = { color: css("--line") };
  monCharts[id] = new Chart($(id), { type: "line", data: { labels, datasets: datasets.map((d) => ({ ...d, backgroundColor: d.borderColor, pointRadius: 0, borderWidth: 2, tension: .3, spanGaps: true })) },
    options: { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
      scales: { x: { ticks: { color: css("--muted"), maxTicksLimit: 6 }, grid: { display: false } }, y: { beginAtZero: true, ticks: { color: css("--muted"), precision: 0 }, grid } },
      plugins: { legend: { labels: { color: css("--ink"), boxWidth: 10, boxHeight: 10 } } } } });
}
async function loadMonitoring() {
  const m = await api(`/api/metrics?minutes=${encodeURIComponent($("monWindow").value)}`);
  const w = m.window, up = m.uptime_s;
  const kpi = (label, value, sub) => `<div class="kpi"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><span class="sub">${esc(sub)}</span></div>`;
  $("monKpis").innerHTML = [
    kpi("Requests", w.requests, `${w.errors} errors (${w.error_rate == null ? "-" : pct(w.error_rate)})`),
    kpi("p95 latency", w.p95_ms == null ? "-" : `${Math.round(w.p95_ms)} ms`, `p50 ${w.p50_ms == null ? "-" : Math.round(w.p50_ms) + " ms"}`),
    kpi("CRM rows processed", w.rows, "by visitors in this window"),
    kpi("Review rate", w.review_rate == null ? "-" : pct(w.review_rate), `${w.review_rows} rows sent to a human`),
    kpi("LLM calls", w.llm_live + w.llm_recorded, `${w.llm_live} live · $${Number(w.cost_usd).toFixed(4)} spent`),
  ].join("");
  $("monMeta").textContent = `up ${up >= 3600 ? Math.floor(up / 3600) + "h " : ""}${Math.floor((up % 3600) / 60)}m · tracing: ${m.tracing}`;
  const step = m.series.length > 180 ? Math.ceil(m.series.length / 120) : 1;   // downsample long windows
  const S = [];
  for (let i = 0; i < m.series.length; i += step) {
    const chunk = m.series.slice(i, i + step);
    const sum = (k) => chunk.reduce((a, b) => a + (b[k] || 0), 0);
    const p95 = chunk.map((b) => b.p95_ms).filter((x) => x != null);
    S.push({ t: chunk[0].t, requests: sum("requests"), errors: sum("errors"), rows: sum("rows"), review_rows: sum("review_rows"),
             llm_live: sum("llm_live"), llm_recorded: sum("llm_recorded"), p95_ms: p95.length ? Math.max(...p95) : null });
  }
  const labels = S.map((b) => new Date(b.t * 1000).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" }));
  countChart("monReq", labels, [{ label: "requests", data: S.map((b) => b.requests), borderColor: css("--accent") }, { label: "errors", data: S.map((b) => b.errors), borderColor: css("--bad") }]);
  countChart("monLat", labels, [{ label: "p95 ms", data: S.map((b) => b.p95_ms), borderColor: css("--warn") }]);
  countChart("monRows", labels, [{ label: "rows", data: S.map((b) => b.rows), borderColor: css("--accent") }, { label: "sent to review", data: S.map((b) => b.review_rows), borderColor: css("--warn") }]);
  countChart("monLlm", labels, [{ label: "live", data: S.map((b) => b.llm_live), borderColor: css("--good") }, { label: "recorded", data: S.map((b) => b.llm_recorded), borderColor: css("--muted") }]);
  $("monEndpoints").innerHTML = "<thead><tr><th>Endpoint</th><th class='num'>Requests</th></tr></thead><tbody>" +
    (Object.entries(m.endpoints).map(([k, v]) => `<tr><td><code>${esc(k)}</code></td><td class="num">${esc(v)}</td></tr>`).join("") || `<tr><td colspan="2" class="muted">No traffic yet. Try the agent on the Overview tab, then come back.</td></tr>`) + "</tbody>";
}
function startMonitoring() {
  if (state.monTimer) return;
  const tick = () => loadMonitoring().catch((e) => { $("monMeta").textContent = e.message; });
  tick(); state.monTimer = setInterval(() => { if (!document.hidden) tick(); }, 10000);
}
function stopMonitoring() { clearInterval(state.monTimer); state.monTimer = null; }

// ---------- results ----------
function renderResults() {
  const hist = state.history, last = shippedEntry(), first = hist[0];
  if (!last) { $("kpis").innerHTML = `<p class="empty">No history yet.</p>`; return; }
  const kpi = (label, value, sub, cls = "") => `<div class="kpi"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div><span class="sub">${esc(sub)}</span></div>`;
  $("kpis").innerHTML = [
    kpi("Verified accuracy", pct(last.test.verified_accuracy), `${first.version}: ${pct(first.test.verified_accuracy)}`, "good"),
    kpi("Silent error rate", pct(last.test.silent_error_rate), `${first.version}: ${pct(first.test.silent_error_rate)}`, "good"),
    kpi("Sent to review", pct(last.test.review_rate), "refused to guess"),
    kpi("Regression cases", last.regression_cases, "all pass in CI"),
    kpi("ICP precision", pct(last.test.icp_precision), `recall ${pct(last.test.icp_recall)}`),
  ].join("");
  $("versions").innerHTML = "<thead><tr><th>Version</th><th>Change</th><th class='num'>Verified</th><th class='num'>Silent err.</th></tr></thead><tbody>" +
    hist.map((h) => `<tr><td>${chip(h.version)}${h.status === "experiment" ? `<span class="sub">${chip("experiment", "meh")}</span>` : h.version === last.version ? `<span class="sub">${chip("shipped", "ok")}</span>` : ""}</td>
      <td>${esc(h.summary)}</td><td class="num">${pct(h.test.verified_accuracy)}</td><td class="num">${pct(h.test.silent_error_rate)}</td></tr>`).join("") + "</tbody>";
  const ins = state.insights;
  if (ins && ins.versions) {
    $("sig").innerHTML = "<thead><tr><th>Version</th><th>Verified (95% CI)</th><th>Gain vs previous</th><th>Silent errors (95% CI)</th></tr></thead><tbody>" +
      ins.versions.map((v) => `<tr><td>${chip(v.version)}</td>
        <td>${pct(v.verified_accuracy)} <span class="muted">[${pct(v.ci.verified_accuracy[0])}, ${pct(v.ci.verified_accuracy[1])}]</span></td>
        <td>${v.vs_prev ? sigChip(v.vs_prev.verified_accuracy) : `<span class="muted">baseline</span>`}</td>
        <td>${pct(v.silent_error_rate)} <span class="muted">[${pct(v.ci.silent_error_rate[0])}, ${pct(v.ci.silent_error_rate[1])}]</span></td></tr>`).join("") + "</tbody>";
    const s = ins.versions[ins.versions.length - 1];
    $("sigNote").innerHTML = `${esc(ins.companies)} held-out companies, ${num(ins.bootstrap_resamples)} bootstrap resamples. ${esc(s.version)} made <b>0 silent errors</b>; with this sample the true rate is at most <b>${pct(s.ci.silent_error_rate[1])}</b> (95%).`;
  }
}
function sigChip(d) {
  if (d.delta === 0 && d.ci[0] === 0 && d.ci[1] === 0) return `${pp(0)} pts ${chip("identical", "ok")}`;
  const good = d.ci[0] > 0, bad = d.ci[1] < 0;
  return `${pp(d.delta)} pts <span class="muted">[${pp(d.ci[0])}, ${pp(d.ci[1])}]</span> ${chip(good ? "real" : bad ? "worse" : "not significant", good ? "ok" : bad ? "no" : "meh")}`;
}
function drawResultsCharts() {
  if (state.chartsDone.results || !state.history.length) return;
  state.chartsDone.results = true;
  const r = released();
  lineChart($("chart"), [
    { label: "Verified accuracy", data: r.map((h) => 100 * h.test.verified_accuracy), borderColor: css("--good"), backgroundColor: css("--good"), tension: .3 },
    { label: "Silent error rate", data: r.map((h) => 100 * h.test.silent_error_rate), borderColor: css("--bad"), backgroundColor: css("--bad"), tension: .3 },
    { label: "Sent to review", data: r.map((h) => 100 * h.test.review_rate), borderColor: css("--warn"), backgroundColor: css("--warn"), borderDash: [4, 4], tension: .3 },
  ], "", 0, 100, "", r.map((h) => h.version));
}

// ---------- insights ----------
const palette = { v1: "--muted", v3: "--warn", v4: "--accent", v5: "--good" };
function renderInsights() {
  const ins = state.insights; if (!ins || !ins.versions) return;
  const s = ins.versions[ins.versions.length - 1];
  const cur = ins.risk_coverage[s.version] || Object.values(ins.risk_coverage).pop();
  $("riskNote").innerHTML = `Dots mark where each version hands off to review. <b>${esc(s.version)}</b> handles <b>${pct(cur.operating.coverage)}</b> of rows unattended at <b>${pct(cur.operating.error)}</b> error; forcing 100% automation would cost <b>${pct(cur.full_automation_error)}</b> errors.`;
  const st = ins.stress, n = st.rates.length - 1, s5 = st.series[s.version], s4 = st.series.v4, s1 = st.series.v1;
  $("stressNote").innerHTML = `With every website removed, <b>${esc(s.version)} still verifies ${pct(s5.verified_accuracy[n])}</b> (rules-only v4: ${pct(s4.verified_accuracy[n])}). v3 and v4 overlap; v4 sends doubtful rows to review instead of getting them wrong. v1 <i>improves</i> without websites (${pct(s1.verified_accuracy[0])} → ${pct(s1.verified_accuracy[n])}) because its CRM websites were stale.`;
  $("experiments").innerHTML = "<thead><tr><th>Experiment</th><th>Verified vs shipped</th><th>Decision</th></tr></thead><tbody>" +
    ins.experiments.map((x) => `<tr><td>${chip(x.version)}<span class="sub">${esc(x.summary)}</span></td><td>${sigChip(x.vs_shipped.verified_accuracy)}</td>
      <td>${chip(x.decision, x.decision === "ship" ? "ok" : x.decision === "rejected" ? "no" : "meh")}<span class="sub">${esc(x.reason)}</span></td></tr>`).join("") + "</tbody>";
  const money = (x) => (x == null ? "-" : "$" + x.toFixed(x > 0 && x < 0.01 ? 5 : 3));
  $("cost").innerHTML = "<thead><tr><th>Version</th><th class='num'>LLM calls / 1k rows</th><th class='num'>$ / 1k rows</th><th class='num'>$ / verified account</th></tr></thead><tbody>" +
    ins.versions.filter((v) => ["v1", "v4", s.version].includes(v.version)).map((v) => `<tr><td>${chip(v.version)}</td><td class="num">${esc(v.cost.llm_calls_per_1k_rows)}</td>
      <td class="num">${money(v.cost.usd_per_1k_rows)}</td><td class="num">${money(v.cost.usd_per_verified_account)}</td></tr>`).join("") + "</tbody>";
  $("costNote").innerHTML = `Model: <b>${esc(ins.llm.model)}</b>; median live call ${ins.llm.median_live_ms ? (ins.llm.median_live_ms / 1000).toFixed(2) + " s" : "-"}.`;
}
function renderFrameworks() {
  const fw = state.frameworks; if (!fw || !fw.systems) return;
  $("fwSub").textContent = `${fw.sample_companies} randomly sampled held-out companies (${fw.sample_rows} CRM rows), scored by the same eval. Model: ${fw.model}.`;
  const money = (x) => "$" + Number(x).toFixed(3);
  $("frameworks").innerHTML = "<thead><tr><th>System</th><th class='num'>Verified</th><th>vs pipeline</th><th class='num'>Silent errors</th><th class='num'>Review</th><th class='num'>LLM calls / row</th><th class='num'>Tokens / row</th><th class='num'>$ / 1k rows</th></tr></thead><tbody>" +
    fw.systems.map((r) => `<tr><td><b>${esc(r.system)}</b></td><td class="num">${pct(r.verified_accuracy)}</td>
      <td>${r.vs_pipeline ? sigChip(r.vs_pipeline.verified_accuracy) : `<span class="muted">baseline</span>`}</td>
      <td class="num">${pct(r.silent_error_rate)} <span class="muted">[≤ ${pct(r.silent_error_ci[1])}]</span></td><td class="num">${pct(r.review_rate)}</td>
      <td class="num">${esc(r.llm_calls_per_row)}</td><td class="num">${num(r.tokens_per_row)}</td><td class="num">${money(r.usd_per_1k_rows)}</td></tr>`).join("") + "</tbody>";
  const tool = fw.systems[fw.systems.length - 1], pipe = fw.systems[0], graph = fw.systems[1];
  const types = Object.entries(tool.failure_types || {}).filter(([k]) => k !== "sent_to_review").map(([k, v]) => `${esc(k)} (${esc(v)})`).join(", ");
  $("fwNote").innerHTML = `The LangGraph port reproduces the pipeline exactly (${pct(graph.verified_accuracy)}), so the framework is a packaging choice. ` +
    `The autonomous agent decides its own tool calls: ${pct(tool.verified_accuracy)} verified with ${pct(tool.silent_error_rate)} silent errors, at ${esc(tool.llm_calls_per_row)} LLM calls and ${num(tool.tokens_per_row)} tokens per row ` +
    `(pipeline: ${esc(pipe.llm_calls_per_row)} calls, ${num(pipe.tokens_per_row)} tokens). ${types ? "Where it went wrong without saying so: " + types + "." : ""}`;
}

function drawInsightsCharts() {
  const ins = state.insights;
  if (state.chartsDone.insights || !ins || !ins.versions) return;
  state.chartsDone.insights = true;
  lineChart($("riskChart"), Object.entries(ins.risk_coverage).map(([v, c]) => ({
    label: v, data: c.points.map((p) => ({ x: 100 * p.coverage, y: 100 * p.error })), borderColor: css(palette[v] || "--muted"), backgroundColor: css(palette[v] || "--muted"),
    pointRadius: c.points.map((p) => (c.operating && p.coverage === c.operating.coverage && p.error === c.operating.error ? 6 : 0)), tension: .15,
  })), "rows handled automatically", 0, 12, "error");
  const st = ins.stress;
  lineChart($("stressChart"), Object.entries(st.series).map(([v, s]) => ({
    label: v, data: s.verified_accuracy.map((y, i) => ({ x: 100 * st.rates[i], y: 100 * y })), borderColor: css(palette[v] || "--muted"), backgroundColor: css(palette[v] || "--muted"), tension: .2,
  })), "CRM rows with the website removed", 40, 100, "verified");
}

// ---------- explorer ----------
async function loadRuns(select) {
  const runs = await api("/api/runs");
  $("runSel").innerHTML = runs.map((r) => `<option value="${esc(r.id)}">${esc(r.version)} · ${esc(r.split)} · ${pct(r.metrics.verified_accuracy)} · ${esc(String(r.started_at).replace("T", " "))}</option>`).join("");
  const last = shippedEntry();
  const pick = [select, last && last.test_run].find((id) => id && runs.some((r) => r.id === id)) || (runs[0] && runs[0].id);
  if (pick) { $("runSel").value = pick; await openRun(pick); }
  loadCases();
}
async function openRun(id) {
  const run = await api(`/api/runs/${encodeURIComponent(id)}`);
  Object.assign(state, { run, failures: await api(`/api/runs/${encodeURIComponent(id)}/failures`), trace: null, accounts: null });
  const m = run.metrics;
  const rows = [["Verified accuracy", pct(m.verified_accuracy)], ["Silent error rate", pct(m.silent_error_rate)], ["Sent to review", pct(m.review_rate)],
    ["Entity accuracy", pct(m.entity_accuracy)], ["Dedup F1", pct(m.dedup_f1)], ["ICP precision / recall", `${pct(m.icp_precision)} / ${pct(m.icp_recall)}`],
    ["LLM calls / cost", `${m.llm_calls} / $${Number(m.cost_usd || 0).toFixed(4)}`], ["Tool calls / time", `${run.tool_calls} / ${Math.round(run.duration_ms)} ms`]];
  $("runMetrics").innerHTML = rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${esc(v)}</td></tr>`).join("");
  const max = Math.max(1, ...run.clusters.map((c) => c.count));
  $("clusters").innerHTML = run.clusters.length ? run.clusters.map((c) =>
    `<button type="button" class="bar" data-sig="${esc(c.signature)}"><span class="name" title="${esc(c.signature)}">${esc(c.signature)}</span>
     <span class="track"><span class="fill" style="width:${(100 * c.count / max).toFixed(1)}%"></span></span><span class="num">${esc(c.count)}</span></button>`).join("")
    : `<p class="empty">No failures in this run.</p>`;
  showFailures(run.clusters[0] && run.clusters[0].signature);
}
function showFailures(sig) {
  document.querySelectorAll("#clusters .bar").forEach((b) => b.classList.toggle("active", b.dataset.sig === sig));
  const list = state.failures.filter((f) => f.signature === sig);
  $("failTitle").textContent = sig ? `Failures: ${sig}` : "Failures";
  $("failSub").textContent = state.config.read_only ? "Click a failure to replay its trace. (Promoting to a test is disabled on the public demo.)"
    : "Click a failure to replay its trace, or promote it to a regression test.";
  $("failures").innerHTML = "<thead><tr><th>Company</th><th>CRM rows</th><th>Expected → got</th><th></th></tr></thead><tbody>" + list.map((f, i) =>
    `<tr class="click" data-i="${i}"><td>${esc(f.company)}</td><td class="muted">${f.rows.map((r) => esc(r.account_name)).join("<br>")}</td>
     <td>${f.field ? `<span class="good">${esc(JSON.stringify(f.expected))}</span> → <span class="bad">${esc(JSON.stringify(f.got))}</span>` : `<span class="muted">${esc(f.detail)}</span>`}</td>
     <td>${f.promoted_case ? chip("case") : state.config.read_only ? "" : `<button class="btn small" type="button" data-promote="${esc(f.id)}">Promote</button>`}</td></tr>`).join("") + "</tbody>";
  $("failures").onclick = (e) => {
    const btn = e.target.closest("button[data-promote]");
    if (btn) return promote(btn);
    const tr = e.target.closest("tr.click"); if (tr) showTrace(list[Number(tr.dataset.i)]);
  };
}
async function promote(btn) {
  btn.disabled = true;
  try {
    const res = await api(`/api/failures/${encodeURIComponent(btn.dataset.promote)}/promote`, { method: "POST" });
    const f = state.failures.find((x) => x.id === btn.dataset.promote); if (f) f.promoted_case = res.case;
    btn.replaceWith(Object.assign(document.createElement("span"), { className: "chip", textContent: "case" }));
    loadCases();
  } catch (e) { btn.disabled = false; btn.title = e.message; }
}
async function loadCases() {
  const cases = await api("/api/regressions");
  $("cases").innerHTML = "<thead><tr><th>Case</th><th>Company</th><th>Why it was promoted</th><th>Added</th></tr></thead><tbody>" +
    cases.map((c) => `<tr><td>${chip(c.signature)}</td><td>${esc(c.company)}</td><td class="muted">${esc(c.detail)}</td><td>${esc(c.created)}</td></tr>`).join("") + "</tbody>";
}
async function showTrace(f) {
  const id = encodeURIComponent(state.run.id);
  state.trace = state.trace || await api(`/api/runs/${id}/trace`);
  state.accounts = state.accounts || await api(`/api/runs/${id}/accounts`);
  const keys = new Set(f.crm_ids);
  state.accounts.filter((a) => a.crm_ids.some((c) => keys.has(c))).forEach((a) => keys.add(a.key));
  const evs = state.trace.filter((e) => keys.has(e.account));
  $("traceTitle").textContent = `Trace: ${f.company} (${evs.length} steps)`;
  $("trace").innerHTML = evs.length ? evs.map(stepHtml).join("") : `<p class="empty">No trace events for this failure.</p>`;
}
function setupExplorer() {
  $("runSel").addEventListener("change", (e) => openRun(e.target.value));
  $("clusters").addEventListener("click", (e) => { const b = e.target.closest(".bar"); if (b) showFailures(b.dataset.sig); });
  $("runBtn").addEventListener("click", async () => {
    $("runBtn").disabled = true; $("runBtn").textContent = "Running…";
    try { const r = await postJSON("/api/runs", { version: $("newVersion").value, split: $("newSplit").value, llm: "replay" }); await loadRuns(r.id); }
    catch (e) { $("runBtn").title = e.message; }
    finally { $("runBtn").disabled = false; $("runBtn").textContent = "Run"; }
  });
}

// ---------- boot ----------
(async () => {
  setupTry(); setupBatch(); setupExplorer(); setupReview();
  $("monWindow").addEventListener("change", () => loadMonitoring().catch(() => {}));
  const q = new URLSearchParams(location.search);
  if (q.get("clean") === "sample" && !location.hash) location.hash = "#clean";
  if (q.get("run") && !location.hash) location.hash = "#explorer";
  showTab(currentTab(), false);
  if (q.get("try")) {   // shareable link: /?try=UberCab&site=...&country=...
    $("tName").value = q.get("try").slice(0, 120); $("tSite").value = (q.get("site") || "").slice(0, 200); $("tCountry").value = (q.get("country") || "").slice(0, 60);
    runTry();
  } else if (q.get("clean") === "sample") runBatch();
  const [config, history, insights, versions, frameworks] = await Promise.all([
    api("/api/config").catch(() => ({ read_only: true })), api("/api/history").catch(() => []),
    api("/api/insights").catch(() => null), api("/api/versions").catch(() => []), api("/api/frameworks").catch(() => null),
  ]);
  Object.assign(state, { config, history, insights, frameworks });
  $("newVersion").innerHTML = versions.map((v) => `<option>${esc(v.version)}</option>`).join("");
  if (versions.length) $("newVersion").value = (shippedEntry() || versions[versions.length - 1]).version;
  renderBig(); renderResults(); renderInsights(); renderFrameworks(); renderLearning();
  showTab(currentTab());
})();
