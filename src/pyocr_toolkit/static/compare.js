/* Document-level comparison of two sources ("diff studio"):
 * headline metrics, a clickable per-page CER chart and an aligned side-by-side
 * diff of the selected page. */
"use strict";

const cRoot = $("#compare-root");
const DOC_ID = cRoot.dataset.docId;
const cmp = { a: null, b: null, pages: [], page: null };

const optLabel = (sel) => sel.selectedOptions[0]?.textContent.replace(/\s*\(\d+\/\d+\)$/, "") || "";
const textRef = (v) => (v.startsWith("llm:") ? { source: "llm", key: v.slice(4) }
  : v.startsWith("ocr:") ? { source: "ocr", engine: v.slice(4) }
  : v.startsWith("native:") ? { source: "native", library: v.slice(7) } : { source: v });

function pickDefaults() {
  const p = new URLSearchParams(location.search);
  const a = $("#src-a"), b = $("#src-b");
  const enabled = (sel) => [...sel.options].filter((o) => !o.disabled).map((o) => o.value);
  const has = (sel, v) => enabled(sel).includes(v);
  const llm = enabled(a).find((v) => v.startsWith("llm:"));
  // text a person corrected is ground truth: the natural reference whenever it exists
  a.value = has(a, p.get("a")) ? p.get("a") : has(a, "corrected") ? "corrected" : (llm || "native");
  const ocrIds = enabled(b).filter((v) => v === "ocr" || v.startsWith("ocr:"));
  const fallback = [...ocrIds, "native", ...enabled(b)].find((v) => has(b, v) && v !== a.value);
  b.value = has(b, p.get("b")) && p.get("b") !== a.value ? p.get("b") : (fallback || a.value);
}

function stat(k, v, cls = "", extra = "") {
  return `<div class="stat-tile"><div class="k">${k}</div><div class="v ${cls}">${v}</div>${extra}</div>`;
}

async function run() {
  const a = $("#src-a").value, b = $("#src-b").value;
  if (a === b) { toast("Pick two different sources.", true); return; }
  Object.assign(cmp, { a, b });
  const qs = new URLSearchParams({ a, b, ignore_case: $("#opt-case").checked, ignore_punctuation: $("#opt-punct").checked });
  history.replaceState(null, "", `?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}${cmp.page != null ? `#p${cmp.page + 1}` : ""}`);
  const res = await api(`/api/documents/${DOC_ID}/compare?${qs}`);
  cmp.pages = res.pages;
  const o = res.overall;
  const scored = res.pages.filter((r) => r.metrics);

  $("#cmp-overall").innerHTML = o
    ? stat("Character error", pct(o.cer), errorRateClass(o.cer),
        `<div class="small muted">${o.pages_under_2pct} of ${o.pages_scored} pages under 2%</div>`) +
      stat("Word error", pct(o.wer), errorRateClass(o.wer),
        `<div class="small muted" title="WER with the words compared as a bag, so reading order doesn't count">${pct(o.wer_order_free)} in any order</div>`) +
      stat("Word errors", errorTriple(o.errors), "", stackBar(o.errors)) +
      stat("Numbers read exactly", numbersValue(o.numbers), numbersClass(o.numbers),
        '<div class="small muted">amounts, dates, IDs</div>') +
      stat("Worst page", `<button class="link" data-goto="${o.worst_page}">p${o.worst_page + 1}</button>
        <small>${pct(scored.find((r) => r.page === o.worst_page).metrics.cer)}</small>`, "bad")
    : '<div class="callout warn" style="grid-column:1/-1">No page has text from both sources yet. Extract them first on the Overview page.</div>';

  // Bar heights use a square-root scale so a single very bad page doesn't
  // flatten every other bar; colours follow errorRateClass (blue/amber/red).
  const max = Math.max(0.001, ...scored.map((r) => Math.min(1, r.metrics.cer)));
  $("#cmp-bars").innerHTML = res.pages.map((r) => {
    if (!r.metrics) return `<span class="bar missing" title="Page ${r.page + 1}: not scored"></span>`;
    const h = Math.max(4, Math.sqrt(Math.min(1, r.metrics.cer) / max) * 100);
    return `<button class="bar ${errorRateClass(r.metrics.cer)}" data-goto="${r.page}" style="height:${h}%"
      title="Page ${r.page + 1}: CER ${pct(r.metrics.cer)}" aria-label="Page ${r.page + 1}: CER ${pct(r.metrics.cer)}"></button>`;
  }).join("");
  $("#cmp-chart").hidden = !scored.length;
  $("#cmp-chart .chart-note")?.remove();
  if (scored.length && scored.every((r) => r.metrics.cer === 0)) {
    $("#cmp-bars").insertAdjacentHTML("beforebegin", '<p class="chart-note">Every scored page matches exactly.</p>');
  }

  $("#cmp-table tbody").innerHTML = res.pages.map((r) => r.metrics ? `
    <tr data-goto="${r.page}" class="clickable">
      <td><b>${r.page + 1}</b></td>
      <td><span class="v ${errorRateClass(r.metrics.cer)}">${pct(r.metrics.cer)}</span></td>
      <td>${pct(r.metrics.wer)}</td><td>${pct(r.metrics.wer_order_free)}</td>
      <td>${fmtInt(r.errors.misread)}</td><td>${fmtInt(r.errors.missed)}</td><td>${fmtInt(r.errors.added)}</td>
      <td><span class="v ${numbersClass(r.numbers)}">${r.numbers.total ? `${r.numbers.correct}/${r.numbers.total}` : "–"}</span></td>
      <td>${fmtInt(r.metrics.reference_chars)}</td><td>${fmtInt(r.metrics.hypothesis_chars)}</td>
      <td class="actions"><a class="btn small ghost" href="/documents/${DOC_ID}/pages/${r.page + 1}#compare">Inspect</a></td>
    </tr>` : `
    <tr class="muted"><td>${r.page + 1}</td><td colspan="9">not scored: no text from
      ${r.missing.map((m) => (m === a ? "reference" : "candidate")).join(" or ")}</td>
      <td class="actions"><a class="btn small ghost" href="/documents/${DOC_ID}/pages/${r.page + 1}">Open</a></td></tr>`).join("");
  $("#cmp-table-wrap").hidden = false;

  if (!scored.length) { $("#cmp-page").hidden = true; return; }
  const fromHash = +(location.hash.match(/^#p(\d+)$/)?.[1] || 0) - 1;
  const keep = [cmp.page, fromHash].find((n) => scored.some((r) => r.page === n));
  await showPage(keep ?? o.worst_page);
}

// ---- error breakdown (misread / missed / added, kinds, numbers, CER ladder)
const KIND_LABELS = { number: "Numbers", word: "Misread words", accents: "Accents / ß", case: "Case",
  punctuation: "Punctuation", spacing: "Split / joined", missed: "Missed", added: "Added", noise: "Stray marks" };

const errorTriple = (e) => `${fmtInt(e.misread)} <small>·</small> ${fmtInt(e.missed)} <small>·</small> ${fmtInt(e.added)}`;
const numbersValue = (n) => (n.total ? `${fmtInt(n.correct)} <small>of ${fmtInt(n.total)}</small>` : '<small>no numbers</small>');
const numbersClass = (n) => (!n.total ? "" : n.correct === n.total ? "good" : n.correct / n.total >= 0.95 ? "warn" : "bad");

/** A bar split into misread / missed / added words, with a legend. */
function stackBar(e) {
  const total = e.misread + e.missed + e.added;
  const seg = (k) => (total && e[k] ? `<i class="${k}" style="width:${(e[k] / total) * 100}%"></i>` : "");
  return `<div class="stackbar" aria-hidden="true">${seg("misread")}${seg("missed")}${seg("added")}</div>
    <div class="stack-legend"><span><i class="misread"></i>misread</span><span><i class="missed"></i>missed</span><span><i class="added"></i>added</span></div>`;
}

/** How much of the word error is reading order alone: WER minus order-free WER. */
function orderHtml(m) {
  const cost = Math.max(0, m.wer - m.wer_order_free);
  const line = `<div class="err-k">Word order <span class="muted">WER ${pct(m.wer)} · ${pct(m.wer_order_free)} ignoring word order</span></div>`;
  if (cost >= 0.05 && cost >= 0.3 * m.wer) {
    return `${line}<div class="callout warn order-warn"><span><b>${(cost * 100).toFixed(1)} points of the word error come from reading
      order alone.</b> B has these words but in a different order: columns, tables or side notes were probably read in a
      different sequence. Only ${pct(m.wer_order_free)} is misread, missing or extra words. The word errors, numbers and
      CER above count moved words as errors too, so read them with that in mind.</span></div>`;
  }
  return `${line}<span class="small muted">${cost < 0.005 ? "Same reading order: the word errors are misread, missing or extra words."
    : `${(cost * 100).toFixed(1)} points come from words in a different order.`}</span>`;
}

function renderErrors(res) {
  const e = res.errors, n = res.numbers;
  const examples = (kind) => e.examples.filter((x) => x.kind === kind).slice(0, 8)
    .map((x) => `${x.ref || "∅"} → ${x.hyp || "∅"}`).join("\n");
  const kinds = Object.entries(e.kinds).sort((a, b) => b[1] - a[1]).map(([k, v]) =>
    `<span class="kind-chip ${k}" title="${esc(examples(k))}">${KIND_LABELS[k] || esc(k)} <b>${fmtInt(v)}</b></span>`).join("");
  const conf = e.confusions.slice(0, 6).map((c) =>
    `<span class="num-chip">${esc(c.ref || "∅")}→${esc(c.hyp || "∅")} <small>×${c.count}</small></span>`).join("");
  const wrong = n.wrong.slice(0, 6).map((w) =>
    `<span class="num-chip bad">${esc(w.ref)} → ${w.hyp ? esc(w.hyp) : "<i>missing</i>"}</span>`).join("");
  const more = n.total - n.correct - Math.min(6, n.wrong.length);
  const ladder = res.ladder.map((s, i) => `${i ? '<span class="ladder-sep" aria-hidden="true">›</span>' : ""}
    <span class="ladder-step"><span class="k">${esc(s.label)}</span><b class="${errorRateClass(s.cer)}">${pct(s.cer)}</b></span>`).join("");
  $("#pg-errors").innerHTML = `
    <div class="err-block">
      <div class="err-k">Word errors <span class="muted">${errorTriple(e)}</span></div>
      ${stackBar(e)}
      <div class="kind-chips">${kinds || '<span class="small muted">none</span>'}</div>
      ${conf ? `<div class="err-sub"><span class="small muted">Most common confusions</span><div class="kind-chips">${conf}</div></div>` : ""}
    </div>
    <div class="err-block">
      <div class="err-k">Numbers read exactly <b class="${numbersClass(n)}">${n.total ? `${n.correct} of ${n.total}` : "none on this page"}</b></div>
      ${wrong ? `<div class="kind-chips">${wrong}${more > 0 ? `<span class="small muted">+${more} more</span>` : ""}</div>`
        : n.total ? '<span class="small muted">Every amount, date and ID matches.</span>' : ""}
      ${n.present > n.correct ? `<span class="small muted">${n.present} of ${n.total} are in B, ${n.present - n.correct} of them at another position (word order).</span>` : ""}
      ${n.extra ? `<span class="small muted">${n.extra} number${n.extra === 1 ? "" : "s"} only in B</span>` : ""}
    </div>
    <div class="err-block wide">${orderHtml(res.metrics)}</div>
    <div class="err-block wide">
      <div class="err-k">CER as differences are forgiven <span class="muted small">each step adds to the one before; what's left is real misreading</span></div>
      <div class="ladder">${ladder}</div>
    </div>`;
}

/** Two aligned columns from the word-diff segments: the left column marks
 *  what only the reference has, the right what only the candidate has. */
function sideBySide(segments) {
  const lead = (t) => t.match(/^\n*/)[0];
  const mark = (t, cls) => (t ? `${lead(t)}<span class="${cls}">${esc(t.slice(lead(t).length))}</span>` : "");
  const left = [], right = [];
  for (const s of segments) {
    if (s.op === "equal") { left.push(esc(s.ref)); right.push(esc(s.hyp || s.ref)); continue; }
    if (s.op === "delete" || s.op === "replace") left.push(mark(s.ref, "del-mark"));
    if (s.op === "insert" || s.op === "replace") right.push(mark(s.hyp, "ins-mark"));
  }
  return [left.filter(Boolean).join(" "), right.filter(Boolean).join(" ")];
}

async function showPage(n) {
  const row = cmp.pages.find((r) => r.page === n);
  if (!row?.metrics) return;
  cmp.page = n;
  history.replaceState(null, "", `${location.search}#p${n + 1}`);
  $$("#cmp-bars .bar").forEach((b) => b.classList.toggle("selected", +b.dataset.goto === n));
  const res = await postJSON(`/api/documents/${DOC_ID}/pages/${n}/compare`, {
    reference: textRef(cmp.a), hypothesis: textRef(cmp.b),
    ignore_case: $("#opt-case").checked, ignore_punctuation: $("#opt-punct").checked,
  });
  const [l, r] = sideBySide(res.diff);
  renderErrors(res);
  $("#pg-title").textContent = `Page ${n + 1}`;
  const pill = $("#pg-pill");
  pill.className = `badge ${{ good: "info", warn: "scan", bad: "bad" }[errorRateClass(row.metrics.cer)] || ""}`;
  pill.textContent = `CER ${pct(row.metrics.cer)}`;
  $("#pg-wer").textContent = `WER ${pct(row.metrics.wer)} · ${pct(row.metrics.wer_order_free)} in any order`;
  $("#pg-open").href = `/documents/${DOC_ID}/pages/${n + 1}#compare`;
  $("#pg-a-label").textContent = optLabel($("#src-a"));
  $("#pg-b-label").textContent = optLabel($("#src-b"));
  $("#pg-a").innerHTML = l;
  $("#pg-b").innerHTML = r;
  const scored = cmp.pages.filter((p) => p.metrics).map((p) => p.page);
  $("#pg-prev").disabled = scored[0] === n;
  $("#pg-next").disabled = scored[scored.length - 1] === n;
  $("#cmp-page").hidden = false;
}

function step(dir) {
  const scored = cmp.pages.filter((p) => p.metrics).map((p) => p.page);
  const i = scored.indexOf(cmp.page) + dir;
  if (i >= 0 && i < scored.length) return showPage(scored[i]);
}

// keep the two diff columns scrolled together
let syncing = false;
for (const [from, to] of [["#pg-a", "#pg-b"], ["#pg-b", "#pg-a"]]) {
  $(from).addEventListener("scroll", () => {
    if (syncing) { syncing = false; return; }
    const f = $(from), t = $(to);
    syncing = true;
    t.scrollTop = (f.scrollTop / Math.max(1, f.scrollHeight - f.clientHeight)) * (t.scrollHeight - t.clientHeight);
  });
}

cRoot.addEventListener("click", guard(async (e) => {
  const el = e.target.closest("[data-goto]");
  if (!el || e.target.closest("a")) return;
  await showPage(+el.dataset.goto);
  if (el.closest("#cmp-table, #cmp-overall")) $("#cmp-page").scrollIntoView({ behavior: "smooth", block: "start" });
}));
$("#pg-prev").addEventListener("click", guard(() => step(-1)));
$("#pg-next").addEventListener("click", guard(() => step(1)));
$("#run").addEventListener("click", guard(run));
$("#swap").addEventListener("click", () => {
  const a = $("#src-a").value;
  $("#src-a").value = $("#src-b").value;
  $("#src-b").value = a;
  if (cmp.a) guard(run)();
});

// ==================================================================== all sources
// Several sources voted word by word into a consensus (compare.consensus on the
// server); each source is scored against it. No source is assumed to be right.
const PALETTE = ["#2563eb", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#64748b", "#dc2626"];
const MAX_SOURCES = 8;
const all = { ids: [], doc: null, page: null, pageRes: null };
// Review: the column picked, and the readings a person chose (column index -> text; "" = no word)
const rv = { on: false, sel: null, choices: {} };
const chipIds = $$("#all-sources input").map((i) => i.value);
const srcColor = (id) => PALETTE[Math.max(0, chipIds.indexOf(id)) % PALETTE.length];
const srcLabel = (id) => all.doc?.labels[id] || all.pageRes?.labels[id] || id;
const dot = (id) => `<i class="src-dot" style="background:${srcColor(id)}"></i>`;
const optsQS = () => ({ ignore_case: $("#opt-case").checked, ignore_punctuation: $("#opt-punct").checked });
const pickedIds = () => $$("#all-sources input:checked").map((i) => i.value);
const currentMode = () => $('input[name="cmp-mode"]:checked').value;
$$("#all-sources .src-dot").forEach((d) => (d.style.background = srcColor(d.dataset.src)));

function pickAllDefaults() {
  const fromUrl = (new URLSearchParams(location.search).get("s") || "").split(",").filter((v) => chipIds.includes(v));
  // One vote per independent reading: the PyMuPDF text layer, every OCR engine and every LLM run.
  // Extra PDF libraries read the same text layer, so they are left out unless picked.
  const inputs = $$("#all-sources input");
  // Corrected text is the answer, not a reading, so it doesn't vote either.
  const defaults = inputs.filter((i) => !("library" in i.dataset) && i.dataset.kind !== "corrected")
    .map((i) => i.value).slice(0, MAX_SOURCES);
  const want = new Set(fromUrl.length ? fromUrl : defaults);
  inputs.forEach((i) => (i.checked = want.has(i.value)));
  updatePickHint();
}

function updatePickHint() {
  const n = pickedIds().length;
  $("#all-pick-hint").textContent = n > MAX_SOURCES ? `${n} picked: at most ${MAX_SOURCES}`
    : n < 2 ? "Pick at least two."
    : n === 2 ? "2 picked: two sources can only show where they differ, not who is right. Pick a third to vote."
    : `${n} picked`;
  $("#all-run").disabled = n < 2 || n > MAX_SOURCES;
}

function setMode(mode, go = true) {
  $(`input[name="cmp-mode"][value="${mode}"]`).checked = true;
  $("#pair-view").hidden = mode !== "pair";
  $("#all-view").hidden = mode !== "all";
  $$("[data-mode-only]").forEach((el) => (el.hidden = el.dataset.modeOnly !== mode));
  if (!go) return;
  if (mode === "all" && !all.doc && pickedIds().length >= 2) guard(runAll)();
  if (mode === "pair") {
    if (cmp.a) history.replaceState(null, "", `?a=${encodeURIComponent(cmp.a)}&b=${encodeURIComponent(cmp.b)}`);
    else if ($("#src-a").value !== $("#src-b").value) guard(run)();
  }
}

async function runAll() {
  const ids = pickedIds();
  if (ids.length < 2 || ids.length > MAX_SOURCES) { toast(`Pick between 2 and ${MAX_SOURCES} sources.`, true); return; }
  all.ids = ids;
  history.replaceState(null, "", `?mode=all&s=${encodeURIComponent(ids.join(","))}`);
  const res = await api(`/api/documents/${DOC_ID}/consensus?${new URLSearchParams({ sources: ids.join(","), ...optsQS() })}`);
  all.doc = res;
  renderAllOverall(res);
  renderCards(res);
  renderHeat(res);
  renderMatrix(res);
  const scored = res.pages.filter((p) => p.scored);
  if (!scored.length) { $("#all-page").hidden = true; return; }
  const fromHash = +(location.hash.match(/^#p(\d+)$/)?.[1] || 0) - 1;
  const keep = [all.page, fromHash].find((n) => scored.some((r) => r.page === n));
  // open on the page with the most close calls, then the most disagreement
  const busiest = scored.reduce((a, b) =>
    (b.counts.tie * 1e6 + b.counts.majority > a.counts.tie * 1e6 + a.counts.majority ? b : a)).page;
  await showConsPage(keep ?? busiest);
}

function renderAllOverall(res) {
  const el = $("#all-overall");
  el.hidden = false;
  if (!res.pages_scored) {
    el.innerHTML = '<div class="callout warn">No page has text from two or more of these sources yet. Extract them first on the Overview page.</div>';
    return;
  }
  const c = res.counts;
  const twoOnly = res.pages.filter((p) => p.scored && res.sources.length - p.missing.length === 2).length;
  el.innerHTML = `
    <div class="cons-stat"><span class="k">Consensus</span><b>${fmtInt(c.words)}</b><span class="small muted">words on ${plural(res.pages_scored, "page")}</span></div>
    <div class="cons-stat"><b>${fmtInt(c.agree)}</b><span class="small muted">words every source agrees on</span></div>
    <div class="cons-stat"><b>${fmtInt(c.majority)}</b><span class="small muted">differences settled by majority</span></div>
    <div class="cons-stat"><b class="${c.tie ? "warn" : ""}">${fmtInt(c.tie)}</b><span class="small muted">close calls: no majority</span></div>` +
    (twoOnly ? `<div class="callout warn"><span>${plural(twoOnly, "page")} had text from only two of these sources. Two
      readings can't outvote each other: every difference there is a tie, settled by whichever source gave the word
      slots. Add a third source on those pages for a real vote.</span></div>` : "");
}

function renderCards(res) {
  const rows = res.sources.map((id) => ({ id, ...res.overall[id] })).filter((r) => r.pages);
  rows.sort((a, b) => a.cer - b.cer);
  $("#all-cards").innerHTML = rows.map((r, i) => `
    <article class="cons-card">
      <div class="cons-card-head">${dot(r.id)}<b title="${esc(srcLabel(r.id))}">${esc(srcLabel(r.id))}</b></div>
      <div class="cons-card-v"><span class="v ${errorRateClass(r.cer)}">${pct(r.cer)}</span>
        <span class="small muted" title="${pct(r.wer_order_free)} ignoring word order">CER · WER ${pct(r.wer)}</span>
        ${i === 0 && rows.length > 2 ? '<span class="badge ok">closest to consensus</span>' : ""}</div>
      <div>${stackBar(r.errors)}</div>
      <div class="cons-card-foot"><span>${errorTriple(r.errors)} <span class="muted">words</span></span>
        <span class="${numbersClass(r.numbers)}" title="numbers read exactly">${r.numbers.total ? `${r.numbers.correct}/${r.numbers.total} numbers` : "no numbers"}</span></div>
      <div class="small muted">voted on ${r.pages} of ${res.pages_scored} scored ${r.pages === 1 ? "page" : "pages"}</div>
    </article>`).join("");
}

function renderHeat(res) {
  const pages = res.pages;
  const head = `<thead><tr><th></th>${pages.map((p) => `<th scope="col">${p.page + 1}</th>`).join("")}</tr></thead>`;
  const body = res.sources.map((id) => `<tr><th scope="row" title="${esc(srcLabel(id))}">${dot(id)}<span>${esc(srcLabel(id))}</span></th>${
    pages.map((p) => {
      const cer = p.scored ? p.cer[id] : undefined;
      if (cer === undefined) return `<td><span class="cell missing" title="Page ${p.page + 1}: no text"></span></td>`;
      return `<td><button class="cell ${errorRateClass(cer)}" data-cgoto="${p.page}"
        title="Page ${p.page + 1}: ${esc(srcLabel(id))}, CER ${pct(cer)}" aria-label="Page ${p.page + 1}, ${esc(srcLabel(id))}: ${pct(cer)}"></button></td>`;
    }).join("")}</tr>`).join("");
  $("#all-heat").innerHTML = head + `<tbody>${body}</tbody>`;
  $("#all-heat-card").hidden = !res.pages_scored;
}

function renderMatrix(res) {
  const ids = res.sources.filter((id) => res.overall[id].pages);
  const head = `<thead><tr><th></th>${ids.map((id) => `<th scope="col" title="${esc(srcLabel(id))}">${dot(id)}</th>`).join("")}</tr></thead>`;
  const body = ids.map((a) => `<tr><th scope="row" title="${esc(srcLabel(a))}">${dot(a)}<span>${esc(srcLabel(a))}</span></th>${
    ids.map((b) => {
      const v = res.matrix[a][b];
      if (a === b) return '<td class="diag">·</td>';
      return v == null ? '<td class="muted">–</td>'
        : `<td class="m ${errorRateClass(v)}" title="${esc(srcLabel(b))} scored against ${esc(srcLabel(a))}">${pct(v)}</td>`;
    }).join("")}</tr>`).join("");
  $("#all-matrix").innerHTML = head + `<tbody>${body}</tbody>`;
  $("#all-matrix-card").hidden = ids.length < 2;
}

async function showConsPage(n) {
  const res = await api(`/api/documents/${DOC_ID}/pages/${n}/consensus?${new URLSearchParams({ sources: all.ids.join(","), ...optsQS() })}`);
  if (all.page !== n || all.pageRes?.sources.join() !== res.sources.join()) Object.assign(rv, { sel: null, choices: {} });
  all.page = n;
  all.pageRes = res;
  history.replaceState(null, "", `${location.search}#p${n + 1}`);
  $("#cp-title").textContent = `Page ${n + 1}`;
  const c = res.counts;
  $("#cp-counts").innerHTML = `${c.tie ? `<b class="warn">${plural(c.tie, "close call")}</b> · ` : ""}${plural(c.majority, "difference")} settled by majority` +
    (res.missing.length ? ` · no text here from ${res.missing.map((id) => esc(srcLabel(id))).join(", ")}` : "");
  $("#cp-legend").innerHTML = res.sources.map((id) => {
    const sc = res.per_source[id].score;
    return `<span class="cons-legend-item" title="${esc(srcLabel(id))}${id === res.pivot ? " (its words gave the slots)" : ""}">
      ${dot(id)}<span class="lbl">${esc(srcLabel(id))}</span><b class="${errorRateClass(sc.cer)}">${pct(sc.cer)}</b></span>`;
  }).join("");
  const warn = $("#cp-warn");
  warn.hidden = res.sources.length > 2;
  if (!warn.hidden) {
    warn.innerHTML = `<span>Only two sources have text on this page, so there is no majority: wherever they differ, the
      consensus follows <b>${esc(srcLabel(res.pivot))}</b>, and its score here says nothing. Add a third source for a real vote.</span>`;
  }
  $("#cp-open").href = `/documents/${DOC_ID}/pages/${n + 1}`;
  renderConsText();
  renderReview();
  $$("#all-heat [data-cgoto]").forEach((b) => b.classList.toggle("selected", +b.dataset.cgoto === n));
  const scored = all.doc.pages.filter((p) => p.scored).map((p) => p.page);
  $("#cp-prev").disabled = scored[0] === n;
  $("#cp-next").disabled = scored[scored.length - 1] === n;
  $("#all-page").hidden = false;
}

/** The consensus as text, laid out in the spine's lines. Agreed words are plain;
 *  a disputed word shows the winner with its vote count and, under it, what
 *  each dissenting source read instead. */
function renderConsText() {
  const res = all.pageRes;
  if (!res) return;
  const only = $("#cp-only").checked;
  const n = res.sources.length;
  const colHtml = (c) => {
    const i = c.idx;
    if (rv.on) return reviewColHtml(c, i, n);
    if (c.status === "agree") return `<span class="cw">${esc(c.winner)}</span>`;
    const alts = c.dissent.map((i) => `<span class="cw-alt" title="${esc(srcLabel(res.sources[i]))}">${dot(res.sources[i])}${
      c.tokens[i] ? esc(c.tokens[i]) : '<span class="muted">∅</span>'}</span>`).join("");
    const label = c.status === "tie" ? "close call, no majority" : "settled by majority";
    return `<span class="cw stack ${c.status}${c.winner ? "" : " dropped"}" title="${c.votes} of ${n} sources agree (${label})">
      <span class="cw-win">${c.winner ? esc(c.winner) : "∅"}<small>${c.votes}/${n}</small></span>${alts}</span>`;
  };
  const lines = [];
  let cur = [];
  for (const [idx, col] of res.columns.entries()) {
    const c = { ...col, idx };
    cur.push(c);
    if (c.br) { lines.push({ cols: cur, br: c.br }); cur = []; }
  }
  if (cur.length) lines.push({ cols: cur, br: 0 });
  const out = [];
  let hidden = 0;
  const flush = () => {
    if (hidden) out.push(`<div class="cons-skip">${plural(hidden, "line")} where every source agrees</div>`);
    hidden = 0;
  };
  for (const ln of lines) {
    if (only && ln.cols.every((c) => c.status === "agree")) { hidden++; continue; }
    flush();
    out.push(`<div class="cons-line${ln.br === 2 ? " para" : ""}">${ln.cols.map(colHtml).join("")}</div>`);
  }
  flush();
  $("#cp-text").innerHTML = out.join("") || '<p class="muted">Every source agrees on this page.</p>';
}

// ---- review: pick the right reading for each disputed word, save as the "corrected" source
const reading = (i) => (i in rv.choices ? rv.choices[i] : all.pageRes.columns[i].winner);
const isOpen = (c, i) => c.status === "tie" && !(i in rv.choices);

function reviewColHtml(c, i, n) {
  const text = reading(i);
  const cls = ["cw", "rv-w", c.status !== "agree" ? `stack ${c.status}` : "",
    i in rv.choices ? "chosen" : "", i === rv.sel ? "sel" : "", text ? "" : "dropped"].join(" ");
  const top = text ? esc(text) : "∅";
  if (c.status === "agree" && !(i in rv.choices)) {
    return `<button type="button" class="${cls}" data-col="${i}">${top}</button>`;
  }
  return `<button type="button" class="${cls}" data-col="${i}" title="${c.votes} of ${n} sources agree">
    <span class="cw-win">${top}${c.status !== "agree" ? `<small>${i in rv.choices ? "✓" : `${c.votes}/${n}`}</small>` : ""}</span></button>`;
}

/** Text of the page with the person's choices applied, laid out like the consensus. */
function buildText() {
  const parts = [];
  all.pageRes.columns.forEach((c, i) => {
    const t = reading(i).trim();
    const sep = c.br === 2 ? "\n\n" : c.br === 1 ? "\n" : " ";
    if (t) parts.push(t, sep);
    else if (c.br && parts.length) parts[parts.length - 1] = sep;
  });
  return parts.join("").trim();
}

function setReview(on) {
  rv.on = on;
  $("#cp-review").setAttribute("aria-pressed", String(on));
  $("#cp-review").classList.toggle("primary", on);
  $("#cp-body").classList.toggle("reviewing", on);
  if (on && rv.sel == null) {
    const cols = all.pageRes.columns;
    const first = cols.findIndex((c) => c.status === "tie");
    rv.sel = first >= 0 ? first : Math.max(0, cols.findIndex((c) => c.status !== "agree"));
  }
  renderConsText();
  renderReview();
}

function renderReview() {
  const res = all.pageRes;
  for (const id of ["#rv-page", "#rv-panel", "#rv-save"]) $(id).hidden = !rv.on || !res;
  if (!rv.on || !res) return;
  const img = $("#rv-img");
  const src = `/api/documents/${DOC_ID}/pages/${res.page}/image`;
  if (img.getAttribute("src") !== src) { img.src = src; img.alt = `Page ${res.page + 1}`; }
  renderChoicePanel();
  renderSaveBar();
  placeHighlight();
}

function placeHighlight() {
  const res = all.pageRes, hl = $("#rv-hl");
  const c = res.columns[rv.sel];
  hl.hidden = !c?.box;
  if (!c?.box) return;
  const [x0, y0, x1, y1] = c.box, [w, h] = res.page_size;
  Object.assign(hl.style, { left: `${(x0 / w) * 100}%`, top: `${(y0 / h) * 100}%`,
    width: `${((x1 - x0) / w) * 100}%`, height: `${((y1 - y0) / h) * 100}%` });
  hl.classList.toggle("approx", c.box_approx);
  // keep the word in view inside the scrolling page panel
  const pane = $("#rv-page");
  const top = (y0 / h) * $("#rv-img").clientHeight;
  if (top < pane.scrollTop + 40 || top > pane.scrollTop + pane.clientHeight - 60) {
    pane.scrollTo({ top: Math.max(0, top - pane.clientHeight / 3), behavior: "smooth" });
  }
}

function renderChoicePanel() {
  const res = all.pageRes, n = res.sources.length, i = rv.sel;
  const c = res.columns[i];
  if (!c) { $("#rv-panel").innerHTML = '<p class="muted small">Click a word to review it.</p>'; return; }
  const groups = new Map();  // reading -> source indexes, winner first
  groups.set(c.winner, []);
  c.tokens.forEach((t, k) => { if (!groups.has(t)) groups.set(t, []); groups.get(t).push(k); });
  const current = reading(i);
  const opts = [...groups].filter(([, who]) => who.length).map(([t, who]) => `
    <button type="button" class="rv-opt${t === current ? " on" : ""}" data-val="${esc(t)}">
      <span class="rv-tok">${t ? esc(t) : '<i class="muted">no word here</i>'}</span>
      <span class="rv-who">${who.map((k) => dot(res.sources[k])).join("")} ${who.length}/${n}</span>
    </button>`).join("");
  const status = { agree: ["ok", "all sources agree"], majority: ["info", "settled by majority"], tie: ["scan", "close call"] }[c.status];
  const open = res.columns.filter(isOpen).length;
  $("#rv-panel").innerHTML = `
    <div class="rv-head">
      <span class="badge ${status[0]}">${status[1]}</span>
      <span class="small muted">${c.votes} of ${n} sources read “${esc(c.winner || "nothing")}”</span>
      <div class="spacer"></div>
      <button type="button" class="btn small ghost" id="rv-prev" title="Previous disputed word">←</button>
      <button type="button" class="btn small ghost" id="rv-next" title="Next disputed word">→</button>
    </div>
    <div class="rv-opts">${opts}</div>
    <form class="rv-custom" id="rv-form">
      <input id="rv-input" aria-label="What the page says" placeholder="Or type what the page says (empty = no word)" value="${esc(current)}">
      <button class="btn small">Use</button>
    </form>
    <div class="small muted">${c.box ? (c.box_approx ? "Highlight is approximate: no source with word boxes read this word." : "")
      : "No word box: none of the sources with word boxes read this word."}
      ${open ? `${plural(open, "close call")} still open.` : "No close calls left."}</div>`;
}

function renderSaveBar() {
  const res = all.pageRes;
  const open = res.columns.filter(isOpen).length;
  const saved = res.corrected;
  const when = saved ? new Date(saved.updated_at * 1000).toLocaleString() : "";
  const other = res.sources.find((s) => s !== "corrected") || "";
  $("#rv-save").innerHTML = `
    <div class="small">${saved ? `This page has corrected text, saved ${esc(when)}. Saving replaces it.`
      : "Saving makes this page's text the <b>Corrected</b> source: ground truth to score every other source against."}
      ${open ? `<span class="warn">${plural(open, "close call")} not decided: ${open === 1 ? "it keeps" : "they keep"} the reading shown.</span>` : ""}</div>
    <div class="row" style="margin:0;gap:8px">
      ${saved ? `<a class="btn small ghost" href="?a=corrected&b=${encodeURIComponent(other)}">Compare against it</a>
        <button type="button" class="btn small ghost danger" id="rv-delete">Delete</button>` : ""}
      <button type="button" class="btn small primary" id="rv-saveb">Save corrected text</button>
    </div>`;
}

function selectCol(i) {
  rv.sel = i;
  renderConsText();
  renderChoicePanel();
  placeHighlight();
}

function stepDisputed(dir) {
  const cols = all.pageRes.columns;
  for (let i = rv.sel + dir; i >= 0 && i < cols.length; i += dir) {
    if (cols[i].status !== "agree") return selectCol(i);
  }
}

function choose(value) {
  const i = rv.sel;
  rv.choices[i] = value;
  // move on to the next close call still open, else the next disputed word
  const cols = all.pageRes.columns;
  let next = cols.findIndex((c, k) => k > i && isOpen(c, k));
  if (next < 0) next = cols.findIndex((c, k) => k > i && c.status !== "agree" && !(k in rv.choices));
  selectCol(next >= 0 ? next : i);
  renderSaveBar();
}

async function saveCorrected() {
  const res = all.pageRes;
  const text = buildText();
  const r = await fetch(`/api/documents/${DOC_ID}/pages/${res.page}/corrected`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, based_on: res.sources }),
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  const saved = await r.json();
  res.corrected = { updated_at: saved.updated_at, based_on: saved.based_on };
  renderSaveBar();
  toast(`Saved page ${res.page + 1} as corrected text`);
}

async function deleteCorrected() {
  const res = all.pageRes;
  if (!confirm(`Delete the corrected text of page ${res.page + 1}?`)) return;
  const r = await fetch(`/api/documents/${DOC_ID}/pages/${res.page}/corrected`, { method: "DELETE" });
  if (!r.ok && r.status !== 404) throw new Error(r.statusText);
  res.corrected = null;
  renderSaveBar();
  toast("Corrected text deleted");
}

function stepCons(dir) {
  const scored = all.doc.pages.filter((p) => p.scored).map((p) => p.page);
  const i = scored.indexOf(all.page) + dir;
  if (i >= 0 && i < scored.length) return showConsPage(scored[i]);
}

$("#all-heat").addEventListener("click", guard(async (e) => {
  const el = e.target.closest("[data-cgoto]");
  if (el) await showConsPage(+el.dataset.cgoto);
}));
$("#all-sources").addEventListener("change", updatePickHint);
$("#all-run").addEventListener("click", guard(runAll));
$("#cp-only").addEventListener("change", renderConsText);
$("#cp-prev").addEventListener("click", guard(() => stepCons(-1)));
$("#cp-next").addEventListener("click", guard(() => stepCons(1)));
$("#cp-copy").addEventListener("click", () => all.pageRes && copyText(buildText()));
$("#cp-review").addEventListener("click", () => all.pageRes && setReview(!rv.on));
$("#cp-text").addEventListener("click", (e) => {
  const el = e.target.closest("[data-col]");
  if (el && rv.on) selectCol(+el.dataset.col);
});
$("#rv-panel").addEventListener("click", (e) => {
  const opt = e.target.closest(".rv-opt");
  if (opt) return choose(opt.dataset.val);
  if (e.target.closest("#rv-prev")) return stepDisputed(-1);
  if (e.target.closest("#rv-next")) return stepDisputed(1);
});
$("#rv-panel").addEventListener("submit", (e) => {
  e.preventDefault();
  choose($("#rv-input").value.trim());
});
$("#rv-save").addEventListener("click", guard(async (e) => {
  if (e.target.closest("#rv-saveb")) await saveCorrected();
  if (e.target.closest("#rv-delete")) await deleteCorrected();
}));
$("#rv-img").addEventListener("load", placeHighlight);
$$('input[name="cmp-mode"]').forEach((r) => r.addEventListener("change", () => setMode(currentMode())));
$$("#opt-case, #opt-punct").forEach((i) => i.addEventListener("change", () => {
  if (currentMode() === "all") { if (all.doc) guard(runAll)(); } else if (cmp.a) guard(run)();
}));

pickDefaults();
pickAllDefaults();
setMode(new URLSearchParams(location.search).get("mode") === "all" ? "all" : "pair");
