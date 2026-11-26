/* Library-wide search. */
"use strict";

const params = new URLSearchParams(location.search);
const srcParams = params.getAll("src");
if (srcParams.length) $$("#lib-search input[name=src]").forEach((i) => { i.checked = srcParams.includes(i.value); });

async function runLibrarySearch() {
  const q = $("#q").value.trim();
  const out = $("#lib-results");
  if (!q) { out.innerHTML = ""; return; }
  const sources = $$("#lib-search input[name=src]:checked").map((i) => i.value).join(",") || "native";
  out.innerHTML = '<p class="muted">Searching…</p>';
  const [res] = await Promise.all([api(`/api/search?q=${encodeURIComponent(q)}&sources=${sources}`), getConfig()]);
  const hitLogo = (h) => {
    const brand = h.source === "ocr" ? ocrEngineInfo(h.engine)?.brand : h.source === "llm" ? providerBrand(h.provider) : null;
    return brand ? logoHtml(brand, "sm") + " " : "";
  };
  if (!res.documents.length) {
    out.innerHTML = `<p class="muted">No matches for <b>${esc(q)}</b>.</p>`;
    return;
  }
  out.innerHTML = `<p class="small muted">${res.total} match${res.total === 1 ? "" : "es"} in ${res.documents.length} document(s)</p>` +
    res.documents.map((d) => `
      <section class="card search-doc">
        <div class="section-head"><a href="/documents/${d.document.id}"><b>${esc(d.document.filename)}</b></a>
          <span class="badge hit">${d.total} match${d.total === 1 ? "" : "es"}</span></div>
        ${d.pages.map((p) => `
          <a class="sr-page" href="/documents/${d.document.id}/pages/${p.page + 1}?q=${encodeURIComponent(q)}">
            <b>Page ${p.page + 1}</b>
            ${p.hits.map((h) => `<div class="sr-hit"><span class="src">${hitLogo(h)}${esc(h.label)} · ${h.count}</span>
              ${h.snippets.slice(0, 2).map((s) => `<div>${esc(s.before)}<mark>${esc(s.match)}</mark>${esc(s.after)}</div>`).join("")}
            </div>`).join("")}
          </a>`).join("")}
      </section>`).join("");
}

guard(runLibrarySearch)();
