/**
 * Measles Evidence Watch — frontend
 * Modelled on the Ebola Evidence Watch design system.
 * Reads data/evidence.json and data/coverage.json.
 */
"use strict";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PAGE_SIZE = 25;

const POLICY_AREAS = [
  { id: "all",                  label: "All areas",                             cdot: null },
  { id: "pregnancy_mmr",        label: "MMR in pregnancy",                      cdot: "var(--pa-pregnancy-mmr)" },
  { id: "pregnancy_infection",  label: "Measles/rubella infection in pregnancy", cdot: "var(--pa-pregnancy-infection)" },
  { id: "schedule_timing",      label: "Second-dose timing",                    cdot: "var(--pa-schedule-timing)" },
  { id: "general_measles",      label: "General measles",                       cdot: "var(--pa-general-measles)" },
];

const CHANNELS = [
  { id: "journals",     label: "Journals",          cdot: "var(--t-journal)" },
  { id: "preprints",    label: "Preprints",          cdot: "var(--t-preprint)" },
  { id: "trials",       label: "Clinical trials",    cdot: "var(--t-trial)" },
  { id: "who_sage",     label: "WHO / SAGE",         cdot: "var(--t-guideline)" },
  { id: "nitag",        label: "National NITAG",     cdot: "var(--t-guideline)" },
  { id: "surveillance", label: "Surveillance",       cdot: "var(--t-surveillance)" },
  { id: "news",         label: "News",               cdot: "var(--t-news)" },
  { id: "press",        label: "Press releases",     cdot: "var(--t-press)" },
];

const SOURCE_TYPES = [
  { id: "journal",         label: "Journal article" },
  { id: "journal_article", label: "Journal article" },   // from sources.py
  { id: "preprint",        label: "Preprint" },
  { id: "clinical_trial",  label: "Clinical trial" },
  { id: "guideline",       label: "Guideline" },
  { id: "surveillance",    label: "Surveillance" },
  { id: "outbreak_report", label: "Outbreak report" },
  { id: "news",            label: "News" },
  { id: "press_release",   label: "Press release" },
];

// Canonical source_type labels (normalised)
const ST_LABEL = {
  journal:         "Journal article",
  journal_article: "Journal article",
  preprint:        "Preprint",
  clinical_trial:  "Clinical trial",
  guideline:       "Guideline",
  surveillance:    "Surveillance",
  outbreak_report: "Outbreak report",
  news:            "News",
  press_release:   "Press release",
  other:           "Other",
};

// Type-dot colours for badge (match CSS vars)
const ST_DOT = {
  journal:         "var(--t-journal)",
  journal_article: "var(--t-journal)",
  preprint:        "var(--t-preprint)",
  clinical_trial:  "var(--t-trial)",
  guideline:       "var(--t-guideline)",
  surveillance:    "var(--t-surveillance)",
  outbreak_report: "var(--t-outbreak)",
  news:            "var(--t-news)",
  press_release:   "var(--t-press)",
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let allRecords  = [];
let coverage    = {};
let filtered    = [];
let currentPage = 1;

const state = {
  search:    "",
  policy:    "all",       // radio — single select
  channels:  new Set(),   // checkbox — multi-select (empty = all)
  types:     new Set(),   // checkbox — multi-select (empty = all)
  species:   "all",       // radio
  pregnancy: false,
  canadian:  false,
  days:      "all",       // date-preset
  sort:      "date_desc",
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function recDate(r) {
  return r.date || r.published_date || r.published || "";
}

function esc(str) {
  return String(str || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function fmtDate(s) {
  if (!s) return "";
  const d = new Date(s);
  if (isNaN(d)) return s;
  return d.toLocaleDateString("en-CA", { year: "numeric", month: "short", day: "numeric" });
}

// ---------------------------------------------------------------------------
// Theme toggle
// ---------------------------------------------------------------------------

(function initTheme() {
  const stored = localStorage.getItem("theme");
  if (stored) document.documentElement.setAttribute("data-theme", stored);
})();

document.getElementById("theme-toggle").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("theme", next); } catch(e) {}
  document.getElementById("theme-toggle").textContent = next === "dark" ? "🌙" : "☀️";
});

// ---------------------------------------------------------------------------
// Fetch data
// ---------------------------------------------------------------------------

async function loadData() {
  try {
    const [evResp, covResp] = await Promise.all([
      fetch("evidence.json"),
      fetch("coverage.json"),
    ]);
    allRecords = await evResp.json();
    coverage   = covResp.ok ? await covResp.json() : {};
  } catch (err) {
    console.error("Failed to load data:", err);
    document.getElementById("cards").innerHTML =
      `<div class="empty"><div class="empty-icon">⚠️</div><h3>Could not load data</h3><p>Check the browser console for details.</p></div>`;
    return;
  }

  renderKPIs();
  renderCoverage();
  renderSituation();
  renderSidebar();
  applyFiltersAndRender();
  renderUpdatedStamp();
}

// ---------------------------------------------------------------------------
// KPI tiles
// ---------------------------------------------------------------------------

function renderKPIs() {
  const total     = allRecords.length;
  const pregnancy = allRecords.filter(r => r.pregnancy_related).length;
  const canadian  = allRecords.filter(r => r.canadian).length;
  const nitag     = allRecords.filter(r => r.channel === "who_sage" || r.channel === "nitag").length;
  const epi       = allRecords.filter(r => r.channel === "surveillance" && !isJournalPublisher(r)).length;
  const journals  = allRecords.filter(r => normType(r.source_type) === "journal_article").length;

  const tiles = [
    { label: "Total records",       value: total.toLocaleString(),     sub: "all channels",            dot: null },
    { label: "Pregnancy-related",   value: pregnancy.toLocaleString(), sub: "MMR + infection",         dot: "var(--pa-pregnancy-mmr)" },
    { label: "Canadian evidence",   value: canadian.toLocaleString(),  sub: "PHAC/NACI/provincial",    dot: "var(--canada)" },
    { label: "NITAG updates",       value: nitag.toLocaleString(),     sub: "WHO/SAGE + national",     dot: "var(--t-guideline)" },
    { label: "Epi updates",         value: epi.toLocaleString(),       sub: "PHAC/WHO/CDC/ECDC/PHO",   dot: "var(--t-surveillance)" },
    { label: "Journal publications",value: journals.toLocaleString(),  sub: "peer-reviewed",           dot: "var(--t-journal)" },
  ];

  document.getElementById("kpis").innerHTML = tiles.map(t => `
    <div class="kpi">
      <div class="label">
        ${t.dot ? `<span class="dot" style="background:${t.dot}"></span>` : ""}
        ${esc(t.label)}
      </div>
      <div class="value">${t.value}</div>
      <div class="sub">${esc(t.sub)}</div>
    </div>
  `).join("");
}

// ---------------------------------------------------------------------------
// Situation banner
// ---------------------------------------------------------------------------

// Keywords that identify trusted epi/public-health agencies
const EPI_AGENCIES = [
  "phac", "aspc", "canada communicable disease", "ccdr",
  "who.int", "world health organization", "paho",
  "cdc.gov", "centers for disease control", "mmwr",
  "ecdc.europa.eu", "european centre for disease",
  "public health ontario", "publichealthontario",
  "ukhsa", "public health england",
  "reliefweb", "promed", "healthmap",
];

// Publishers whose articles should never surface in the situation banner
const JOURNAL_PUBLISHERS = [
  "thieme", "elsevier", "springer", "wiley", "tandfonline",
  "nature.com", "sciencedirect", "nejm.org", "bmj.com",
  "jamanetwork", "thelancet", "oup.com", "sagepub", "karger",
  "cambridge.org/core", "journals.lww", "plos", "pubmed.ncbi",
  "ncbi.nlm.nih.gov",
];

function isEpiAgencySource(r) {
  const hay = [r.source, r.journal, r.authors, r.url].join(" ").toLowerCase();
  return EPI_AGENCIES.some(k => hay.includes(k));
}

function isJournalPublisher(r) {
  const url = (r.url || "").toLowerCase();
  return JOURNAL_PUBLISHERS.some(d => url.includes(d));
}

function renderSituation() {
  const slot = document.getElementById("situation-slot");

  // Strict: surveillance channel + known agency + not a journal publisher
  const latest =
    allRecords.find(r =>
      r.channel === "surveillance" && isEpiAgencySource(r) && !isJournalPublisher(r)
    ) ||
    allRecords.find(r =>
      r.channel === "surveillance" && !isJournalPublisher(r)
    ) ||
    allRecords.find(r => r.channel === "who_sage") ||
    null;

  if (!latest) { slot.innerHTML = ""; return; }

  slot.innerHTML = `
    <div class="situation" role="status" aria-live="polite">
      <span class="pulse"></span>
      <div class="body">
        <div class="tag">Latest signal · ${fmtDate(recDate(latest))}</div>
        <h3>${esc(latest.title || "(No title)")}</h3>
        ${latest.summary ? `<p>${esc(latest.summary.slice(0, 200))}…</p>` : ""}
      </div>
      ${latest.url ? `<a href="${esc(latest.url)}" target="_blank" rel="noopener" style="white-space:nowrap;font-weight:600;font-size:13px;color:var(--brand)">↗ Open</a>` : ""}
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Source coverage panel
// ---------------------------------------------------------------------------

function renderCoverage() {
  const slot = document.getElementById("coverage-slot");
  if (!coverage.channels || !coverage.channels.length) { slot.innerHTML = ""; return; }

  const total = coverage.total || allRecords.length;
  const errors = (coverage.fetcher_log || []).filter(f => f.error).length;
  const badgeClass = errors > 0 ? "warn" : "ok";
  const badgeText  = errors > 0 ? `${errors} error${errors > 1 ? "s" : ""}` : `${coverage.channels.length} sources active`;

  const items = coverage.channels.map(ch => {
    const hasCount = ch.count > 0;
    return `
      <div class="cov-item ${hasCount ? "ok" : "watch"}">
        <span class="cov-dot"></span>
        <span class="cov-name">${esc(ch.icon || "")} ${esc(ch.label)}</span>
        <span class="cov-nums">${ch.count.toLocaleString()}</span>
      </div>
    `;
  }).join("");

  slot.innerHTML = `
    <details class="coverage" open>
      <summary>
        <svg class="cov-ic" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 12h2M20 12h2M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/><circle cx="12" cy="12" r="4"/></svg>
        <span class="cov-title">Source coverage</span>
        <span class="cov-badge ${badgeClass}">${esc(badgeText)}</span>
        <span class="cov-hint">${total.toLocaleString()} records total</span>
        <span class="cov-chev">▾</span>
      </summary>
      <div class="cov-grid">${items}</div>
      <div class="cov-foot">Last refreshed: ${coverage.updated_at ? new Date(coverage.updated_at).toLocaleString("en-CA", { dateStyle: "medium", timeStyle: "short" }) : "—"}</div>
    </details>
  `;
}

// ---------------------------------------------------------------------------
// Sidebar filters (built from data)
// ---------------------------------------------------------------------------

function renderSidebar() {
  // Count helpers
  const countByPolicy = {};
  const countByChannel = {};
  const countByType = {};
  const countBySpecies = { human: 0, animal: 0, na: 0 };

  for (const r of allRecords) {
    for (const area of (r.policy_area || ["general_measles"])) {
      countByPolicy[area] = (countByPolicy[area] || 0) + 1;
    }
    const ch = r.channel || "news";
    countByChannel[ch] = (countByChannel[ch] || 0) + 1;

    const st = normType(r.source_type);
    countByType[st] = (countByType[st] || 0) + 1;

    const sp = r.species || "na";
    if (sp in countBySpecies) countBySpecies[sp]++;
  }

  // Policy area (radio)
  document.getElementById("f-policy").innerHTML = POLICY_AREAS.map(pa => `
    <label class="radio-row ${state.policy === pa.id ? "on" : ""}">
      <input type="radio" name="policy" value="${pa.id}" ${state.policy === pa.id ? "checked" : ""} />
      <span class="rbox"></span>
      ${pa.cdot ? `<span class="cdot" style="background:${pa.cdot}"></span>` : ""}
      ${esc(pa.label)}
      ${pa.id !== "all" ? `<span class="count">${(countByPolicy[pa.id] || 0).toLocaleString()}</span>` : ""}
    </label>
  `).join("");

  // Channel (checkbox)
  document.getElementById("f-channel").innerHTML = CHANNELS.map(ch => {
    const on = state.channels.has(ch.id);
    const cnt = countByChannel[ch.id] || 0;
    return `
      <label class="choice ${on ? "on" : ""}">
        <input type="checkbox" name="channel" value="${ch.id}" ${on ? "checked" : ""} />
        <span class="box"><svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M2 6.2 5 9l5-6"/></svg></span>
        <span class="cdot" style="background:${ch.cdot}"></span>
        ${esc(ch.label)}
        <span class="count">${cnt.toLocaleString()}</span>
      </label>
    `;
  }).join("");

  // Source type (checkbox — deduplicated)
  const seenTypes = new Set();
  const typeRows = [];
  for (const [st, label] of [
    ["journal_article", "Journal article"],
    ["preprint", "Preprint"],
    ["clinical_trial", "Clinical trial"],
    ["guideline", "Guideline"],
    ["surveillance", "Surveillance"],
    ["outbreak_report", "Outbreak report"],
    ["news", "News"],
    ["press_release", "Press release"],
  ]) {
    // count both "journal" and "journal_article" under the same row
    const normed = normType(st);
    if (seenTypes.has(normed)) continue;
    seenTypes.add(normed);
    const on = state.types.has(normed);
    const cnt = (countByType[normed] || 0);
    typeRows.push(`
      <label class="choice ${on ? "on" : ""}">
        <input type="checkbox" name="source_type" value="${normed}" ${on ? "checked" : ""} />
        <span class="box"><svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M2 6.2 5 9l5-6"/></svg></span>
        <span class="cdot" style="background:${ST_DOT[st] || "var(--ink-muted)"}"></span>
        ${esc(label)}
        <span class="count">${cnt.toLocaleString()}</span>
      </label>
    `);
  }
  document.getElementById("f-type").innerHTML = typeRows.join("");

  // Species (radio)
  const speciesOpts = [
    { id: "all", label: "All populations" },
    { id: "human", label: "Human studies" },
    { id: "animal", label: "Animal / preclinical" },
  ];
  document.getElementById("f-species").innerHTML = speciesOpts.map(sp => `
    <label class="radio-row ${state.species === sp.id ? "on" : ""}">
      <input type="radio" name="species" value="${sp.id}" ${state.species === sp.id ? "checked" : ""} />
      <span class="rbox"></span>
      ${esc(sp.label)}
      ${sp.id !== "all" ? `<span class="count">${(countBySpecies[sp.id] || 0).toLocaleString()}</span>` : ""}
    </label>
  `).join("");

  // Toggle states
  document.querySelectorAll(".toggle-row").forEach(row => {
    const key = row.dataset.toggle;
    const isOn = key === "pregnancy" ? state.pregnancy : key === "canadian" ? state.canadian : false;
    row.classList.toggle("on", isOn);
  });

  wireFilters();
}

// Normalise source_type to canonical key
function normType(st) {
  if (st === "journal" || st === "journal_article") return "journal_article";
  return st || "other";
}

// ---------------------------------------------------------------------------
// Wire sidebar listeners
// ---------------------------------------------------------------------------

function wireFilters() {
  // Policy radio
  document.querySelectorAll("input[name=policy]").forEach(r => {
    r.addEventListener("change", () => {
      if (r.checked) { state.policy = r.value; refreshAll(); }
    });
  });

  // Channel checkboxes
  document.querySelectorAll("input[name=channel]").forEach(cb => {
    cb.addEventListener("change", () => {
      if (cb.checked) state.channels.add(cb.value);
      else state.channels.delete(cb.value);
      refreshAll();
    });
  });

  // Source type checkboxes
  document.querySelectorAll("input[name=source_type]").forEach(cb => {
    cb.addEventListener("change", () => {
      if (cb.checked) state.types.add(cb.value);
      else state.types.delete(cb.value);
      refreshAll();
    });
  });

  // Species radio
  document.querySelectorAll("input[name=species]").forEach(r => {
    r.addEventListener("change", () => {
      if (r.checked) { state.species = r.value; refreshAll(); }
    });
  });

  // Toggle rows
  document.querySelectorAll(".toggle-row").forEach(row => {
    row.addEventListener("click", () => {
      const key = row.dataset.toggle;
      if (key === "pregnancy") state.pregnancy = !state.pregnancy;
      if (key === "canadian")  state.canadian  = !state.canadian;
      row.classList.toggle("on", key === "pregnancy" ? state.pregnancy : state.canadian);
      refreshAll();
    });
  });

  // Date presets
  document.querySelectorAll("#f-date button").forEach(btn => {
    btn.addEventListener("click", () => {
      state.days = btn.dataset.days;
      document.querySelectorAll("#f-date button").forEach(b => b.classList.toggle("on", b === btn));
      refreshAll();
    });
  });
}

// ---------------------------------------------------------------------------
// Reset
// ---------------------------------------------------------------------------

document.getElementById("clear-filters").addEventListener("click", () => {
  state.search   = "";
  state.policy   = "all";
  state.channels = new Set();
  state.types    = new Set();
  state.species  = "all";
  state.pregnancy = false;
  state.canadian  = false;
  state.days     = "all";
  document.getElementById("search-input").value = "";
  document.getElementById("sort-select").value = "date_desc";
  state.sort = "date_desc";
  document.querySelectorAll("#f-date button").forEach(b => b.classList.toggle("on", b.dataset.days === "all"));
  renderSidebar();
  refreshAll();
});

// ---------------------------------------------------------------------------
// Filter + sort
// ---------------------------------------------------------------------------

function refreshAll() {
  applyFiltersAndRender();
  updateClearBtn();
}

function hasActiveFilters() {
  return state.search || state.policy !== "all" || state.channels.size > 0 ||
    state.types.size > 0 || state.species !== "all" || state.pregnancy ||
    state.canadian || state.days !== "all";
}

function updateClearBtn() {
  document.getElementById("clear-filters").disabled = !hasActiveFilters();
  // Mobile filter count badge
  const n = [
    state.policy !== "all",
    state.channels.size > 0,
    state.types.size > 0,
    state.species !== "all",
    state.pregnancy,
    state.canadian,
    state.days !== "all",
    !!state.search,
  ].filter(Boolean).length;
  const badge = document.getElementById("mobile-fcount");
  badge.hidden = n === 0;
  badge.textContent = n;
}

function applyFiltersAndRender() {
  const q = state.search.toLowerCase().trim();

  // Compute date cutoff
  let cutoff = null;
  if (state.days !== "all") {
    const d = new Date();
    d.setDate(d.getDate() - Number(state.days));
    cutoff = d.toISOString().slice(0, 10);
  }

  filtered = allRecords.filter(r => {
    // Text
    if (q) {
      const hay = [r.title, r.summary, r.authors, r.journal, r.source].join(" ").toLowerCase();
      if (!q.split(/\s+/).every(w => hay.includes(w))) return false;
    }

    // Policy (radio — single select)
    if (state.policy !== "all") {
      if (!Array.isArray(r.policy_area) || !r.policy_area.includes(state.policy)) return false;
    }

    // Channel (multi — any match)
    if (state.channels.size > 0 && !state.channels.has(r.channel)) return false;

    // Source type (multi — match normalised)
    if (state.types.size > 0 && !state.types.has(normType(r.source_type))) return false;

    // Species
    if (state.species !== "all" && r.species !== state.species) return false;

    // Toggles
    if (state.pregnancy && !r.pregnancy_related) return false;
    if (state.canadian && !r.canadian) return false;

    // Date cutoff
    if (cutoff) {
      const d = recDate(r);
      if (!d || d < cutoff) return false;
    }

    return true;
  });

  filtered = sortRecords(filtered, state.sort);
  currentPage = 1;
  renderPage();
  renderTimeline();
  renderCountLine();
  updateClearBtn();
}

function sortRecords(records, sort) {
  const copy = [...records];
  if (sort === "date_desc") return copy.sort((a, b) => (recDate(b) || "").localeCompare(recDate(a) || ""));
  if (sort === "date_asc")  return copy.sort((a, b) => (recDate(a) || "").localeCompare(recDate(b) || ""));
  if (sort === "altmetric") return copy.sort((a, b) => (b.altmetric_score || 0) - (a.altmetric_score || 0));
  if (sort === "title")     return copy.sort((a, b) => (a.title || "").localeCompare(b.title || ""));
  return copy;
}

// ---------------------------------------------------------------------------
// Timeline chart
// ---------------------------------------------------------------------------

function renderTimeline() {
  const el = document.getElementById("timeline");
  if (filtered.length === 0) { el.hidden = true; return; }

  // Bucket by month (last 24 months)
  const months = {};
  const now = new Date();
  for (let i = 23; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    months[key] = 0;
  }
  for (const r of filtered) {
    const d = recDate(r);
    if (d && d.length >= 7) {
      const key = d.slice(0, 7);
      if (key in months) months[key]++;
    }
  }
  const entries = Object.entries(months);
  const max = Math.max(...entries.map(e => e[1]), 1);

  document.getElementById("tl-chart").innerHTML = entries.map(([mon, cnt]) => {
    const h = Math.round((cnt / max) * 100);
    return `<div class="tl-bar" style="height:${Math.max(h, cnt > 0 ? 4 : 2)}%" title="${mon}: ${cnt}">
      <span class="tip">${mon}<br>${cnt} record${cnt !== 1 ? "s" : ""}</span>
    </div>`;
  }).join("");

  // Axis labels (every 6 months)
  const axisKeys = entries.filter((_, i) => i % 6 === 0 || i === entries.length - 1).map(e => e[0]);
  document.getElementById("tl-axis").innerHTML = `
    <span>${axisKeys[0]}</span>
    ${axisKeys.slice(1, -1).map(k => `<span>${k}</span>`).join("")}
    <span>${axisKeys[axisKeys.length - 1]}</span>
  `;
  document.getElementById("tl-note").textContent = `${filtered.length} records`;
  el.hidden = false;
}

// ---------------------------------------------------------------------------
// Count line
// ---------------------------------------------------------------------------

function renderCountLine() {
  const el = document.getElementById("count-line");
  if (filtered.length === allRecords.length) {
    el.innerHTML = `<b>${allRecords.length.toLocaleString()}</b> records`;
  } else {
    el.innerHTML = `<b>${filtered.length.toLocaleString()}</b> of ${allRecords.length.toLocaleString()} records`;
  }
}

// ---------------------------------------------------------------------------
// Render cards
// ---------------------------------------------------------------------------

function renderPage() {
  const container = document.getElementById("cards");
  const start = (currentPage - 1) * PAGE_SIZE;
  const page  = filtered.slice(start, start + PAGE_SIZE);

  if (page.length === 0) {
    container.innerHTML = `
      <div class="empty">
        <div class="empty-icon">🔍</div>
        <h3>No records found</h3>
        <p>Try adjusting your filters.</p>
      </div>`;
  } else {
    container.innerHTML = page.map(renderCard).join("");
    // Expand / collapse
    container.querySelectorAll(".card .more").forEach(btn => {
      btn.addEventListener("click", () => {
        const card = btn.closest(".card");
        const expanded = card.classList.toggle("expanded");
        btn.textContent = expanded ? "Show less" : "Show more";
      });
    });
  }
  renderPagination();
}

function renderCard(rec) {
  const date   = recDate(rec);
  const stNorm = normType(rec.source_type);
  const stLabel = rec.source_type_label || ST_LABEL[stNorm] || ST_LABEL[rec.source_type] || "Other";
  const stDot  = ST_DOT[stNorm] || ST_DOT[rec.source_type] || "var(--ink-muted)";

  // Policy area badges (first two)
  const areas = rec.policy_area || ["general_measles"];
  const areaBadges = areas.slice(0, 2).map(a => {
    const cls = `pa-${a.replace(/_/g, "-")}`;
    const lbl = POLICY_AREAS.find(p => p.id === a)?.label || a;
    return `<span class="badge ${cls}">${esc(lbl)}</span>`;
  }).join("");

  // Flag badges
  const flags = [
    rec.reviewed   ? `<span class="badge flag-reviewed">✓ Reviewed</span>` : "",
    rec.canadian   ? `<span class="badge flag-canada">🍁 Canada</span>`    : "",
    rec.pharma     ? `<span class="badge flag-pharma">Industry</span>`       : "",
    rec.species === "animal" ? `<span class="badge flag-animal">Animal</span>` : "",
    rec.pregnancy_related ? `<span class="badge flag-pregnancy">🤰 Pregnancy</span>` : "",
  ].filter(Boolean).join("");

  const metaParts = [
    rec.authors ? `<span class="src">${esc(rec.authors.split(",").slice(0, 2).join(", ") + (rec.authors.includes(",") ? " et al." : ""))}</span>` : "",
    rec.journal ? `<span class="jrnl">${esc(rec.journal)}</span>` : "",
    rec.source  && !rec.journal ? `<span>${esc(rec.source)}</span>` : "",
  ].filter(Boolean).join("<span style='color:var(--hairline-strong)'> · </span>");

  const altmetric = rec.altmetric_score
    ? (() => {
        const p = Math.min(Math.round(rec.altmetric_score / 200 * 100), 100);
        return `<div class="altmetric" title="Altmetric score: ${Math.round(rec.altmetric_score)}">
          <div class="donut" style="--p:${p}"><span>${Math.round(rec.altmetric_score)}</span></div>
          <div class="amlabel"><b>Altmetric</b>attention score</div>
        </div>`;
      })()
    : "";

  return `
    <article class="card" role="listitem">
      <div class="row1">
        <span class="badge type"><span class="bdot" style="background:${stDot}"></span>${esc(stLabel)}</span>
        ${areaBadges}
        ${flags}
        <span class="spacer-x"></span>
        <span class="date">${fmtDate(date)}</span>
      </div>
      <h3 class="title">
        ${rec.url
          ? `<a href="${esc(rec.url)}" target="_blank" rel="noopener">${esc(rec.title || "(No title)")}</a>`
          : esc(rec.title || "(No title)")
        }
      </h3>
      ${metaParts ? `<div class="meta">${metaParts}</div>` : ""}
      ${rec.summary ? `<p class="summary">${esc(rec.summary)}</p>` : ""}
      <div class="foot">
        ${rec.summary && rec.summary.length > 280 ? `<button class="more" type="button">Show more</button>` : ""}
        ${altmetric}
        ${rec.url
          ? `<a class="link-out" href="${esc(rec.url)}" target="_blank" rel="noopener">
               Open ↗
             </a>`
          : ""
        }
      </div>
    </article>`;
}

// ---------------------------------------------------------------------------
// Pagination
// ---------------------------------------------------------------------------

function renderPagination() {
  const total = Math.ceil(filtered.length / PAGE_SIZE);
  const pg    = document.getElementById("pagination");
  if (total <= 1) { pg.innerHTML = ""; return; }

  const maxV = 7, half = Math.floor(maxV / 2);
  let start = Math.max(1, currentPage - half);
  let end   = Math.min(total, start + maxV - 1);
  if (end - start < maxV - 1) start = Math.max(1, end - maxV + 1);

  let nums = "";
  if (start > 1) nums += `<button class="page-btn" data-page="1">1</button><span>…</span>`;
  for (let p = start; p <= end; p++) {
    nums += `<button class="page-btn ${p === currentPage ? "active" : ""}" data-page="${p}">${p}</button>`;
  }
  if (end < total) nums += `<span>…</span><button class="page-btn" data-page="${total}">${total}</button>`;

  pg.innerHTML = `
    <button class="page-btn" id="page-prev" ${currentPage === 1 ? "disabled" : ""}>‹ Prev</button>
    ${nums}
    <button class="page-btn" id="page-next" ${currentPage === total ? "disabled" : ""}>Next ›</button>
  `;
  pg.querySelector("#page-prev")?.addEventListener("click", () => goPage(currentPage - 1));
  pg.querySelector("#page-next")?.addEventListener("click", () => goPage(currentPage + 1));
  pg.querySelectorAll("[data-page]").forEach(btn =>
    btn.addEventListener("click", () => goPage(Number(btn.dataset.page)))
  );
}

function goPage(p) {
  const total = Math.ceil(filtered.length / PAGE_SIZE);
  currentPage = Math.max(1, Math.min(p, total));
  renderPage();
  document.getElementById("main-content").scrollIntoView({ behavior: "smooth" });
}

// ---------------------------------------------------------------------------
// Updated stamp
// ---------------------------------------------------------------------------

function renderUpdatedStamp() {
  const el = document.getElementById("updated-stamp");
  if (!coverage.updated_at) return;
  const d = new Date(coverage.updated_at);
  el.innerHTML = `Updated<br><b>${d.toLocaleDateString("en-CA", { month: "short", day: "numeric", year: "numeric" })}</b>`;
}

// ---------------------------------------------------------------------------
// Mobile sidebar
// ---------------------------------------------------------------------------

document.getElementById("open-filters")?.addEventListener("click", () => {
  document.getElementById("filters").classList.add("open");
  document.getElementById("filters-backdrop").classList.add("open");
  document.getElementById("open-filters").setAttribute("aria-expanded", "true");
});
document.getElementById("close-filters")?.addEventListener("click", closeSidebar);
document.getElementById("filters-backdrop")?.addEventListener("click", closeSidebar);
function closeSidebar() {
  document.getElementById("filters").classList.remove("open");
  document.getElementById("filters-backdrop").classList.remove("open");
  document.getElementById("open-filters")?.setAttribute("aria-expanded", "false");
}

// ---------------------------------------------------------------------------
// Sort select
// ---------------------------------------------------------------------------

document.getElementById("sort-select").addEventListener("change", e => {
  state.sort = e.target.value;
  applyFiltersAndRender();
});

// ---------------------------------------------------------------------------
// Search (debounced)
// ---------------------------------------------------------------------------

let searchTimer;
document.getElementById("search-input").addEventListener("input", e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = e.target.value;
    applyFiltersAndRender();
  }, 280);
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

loadData();
