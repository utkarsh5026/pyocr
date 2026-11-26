/* Document-level comparison of two sources. */
"use strict";

const cRoot = $("#compare-root");
const DOC_ID = cRoot.dataset.docId;

function pickDefaults() {
  const p = new URLSearchParams(location.search);
  const a = $("#src-a"), b = $("#src-b");
  const enabled = (sel) => [...sel.options].filter((o) => !o.disabled).map((o) => o.value);
  const has = (sel, v) => enabled(sel).includes(v);
  const llm = enabled(a).find((v) => v.startsWith("llm:"));
  a.value = has(a, p.get("a")) ? p.get("a") : (llm || "native");
  const fallback = ["ocr", "native", ...enabled(b)].find((v) => has(b, v) && v !== a.value);
  b.value = has(b, p.get("b")) && p.get("b") !== a.value ? p.get("b") : (fallback || a.value);
}

async function run() {
  const a = $("#src-a").value, b = $("#src-b").value;
  if (a === b) { toast("Pick two different sources.", true); return; }
  const qs = new URLSearchParams({ a, b, ignore_case: $("#opt-case").checked, ignore_punctuation: $("#opt-punct").checked });
  history.replaceState(null, "", `?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
  const res = await api(`/api/documents/${DOC_ID}/compare?${qs}`);
  const o = res.overall;
  const metric = (k, v, cls = "") => `<div class="metric"><div class="v ${cls}">${v}</div><div class="k">${k}</div></div>`;
  $("#cmp-overall").innerHTML = o
    ? metric("Document CER", pct(o.cer), errorRateClass(o.cer)) +
      metric("Document WER", pct(o.wer), errorRateClass(o.wer)) +
      metric("Pages scored", `${o.pages_scored} / ${res.pages.length}`) +
      metric("Pages over 10% CER", o.pages_over_10pct, o.pages_over_10pct ? "bad" : "good") +
      metric("Worst page", `<a href="#row-${o.worst_page}">p${o.worst_page + 1}</a>`)
    : '<p class="callout warn">No page has text from both sources yet. Extract them first on the Overview page.</p>';

  // tiny bar chart: CER per page
  const chart = $("#cmp-chart");
  const scored = res.pages.filter((r) => r.metrics);
  chart.hidden = !scored.length;
  const max = Math.max(0.1, ...scored.map((r) => Math.min(1, r.metrics.cer)));
  chart.innerHTML = `<div class="small muted">CER per page (lower is better)</div><div class="bars">` + res.pages.map((r) => {
    if (!r.metrics) return `<div class="bar missing" title="Page ${r.page + 1}: not scored"></div>`;
    const h = Math.max(2, (Math.min(1, r.metrics.cer) / max) * 100);
    return `<a class="bar ${errorRateClass(r.metrics.cer)}" href="#row-${r.page}" style="height:${h}%"
      title="Page ${r.page + 1}: CER ${pct(r.metrics.cer)}"></a>`;
  }).join("") + "</div>";

  const tbody = $("#cmp-table tbody");
  tbody.innerHTML = res.pages.map((r) => r.metrics ? `
    <tr id="row-${r.page}" data-page="${r.page}" class="clickable">
      <td><b>${r.page + 1}</b></td>
      <td><span class="v ${errorRateClass(r.metrics.cer)}">${pct(r.metrics.cer)}</span></td>
      <td>${pct(r.metrics.wer)}</td>
      <td>${fmtInt(r.metrics.reference_chars)}</td><td>${fmtInt(r.metrics.hypothesis_chars)}</td>
      <td class="actions"><a class="btn small ghost" href="/documents/${DOC_ID}/pages/${r.page + 1}#compare">inspect</a></td>
    </tr>` : `
    <tr id="row-${r.page}" class="muted"><td>${r.page + 1}</td><td colspan="4">not scored: no text from
      ${r.missing.map((m) => (m === a ? "reference" : "candidate")).join(" or ")}</td>
      <td class="actions"><a class="btn small ghost" href="/documents/${DOC_ID}/pages/${r.page + 1}">open</a></td></tr>`).join("");
  $("#cmp-table").hidden = false;
  $$("#cmp-table tr.clickable").forEach((tr) => tr.addEventListener("click", guard((e) => {
    if (e.target.closest("a")) return;
    return toggleDiff(tr, a, b);
  })));
}

const textRef = (v) => (v.startsWith("llm:") ? { source: "llm", key: v.slice(4) } : { source: v });

async function toggleDiff(tr, a, b) {
  const next = tr.nextElementSibling;
  if (next?.classList.contains("diff-row")) { next.remove(); return; }
  const res = await postJSON(`/api/documents/${DOC_ID}/pages/${tr.dataset.page}/compare`, {
    reference: textRef(a), hypothesis: textRef(b),
    ignore_case: $("#opt-case").checked, ignore_punctuation: $("#opt-punct").checked,
  });
  const row = document.createElement("tr");
  row.className = "diff-row";
  row.innerHTML = `<td colspan="6"><div class="legend small"><span class="del">missing in candidate</span>
    <span class="ins">extra in candidate</span><span class="rep"><s>reference</s> → candidate</span></div>
    <pre class="text diff">${renderDiff(res.diff)}</pre></td>`;
  tr.after(row);
}

$("#run").addEventListener("click", guard(run));
$("#swap").addEventListener("click", () => {
  const a = $("#src-a").value;
  $("#src-a").value = $("#src-b").value;
  $("#src-b").value = a;
});
pickDefaults();
if ($("#src-a").value !== $("#src-b").value) guard(run)();
