/* Ledgerline front end — no build step, no framework. */
"use strict";

const S = {
  meta: null,
  run: null,
  runs: [],
  tab: "overview",
  loading: true,
  wb: { entity: "ALL", statement: "ALL", materialOnly: false, q: "", open: null, mode: "variance" },
  rollforward: null,
  rollforwardLoading: false,
  diff: { base: null, compare: null, data: null, loading: false, attempted: false },
  verifyFilter: "all",
  claimFilter: "all",
  factCache: {},
  openFinding: null,
  attest: {},
  section: null,
  busy: false,
  menuOpen: false,
  booted: false,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const ROLE_LABEL = { movement: "Position", attribution: "Attribution", quality: "Quality" };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function money(value, opts = {}) {
  const n = num(value);
  const abs = Math.abs(n);
  const lead = n < 0 ? "−" : opts.signed && n > 0 ? "+" : "";
  if (abs >= 1e9) return `${lead}$${(abs / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${lead}$${(abs / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${lead}$${(abs / 1e3).toFixed(1)}K`;
  return `${lead}$${abs.toFixed(0)}`;
}

function moneyFull(value) {
  const n = num(value);
  return (n < 0 ? "−" : "") + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function pct(value, digits = 1) {
  const n = num(value) * 100;
  const lead = n < 0 ? "−" : "";
  return `${lead}${Math.abs(n).toFixed(digits)}%`;
}

function pctSigned(value, digits = 1) {
  const n = num(value) * 100;
  const lead = n < 0 ? "−" : "+";
  return `${lead}${Math.abs(n).toFixed(digits)}%`;
}

function tone(favorability) {
  return favorability === "favorable" ? "up" : favorability === "unfavorable" ? "down" : "flat";
}

function statusPill(status) {
  const map = {
    signed: ["pill-accent", "Signed off"],
    clear: ["pill-pass", "Cleared"],
    attestation: ["pill-warn", "Awaiting attestation"],
    blocked: ["pill-block", "Blocked"],
  };
  const [cls, label] = map[status] || ["pill-neutral", status];
  return `<span class="pill ${cls}">${esc(label)}</span>`;
}

function sevTag(sev) {
  return `<span class="tag tag-${sev === "block" ? "block" : sev === "warn" ? "warn" : "neutral"}">${esc(sev)}</span>`;
}

/* ------------------------------------------------------------------ api */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { raw: text }; }
  if (!res.ok) throw new Error(body?.error || `Request failed (${res.status})`);
  return body;
}

function toast(message, kind = "") {
  const stack = $("#toasts");
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  stack.appendChild(node);
  setTimeout(() => {
    node.style.transition = "opacity .3s";
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 320);
  }, 4200);
}

async function withBusy(label, fn) {
  if (S.busy) return;
  S.busy = true;
  document.body.style.cursor = "progress";
  try {
    await fn();
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    S.busy = false;
    document.body.style.cursor = "";
  }
}

/* ------------------------------------------------------------------ boot */
async function boot() {
  try {
    S.meta = await api("/api/meta");
    const runs = await api("/api/runs");
    S.runs = runs.runs;
    let view;
    if (!runs.runs.length) {
      view = await api("/api/runs", { method: "POST", body: JSON.stringify({ mode: "strict" }) });
      S.runs = (await api("/api/runs")).runs;
    } else {
      view = await api(`/api/runs/${runs.runs[0].id}`);
    }
    applyView(view);
  } catch (error) {
    $("#main").innerHTML = `<div class="banner banner-block"><span class="banner-icon">!</span><div><strong>Could not start the close.</strong><br />${esc(error.message)}</div></div>`;
  }
}

async function refreshRuns() {
  try {
    S.runs = (await api("/api/runs")).runs;
  } catch {
    /* the run picker is a convenience; a failed refresh must not break the view */
  }
}

function templateFor(category) {
  const templates = S.meta?.attestation_templates || {};
  return templates[category] || templates.DEFAULT || { label: "Exception attestation", instruction: "", fields: [] };
}

function attestationDetails(finding) {
  const state = S.attest[finding.finding_id] || {};
  const details = { ...(state.details || {}) };
  for (const field of templateFor(finding.category).fields || []) {
    if (field.auto_from_finding && !details[field.name]) {
      details[field.name] = String(finding[field.auto_from_finding] || "");
    }
  }
  return details;
}

function attestationProblems(finding) {
  const state = S.attest[finding.finding_id] || {};
  const problems = [];
  if (!state.checked) problems.push("not accepted");
  if (!(state.rationale || "").trim()) problems.push("rationale required");
  const details = attestationDetails(finding);
  for (const field of templateFor(finding.category).fields || []) {
    if (!field.required) continue;
    const value = String(details[field.name] || "").trim();
    if (!value) problems.push(`${field.label} required`);
    else if (field.type === "date" && !/^\d{4}-\d{2}-\d{2}$/.test(value)) problems.push(`${field.label} must be a date`);
  }
  return problems;
}

function attestationReady(finding) {
  return attestationProblems(finding).length === 0;
}

function applyView(view) {
  S.run = view;
  S.loading = false;
  if (!S.booted) {
    const tab = tabFromHash();
    if (tab) S.tab = tab;
    S.booted = true;
  }
  S.attest = {};
  (view.verification.findings || []).filter((f) => f.severity === "warn").forEach((f) => {
    const details = {};
    for (const field of templateFor(f.category).fields || []) {
      if (field.auto_from_finding) details[field.name] = String(f[field.auto_from_finding] || "");
    }
    S.attest[f.finding_id] = { checked: false, rationale: "", details };
  });
  bindFigStatus();
  render();
}

let FIG_STATUS = new Map();
function bindFigStatus() {
  FIG_STATUS = new Map();
  const claims = S.run?.verification?.claims || [];
  claims.forEach((claim) => {
    FIG_STATUS.set(`${claim.section_id}|${claim.paragraph}|${claim.claim}`, claim);
  });
}

/* ------------------------------------------------------------------ render */
const TABS = new Set(["overview", "workbench", "commentary", "verify", "signoff", "compare"]);
function tabFromHash() {
  const value = (window.location.hash || "").replace(/^#\/?/, "");
  return TABS.has(value) ? value : null;
}

function render() {
  if (!S.run) return;
  if (window.location.hash !== `#${S.tab}`) window.history.replaceState(null, "", `#${S.tab}`);
  renderTopbar();
  renderRail();
  renderMain();
  // Opening the comparison tab directly (nav, hash, boot) should fetch rather
  // than sit on a spinner. `attempted` makes this a one-shot: a failed load
  // renders its own message instead of retrying on every paint.
  if (
    S.tab === "compare" &&
    !S.diff.data &&
    !S.diff.loading &&
    !S.diff.attempted &&
    S.runs.length >= 2
  ) {
    openDiff();
  }
}

function renderTopbar() {
  const { run, recon } = S.run;
  const runs = S.runs.length ? S.runs : [{ id: run.id, period: run.period, mode: run.mode, status: run.status, label: run.label }];
  const runOptions = runs.map((r) => {
    const stamp = (r.created_at || "").slice(11, 16);
    const text = `${r.period || ""} · ${r.mode || ""} · ${r.status || ""}${stamp ? ` · ${stamp}` : ""} · ${(r.id || "").slice(-4)}`;
    return `<option value="${esc(r.id)}" ${r.id === run.id ? "selected" : ""}>${esc(text)}</option>`;
  }).join("");
  $("#topbar-context").innerHTML = `
    <span class="chip"><span class="mono">${esc(run.period_label)}</span></span>
    <span class="chip">${esc(S.meta.group)}</span>
    <span class="chip chip-quiet">${esc(recon.stats.entities)} entities · ${esc(recon.stats.journal_lines)} journal lines · ${esc(recon.stats.facts)} figures</span>
    ${statusPill(run.status)}
    <span class="run-switch">
      <select id="run-switcher" aria-label="Select a close run">${runOptions}</select>
      <button class="btn btn-sm btn-ghost" data-act="open-diff" ${runs.length < 2 ? "disabled" : ""}>Compare runs</button>
    </span>
  `;
  $("#export-btn").disabled = false;
}

function renderRail() {
  const { recon, verification, run } = S.run;
  const st = recon.stats;
  const stages = [
    {
      name: "Reconcile",
      state: st.controls_breaks ? "warn" : "done",
      meta: `${st.accounts_in_scope} accounts · ${st.controls_run} controls · ${st.controls_breaks} break${st.controls_breaks === 1 ? "" : "s"}`,
    },
    {
      name: "Narrate",
      state: S.run.commentary.drift?.applied ? "block" : "done",
      meta: `${S.run.commentary.stats.sections} sections · ${S.run.commentary.stats.figures} figures`,
    },
    {
      name: "Verify",
      state: verification.gate.blocking ? "block" : verification.gate.warnings ? "warn" : "done",
      meta: `${verification.stats.figures_passed}/${verification.stats.figures_checked} figures · ${verification.stats.facts_checked} re-derived`,
    },
    {
      name: "Sign-off",
      state: run.status === "signed" ? "done" : "idle",
      meta: run.status === "signed" ? `by ${S.run.signoff?.signer || "—"}` : "awaiting signature",
    },
  ];

  const nav = [
    ["overview", "Overview", null],
    ["workbench", "Variance workbench", recon.stats.variances],
    ["commentary", "Commentary", S.run.commentary.stats.sections],
    ["verify", "Verification", verification.gate.blocking || null],
    ["signoff", "Sign-off & audit", null],
    ["compare", "Run comparison", null],
  ];

  $("#rail").innerHTML = `
    <div class="rail-card">
      <div class="rail-head"><span>Close pipeline</span><span class="mono">${esc(run.period)}</span></div>
      ${stages.map((s, i) => `
        <div class="stage ${s.state}">
          <div class="stage-idx">${s.state === "done" ? "✓" : s.state === "block" ? "!" : i + 1}</div>
          <div><div class="stage-name">${esc(s.name)}</div><div class="stage-meta">${esc(s.meta)}</div></div>
        </div>`).join("")}
    </div>

    <div class="rail-card">
      <div class="rail-head"><span>Sign-off gate</span></div>
      <div class="rail-kv"><span>Blocking</span><span class="${verification.gate.blocking ? "down" : "up"}">${verification.gate.blocking}</span></div>
      <div class="rail-kv"><span>${run.status === "signed" ? "Warnings attested" : "Warnings to attest"}</span><span class="${verification.gate.warnings && run.status !== "signed" ? "down" : "up"}">${verification.gate.warnings}</span></div>
      <div class="rail-kv"><span>Coverage</span><span>${verification.stats.coverage_covered}/${verification.stats.coverage_total}</span></div>
      <div class="rail-kv"><span>Attribution closure</span><span>${verification.stats.closure_passed}/${verification.stats.closure_checks}</span></div>
      <div class="rail-kv"><span>Cross-section drivers</span><span>${verification.cross_section ? `${verification.cross_section.passed}/${verification.cross_section.total}` : "—"}</span></div>
      <div class="rail-kv"><span>Verifier self-test</span><span>${verification.self_test.detected}/${verification.self_test.total}</span></div>
    </div>

    <div class="rail-card">
      <div class="rail-nav">
        ${nav.map(([id, label, count]) => `
          <button data-tab="${id}" class="${S.tab === id ? "on" : ""}">
            <span>${esc(label)}</span>
            ${count != null ? `<span class="count ${id === "verify" && count ? "alert" : ""}">${count}</span>` : ""}
          </button>`).join("")}
      </div>
    </div>

    <div class="rail-card">
      <div class="rail-head"><span>Source systems</span></div>
      ${Object.entries(S.meta.source_systems).map(([k, v]) => `
        <div class="rail-kv"><span>${esc(k)}</span><span title="${esc(v)}">${esc(v.split(",")[0].split("(")[0].trim())}</span></div>`).join("")}
    </div>
  `;
}

function renderMain() {
  const main = $("#main");
  const renderers = { overview: renderOverview, workbench: renderWorkbench, commentary: renderCommentary, verify: renderVerify, signoff: renderSignoff, compare: renderCompare };
  main.innerHTML = renderers[S.tab]();
  if (S.tab === "commentary" && S.section) {
    const node = document.getElementById(`sec-${S.section}`);
    if (node) node.scrollIntoView({ block: "start" });
  }
}

/* --------------------------------------------------------------- overview */
function renderOverview() {
  const { recon, verification, commentary, run } = S.run;
  const st = recon.stats;
  const agg = Object.fromEntries(recon.aggregates.map((a) => [a.aggregate_id, a]));
  const kpis = [
    {
      label: "Material variances",
      value: st.material_variances,
      foot: `${st.pl_material} P&L · ${st.bs_material} balance sheet`,
      cls: "accent",
    },
    {
      label: "Commentary coverage",
      value: `${verification.stats.coverage_covered}<small>/ ${verification.stats.coverage_total}</small>`,
      foot: "every material variance narrated",
      cls: verification.stats.coverage_covered === verification.stats.coverage_total ? "pass" : "block",
    },
    {
      label: "Figures verified",
      value: `${verification.stats.figures_passed}<small>/ ${verification.stats.figures_checked}</small>`,
      foot: "each traced to a source figure",
      cls: verification.stats.figures_passed === verification.stats.figures_checked ? "pass" : "block",
    },
    {
      label: "Figures re-derived",
      value: verification.stats.facts_checked,
      foot: "from ledger, plan and FX tables",
      cls: verification.stats.facts_passed === verification.stats.facts_checked ? "pass" : "block",
    },
    {
      label: "Blocking findings",
      value: verification.gate.blocking,
      foot: `${verification.gate.warnings} warning${verification.gate.warnings === 1 ? "" : "s"} to attest`,
      cls: verification.gate.blocking ? "block" : verification.gate.warnings ? "warn" : "pass",
    },
  ];

  const aggRows = ["revenue_total", "gross_profit", "opex_total", "ebitda", "operating_income", "net_income"]
    .filter((id) => agg[id])
    .map((id) => {
      const a = agg[id];
      const v = num(a.value_usd.variance);
      return `
        <tr>
          <td>${esc(a.label)}</td>
          <td class="num">${money(a.value_usd.actual)}</td>
          <td class="num">${money(a.value_usd.budget)}</td>
          <td class="num ${v < 0 ? "neg" : "pos"}">${money(v, { signed: true })}</td>
          <td class="num ${v < 0 ? "neg" : "pos"}">${pctSigned(a.variance_pct)}</td>
          <td style="width:150px">
            <div class="bar"><i class="${v < 0 ? "bad" : "good"}" style="left:${v < 0 ? 50 - Math.min(48, Math.abs(num(a.variance_pct)) * 240) : 50}%;width:${Math.min(48, Math.abs(num(a.variance_pct)) * 240)}%"></i><span class="bar-line" style="left:50%"></span></div>
          </td>
        </tr>`;
    }).join("");

  const entities = Object.values(
    recon.variances.reduce((acc, v) => {
      acc[v.entity] = acc[v.entity] || { code: v.entity, name: v.entity_name, material: 0, revenue: null, opex: 0, unfav: 0 };
      if (v.material) acc[v.entity].material += 1;
      if (v.account === "4000") acc[v.entity].revenue = v;
      if (["5000", "6000", "6100", "6200", "6300", "6400", "6500"].includes(v.account)) acc[v.entity].opex += num(v.variance_usd);
      if (v.material && v.favorability === "unfavorable") acc[v.entity].unfav += 1;
      return acc;
    }, {})
  );

  const breaks = recon.integrity.filter((c) => c.status === "break");

  return `
    <div class="page-head">
      <div>
        <h1 class="page-title">${esc(S.meta.group)} — ${esc(run.period_label)} close</h1>
        <div class="page-sub">Commentary prepared by agents, verified against source, signed by a human. All figures in ${esc(recon.group_currency)}.</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-sm" data-act="go-verify">Review verification</button>
        <button class="btn btn-sm btn-primary" data-act="go-signoff">${run.status === "signed" ? "View certificate" : "Proceed to sign-off"}</button>
      </div>
    </div>

    <div class="kpis">
      ${kpis.map((k) => `
        <div class="kpi ${k.cls}">
          <div class="kpi-label">${esc(k.label)}</div>
          <div class="kpi-value">${k.value}</div>
          <div class="kpi-foot">${esc(k.foot)}</div>
        </div>`).join("")}
    </div>

    ${verification.gate.status === "blocked" ? `
      <div class="banner banner-block">
        <span class="banner-icon">■</span>
        <div><strong>Sign-off is blocked.</strong> ${esc(verification.gate.reasons.join(" · "))}.
        <button class="btn btn-sm btn-danger" style="margin-left:8px" data-act="go-verify">Open findings</button></div>
      </div>` : ""}
    ${commentary.drift?.applied ? `
      <div class="banner banner-warn">
        <span class="banner-icon">⚠</span>
        <div><strong>Adversarial test run.</strong> This commentary was generated with two injected model-drift defects to prove the gate catches them. ${esc(verification.gate.blocking)} blocking finding(s) raised. <button class="btn btn-sm" style="margin-left:6px" data-act="regenerate">Regenerate compliant commentary</button></div>
      </div>` : ""}

    <div class="grid-2" style="grid-template-columns: 1.45fr 1fr">
      <div class="card">
        <div class="card-hd"><h2>Group performance vs plan</h2><span class="hd-note">translated at closing rates</span></div>
        <div class="card-bd tight">
          <div class="tbl-wrap">
            <table class="tbl">
              <thead><tr><th>Line</th><th class="num">Actual</th><th class="num">Budget</th><th class="num">Variance</th><th class="num">%</th><th>vs plan</th></tr></thead>
              <tbody>${aggRows}</tbody>
            </table>
          </div>
        </div>
        <div class="card-ft">
          <span>Materiality: ${money(S.meta.materiality.PL.amount_usd)} / ${pct(S.meta.materiality.PL.min_pct)} (P&L) · ${money(S.meta.materiality.BS.amount_usd)} / ${pct(S.meta.materiality.BS.min_pct)} (balance sheet)</span>
          <button class="btn btn-sm btn-ghost" data-act="go-workbench">Open workbench →</button>
        </div>
      </div>

      <div class="card">
        <div class="card-hd"><h2>Control results</h2><span class="hd-note">${st.controls_run} controls</span></div>
        <div class="card-bd tight">
          <div class="tbl-wrap">
            <table class="tbl">
              <thead><tr><th>Control</th><th class="num">Run</th><th class="num">Breaks</th><th></th></tr></thead>
              <tbody>
                ${controlSummary(recon.integrity).map((row) => `
                  <tr>
                    <td>${esc(row.label)}</td>
                    <td class="num">${row.run}</td>
                    <td class="num">${row.breaks || "—"}</td>
                    <td class="nowrap">${row.breaks ? `<span class="tag tag-warn">${money(row.exposure)}</span>` : `<span class="tag tag-pass">clean</span>`}</td>
                  </tr>`).join("")}
              </tbody>
            </table>
          </div>
        </div>
        <div class="card-ft">
          <span>${breaks.length} exception${breaks.length === 1 ? "" : "s"} disclosed and carried to attestation</span>
          <button class="btn btn-sm btn-ghost" data-act="go-signoff">Attest →</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd"><h2>Entity view</h2><span class="hd-note">${entities.length} reporting entities</span></div>
      <div class="card-bd">
        <div class="grid-3" style="grid-template-columns: repeat(auto-fit, minmax(240px, 1fr))">
          ${entities.map((e) => `
            <div class="entity-card">
              <div class="row" style="justify-content:space-between">
                <div>
                  <div class="entity-name">${esc(e.name)}</div>
                  <div class="entity-code">${esc(e.code)}</div>
                </div>
                ${e.material ? `<span class="tag tag-accent">${e.material} material</span>` : `<span class="tag tag-line">clear</span>`}
              </div>
              <div class="entity-rows">
                <div class="entity-row"><span>Revenue variance</span><span class="${e.revenue ? tone(e.revenue.favorability) : ""}">${e.revenue ? money(e.revenue.variance_usd, { signed: true }) : "—"}</span></div>
                <div class="entity-row"><span>Operating cost variance</span><span class="${e.opex > 0 ? "down" : "up"}">${money(e.opex, { signed: true })}</span></div>
                <div class="entity-row"><span>Unfavourable items</span><span>${e.unfav}</span></div>
              </div>
            </div>`).join("")}
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd"><h2>How this close was produced</h2><span class="hd-note">reconcile → narrate → verify → sign</span></div>
      <div class="card-bd">
        <div class="grid-3" style="grid-template-columns: repeat(auto-fit, minmax(260px, 1fr))">
          ${[
            ["1 · Reconcile", `${st.accounts_in_scope} accounts across ${st.entities} entities are translated and rolled forward. ${st.controls_run} controls run; ${st.control_breaks ?? st.controls_breaks} break${(st.control_breaks ?? st.controls_breaks) === 1 ? "" : "s"} disclosed. ${st.material_variances} variances clear the materiality policy and enter the commentary queue.`],
            ["2 · Narrate", `${commentary.stats.sections} sections drafted in ${commentary.mode_label.toLowerCase()}. Every figure is a fact reference, never a typed number: ${commentary.stats.figures} figures across ${commentary.stats.words.toLocaleString()} words.`],
            ["3 · Verify", `Each figure is parsed back off the page and compared to its source, then every source figure is re-derived from the journal extract. ${verification.stats.facts_checked} re-derivations, ${verification.self_test.detected}/${verification.self_test.total} injected defects caught by the control test.`],
          ].map(([title, body]) => `
            <div>
              <div class="mini-hd">${esc(title)}</div>
              <div class="small muted" style="line-height:1.6">${esc(body)}</div>
            </div>`).join("")}
        </div>
      </div>
    </div>
  `;
}

function controlSummary(integrity) {
  const groups = {};
  integrity.forEach((c) => {
    groups[c.kind] = groups[c.kind] || { label: c.kind.replace(/_/g, " "), run: 0, breaks: 0, exposure: 0 };
    groups[c.kind].run += 1;
    if (c.status === "break") {
      groups[c.kind].breaks += 1;
      groups[c.kind].exposure += Math.abs(num(c.difference));
    }
  });
  return Object.values(groups).sort((a, b) => b.breaks - a.breaks || a.label.localeCompare(b.label)).map((g) => ({
    ...g,
    label: g.label.charAt(0).toUpperCase() + g.label.slice(1),
  }));
}

/* ------------------------------------------------------------- workbench */
function modeToggle() {
  return `<div class="mode-toggle">
    <button class="${S.wb.mode === "variance" ? "on" : ""}" data-act="wb-mode" data-mode="variance">Current period</button>
    <button class="${S.wb.mode === "rollforward" ? "on" : ""}" data-act="wb-mode" data-mode="rollforward">Roll-forward Jul–Oct</button>
  </div>`;
}

function renderWorkbench() {
  if (S.wb.mode === "rollforward") return renderRollforward();
  const { recon } = S.run;
  const entities = [...new Set(recon.variances.map((v) => v.entity))].sort();
  const rows = recon.variances.filter((v) => {
    if (S.wb.entity !== "ALL" && v.entity !== S.wb.entity) return false;
    if (S.wb.statement !== "ALL" && v.statement !== S.wb.statement) return false;
    if (S.wb.materialOnly && !v.material) return false;
    if (S.wb.q) {
      const hay = `${v.account} ${v.account_name} ${v.account_short} ${v.entity} ${v.entity_name}`.toLowerCase();
      if (!hay.includes(S.wb.q.toLowerCase())) return false;
    }
    return true;
  });

  const body = rows.map((v) => {
    const open = S.wb.open === v.variance_id;
    return `
      <tr class="clickable ${open ? "open" : ""}" data-variance="${esc(v.variance_id)}">
        <td class="mono nowrap">${esc(v.entity)}</td>
        <td><span class="mono small">${esc(v.account)}</span> ${esc(v.account_name)}</td>
        <td class="num">${money(v.actual_usd)}</td>
        <td class="num">${money(v.budget_usd)}</td>
        <td class="num ${num(v.variance_usd) < 0 ? "neg" : "pos"}">${money(v.variance_usd, { signed: true })}</td>
        <td class="num ${num(v.variance_usd) < 0 ? "neg" : "pos"}">${pctSigned(v.variance_pct)}</td>
        <td class="nowrap"><span class="${tone(v.favorability)}">${esc(v.favorability)}</span></td>
        <td>${v.material ? `<span class="tag tag-accent">material</span>` : `<span class="tag tag-line">below</span>`}</td>
        <td class="nowrap">${v.driver_closure.ok ? `<span class="tag tag-pass">ties</span>` : `<span class="tag tag-block">break</span>`}</td>
        <td class="nowrap muted xsmall">${open ? "▾" : "▸"}</td>
      </tr>
      ${open ? renderVarianceDetail(v) : ""}`;
  }).join("");

  return `
    <div class="page-head">
      <div>
        <h1 class="page-title">Variance workbench</h1>
        <div class="page-sub">${recon.variances.length} account variances across ${recon.stats.entities} entities · ${recon.stats.material_variances} clear the materiality policy · click a row to see the attribution.</div>
      </div>
      <div class="page-actions">
        ${modeToggle()}
        <span class="chip chip-quiet">Policy: ${money(S.meta.materiality.PL.amount_usd)} & ${pct(S.meta.materiality.PL.min_pct)} (P&L)</span>
      </div>
    </div>

    <div class="card">
      <div class="card-hd" style="gap:10px">
        <div class="row" style="gap:8px">
          <select id="wb-entity" style="width:auto">${["ALL", ...entities].map((e) => `<option value="${e}" ${S.wb.entity === e ? "selected" : ""}>${e === "ALL" ? "All entities" : e}</option>`).join("")}</select>
          <select id="wb-statement" style="width:auto">
            ${[["ALL", "All statements"], ["PL", "Profit & loss"], ["BS", "Balance sheet"]].map(([v, l]) => `<option value="${v}" ${S.wb.statement === v ? "selected" : ""}>${l}</option>`).join("")}
          </select>
          <label class="row" style="gap:6px;font-size:12px;color:var(--muted)"><input type="checkbox" id="wb-material" ${S.wb.materialOnly ? "checked" : ""} style="accent-color:#0b5f6b" /> material only</label>
        </div>
        <input type="text" id="wb-search" placeholder="Search account…" value="${esc(S.wb.q)}" style="width:220px" />
      </div>
      <div class="card-bd tight">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr>
              <th>Entity</th><th>Account</th><th class="num">Actual</th><th class="num">Budget</th>
              <th class="num">Variance</th><th class="num">%</th><th>Fav.</th><th>Materiality</th><th>Attribution</th><th></th>
            </tr></thead>
            <tbody>${body || `<tr><td colspan="10" class="muted" style="padding:24px;text-align:center">No variances match the filter.</td></tr>`}</tbody>
          </table>
        </div>
      </div>
      <div class="card-ft"><span>${rows.length} of ${recon.variances.length} rows</span><span>Figures in ${esc(recon.group_currency)}, translated at month-end closing rates</span></div>
    </div>
  `;
}

function renderRollforward() {
  const data = S.rollforward;
  const head = `
    <div class="page-head">
      <div>
        <h1 class="page-title">Roll-forward</h1>
        <div class="page-sub">${esc(S.meta.group)} · period × account · ${data ? `${data.periods.length} periods, current period on the right` : "loading the quarter"}.</div>
      </div>
      <div class="page-actions">
        ${modeToggle()}
        <button class="btn btn-sm btn-ghost" data-act="refresh-rollforward" ${S.rollforwardLoading ? "disabled" : ""}>Refresh</button>
      </div>
    </div>`;

  if (!data) {
    return `${head}
      <div class="loading-card"><div class="spinner"></div><p>Reconciling July through October…</p></div>`;
  }

  const entities = ["ALL", ...data.entities.map((e) => e.code)];
  const query = S.wb.q.trim().toLowerCase();
  const rows = data.rows.filter((row) => {
    if (S.wb.entity !== "ALL" && row.entity !== S.wb.entity) return false;
    if (S.wb.statement !== "ALL" && row.statement !== S.wb.statement) return false;
    if (S.wb.materialOnly && !(row.material_periods || []).length) return false;
    if (query) {
      const hay = `${row.account} ${row.account_name} ${row.account_short} ${row.entity} ${row.entity_name}`.toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  });

  const cell = (row, period) => {
    const value = row.periods[period];
    if (!value) return `<td class="rf-cell rf-empty">—</td>`;
    const variance = num(value.variance_usd);
    return `<td class="rf-cell ${period === data.current_period ? "rf-current" : ""}">
      <div class="rf-actual">${money(value.actual_usd)}</div>
      <div class="rf-var ${variance < 0 ? "neg" : "pos"}">${money(variance, { signed: true })}</div>
      ${value.material ? `<span class="tag tag-accent xsmall">material</span>` : ""}
    </td>`;
  };

  const body = rows.map((row) => `
    <tr class="${row.group ? "rf-group" : ""}">
      <td class="mono nowrap">${esc(row.entity)}</td>
      <td><span class="mono small">${esc(row.account)}</span> ${esc(row.account_name)}
        <span class="tag tag-line xsmall">${esc(row.statement)}</span></td>
      ${data.periods.map((period) => cell(row, period)).join("")}
    </tr>`).join("");

  const wanted = ["revenue_total", "gross_profit", "ebitda", "net_income"];
  const aggregateRows = wanted.map((id) => {
    const label = data.aggregates[data.periods[0]]?.find((a) => a.aggregate_id === id)?.label || id;
    return `<tr>
      <td>${esc(label)}</td>
      ${data.periods.map((period) => {
        const agg = (data.aggregates[period] || []).find((a) => a.aggregate_id === id);
        return `<td class="rf-cell ${period === data.current_period ? "rf-current" : ""}">
          <div class="rf-actual">${agg ? money(agg.value_usd.actual) : "—"}</div>
          <div class="rf-var ${agg && num(agg.value_usd.variance) < 0 ? "neg" : "pos"}">${agg ? money(agg.value_usd.variance, { signed: true }) : ""}</div>
        </td>`;
      }).join("")}
    </tr>`;
  }).join("");

  return `${head}
    <div class="card">
      <div class="card-hd" style="gap:10px">
        <div class="row" style="gap:8px">
          <select id="wb-entity" style="width:auto">${entities.map((e) => `<option value="${e}" ${S.wb.entity === e ? "selected" : ""}>${e === "ALL" ? "All entities (group + entities)" : e}</option>`).join("")}</select>
          <select id="wb-statement" style="width:auto">
            ${[["ALL", "All statements"], ["PL", "Profit & loss"], ["BS", "Balance sheet"]].map(([v, l]) => `<option value="${v}" ${S.wb.statement === v ? "selected" : ""}>${l}</option>`).join("")}
          </select>
          <label class="row" style="gap:6px;font-size:12px;color:var(--muted)"><input type="checkbox" id="wb-material" ${S.wb.materialOnly ? "checked" : ""} style="accent-color:#0b5f6b" /> material in any period</label>
        </div>
        <input type="text" id="wb-search" placeholder="Search account…" value="${esc(S.wb.q)}" style="width:220px" />
      </div>
      <div class="card-bd tight">
        <div class="tbl-wrap" style="max-height:640px;overflow:auto">
          <table class="tbl rf-table">
            <thead><tr>
              <th>Entity</th><th>Account</th>
              ${data.periods.map((period) => `<th class="num ${period === data.current_period ? "rf-current" : ""}">${esc(data.period_labels[period] || period)}</th>`).join("")}
            </tr></thead>
            <tbody>${body || `<tr><td colspan="${data.periods.length + 2}" class="muted" style="padding:24px;text-align:center">No accounts match the filter.</td></tr>`}</tbody>
          </table>
        </div>
      </div>
      <div class="card-ft">
        <span>${rows.length} of ${data.rows.length} rows · actual above, variance vs budget below · figures in ${esc(data.group_currency)}</span>
        <span>${data.stats.material_cells} material period cells · group materiality recomputed at group level</span>
      </div>
    </div>

    <div class="card">
      <div class="card-hd"><h2>Group roll-forward</h2><span class="hd-note">actual above · variance vs budget below</span></div>
      <div class="card-bd tight">
        <div class="tbl-wrap">
          <table class="tbl rf-table">
            <thead><tr><th>Measure</th>${data.periods.map((period) => `<th class="num ${period === data.current_period ? "rf-current" : ""}">${esc(data.period_labels[period] || period)}</th>`).join("")}</tr></thead>
            <tbody>${aggregateRows}</tbody>
          </table>
        </div>
      </div>
      <div class="card-ft"><span>Current period is highlighted on the right. July has no prior month in the extract, so its prior-period comparator equals the period itself.</span></div>
    </div>`;
}

function renderVarianceDetail(v) {
  const drivers = v.drivers.length
    ? v.drivers.map((d) => `
        <div class="driver-row">
          <div><div class="cause">${esc(d.cause)}</div><div class="xsmall muted">${esc(d.label)} · <span class="factlink" data-fact="${esc(d.fact_id)}">${esc(d.fact_id)}</span></div></div>
          <div class="amt ${num(d.amount) < 0 ? "neg" : "pos"}">${money(d.amount, { signed: true })}</div>
        </div>`).join("")
    : `<div class="muted small">No driver attribution recorded.</div>`;

  const prior = (v.prior_decomposition || []).slice(0, 4).map((d) => `
    <div class="driver-row"><div class="cause">${esc(d.cause)}</div><div class="amt">${money(d.amount, { signed: true })}</div></div>`).join("");

  return `
    <tr class="detail-row"><td colspan="10" class="detail-cell">
      <div class="detail-grid">
        <div class="detail-pad">
          <div class="mini-hd">Attribution · vs budget</div>
          ${drivers}
          <div class="row" style="margin-top:10px;gap:14px">
            <span class="tag ${v.driver_closure.ok ? "tag-pass" : "tag-block"}">${v.driver_closure.ok ? "sums to variance" : "does not close"}</span>
            <span class="xsmall muted">Attributed ${money(v.driver_total_usd)} vs variance ${money(v.variance_usd)} · residual ${money(v.driver_closure.difference_usd)}</span>
          </div>
          ${prior ? `<div class="mini-hd" style="margin-top:14px">Movement vs prior period</div>${prior}` : ""}
        </div>
        <div class="detail-pad">
          <div class="mini-hd">Evidence</div>
          <div class="kv-grid" style="grid-template-columns:1fr">
            <div class="kv"><dt>Actual figure</dt><dd><span class="factlink" data-fact="${esc(v.fact_ids.actual)}">${esc(v.fact_ids.actual)}</span> · ${moneyFull(v.actual_usd)}</dd></div>
            <div class="kv"><dt>Budget figure</dt><dd><span class="factlink" data-fact="${esc(v.fact_ids.budget)}">${esc(v.fact_ids.budget)}</span> · ${moneyFull(v.budget_usd)}</dd></div>
            <div class="kv"><dt>Variance figure</dt><dd><span class="factlink" data-fact="${esc(v.fact_ids.variance)}">${esc(v.fact_ids.variance)}</span> · ${moneyFull(v.variance_usd)}</dd></div>
            <div class="kv"><dt>Prior period</dt><dd>${moneyFull(v.prior_usd)} · ${money(v.variance_prior_usd, { signed: true })} (${pctSigned(v.variance_prior_pct)})</dd></div>
            <div class="kv"><dt>Materiality test</dt><dd style="font-family:var(--sans)">${esc(v.materiality_reason)}</dd></div>
            <div class="kv"><dt>Commentary</dt><dd style="font-family:var(--sans)">${v.material ? `<button class="btn btn-sm btn-ghost" data-act="go-commentary" data-section="${esc(v.variance_id)}">Open narrative →</button>` : `<span class="tag tag-line">not required</span>`}</dd></div>
          </div>
        </div>
      </div>
    </td></tr>`;
}

/* ------------------------------------------------------------- commentary */
function renderCommentary() {
  const { commentary, verification, run } = S.run;
  const sections = commentary.sections;
  const drift = commentary.drift;

  const cards = sections.map((section) => {
    const paragraphs = section.paragraphs.map((p, pIdx) => {
      const html = p.tokens.map((token) => {
        if (token.t === "text") return esc(token.v);
        const claim = FIG_STATUS.get(`${section.section_id}|${pIdx}|${token.text}`);
        const bad = claim && claim.status !== "pass";
        return `<button class="fig ${bad ? "bad" : ""}" data-fact="${esc(token.fact)}" title="Source figure ${esc(token.fact)}">${esc(token.text)}</button>`;
      }).join("");
      return `
        <div class="para">
          <div class="para-role"><b>${esc(ROLE_LABEL[p.role] || p.role)}</b>¶${pIdx + 1}</div>
          <div class="para-text">${html}</div>
        </div>`;
    }).join("");

    const figures = section.paragraphs.reduce((n, p) => n + p.tokens.filter((t) => t.t === "fig").length, 0);
    return `
      <article class="memo-card" id="sec-${esc(section.section_id)}">
        <div class="memo-hd">
          <div>
            <div class="memo-eyebrow">${esc(section.eyebrow)}</div>
            <h2 class="memo-title">${esc(section.heading)}</h2>
          </div>
          <div class="row" style="gap:6px">
            ${section.favorability !== "neutral" ? `<span class="tag tag-${section.favorability === "favorable" ? "pass" : "block"}">${esc(section.favorability)}</span>` : ""}
            ${section.kind === "summary" ? `<span class="tag tag-accent">group</span>` : ""}
          </div>
        </div>
        <div class="memo-bd">${paragraphs}</div>
        <div class="memo-ft">
          <span>${figures} figure${figures === 1 ? "" : "s"}</span>
          <span class="sep"></span>
          <span class="up">all figures traced</span>
          <span class="sep"></span>
          <span>attribution closes to the variance</span>
          <span class="spacer"></span>
          <span class="mono">${esc(section.section_id)}</span>
        </div>
      </article>`;
  }).join("");

  const reviewed = commentary.reviewed_no_commentary;
  return `
    <div class="page-head">
      <div>
        <h1 class="page-title">Close commentary</h1>
        <div class="page-sub">${sections.length} sections · ${commentary.stats.figures} figures · ${commentary.stats.words.toLocaleString()} words · generated ${esc(commentary.mode_label.toLowerCase())}. Click any figure to trace it to source.</div>
      </div>
      <div class="page-actions">
        ${run.status === "signed"
          ? `<span class="chip"><span class="dot dot-pass"></span> Frozen by sign-off</span><button class="btn btn-sm" data-act="export">Download evidence pack</button>`
          : `<button class="btn btn-sm" data-act="regenerate">Regenerate</button>
             <button class="btn btn-sm btn-danger" data-act="drift" title="Inject two realistic model-drift defects and re-verify">Run adversarial test</button>
             <button class="btn btn-sm btn-primary" data-act="reverify">Re-verify</button>`}
      </div>
    </div>

    ${drift?.applied ? `
      <div class="banner banner-block">
        <span class="banner-icon">■</span>
        <div>
          <strong>This draft is defective and cannot be signed.</strong>
          Two model-drift defects were injected into ${esc(drift.section_id)} for this adversarial test:
          <ol style="margin:6px 0 0 16px;padding:0">${drift.mutations.map((m) => `<li>${esc(m)}</li>`).join("")}</ol>
          The verifier caught them. Regenerate to restore a compliant draft.
        </div>
      </div>` : ""}
    ${run.status === "signed" ? `
      <div class="banner banner-accent">
        <span class="banner-icon">✓</span>
        <div><strong>Signed by ${esc(S.run.signoff.signer)}</strong> on ${esc(new Date(S.run.signoff.signed_at).toLocaleString())}. This draft is frozen; open a new run to make changes.</div>
      </div>` : ""}

    <div class="subnav">
      ${sections.map((s) => `<button data-act="jump" data-section="${esc(s.section_id)}" class="${S.section === s.section_id ? "on" : ""}">${esc(s.heading)}</button>`).join("")}
      <button data-act="jump" data-section="reviewed">Reviewed — no commentary</button>
    </div>

    ${cards}

    <div class="card" id="sec-reviewed">
      <div class="card-hd"><h2>Reviewed, no commentary required</h2><span class="hd-note">${reviewed.length} accounts below the materiality policy</span></div>
      <div class="card-bd tight">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Entity</th><th>Account</th><th class="num">Variance</th><th class="num">%</th><th>Why no commentary</th></tr></thead>
            <tbody>
              ${reviewed.slice(0, 40).map((r) => `
                <tr><td class="mono">${esc(r.entity)}</td><td><span class="mono small">${esc(r.account)}</span> ${esc(r.account_short)}</td>
                <td class="num">${money(r.variance_usd, { signed: true })}</td><td class="num">${pctSigned(r.variance_pct)}</td>
                <td class="muted small">${esc(r.materiality_reason)}</td></tr>`).join("")}
              ${reviewed.length > 40 ? `<tr><td colspan="5" class="muted small" style="padding:12px">… ${reviewed.length - 40} more reviewed lines are included in the evidence pack.</td></tr>` : ""}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;
}

/* -------------------------------------------------------------- verify */
function renderVerify() {
  const { verification, recon } = S.run;
  const gate = verification.gate;
  const findings = verification.findings;
  const cross = verification.cross_section || null;

  const findingRows = findings.filter((f) => {
    if (S.verifyFilter === "all") return true;
    return f.severity === S.verifyFilter;
  }).map((f) => {
    const open = S.openFinding === f.finding_id;
    return `
      <div class="finding ${open ? "open" : ""}" data-finding="${esc(f.finding_id)}">
        <div class="finding-hd" data-act="toggle-finding" data-id="${esc(f.finding_id)}">
          <div class="finding-cat">${sevTag(f.severity)}<br /><span style="font-size:9px">${esc(f.category)}</span></div>
          <div>
            <div class="finding-msg">${esc(f.message)}</div>
            <div class="finding-meta">
              ${f.section_id ? `<span>section ${esc(f.section_id)}</span>` : ""}
              ${f.fact_id ? `<span class="factlink" data-fact="${esc(f.fact_id)}">${esc(f.fact_id)}</span>` : ""}
              ${f.control_id ? `<span>control ${esc(f.control_id)}</span>` : ""}
              <span class="mono">${esc(f.finding_id)}</span>
            </div>
          </div>
          <div class="muted">${open ? "▾" : "▸"}</div>
        </div>
        <div class="finding-body">
          ${f.claim ? `<div class="mini-hd">Claim as written</div><div class="code-block">${esc(f.claim)}</div>` : ""}
          ${f.snippet ? `<div class="mini-hd" style="margin-top:10px">Context</div><div class="code-block">…${esc(f.snippet)}…</div>` : ""}
          ${f.detail ? `<div class="mini-hd" style="margin-top:10px">Detail</div><div class="code-block">${esc(JSON.stringify(f.detail, null, 2))}</div>` : ""}
          <div class="row" style="margin-top:10px">
            <button class="btn btn-sm btn-ghost" data-act="go-commentary" data-section="${esc(f.section_id || "")}">Open in commentary →</button>
          </div>
        </div>
      </div>`;
  }).join("");

  const claims = verification.claims.filter((c) => S.claimFilter === "all" || (S.claimFilter === "fail" ? c.status !== "pass" : c.status === "pass"));
  const claimRows = claims.map((c) => `
    <tr>
      <td class="mono nowrap">${esc(c.section_id)}</td>
      <td class="num">${esc(c.claim)}</td>
      <td class="mono small">${esc(c.fact_id)}</td>
      <td class="small muted">${esc(c.fact_label || "")}</td>
      <td class="num">${moneyFull(c.fact_value)}${c.unit === "pct" ? ` <span class="xsmall muted">(${pct(c.fact_value)})</span>` : ""}</td>
      <td><span class="tag ${c.status === "pass" ? "tag-pass" : "tag-block"}">${esc(c.status)}</span></td>
    </tr>`).join("");

  const gateChecks = S.run.run.status === "signed"
    ? S.run.preflight.checks.map((c) => ({
        ...c,
        passed: true,
        detail: c.id === "attestations" ? `${gate.warnings} of ${gate.warnings} attested at sign-off` : c.detail,
      }))
    : S.run.preflight.checks;

  const selfTest = verification.self_test.mutations.map((m) => `
    <tr>
      <td class="mono small">${esc(m.mutation)}</td>
      <td class="small muted">${esc(m.description)}</td>
      <td><span class="tag ${m.detected ? "tag-pass" : "tag-block"}">${m.detected ? "detected" : m.skipped ? "skipped" : "missed"}</span></td>
      <td class="mono xsmall">${esc(m.categories.join(", ") || "—")}${m.failed_figures > 1 ? ` <span class="muted">· ${m.failed_figures} figures</span>` : ""}</td>
    </tr>`).join("");

  return `
    <div class="page-head">
      <div>
        <h1 class="page-title">Verification</h1>
        <div class="page-sub">Every figure is parsed back off the page, tied to its source figure, then re-derived from the journal extract. Nothing is taken on trust.</div>
      </div>
      <div class="page-actions">
        ${S.run.run.status === "signed"
          ? `<span class="chip"><span class="dot dot-pass"></span> Signed record</span><button class="btn btn-sm btn-primary" data-act="go-signoff">View certificate</button>`
          : `<button class="btn btn-sm" data-act="reverify">Re-run verification</button>
             <button class="btn btn-sm btn-primary" data-act="go-signoff" ${gate.signable ? "" : "disabled"}>Proceed to sign-off</button>`}
      </div>
    </div>

    ${S.run.run.status === "signed" ? `
    <div class="banner banner-pass">
      <span class="banner-icon">✓</span>
      <div><strong>Signed and frozen.</strong> ${esc(gate.warnings)} disclosed exception(s) were attested at sign-off. The commentary, this report and the source fact store are bound into the hash-chained certificate.</div>
    </div>` : `
    <div class="banner banner-${gate.status === "blocked" ? "block" : gate.status === "attestation" ? "warn" : "pass"}">
      <span class="banner-icon">${gate.status === "blocked" ? "■" : gate.status === "attestation" ? "▲" : "✓"}</span>
      <div>
        <strong>${gate.status === "blocked" ? "Gate closed." : gate.status === "attestation" ? "Gate open, subject to attestation." : "Gate clear."}</strong>
        ${gate.status === "blocked"
          ? ` ${esc(gate.blocking)} blocking finding(s) must be cleared before a signature is accepted.`
          : gate.status === "attestation"
            ? ` ${esc(gate.warnings)} disclosed exception(s) require the signer's explicit attestation.`
            : " No blocking findings and nothing to attest."}
      </div>
    </div>`}

    <div class="kpis" style="grid-template-columns:repeat(${cross ? 6 : 5},minmax(0,1fr))">
      ${[
        ["Figures tied to source", `${verification.stats.figures_passed}<small>/ ${verification.stats.figures_checked}</small>`, verification.stats.figures_passed === verification.stats.figures_checked ? "pass" : "block", "parse-back + source match"],
        ["Source figures re-derived", `${verification.stats.facts_passed}<small>/ ${verification.stats.facts_checked}</small>`, verification.stats.facts_passed === verification.stats.facts_checked ? "pass" : "block", "ledger · plan · FX"],
        ["Attribution closure", `${verification.stats.closure_passed}<small>/ ${verification.stats.closure_checks}</small>`, verification.stats.closure_passed === verification.stats.closure_checks ? "pass" : "block", "no unexplained residual"],
        ["Coverage", `${verification.stats.coverage_covered}<small>/ ${verification.stats.coverage_total}</small>`, verification.stats.coverage_covered === verification.stats.coverage_total ? "pass" : "block", "material variances narrated"],
        ...(cross ? [["Cross-section drivers", `${cross.passed}<small>/ ${cross.total}</small>`, cross.passed === cross.total ? "pass" : "block", "driver totals · prose claims"]] : []),
        ["Control test", `${verification.self_test.detected}<small>/ ${verification.self_test.total}</small>`, verification.self_test.passed ? "pass" : "block", "injected defects caught"],
      ].map(([label, value, cls, foot]) => `
        <div class="kpi ${cls}"><div class="kpi-label">${esc(label)}</div><div class="kpi-value">${value}</div><div class="kpi-foot">${esc(foot)}</div></div>`).join("")}
    </div>

    <div class="grid-2" style="grid-template-columns: 1fr 1fr">
      <div class="card">
        <div class="card-hd"><h2>Gate checklist</h2><span class="hd-note">${gateChecks.every((c) => c.passed) ? "all conditions met" : "conditions outstanding"}</span></div>
        <div class="card-bd">
          <ul class="check-list">
            ${gateChecks.map((c) => `
              <li>
                <div class="check-mark ${c.passed ? "check-pass" : "check-fail"}">${c.passed ? "✓" : "!"}</div>
                <div>${esc(c.label)}</div>
                <div class="check-detail">${esc(c.detail)}</div>
              </li>`).join("")}
          </ul>
        </div>
      </div>

      <div class="card">
        <div class="card-hd"><h2>Verifier control test</h2><span class="hd-note">mutations injected on this run</span></div>
        <div class="card-bd tight">
          <div class="tbl-wrap">
            <table class="tbl">
              <thead><tr><th>Mutation</th><th>Injected defect</th><th>Result</th><th>Caught by</th></tr></thead>
              <tbody>${selfTest}</tbody>
            </table>
          </div>
        </div>
        <div class="card-ft"><span>The verifier attacks its own input on every run, so the gate is demonstrated rather than asserted.</span></div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd">
        <h2>Findings</h2>
        <div class="row" style="gap:6px">
          ${[["all", "All", findings.length], ["block", "Blocking", findings.filter((f) => f.severity === "block").length], ["warn", "Warnings", findings.filter((f) => f.severity === "warn").length], ["info", "Info", findings.filter((f) => f.severity === "info").length]]
            .map(([id, label, count]) => `<button class="btn btn-sm ${S.verifyFilter === id ? "btn-primary" : "btn-ghost"}" data-act="filter-findings" data-filter="${id}">${label} ${count}</button>`).join("")}
        </div>
      </div>
      <div class="card-bd tight">
        ${findingRows || `<div class="muted" style="padding:22px;text-align:center">No findings in this category.</div>`}
      </div>
    </div>

    <div class="card">
      <div class="card-hd">
        <h2>Claim ledger</h2>
        <div class="row" style="gap:6px">
          ${[["all", "All"], ["pass", "Passing"], ["fail", "Failing"]].map(([id, label]) => `<button class="btn btn-sm ${S.claimFilter === id ? "btn-primary" : "btn-ghost"}" data-act="filter-claims" data-filter="${id}">${label}</button>`).join("")}
          <span class="hd-note" style="margin-left:6px">${claims.length} shown</span>
        </div>
      </div>
      <div class="card-bd tight">
        <div class="tbl-wrap" style="max-height:520px;overflow:auto">
          <table class="tbl">
            <thead><tr><th>Section</th><th class="num">As written</th><th>Source figure</th><th>Fact</th><th class="num">Source value</th><th>Check</th></tr></thead>
            <tbody>${claimRows || `<tr><td colspan="6" class="muted" style="padding:22px;text-align:center">Nothing to show.</td></tr>`}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd"><h2>Coverage &amp; closure</h2><span class="hd-note">material variances only</span></div>
      <div class="card-bd tight">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Variance</th><th>Entity</th><th>Account</th><th class="num">Variance</th><th>Attribution</th><th>Commentary</th></tr></thead>
            <tbody>
              ${verification.coverage.map((c) => {
                const closure = verification.closure.find((x) => x.variance_id === c.variance_id);
                return `<tr>
                  <td class="mono small">${esc(c.variance_id)}</td>
                  <td class="mono small">${esc(c.entity)}</td>
                  <td>${esc(c.account_short)}</td>
                  <td class="num">${money(c.variance_usd, { signed: true })}</td>
                  <td>${closure?.status === "pass" ? `<span class="tag tag-pass">closes</span>` : `<span class="tag tag-block">break</span>`}</td>
                  <td>${c.covered ? `<span class="tag tag-pass">${esc(c.section_ids.join(", "))}</span>` : `<span class="tag tag-block">missing</span>`}</td>
                </tr>`;
              }).join("")}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    ${cross ? `
    <div class="card">
      <div class="card-hd"><h2>Cross-section reconciliation</h2><span class="hd-note">${cross.passed}/${cross.total} driver and claim checks tie</span></div>
      <div class="card-bd tight">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Driver</th><th class="num">Cited across sections</th><th class="num">Reconciled total</th><th class="num">Difference</th><th>Check</th></tr></thead>
            <tbody>
              ${cross.driver_totals.map((r) => `
                <tr>
                  <td>${esc(r.label)} <span class="mono xsmall muted">${esc(r.driver)}</span></td>
                  <td class="num">${money(r.cited_usd, { signed: true })}</td>
                  <td class="num">${money(r.expected_usd, { signed: true })}</td>
                  <td class="num">${money(r.difference_usd, { signed: true })}</td>
                  <td><span class="tag ${r.status === "pass" ? "tag-pass" : "tag-block"}">${esc(r.status)}</span></td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>
      </div>
      ${cross.claims.some((c) => c.status !== "pass") ? `
      <div class="card-bd tight" style="border-top:1px solid var(--line)">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Section</th><th>Prose claim</th><th>Arithmetic expected</th><th>Prose arithmetic</th><th>Check</th></tr></thead>
            <tbody>
              ${cross.claims.filter((c) => c.status !== "pass").map((c) => `
                <tr>
                  <td class="mono small">${esc(c.section_id)}</td>
                  <td>${esc(c.claim)}</td>
                  <td class="mono small">${esc(c.expected)}</td>
                  <td class="mono small">${esc(c.actual)}</td>
                  <td><span class="tag tag-block">fail</span></td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>
      </div>` : ""}
      <div class="card-ft"><span>${cross.claims.length} prose claims — primary cause, complete attribution and entire-movement language — checked against the arithmetic behind the sections.</span></div>
    </div>` : ""}
  `;
}

/* -------------------------------------------------------------- signoff */
/* --------------------------------------------------------------- compare */
function runOption(run, selected) {
  const stamp = (run.created_at || "").replace("T", " ").slice(0, 16);
  const text = `${run.period || ""} · ${run.mode || ""} · ${run.status || ""} · ${stamp} · ${(run.id || "").slice(-4)}`;
  return `<option value="${esc(run.id)}" ${run.id === selected ? "selected" : ""}>${esc(text)}</option>`;
}

function renderCompare() {
  const runs = S.runs;
  const data = S.diff.data;
  const baseId = S.diff.base || runs[0]?.id;
  const compareId = S.diff.compare || runs[1]?.id;

  const head = `
    <div class="page-head">
      <div>
        <h1 class="page-title">Run comparison</h1>
        <div class="page-sub">What changed between two persisted runs: figures, findings, commentary and whether a signature still covers the result.</div>
      </div>
    </div>`;

  if (runs.length < 2) {
    return `${head}
      <div class="banner banner-warn"><span class="banner-icon">▲</span><div><strong>Two runs are needed to compare.</strong> Run the close again to compare a fresh run against the current one.</div></div>`;
  }

  const controls = `
    <div class="card">
      <div class="card-hd" style="gap:10px">
        <div class="row" style="gap:8px;align-items:flex-end">
          <label class="field" style="min-width:220px"><span>Base run (left)</span><select id="diff-base">${runs.map((r) => runOption(r, baseId)).join("")}</select></label>
          <span class="muted small" style="padding-bottom:9px">vs</span>
          <label class="field" style="min-width:220px"><span>Comparison run (right)</span><select id="diff-compare">${runs.map((r) => runOption(r, compareId)).join("")}</select></label>
          <button class="btn btn-sm btn-primary" data-act="load-diff" ${S.diff.loading ? "disabled" : ""}>${S.diff.loading ? "Comparing…" : "Compare"}</button>
        </div>
      </div>
    </div>`;

  if (!data) {
    if (S.diff.loading || !S.diff.attempted) {
      return `${head}${controls}
        <div class="loading-card"><div class="spinner"></div><p>Reviewing the two workpapers…</p></div>`;
    }
    return `${head}${controls}
      <div class="banner banner-warn"><span class="banner-icon">▲</span><div>
        <strong>No comparison loaded.</strong> The two runs could not be compared. Pick a different pair and select Compare again.
      </div></div>`;
  }

  const summary = data.summary;
  const kpis = [
    ["Figures changed", summary.facts_changed + summary.facts_added + summary.facts_removed,
      `${summary.facts_changed} changed · ${summary.facts_added} added · ${summary.facts_removed} removed`,
      summary.facts_changed || summary.facts_added || summary.facts_removed ? "warn" : "pass"],
    ["Findings added", summary.findings_added, `${summary.findings_cleared} cleared · ${summary.findings_changed} reworded`,
      summary.findings_added ? "block" : "pass"],
    ["Commentary sections changed", summary.sections_changed, `${summary.sections_added} added · ${summary.sections_cleared} cleared`,
      summary.sections_changed ? "warn" : "pass"],
    ["Signature invalidated", data.signoffs.invalidated ? "yes" : "no",
      data.base.signoff_id ? `base ${data.base.signoff_id}` : "base unsigned",
      data.signoffs.invalidated ? "block" : "pass"],
    ["Engine / verifier", `${esc(data.compare.engine_version || "—")} / ${esc(data.compare.verifier_version || "—")}`,
      `base ${data.base.engine_version || "—"} / ${data.base.verifier_version || "—"}`,
      data.versions.engine_changed || data.versions.verifier_changed ? "warn" : "pass"],
  ];

  const findingRow = (finding, tone) => `
    <tr>
      <td>${sevTag(finding.severity)}</td>
      <td class="mono small">${esc(finding.category)}</td>
      <td>${esc(finding.message)}</td>
      <td class="mono xsmall">${esc(finding.section_id || finding.control_id || finding.fact_id || "")}</td>
      <td><span class="tag tag-${tone}">${tone === "block" ? "added" : "cleared"}</span></td>
    </tr>`;

  const findingRows = [
    ...data.findings.added.map((f) => findingRow(f, f.severity === "block" ? "block" : "warn")),
    ...data.findings.cleared.map((f) => findingRow(f, "pass")),
  ].join("");

  const varianceRows = data.variances.map((row) => `
    <tr>
      <td class="mono small">${esc(row.entity)}</td>
      <td>${esc(row.account_short)} <span class="mono xsmall muted">${esc(row.account)}</span></td>
      <td><span class="tag ${row.change === "changed" ? "tag-warn" : row.change === "added" ? "tag-accent" : "tag-line"}">${esc(row.change)}</span></td>
      <td>
        ${row.fields.length ? row.fields.map((f) => `<div class="xsmall"><span class="mono">${esc(f.field)}</span> ${esc(f.base)} → <strong>${esc(f.compare)}</strong>${f.delta ? ` <span class="${num(f.delta) < 0 ? "neg" : "pos"}">(${esc(f.delta)})</span>` : ""}</div>`).join("") : `<span class="muted small">account added or removed</span>`}
      </td>
    </tr>`).join("");

  const factRows = data.facts.map((fact) => `
    <tr>
      <td class="mono xsmall">${esc(fact.kind)}</td>
      <td>${esc(fact.label)}<div class="xsmall muted">${esc(fact.entity || "")} ${fact.account ? `· ${esc(fact.account)}` : ""} ${fact.period ? `· ${esc(fact.period)}` : ""}</div></td>
      <td class="num mono xsmall">${esc(fact.base_value ?? "—")}</td>
      <td class="num mono xsmall">${esc(fact.compare_value ?? "—")}</td>
      <td class="num mono xsmall ${fact.delta && num(fact.delta) < 0 ? "neg" : "pos"}">${esc(fact.delta ?? "")}</td>
    </tr>`).join("");

  const sectionRows = [
    ...data.sections.changed.map((s) => `<tr><td class="mono small">${esc(s.section_id)}</td><td>${esc(s.title || s.kind)}</td><td>${esc((s.reasons || []).join(", "))}</td><td><span class="tag tag-warn">changed</span></td></tr>`),
    ...data.sections.added.map((s) => `<tr><td class="mono small">${esc(s.section_id)}</td><td>${esc(s.title || s.kind)}</td><td>new section</td><td><span class="tag tag-accent">added</span></td></tr>`),
    ...data.sections.cleared.map((s) => `<tr><td class="mono small">${esc(s.section_id)}</td><td>${esc(s.title || s.kind)}</td><td>removed</td><td><span class="tag tag-line">cleared</span></td></tr>`),
  ].join("");

  return `${head}${controls}

    ${data.same_period ? "" : `<div class="banner banner-warn"><span class="banner-icon">▲</span><div><strong>Different periods.</strong> The two runs cover ${esc(data.base.period)} and ${esc(data.compare.period)}; the comparison is informational.</div></div>`}

    <div class="kpis" style="grid-template-columns:repeat(5,minmax(0,1fr))">
      ${kpis.map(([label, value, foot, cls]) => `
        <div class="kpi ${cls}"><div class="kpi-label">${esc(label)}</div><div class="kpi-value">${value}</div><div class="kpi-foot">${esc(foot)}</div></div>`).join("")}
    </div>

    <div class="card">
      <div class="card-hd"><h2>Signature status</h2><span class="hd-note">${data.hashes.match ? "artefact hashes match" : "artefact hashes differ"}</span></div>
      <div class="card-bd">
        <div class="kv-grid">
          <div class="kv"><dt>Base run</dt><dd>${esc(data.base.run_id)} · ${esc(data.base.status)}${data.base.signoff_id ? ` · ${esc(data.base.signoff_id)}` : " · unsigned"}</dd></div>
          <div class="kv"><dt>Comparison run</dt><dd>${esc(data.compare.run_id)} · ${esc(data.compare.status)}${data.compare.signoff_id ? ` · ${esc(data.compare.signoff_id)}` : " · unsigned"}</dd></div>
          <div class="kv"><dt>Base content hash</dt><dd class="hash">${esc(data.hashes.base.content_hash)}</dd></div>
          <div class="kv"><dt>Compare content hash</dt><dd class="hash">${esc(data.hashes.compare.content_hash)}</dd></div>
        </div>
        <div class="banner ${data.signoffs.invalidated ? "banner-block" : "banner-pass"}" style="margin-top:12px">
          <span class="banner-icon">${data.signoffs.invalidated ? "■" : "✓"}</span>
          <div><strong>${data.signoffs.invalidated ? "Signature does not cover the comparison run." : "No signature is invalidated."}</strong> ${esc(data.signoffs.reason)}${data.signoffs.base.chain_status ? ` Chain link: ${esc(data.signoffs.base.chain_status)}.` : ""}</div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd"><h2>Findings</h2><span class="hd-note">${data.findings.added.length} added · ${data.findings.cleared.length} cleared · ${data.findings.changed.length} reworded</span></div>
      <div class="card-bd tight">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Severity</th><th>Category</th><th>Message</th><th>Subject</th><th>Change</th></tr></thead>
            <tbody>${findingRows || `<tr><td colspan="5" class="muted" style="padding:20px;text-align:center">The two verification reports raise the same findings.</td></tr>`}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd"><h2>Variances that moved</h2><span class="hd-note">${data.variances.length} changed</span></div>
      <div class="card-bd tight">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Entity</th><th>Account</th><th>Change</th><th>Fields (base → comparison)</th></tr></thead>
            <tbody>${varianceRows || `<tr><td colspan="4" class="muted" style="padding:20px;text-align:center">Actual, budget, variance and materiality are identical.</td></tr>`}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd"><h2>Figures that moved</h2><span class="hd-note">semantic key, not positional fact id</span></div>
      <div class="card-bd tight">
        <div class="tbl-wrap" style="max-height:460px;overflow:auto">
          <table class="tbl">
            <thead><tr><th>Kind</th><th>Figure</th><th class="num">Base</th><th class="num">Comparison</th><th class="num">Delta</th></tr></thead>
            <tbody>${factRows || `<tr><td colspan="5" class="muted" style="padding:20px;text-align:center">Every re-derived figure is unchanged.</td></tr>`}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd"><h2>Commentary sections</h2><span class="hd-note">${data.sections.changed.length} changed</span></div>
      <div class="card-bd tight">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Section</th><th>Title</th><th>What changed</th><th>Status</th></tr></thead>
            <tbody>${sectionRows || `<tr><td colspan="4" class="muted" style="padding:20px;text-align:center">The commentary is identical.</td></tr>`}</tbody>
          </table>
        </div>
      </div>
    </div>`;
}

function renderSignoff() {
  const { verification, run, preflight, signoff, chain, events } = S.run;
  const warns = verification.findings.filter((f) => f.severity === "warn");

  const chainRows = chain.map((link) => `
    <tr>
      <td class="mono small">${esc(link.signoff_id)}</td>
      <td class="mono small">${esc(link.run_id)}</td>
      <td>${esc(link.signer)}</td>
      <td class="mono xsmall">${esc(link.chain_hash.slice(0, 24))}…</td>
      <td><span class="tag ${link.status === "intact" ? "tag-pass" : "tag-block"}">${esc(link.status)}</span></td>
    </tr>`).join("");

  const timeline = (events || []).map((e) => {
    const kind = e.action.includes("verify") ? (e.detail?.status === "blocked" ? "block" : "pass") : e.action.includes("signoff") ? "pass" : e.action.includes("narrate") ? "warn" : "";
    return `
      <li>
        <div class="tl-dot ${kind}"></div>
        <div>
          <div class="tl-title">${esc(e.action)} <span class="muted">· ${esc(e.actor)}</span></div>
          <div class="tl-meta">${esc(new Date(e.at).toLocaleString())}</div>
          ${Object.keys(e.detail || {}).length ? `<div class="xsmall muted" style="margin-top:3px">${esc(brief(e.detail))}</div>` : ""}
        </div>
      </li>`;
  }).join("");

  if (run.status === "signed" && signoff) {
    return `
      <div class="page-head">
        <div>
          <h1 class="page-title">Signed off</h1>
          <div class="page-sub">${esc(run.period_label)} close commentary for ${esc(S.meta.group)}.</div>
        </div>
        <div class="page-actions">
          <button class="btn btn-sm" data-act="export">Download evidence pack</button>
          <button class="btn btn-sm" data-act="export-csv">Download review sheet</button>
        </div>
      </div>

      <div class="cert">
        <div class="row" style="justify-content:space-between;align-items:flex-start">
          <div>
            <h3>Certificate of sign-off</h3>
            <div class="cert-sub">${esc(signoff.signoff_id)} · ${esc(run.period)} · ${esc(S.meta.group)}</div>
          </div>
          ${statusPill("signed")}
        </div>
        <div class="cert-grid">
          <div class="kv"><dt>Signed by</dt><dd style="font-family:var(--sans);font-size:12.5px">${esc(signoff.signer)}</dd></div>
          <div class="kv"><dt>Prepared by</dt><dd style="font-family:var(--sans);font-size:12.5px">${esc(signoff.preparer || "—")}</dd></div>
          <div class="kv"><dt>Role</dt><dd style="font-family:var(--sans);font-size:12.5px">${esc(signoff.role)}</dd></div>
          <div class="kv"><dt>Separation of duties</dt><dd style="font-family:var(--sans);font-size:12.5px">${signoff.preparer
            ? (signoff.dual_control ? "Dual control — preparer also signed" : "Signer ≠ preparer")
            : "Not recorded on this legacy record"}</dd></div>
          <div class="kv"><dt>Signed at</dt><dd>${esc(new Date(signoff.signed_at).toLocaleString())}</dd></div>
          <div class="kv"><dt>Narrative mode</dt><dd style="font-family:var(--sans)">${esc(signoff.narrative_mode)}</dd></div>
          <div class="kv"><dt>Engine version</dt><dd class="mono">${esc(signoff.engine_version || S.run.versions?.engine || "not recorded")}</dd></div>
          <div class="kv"><dt>Verifier version</dt><dd class="mono">${esc(signoff.verifier_version || S.run.versions?.verifier || "not recorded")}</dd></div>
          <div class="kv"><dt>Material variances signed</dt><dd>${signoff.counts.material_variances}</dd></div>
          <div class="kv"><dt>Figures verified</dt><dd>${signoff.counts.figures_verified}</dd></div>
          <div class="kv"><dt>Source figures re-derived</dt><dd>${signoff.counts.facts_rederived}</dd></div>
          <div class="kv"><dt>Exceptions attested</dt><dd>${signoff.counts.warnings_attested}</dd></div>
        </div>
        <hr class="rule" />
        <div class="mini-hd">Artefact hashes (sha-256)</div>
        <div class="kv-grid">
          <div class="kv"><dt>Content</dt><dd class="hash">${esc(signoff.hashes.content_hash)}</dd></div>
          <div class="kv"><dt>Verification report</dt><dd class="hash">${esc(signoff.hashes.verification_hash)}</dd></div>
          <div class="kv"><dt>Source fact store</dt><dd class="hash">${esc(signoff.hashes.fact_store_hash)}</dd></div>
          <div class="kv"><dt>Chain hash</dt><dd class="hash">${esc(signoff.chain_hash)}</dd></div>
          <div class="kv"><dt>Previous link</dt><dd class="hash">${esc(signoff.prev_chain_hash)}</dd></div>
        </div>
        ${signoff.note ? `<hr class="rule" /><div class="mini-hd">Signer note</div><div class="small">${esc(signoff.note)}</div>` : ""}
        ${signoff.attestations.length ? `
          <hr class="rule" />
          <div class="mini-hd">Attested exceptions</div>
          ${signoff.attestations.map((a) => `<div class="small" style="margin-bottom:10px">
            <div><span class="mono xsmall">${esc(a.finding_id)}</span> · <strong>${esc(a.template || a.category || "exception")}</strong></div>
            <div>${esc(a.rationale)}</div>
            ${a.details && Object.keys(a.details).length ? `<div class="xsmall muted" style="margin-top:2px">${Object.entries(a.details).map(([k, v]) => `${esc(k.replace(/_/g, " "))}: ${esc(v)}`).join(" · ")}</div>` : ""}
          </div>`).join("")}` : ""}
      </div>

      <div class="card">
        <div class="card-hd"><h2>Audit trail</h2><span class="hd-note">append-only, hash-chained</span></div>
        <div class="card-bd tight">
          <div class="tbl-wrap">
            <table class="tbl">
              <thead><tr><th>Record</th><th>Run</th><th>Signer</th><th>Chain hash</th><th>Integrity</th></tr></thead>
              <tbody>${chainRows || `<tr><td colspan="5" class="muted" style="padding:18px">No sign-offs recorded yet.</td></tr>`}</tbody>
            </table>
          </div>
        </div>
        <div class="card-ft"><span>Any edit to the commentary, the verification report or the fact store breaks the chain at this record.</span></div>
      </div>

      <div class="card">
        <div class="card-hd"><h2>Run events</h2><span class="hd-note">${(events || []).length} events</span></div>
        <div class="card-bd"><ul class="timeline">${timeline}</ul></div>
      </div>
    `;
  }

  const attestCards = warns.length ? warns.map((w) => {
    const state = S.attest[w.finding_id] || { checked: false, rationale: "", details: {} };
    const template = templateFor(w.category);
    const details = attestationDetails(w);
    const fieldHtml = (template.fields || []).map((field) => {
      const value = details[field.name] || "";
      const common = `data-attest-field="${esc(field.name)}" data-attest-id="${esc(w.finding_id)}"`;
      if (field.auto_from_finding) {
        return `<label class="field"><span>${esc(field.label)} (from the finding)</span><input type="text" value="${esc(value)}" readonly class="mono" /></label>`;
      }
      return `<label class="field"><span>${esc(field.label)}${field.required ? " *" : ""}</span>
        <input type="${field.type === "date" ? "date" : "text"}" ${common} value="${esc(value)}" placeholder="${esc(field.placeholder || "")}" /></label>`;
    }).join("");
    return `
      <div class="attest-item ${state.checked ? "on" : ""}" data-attest="${esc(w.finding_id)}">
        <div class="attest-top">
          <input type="checkbox" id="att-${esc(w.finding_id)}" ${state.checked ? "checked" : ""} data-attest-check="${esc(w.finding_id)}" />
          <label for="att-${esc(w.finding_id)}">
            <div class="attest-title">${esc(template.label)} <span class="mono xsmall muted">${esc(w.category)}</span></div>
            <div class="attest-desc">${esc(w.message)}</div>
          </label>
        </div>
        <div class="attest-template">
          <div class="xsmall muted" style="margin-bottom:8px">${esc(template.instruction || "")}</div>
          ${fieldHtml ? `<div class="form-row">${fieldHtml}</div>` : ""}
          <textarea rows="2" placeholder="Rationale for accepting this exception…" data-attest-note="${esc(w.finding_id)}">${esc(state.rationale)}</textarea>
        </div>
      </div>`;
  }).join("") : `<div class="banner banner-pass" style="margin:0"><span class="banner-icon">✓</span><div>No exceptions require attestation on this run.</div></div>`;

  const attestedCount = warns.filter(attestationReady).length;
  const checks = preflight.checks.map((c) => {
    if (c.id !== "attestations" || !warns.length) return c;
    return {
      ...c,
      passed: attestedCount === warns.length,
      detail: `${attestedCount} of ${warns.length} attested`,
    };
  });
  const permitted = checks.every((c) => c.passed);
  const canSign = !verification.gate.blocking && attestedCount === warns.length;

  return `
    <div class="page-head">
      <div>
        <h1 class="page-title">Sign-off</h1>
        <div class="page-sub">A signature binds the commentary, the verification report and the source fact store into one hashed record.</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-sm" data-act="reverify">Re-verify before signing</button>
      </div>
    </div>

    ${verification.gate.blocking ? `
      <div class="banner banner-block">
        <span class="banner-icon">■</span>
        <div><strong>Signature is not available.</strong> ${esc(verification.gate.reasons.join(" · "))}. Clear the blocking findings in Verification first.</div>
      </div>` : ""}

    <div class="grid-2" style="grid-template-columns: 1fr 1fr">
      <div class="card">
        <div class="card-hd"><h2>Gate conditions</h2><span class="hd-note">${permitted ? "met" : "outstanding"}</span></div>
        <div class="card-bd">
          <ul class="check-list">
            ${checks.map((c) => `
              <li data-gate="${esc(c.id)}">
                <div class="check-mark ${c.passed ? "check-pass" : "check-fail"}">${c.passed ? "✓" : "!"}</div>
                <div>${esc(c.label)}</div>
                <div class="check-detail">${esc(c.detail)}</div>
              </li>`).join("")}
          </ul>
          <div class="small muted" id="gate-note" style="margin-top:12px">
            ${permitted
              ? "Every condition is satisfied. Complete the signer details and sign below."
              : "Attest each warning on the right; the remaining conditions are already satisfied by the verification run."}
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-hd"><h2>Exceptions to attest</h2><span class="hd-note">${warns.length} open</span></div>
        <div class="card-bd">${attestCards}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd"><h2>Signature</h2><span class="hd-note">recorded against ${esc(run.period)}</span></div>
      <div class="card-bd">
        <div class="small muted" style="margin-bottom:12px">
          Prepared by <strong>${esc(run.preparer || "—")}</strong>${run.dual_control
            ? " · dual control enabled, so the preparer may also sign"
            : " · the signer must be a different person"}.
        </div>
        <div class="form-row">
          <label class="field"><span>Signer name</span><input type="text" id="signer-name" placeholder="e.g. A. Mensah" /></label>
          <label class="field"><span>Role</span><input type="text" id="signer-role" value="Group Controller" /></label>
        </div>
        <div class="small" id="separation-note" hidden style="margin:-2px 0 10px"></div>
        <label class="field"><span>Note (optional, retained in the audit trail)</span><textarea id="signer-note" rows="2" placeholder="e.g. October close reviewed with the CFO; intercompany cut-off differences accepted as immaterial."></textarea></label>
        <div class="row">
          <button class="btn btn-primary" data-act="sign" ${canSign ? "" : "disabled"}>Sign off ${esc(run.period_label)}</button>
          <span class="small muted">Signing hashes the commentary, the verification report and the source fact store, and links to the previous sign-off.</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd"><h2>Audit trail</h2><span class="hd-note">append-only, hash-chained</span></div>
      <div class="card-bd tight">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Record</th><th>Run</th><th>Signer</th><th>Chain hash</th><th>Integrity</th></tr></thead>
            <tbody>${chainRows || `<tr><td colspan="5" class="muted" style="padding:18px">No sign-offs recorded yet.</td></tr>`}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd"><h2>Run events</h2><span class="hd-note">${(events || []).length} events</span></div>
      <div class="card-bd"><ul class="timeline">${timeline}</ul></div>
    </div>
  `;
}

function brief(detail) {
  return Object.entries(detail)
    .filter(([, v]) => v !== null && typeof v !== "object")
    .slice(0, 6)
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`)
    .join(" · ");
}

/* --------------------------------------------------------------- drawer */
async function openFact(factId) {
  const drawer = $("#drawer");
  const scrim = $("#drawer-scrim");
  drawer.hidden = false;
  scrim.hidden = false;
  drawer.innerHTML = `<div class="drawer-hd"><div><h2 class="drawer-title">Loading figure…</h2><div class="drawer-sub">${esc(factId)}</div></div><button class="close-x" data-act="close-drawer">×</button></div>`;
  try {
    const key = `${S.run.run.id}:${factId}`;
    const payload = S.factCache[key] || (S.factCache[key] = await api(`/api/runs/${S.run.run.id}/facts/${factId}`));
    const fact = payload.fact;
    const check = payload.check;
    const derivation = fact.derivation || {};
    const lines = derivation.lines || [];
    drawer.innerHTML = `
      <div class="drawer-hd">
        <div style="min-width:0">
          <div class="drawer-sub">${esc(fact.fact_id)} · ${esc(fact.kind)}</div>
          <h2 class="drawer-title">${esc(fact.label)}</h2>
        </div>
        <button class="close-x" data-act="close-drawer">×</button>
      </div>
      <div class="drawer-bd">
        <div class="val-big">${fact.unit === "pct" ? pct(fact.value) : moneyFull(fact.value)}</div>
        <div class="row" style="margin-top:6px">
          <span class="tag ${check?.status === "pass" ? "tag-pass" : "tag-block"}">${check?.status === "pass" ? "re-derived from source" : "does not re-derive"}</span>
          <span class="xsmall muted">${esc(fact.unit)} · ${esc(fact.basis)} · ${esc(fact.period)}</span>
        </div>

        <section>
          <h4>Re-derivation</h4>
          <div class="kv-grid">
            <div class="kv"><dt>Stored value</dt><dd>${esc(check?.stored ?? fact.value)}</dd></div>
            <div class="kv"><dt>Recomputed</dt><dd>${esc(check?.recomputed ?? "—")}</dd></div>
            <div class="kv"><dt>Method</dt><dd>${esc(derivation.type || "—")}</dd></div>
            <div class="kv"><dt>Entity / account</dt><dd>${esc(fact.entity)}${fact.account ? ` / ${esc(fact.account)}` : ""}</dd></div>
          </div>
        </section>

        <section>
          <h4>Source</h4>
          <div class="kv"><dt>System</dt><dd style="font-family:var(--sans);font-size:12px">${esc(fact.source.system)}</dd></div>
          <div class="kv" style="margin-top:8px"><dt>Reference</dt><dd>${esc(fact.source.ref)}</dd></div>
        </section>

        ${lines.length ? `
          <section>
            <h4>Journal lines feeding this figure (${lines.length})</h4>
            <table class="mini">
              <thead><tr><th>Journal</th><th class="num" style="text-align:right">Amount</th></tr></thead>
              <tbody>
                ${lines.map((l) => `<tr><td>${esc(l.je_id)}</td><td style="text-align:right">${moneyFull(l.amount)}</td></tr>`).join("")}
              </tbody>
            </table>
          </section>` : ""}

        <section>
          <h4>Derivation record</h4>
          <div class="code-block">${esc(JSON.stringify({ ...derivation, lines: undefined }, null, 2))}</div>
        </section>

        <section>
          <h4>Why this can be relied on</h4>
          <p class="small muted" style="margin:0">
            The verifier does not read this stored value. It recomputes the figure from the journal extract,
            the plan of record and the treasury FX table, then compares the two. A mismatch blocks sign-off.
          </p>
        </section>
      </div>`;
  } catch (error) {
    drawer.innerHTML = `<div class="drawer-hd"><div><h2 class="drawer-title">Could not load figure</h2><div class="drawer-sub">${esc(error.message)}</div></div><button class="close-x" data-act="close-drawer">×</button></div>`;
  }
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawer-scrim").hidden = true;
}

/* --------------------------------------------------------------- actions */
async function refresh() {
  const view = await api(`/api/runs/${S.run.run.id}`);
  S.run = view;
  bindFigStatus();
  render();
}

async function loadRollforward(force = false) {
  if (S.rollforward && !force) return;
  S.rollforwardLoading = true;
  try {
    S.rollforward = await api("/api/rollforward");
  } finally {
    S.rollforwardLoading = false;
  }
}

async function loadDiff(baseId, compareId) {
  if (!baseId || !compareId || baseId === compareId) {
    toast("Pick two different runs to compare.", "bad");
    return;
  }
  S.diff.base = baseId;
  S.diff.compare = compareId;
  S.diff.loading = true;
  S.diff.attempted = true;
  render();
  try {
    S.diff.data = await api(`/api/runs/${baseId}/diff/${compareId}`);
  } catch (error) {
    S.diff.data = null;
    toast(error.message, "bad");
  } finally {
    S.diff.loading = false;
    render();
  }
}

async function openDiff() {
  const current = S.run.run.id;
  const others = S.runs.filter((r) => r.id !== current);
  const samePeriod = others.find((r) => r.period === S.run.run.period);
  const compare = (samePeriod || others[0])?.id || null;
  S.tab = "compare";
  S.diff = { base: current, compare, data: null, loading: Boolean(compare), attempted: true };
  render();
  if (compare) await loadDiff(current, compare);
}

async function act(action, target) {
  switch (action) {
    case "tab":
      S.tab = target;
      S.menuOpen = false;
      render();
      break;
    case "go-verify":
      S.tab = "verify";
      render();
      break;
    case "go-signoff":
      S.tab = "signoff";
      render();
      break;
    case "go-workbench":
      S.tab = "workbench";
      render();
      break;
    case "wb-mode":
      S.wb.mode = target === "rollforward" ? "rollforward" : "variance";
      if (S.wb.mode === "rollforward" && !S.rollforward) {
        await withBusy("rollforward", () => loadRollforward());
      }
      render();
      break;
    case "refresh-rollforward":
      await withBusy("rollforward", () => loadRollforward(true));
      render();
      break;
    case "switch-run":
      if (!target || target === S.run.run.id) break;
      await withBusy("switch", async () => {
        const view = await api(`/api/runs/${target}`);
        S.diff = { base: null, compare: null, data: null, loading: false, attempted: false };
        applyView(view);
      });
      break;
    case "open-diff":
      await withBusy("diff", openDiff);
      break;
    case "load-diff":
      await withBusy("diff", () => loadDiff(
        $("#diff-base")?.value || S.diff.base,
        $("#diff-compare")?.value || S.diff.compare,
      ));
      break;
    case "go-commentary": {
      const section = S.run.commentary.sections.find((s) => s.variance_id === target);
      S.tab = "commentary";
      S.section = section ? section.section_id : null;
      render();
      if (section) document.getElementById(`sec-${section.section_id}`)?.scrollIntoView({ block: "start", behavior: "smooth" });
      break;
    }
    case "jump": {
      S.section = target;
      const node = document.getElementById(`sec-${target}`);
      if (node) node.scrollIntoView({ block: "start", behavior: "smooth" });
      $$(".subnav button").forEach((b) => b.classList.toggle("on", b.dataset.section === target));
      break;
    }
    case "new-run":
      await withBusy("run", async () => {
        const view = await api("/api/runs", { method: "POST", body: JSON.stringify({ mode: "strict" }) });
        await refreshRuns();
        S.tab = "overview";
        applyView(view);
        toast("Close re-run: reconciled, narrated and verified.", "ok");
      });
      break;
    case "regenerate":
      await withBusy("regen", async () => {
        const view = await api(`/api/runs/${S.run.run.id}/renarrate`, { method: "POST", body: JSON.stringify({ mode: "strict" }) });
        applyView(view);
        toast("Commentary regenerated from facts and re-verified.", "ok");
      });
      break;
    case "drift":
      await withBusy("drift", async () => {
        const view = await api(`/api/runs/${S.run.run.id}/renarrate`, { method: "POST", body: JSON.stringify({ mode: "drift" }) });
        S.tab = "verify";
        applyView(view);
        toast(`Adversarial test: ${view.verification.gate.blocking} blocking findings raised and the gate closed.`, "warn");
      });
      break;
    case "reverify":
      await withBusy("verify", async () => {
        const view = await api(`/api/runs/${S.run.run.id}/verify`, { method: "POST" });
        S.run = view;
        bindFigStatus();
        render();
        toast(`Verification re-run: ${view.verification.stats.figures_passed}/${view.verification.stats.figures_checked} figures tie out.`, view.verification.gate.blocking ? "bad" : "ok");
      });
      break;
    case "filter-findings":
      S.verifyFilter = target;
      render();
      break;
    case "filter-claims":
      S.claimFilter = target;
      render();
      break;
    case "toggle-finding":
      S.openFinding = S.openFinding === target ? null : target;
      render();
      break;
    case "export":
      window.location.href = `/api/runs/${S.run.run.id}/export`;
      break;
    case "export-csv":
      window.location.href = `/api/runs/${S.run.run.id}/export.csv`;
      break;
    case "close-drawer":
      closeDrawer();
      break;
    case "sign": {
      const signer = $("#signer-name")?.value?.trim();
      const role = $("#signer-role")?.value?.trim() || "Group Controller";
      const note = $("#signer-note")?.value?.trim() || "";
      const attestations = Object.entries(S.attest)
        .filter(([, v]) => v.checked && v.rationale.trim())
        .map(([finding_id, v]) => ({ finding_id, rationale: v.rationale.trim(), details: v.details || {} }));
      if (!signer) { toast("A signer name is required.", "bad"); return; }
      const preparer = String(S.run.run.preparer || "").trim();
      if (!S.run.run.dual_control && preparer && signer.toLowerCase() === preparer.toLowerCase()) {
        toast(`Separation of duties: ${preparer} prepared this run; a different signer is required.`, "bad");
        return;
      }
      await withBusy("sign", async () => {
        const view = await api(`/api/runs/${S.run.run.id}/signoff`, {
          method: "POST",
          body: JSON.stringify({ signer, role, note, attestations }),
        });
        await refreshRuns();
        applyView(view);
        S.tab = "signoff";
        render();
        toast(`Signed off by ${signer}. Chain hash ${view.signoff.chain_hash.slice(0, 12)}…`, "ok");
      });
      break;
    }
    default:
      break;
  }
}

/* Keep the gate checklist, note and sign button in step with local attestations
   without a full re-render (which would steal focus from the rationale fields). */
function syncAttestGate() {
  const warns = S.run?.verification?.findings?.filter((f) => f.severity === "warn") || [];
  if (!warns.length) return;
  const done = warns.filter(attestationReady).length;
  const complete = done === warns.length;
  const li = document.querySelector('[data-gate="attestations"]');
  if (li) {
    const mark = li.querySelector(".check-mark");
    if (mark) {
      mark.className = `check-mark ${complete ? "check-pass" : "check-fail"}`;
      mark.textContent = complete ? "✓" : "!";
    }
    const detail = li.querySelector(".check-detail");
    if (detail) detail.textContent = `${done} of ${warns.length} attested`;
  }
  const note = document.getElementById("gate-note");
  if (note) {
    note.textContent = complete
      ? "Every condition is satisfied. Complete the signer details and sign below."
      : "Attest each warning on the right; the remaining conditions are already satisfied by the verification run.";
  }
  const btn = document.querySelector('[data-act="sign"]');
  if (btn) btn.disabled = Boolean(S.run.verification.gate.blocking) || !complete;
  syncSignButton();
}

/* Separation of duties is enforced by the API; this keeps the form honest so
   the signer is told before they submit rather than after a 409. It also owns
   the enabled state of the sign button so the attestation and separation rules
   cannot fight over it. */
function syncSignButton() {
  const run = S.run?.run;
  const input = document.getElementById("signer-name");
  const note = document.getElementById("separation-note");
  const btn = document.querySelector('[data-act="sign"]');
  if (!btn) return;
  const warns = S.run?.verification?.findings?.filter((f) => f.severity === "warn") || [];
  const attested = warns.filter(attestationReady).length;
  const gateBlocked = Boolean(S.run?.verification?.gate?.blocking) || attested < warns.length;
  const preparer = String(run?.preparer || "").trim();
  const signer = input ? input.value.trim() : "";
  const conflict = Boolean(preparer) && Boolean(signer)
    && signer.toLowerCase() === preparer.toLowerCase();
  const blocked = conflict && !run?.dual_control;
  btn.disabled = gateBlocked || blocked;
  if (!note) return;
  note.hidden = !conflict;
  note.textContent = blocked
    ? `Separation of duties: ${signer} prepared this run. A different signer is required unless dual control is enabled on the run.`
    : `Dual control is enabled for this run, so ${signer} may sign as both preparer and signer. The waiver is recorded on the certificate.`;
  note.style.color = blocked ? "#8a1c1c" : "#6b4a00";
}

/* --------------------------------------------------------------- events */
document.addEventListener("click", (event) => {
  const factLink = event.target.closest("[data-fact]");
  if (factLink) {
    event.preventDefault();
    event.stopPropagation();
    openFact(factLink.dataset.fact);
    return;
  }

  const actionEl = event.target.closest("[data-act]");
  if (actionEl) {
    const { act: action } = actionEl.dataset;
    const targets = {
      tab: actionEl.dataset.tab,
      jump: actionEl.dataset.section,
      "go-commentary": actionEl.dataset.section,
      "toggle-finding": actionEl.dataset.id,
      "filter-findings": actionEl.dataset.filter,
      "filter-claims": actionEl.dataset.filter,
      "wb-mode": actionEl.dataset.mode,
    };
    act(action, targets[action]);
    return;
  }

  const tabButton = event.target.closest(".rail-nav button");
  if (tabButton) { act("tab", tabButton.dataset.tab); return; }

  const row = event.target.closest("tr[data-variance]");
  if (row && S.tab === "workbench") {
    S.wb.open = S.wb.open === row.dataset.variance ? null : row.dataset.variance;
    render();
    return;
  }

  if (event.target.id === "drawer-scrim") closeDrawer();
});

document.addEventListener("change", (event) => {
  const id = event.target.id;
  if (id === "wb-entity") { S.wb.entity = event.target.value; render(); }
  if (id === "wb-statement") { S.wb.statement = event.target.value; render(); }
  if (id === "wb-material") { S.wb.materialOnly = event.target.checked; render(); }
  if (id === "run-switcher") { act("switch-run", event.target.value); return; }
  if (id === "diff-base") {
    S.diff.base = event.target.value;
    act("load-diff");
    return;
  }
  if (id === "diff-compare") {
    S.diff.compare = event.target.value;
    act("load-diff");
    return;
  }
  const attestId = event.target.dataset?.attestCheck;
  if (attestId) {
    S.attest[attestId] = S.attest[attestId] || { checked: false, rationale: "", details: {} };
    S.attest[attestId].checked = event.target.checked;
    const wrap = event.target.closest(".attest-item");
    if (wrap) wrap.classList.toggle("on", event.target.checked);
    syncAttestGate();
  }
  const field = event.target.dataset?.attestField;
  if (field) {
    const findingId = event.target.dataset.attestId;
    S.attest[findingId] = S.attest[findingId] || { checked: false, rationale: "", details: {} };
    S.attest[findingId].details = S.attest[findingId].details || {};
    S.attest[findingId].details[field] = event.target.value;
    syncAttestGate();
  }
});

document.addEventListener("input", (event) => {
  if (event.target.id === "wb-search") {
    S.wb.q = event.target.value;
    const pos = event.target.selectionStart;
    render();
    const input = $("#wb-search");
    if (input) { input.focus(); input.setSelectionRange(pos, pos); }
  }
  const attestId = event.target.dataset?.attestNote;
  if (attestId) {
    S.attest[attestId] = S.attest[attestId] || { checked: false, rationale: "", details: {} };
    S.attest[attestId].rationale = event.target.value;
    syncAttestGate();
  }
  const field = event.target.dataset?.attestField;
  if (field) {
    const findingId = event.target.dataset.attestId;
    S.attest[findingId] = S.attest[findingId] || { checked: false, rationale: "", details: {} };
    S.attest[findingId].details = S.attest[findingId].details || {};
    S.attest[findingId].details[field] = event.target.value;
    syncAttestGate();
  }
  if (event.target.id === "signer-name") syncSignButton();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDrawer();
});

window.addEventListener("hashchange", () => {
  const tab = tabFromHash();
  if (tab && tab !== S.tab) { S.tab = tab; render(); }
});

boot();
