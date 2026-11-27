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
  a.value = has(a, p.get("a")) ? p.get("a") : (llm || "native");
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
      stat("Word error", pct(o.wer), errorRateClass(o.wer)) +
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
      <td>${pct(r.metrics.wer)}</td>
      <td>${fmtInt(r.errors.misread)}</td><td>${fmtInt(r.errors.missed)}</td><td>${fmtInt(r.errors.added)}</td>
      <td><span class="v ${numbersClass(r.numbers)}">${r.numbers.total ? `${r.numbers.correct}/${r.numbers.total}` : "–"}</span></td>
      <td>${fmtInt(r.metrics.reference_chars)}</td><td>${fmtInt(r.metrics.hypothesis_chars)}</td>
      <td class="actions"><a class="btn small ghost" href="/documents/${DOC_ID}/pages/${r.page + 1}#compare">Inspect</a></td>
    </tr>` : `
    <tr class="muted"><td>${r.page + 1}</td><td colspan="8">not scored: no text from
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
      ${n.extra ? `<span class="small muted">${n.extra} number${n.extra === 1 ? "" : "s"} only in B</span>` : ""}
    </div>
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
  $("#pg-wer").textContent = `WER ${pct(row.metrics.wer)}`;
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
$$("#opt-case, #opt-punct").forEach((i) => i.addEventListener("change", () => cmp.a && guard(run)()));
$("#swap").addEventListener("click", () => {
  const a = $("#src-a").value;
  $("#src-a").value = $("#src-b").value;
  $("#src-b").value = a;
  if (cmp.a) guard(run)();
});
pickDefaults();
if ($("#src-a").value !== $("#src-b").value) guard(run)();
