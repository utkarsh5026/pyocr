/* Full-text reader: the whole document from one source, page by page. */
"use strict";

const rRoot = $("#reader-root");
const DOC_ID = rRoot.dataset.docId;
let current = null; // {source, label, pages: [{page, text}]}

function sourceFromUrl() {
  const s = new URLSearchParams(location.search).get("source");
  const sel = $("#source-select");
  if (s && [...sel.options].some((o) => o.value === s && !o.disabled)) sel.value = s;
  else {
    // first source that actually has pages, preferring an LLM one
    const opts = [...sel.options].filter((o) => !o.disabled);
    sel.value = (opts.find((o) => o.value.startsWith("llm:")) || opts[0]).value;
  }
}

function renderDownloads(source) {
  const enc = encodeURIComponent(source);
  $("#downloads").innerHTML = [["txt", "Plain text"], ["md", "Markdown"], ["json", "JSON"], ["jsonl", "JSONL (page per line)"]]
    .map(([f, l]) => `<a href="/api/documents/${DOC_ID}/export?source=${enc}&fmt=${f}">${l}</a>`).join("")
    + '<span class="menu-sep small muted">All sources</span>'
    + [["json", "JSON"], ["csv", "CSV"], ["zip", "ZIP"]]
      .map(([f, l]) => `<a href="/api/documents/${DOC_ID}/export/all?fmt=${f}">${l} (every source)</a>`).join("");
}

function render() {
  const q = $("#find").value.trim();
  const showImages = $("#show-images").checked;
  let matches = 0;
  const html = current.pages.map(({ page, text }) => {
    const img = showImages ? `<a href="/documents/${DOC_ID}/pages/${page + 1}"><img loading="lazy" src="/api/documents/${DOC_ID}/pages/${page}/image?dpi=80" alt=""></a>` : "";
    let body;
    if (text == null) {
      body = `<div class="missing">Not extracted for this source yet.
        <a href="/documents/${DOC_ID}/pages/${page + 1}">Open in the page inspector</a> or start an extraction from the
        <a href="/documents/${DOC_ID}">overview</a>.</div>`;
    } else if (!text.trim()) {
      body = '<div class="missing">(no text on this page)</div>';
    } else {
      const re = queryRegex(q);
      if (re) matches += (text.match(re) || []).length;
      body = `<pre class="text">${highlight(text, q)}</pre>`;
    }
    return `<section class="reader-page ${showImages ? "with-image" : ""}" id="page-${page + 1}">
      <div class="reader-page-head"><a href="/documents/${DOC_ID}/pages/${page + 1}"><b>Page ${page + 1}</b></a>
        ${text ? `<span class="small muted">${text.length.toLocaleString()} chars</span>
        <button class="link small" data-copy-page="${page}">copy</button>` : ""}</div>
      <div class="reader-page-body">${img}${body}</div>
    </section>`;
  }).join("");
  $("#reader-pages").innerHTML = html;
  $("#find-count").textContent = q ? `${matches} match${matches === 1 ? "" : "es"}` : "";
  $$("[data-copy-page]").forEach((b) => b.addEventListener("click", () =>
    copyText(current.pages[+b.dataset.copyPage].text)));
  $("#reader-pages mark")?.scrollIntoView({ block: "center" });
}

async function load() {
  const source = $("#source-select").value;
  history.replaceState(null, "", `?source=${encodeURIComponent(source)}`);
  renderDownloads(source);
  $("#reader-pages").innerHTML = '<p class="muted">Loading…</p>';
  current = await api(`/api/documents/${DOC_ID}/text?source=${encodeURIComponent(source)}`);
  const done = current.pages.filter((p) => p.text != null);
  const chars = done.reduce((a, p) => a + p.text.length, 0);
  $("#reader-summary").textContent =
    `${current.label}: ${done.length} of ${current.pages.length} pages extracted · ${chars.toLocaleString()} characters`;
  render();
}

$("#source-select").addEventListener("change", guard(load));
$("#show-images").addEventListener("change", render);
let findTimer;
$("#find").addEventListener("input", () => { clearTimeout(findTimer); findTimer = setTimeout(render, 200); });
$("#copy-all").addEventListener("click", () => copyText(
  current.pages.map((p) => `===== Page ${p.page + 1} =====\n\n${(p.text || "").trim()}`).join("\n\n")));

sourceFromUrl();
guard(load)();
