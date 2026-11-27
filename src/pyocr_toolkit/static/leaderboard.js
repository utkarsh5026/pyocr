/* Leaderboard: every source ranked across documents (Services.leaderboard).
 * Each page is scored against corrected text where a person reviewed it, else
 * the consensus of three or more independent sources. */
"use strict";

const lb = { res: null, sort: "cer", dir: 1 };

const refValue = () => $('input[name="lb-ref"]:checked').value;
const fmtUsd = (c) => (c == null ? "–" : c.usd === 0 ? "free" : c.usd < 0.01 ? `$${c.usd.toFixed(4)}` : `$${c.usd.toFixed(3)}`);

function readUrl() {
  const p = new URLSearchParams(location.search);
  if (["auto", "corrected", "consensus"].includes(p.get("ref"))) $(`input[name="lb-ref"][value="${p.get("ref")}"]`).checked = true;
  $("#lb-shared").checked = p.get("shared") === "1";
}

function writeUrl() {
  const p = new URLSearchParams();
  if ($("#lb-scope").value) p.set("workspace", $("#lb-scope").value);
  if (refValue() !== "auto") p.set("ref", refValue());
  if ($("#lb-shared").checked) p.set("shared", "1");
  history.replaceState(null, "", `${location.pathname}${p.toString() ? `?${p}` : ""}`);
}

async function load() {
  writeUrl();
  const qs = new URLSearchParams({
    reference: refValue(), shared: $("#lb-shared").checked,
    ignore_case: $("#lb-case").checked, ignore_punctuation: $("#lb-punct").checked,
  });
  if ($("#lb-scope").value) qs.set("workspace", $("#lb-scope").value);
  $("#lb-note").textContent = "Scoring…";
  lb.res = await api(`/api/leaderboard?${qs}`);
  $("#lb-note").textContent = "";
  renderSummary();
  renderTable();
  renderHeat();
}

function renderSummary() {
  const r = lb.res, el = $("#lb-summary");
  el.hidden = false;
  const ref = r.pages_by_reference;
  const notes = [];
  if (!r.pages_scored) {
    notes.push(r.shared
      ? "No page has been read by every source. Turn off <i>Only pages every source has read</i>, or run the missing sources."
      : r.reference === "corrected"
        ? "No page has corrected text yet. Review a page on Compare → All sources → <i>Review &amp; correct</i>."
        : "Nothing to score yet: a page needs corrected text, or text from three or more independent sources (say Tesseract and two LLM runs).");
  } else if (!ref.corrected) {
    notes.push(`No reviewed pages here, so this ranks <b>agreement with the consensus</b>, not accuracy: a source the
      others outvote scores badly even when it is right. Review a few pages (Compare → All sources →
      <i>Review &amp; correct</i>) to rank by real accuracy.`);
  }
  el.innerHTML = `
    <div class="cons-stat"><span class="k">Scored</span><b>${fmtInt(r.pages_scored)}</b>
      <span class="small muted">${plural(r.pages_scored, "page")} in ${plural(r.docs.length, "document")}</span></div>
    <div class="cons-stat"><b class="${ref.corrected ? "good" : ""}">${fmtInt(ref.corrected)}</b><span class="small muted">against corrected text</span></div>
    <div class="cons-stat"><b>${fmtInt(ref.consensus)}</b><span class="small muted">against the consensus</span></div>
    <div class="cons-stat"><b class="muted">${fmtInt(r.pages_skipped)}</b><span class="small muted">skipped: no reference</span></div>
    ${notes.map((n) => `<div class="callout warn"><span>${n}</span></div>`).join("")}`;
}

const SORTERS = {
  label: (e) => e.label.toLowerCase(),
  cer: (e) => e.cer ?? Infinity,
  wer: (e) => e.wer ?? Infinity,
  werfree: (e) => e.wer_order_free ?? Infinity,
  numbers: (e) => (e.numbers.total ? 1 - e.numbers.correct / e.numbers.total : Infinity),
  pages: (e) => -e.pages,
  cost: (e) => e.cost_per_page?.usd ?? Infinity,
  speed: (e) => e.seconds_per_page ?? Infinity,
};

function renderTable() {
  const r = lb.res;
  $("#lb-table-card").hidden = !r.entries.length;
  if (!r.entries.length) return;
  const rank = new Map(r.entries.map((e, i) => [e.id, i + 1]));  // server order = by CER
  const key = SORTERS[lb.sort];
  const rows = [...r.entries].sort((a, b) => {
    const x = key(a), y = key(b);
    return (x < y ? -1 : x > y ? 1 : 0) * lb.dir;
  });
  const maxCer = Math.max(0.001, ...r.entries.map((e) => Math.min(1, e.cer || 0)));
  $$("#lb-table th[data-sort]").forEach((th) => {
    th.setAttribute("aria-sort", th.dataset.sort === lb.sort ? (lb.dir > 0 ? "ascending" : "descending") : "none");
  });
  $("#lb-table tbody").innerHTML = rows.map((e) => {
    const n = e.numbers;
    const partial = e.pages < r.pages_scored;
    const bar = Math.max(2, Math.sqrt(Math.min(1, e.cer || 0) / maxCer) * 100);
    return `<tr>
      <td class="muted">${rank.get(e.id)}</td>
      <td><div class="lb-src">${e.brand ? logoHtml(e.brand, "sm") : ""}<span title="${esc(e.label)}">${esc(e.label)}</span></div></td>
      <td class="num"><span class="v ${errorRateClass(e.cer)}">${pct(e.cer)}</span>
        <span class="lb-bar" aria-hidden="true"><i class="${errorRateClass(e.cer)}" style="width:${bar}%"></i></span></td>
      <td class="num">${pct(e.wer)}</td>
      <td class="num">${pct(e.wer_order_free)}</td>
      <td class="small">${fmtInt(e.errors.misread)} · ${fmtInt(e.errors.missed)} · ${fmtInt(e.errors.added)}</td>
      <td class="num"><span class="${n.total ? (n.correct === n.total ? "good" : "bad") : "muted"}">${n.total ? `${n.correct}/${n.total}` : "–"}</span></td>
      <td class="num" title="${partial ? "Scored on fewer pages than other sources: compare with care, or turn on 'Only pages every source has read'" : ""}">
        <span class="${partial ? "warn" : ""}">${fmtInt(e.pages)}</span><span class="muted small">/${fmtInt(r.pages_scored)}</span></td>
      <td class="num" title="${e.cost_per_page ? `≈ ₹${e.cost_per_page.inr.toFixed(3)} per page at list price` : e.kind === "llm" ? "Model not in the catalog: price unknown" : "Runs locally or price unknown"}">${fmtUsd(e.cost_per_page)}</td>
      <td class="num">${e.seconds_per_page == null ? "–" : `${e.seconds_per_page.toFixed(1)} s`}</td>
    </tr>`;
  }).join("");
}

function renderHeat() {
  const r = lb.res;
  $("#lb-heat-card").hidden = !(r.entries.length && r.docs.length > 1);
  if ($("#lb-heat-card").hidden) return;
  const head = `<thead><tr><th></th>${r.docs.map((d, i) => `<th scope="col" title="${esc(d.filename)}">${i + 1}</th>`).join("")}</tr></thead>`;
  const body = r.entries.map((e) => `<tr><th scope="row" title="${esc(e.label)}"><span>${esc(e.label)}</span></th>${
    r.docs.map((d) => {
      const v = e.by_doc[d.id];
      if (v == null) return `<td><span class="cell missing" title="${esc(d.filename)}: not scored"></span></td>`;
      return `<td><a class="cell ${errorRateClass(v)}" href="/documents/${d.id}/compare?mode=all"
        title="${esc(d.filename)}: ${esc(e.label)}, CER ${pct(v)}" aria-label="${esc(d.filename)}, ${esc(e.label)}: ${pct(v)}"></a></td>`;
    }).join("")}</tr>`).join("");
  $("#lb-heat").innerHTML = head + `<tbody>${body}</tbody>`;
}

$("#lb-table thead").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th || !lb.res) return;
  lb.dir = lb.sort === th.dataset.sort ? -lb.dir : 1;
  lb.sort = th.dataset.sort;
  renderTable();
});
for (const el of ["#lb-scope", "#lb-shared", "#lb-case", "#lb-punct"]) $(el).addEventListener("change", guard(load));
$$('input[name="lb-ref"]').forEach((r) => r.addEventListener("change", guard(load)));

readUrl();
getConfig().then(() => guard(load)());
