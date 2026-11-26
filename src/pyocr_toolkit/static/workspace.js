/* Page inspector: one page image + native / OCR / LLM text + per-page diff. */
"use strict";

const root = $("#workspace");
const DOC_ID = root.dataset.docId;
const IS_PDF = root.dataset.isPdf === "true";

const state = {
  config: null,
  picker: null,
  pages: [],
  page: +root.dataset.page,
  zoom: 1,
  fit: true,
  query: new URLSearchParams(location.search).get("q") || "",
  searchHits: {},     // page -> {count, native: boxes[], ocr: boxes[]}
  llmResults: [],
  native: null,
  ocr: null,
};

const docUrl = (suffix = "") => `/api/documents/${DOC_ID}${suffix}`;
const pageUrl = (n, suffix = "") => docUrl(`/pages/${n}${suffix}`);

function setText(el, text, emptyMsg) {
  el.dataset.empty = emptyMsg || "";
  el.innerHTML = text ? highlight(text, state.query) : "";
  const first = el.querySelector("mark");
  if (first) first.scrollIntoView({ block: "center" });
}

// ------------------------------------------------------------------ page list
function pageBadges(p, n) {
  const b = [];
  if (p.looks_scanned) b.push('<span class="badge scan" title="Little or no embedded text - needs OCR">scan</span>');
  else b.push(`<span class="badge" title="Characters in the embedded text layer">${p.native_chars} ch</span>`);
  if (p.has_ocr_cache) b.push('<span class="badge ok" title="Tesseract OCR done">OCR</span>');
  if (p.llm_results) b.push(`<span class="badge ok" title="LLM transcriptions stored">LLM${p.llm_results > 1 ? "×" + p.llm_results : ""}</span>`);
  const hits = state.searchHits[n];
  if (hits) b.push(`<span class="badge hit">${hits.count} hit${hits.count > 1 ? "s" : ""}</span>`);
  return b.join("");
}

function renderPageList() {
  const list = $("#page-list");
  list.innerHTML = state.pages.map((p, n) => `
    <li data-page="${n}">
      <img loading="lazy" src="${pageUrl(n, "/image?dpi=36")}" alt="">
      <div class="pinfo"><b>Page ${n + 1}</b><div class="badges">${pageBadges(p, n)}</div></div>
    </li>`).join("");
  $$("#page-list li").forEach((li) => li.addEventListener("click", () => gotoPage(+li.dataset.page)));
}

function refreshPageBadges(n) {
  const el = $(`#page-list li[data-page="${n}"] .badges`);
  if (el) el.innerHTML = pageBadges(state.pages[n], n);
  if (n === state.page) $("#page-badges").innerHTML = pageBadges(state.pages[n], n);
}

// ------------------------------------------------------------------ navigation
async function gotoPage(n) {
  if (n < 0 || n >= state.pages.length) return;
  state.page = n;
  const qs = state.query ? `?q=${encodeURIComponent(state.query)}` : "";
  history.replaceState(null, "", `/documents/${DOC_ID}/pages/${n + 1}${qs}${location.hash}`);
  document.title = `Page ${n + 1} · ${document.title.split("·").slice(1).join("·").trim()}`;
  $("#page-label").textContent = `${n + 1} / ${state.pages.length}`;
  $("#prev-page").disabled = n === 0;
  $("#next-page").disabled = n === state.pages.length - 1;
  $("#page-badges").innerHTML = pageBadges(state.pages[n], n);
  $("#open-image").href = pageUrl(n, "/image?dpi=200");
  $$("#page-list li").forEach((li) => li.classList.toggle("active", +li.dataset.page === n));
  $(`#page-list li[data-page="${n}"]`)?.scrollIntoView({ block: "nearest" });

  const img = $("#page-img");
  img.onload = applyZoom;
  img.src = pageUrl(n, `/image?dpi=${state.config.preview_dpi}`);
  applyZoom();

  $("#cmp-metrics").innerHTML = "";
  $("#cmp-diff").innerHTML = "";
  $("#cmp-legend").hidden = true;
  $("#cmp-ref").innerHTML = $("#cmp-hyp").innerHTML = "";

  const [native, ocr, llm] = await Promise.all([
    api(pageUrl(n, "/text/native")),
    api(pageUrl(n, "/text/ocr?cached_only=true")),
    api(pageUrl(n, "/llm")),
  ]);
  if (n !== state.page) return; // user moved on while loading
  Object.assign(state, { native, ocr, llmResults: llm });
  renderNative();
  renderOcr();
  renderLlm();
  renderCompareOptions();
  drawHighlights();
}

// ------------------------------------------------------------------ tabs
function renderNative() {
  const layer = state.native, p = state.pages[state.page];
  $("#native-meta").innerHTML = IS_PDF
    ? `${layer.text.trim().length} characters · ${layer.words.length} words embedded in the file` +
      (p.looks_scanned ? ' · <span class="warn">looks scanned: try OCR or an LLM</span>' : "")
    : '<span class="warn">Image file: there is no embedded text layer. Use OCR or an LLM.</span>';
  setText($("#native-text"), layer.text, "No embedded text on this page.");
  renderSide();
}

function renderOcr() {
  const layer = state.ocr;
  const meta = $("#ocr-meta");
  if (!state.config.tesseract_available) {
    meta.innerHTML = '<span class="err">Tesseract is not installed on the server.</span> <a href="/providers">How to install</a>';
  } else if (layer) {
    meta.textContent = `${layer.text.trim().length} characters · ${layer.words.length} words · ` +
      `${(layer.elapsed_ms / 1000).toFixed(1)}s · lang ${state.config.tesseract_language}`;
  } else {
    meta.textContent = "Not run yet for this page.";
  }
  $("#run-ocr").textContent = layer ? "Re-run OCR" : "Run OCR";
  $("#run-ocr").disabled = !state.config.tesseract_available;
  setText($("#ocr-text"), layer ? layer.text : "", "Click “Run OCR” (or press o) to OCR this page with Tesseract.");
  renderSide();
}

function renderLlm(selectKey) {
  const sel = $("#llm-result-select");
  sel.innerHTML = state.llmResults.map((r) =>
    `<option value="${esc(r.key)}">${esc(r.provider)} · ${esc(r.model)} · ${esc(r.prompt_mode)} · ${new Date(r.created_at * 1000).toLocaleString()}</option>`).join("");
  sel.disabled = $("#delete-llm").disabled = state.llmResults.length === 0;
  if (selectKey) sel.value = selectKey;
  showLlmResult(sel.value);
}

function showLlmResult(key) {
  const r = state.llmResults.find((x) => x.key === key);
  if (!r) {
    $("#llm-meta").innerHTML = "";
    setText($("#llm-text"), "", "No transcription yet. Pick a provider and model above and click “Transcribe this page” (or press t).");
    renderSide();
    return;
  }
  const tokens = r.input_tokens != null ? ` · ${fmtInt(r.input_tokens)} in / ${fmtInt(r.output_tokens)} out tokens` : "";
  const warns = (r.warnings || []).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join("");
  $("#llm-meta").innerHTML = `${esc(r.provider_label || r.provider)} · <b>${esc(r.model)}</b> · ` +
    `${(r.latency_ms / 1000).toFixed(1)}s${tokens} · ${r.text.length} chars${warns}`;
  setText($("#llm-text"), r.text);
  renderSide();
}

/** Side-by-side tab: the three sources in columns (LLM = the result picked in the LLM tab). */
function renderSide() {
  if (!state.native) return;
  const llm = state.llmResults.find((x) => x.key === $("#llm-result-select").value);
  $("#side-native-meta").textContent = `${state.native.text.trim().length.toLocaleString()} ch`;
  $("#side-ocr-meta").textContent = state.ocr ? `${state.ocr.text.trim().length.toLocaleString()} ch` : "not run";
  $("#side-llm-meta").textContent = llm ? `${llm.model} · ${llm.text.length.toLocaleString()} ch` : "none yet";
  setSideText($("#side-native"), state.native.text, "No embedded text.");
  setSideText($("#side-ocr"), state.ocr?.text, "Not run yet (press o).");
  setSideText($("#side-llm"), llm?.text, "No transcription yet (press t).");
}

function setSideText(el, text, emptyMsg) {
  el.dataset.empty = emptyMsg;
  el.innerHTML = text ? highlight(text, state.query) : "";
}

function switchTab(name) {
  $$(".tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  $$(".tab-body").forEach((t) => { t.hidden = t.dataset.tab !== name; });
  // "Side by side" is a layout mode: widen the text panel and remember it.
  root.classList.toggle("side", name === "side");
  try { localStorage.setItem("pyocr.sideBySide", name === "side" ? "1" : "0"); } catch (_) { /* private mode */ }
  applyZoom();
}

// ------------------------------------------------------------------ actions
async function runOcr(force = true) {
  const n = state.page, btn = $("#run-ocr");
  btn.disabled = true; btn.textContent = "Running OCR…";
  try {
    const layer = await api(pageUrl(n, `/text/ocr?force=${force}`));
    state.pages[n].has_ocr_cache = true;
    refreshPageBadges(n);
    if (n === state.page) { state.ocr = layer; renderCompareOptions(); }
  } finally {
    if (n === state.page) renderOcr();
  }
}

async function runLlm(force = false) {
  const n = state.page, btn = $("#run-llm");
  const body = state.picker.spec(force);
  btn.disabled = true; btn.textContent = "Transcribing…";
  try {
    const result = await postJSON(pageUrl(n, "/llm"), body);
    if (!result.cached) state.pages[n].llm_results += 1;
    refreshPageBadges(n);
    if (n === state.page) {
      state.llmResults = await api(pageUrl(n, "/llm"));
      renderLlm(result.key);
      renderCompareOptions();
      if (result.cached) toast("Showing the cached result. Use “re-run” to call the model again.");
    }
  } finally {
    btn.disabled = false; btn.textContent = "Transcribe this page";
  }
}

async function deleteLlm() {
  const key = $("#llm-result-select").value;
  if (!key || !confirm("Delete this stored transcription?")) return;
  await api(pageUrl(state.page, `/llm/${encodeURIComponent(key)}`), { method: "DELETE" });
  state.pages[state.page].llm_results -= 1;
  state.llmResults = await api(pageUrl(state.page, "/llm"));
  refreshPageBadges(state.page);
  renderLlm();
  renderCompareOptions();
}

// ------------------------------------------------------------------ zoom & highlights
function applyZoom() {
  const p = state.pages[state.page];
  if (!p) return;
  const naturalWidth = p.width * (state.config.preview_dpi / 72);
  if (state.fit) {
    const avail = $("#page-scroll").clientWidth - 32;
    state.zoom = Math.max(0.2, Math.min(4, avail / naturalWidth));
  }
  $("#page-frame").style.width = `${naturalWidth * state.zoom}px`;
  $("#zoom-label").textContent = `${Math.round(state.zoom * 100)}%`;
}

function drawHighlights() {
  const layer = $("#highlight-layer");
  layer.innerHTML = "";
  const hits = state.searchHits[state.page], p = state.pages[state.page];
  if (!hits || !p) return;
  for (const [kind, boxes] of [["native", hits.native], ["ocr", hits.ocr]]) {
    for (const [x0, y0, x1, y1] of boxes || []) {
      const d = document.createElement("div");
      d.className = kind;
      Object.assign(d.style, {
        left: `${(x0 / p.width) * 100}%`, top: `${(y0 / p.height) * 100}%`,
        width: `${((x1 - x0) / p.width) * 100}%`, height: `${((y1 - y0) / p.height) * 100}%`,
      });
      layer.appendChild(d);
    }
  }
}

// ------------------------------------------------------------------ search
async function runSearch(ev) {
  ev?.preventDefault();
  const q = $("#search-input").value.trim();
  state.query = q;
  state.searchHits = {};
  const box = $("#search-results");
  history.replaceState(null, "", `/documents/${DOC_ID}/pages/${state.page + 1}${q ? `?q=${encodeURIComponent(q)}` : ""}`);
  if (!q) {
    box.hidden = true;
    state.pages.forEach((_, n) => refreshPageBadges(n));
    drawHighlights();
    rerenderTexts();
    return;
  }
  const sources = $$(".search-sources input:checked").map((i) => i.value).join(",");
  const res = await api(docUrl(`/search?q=${encodeURIComponent(q)}&sources=${sources}`));
  let html = `<div class="sr-summary">${res.total} match${res.total === 1 ? "" : "es"} on ${res.pages.length} page(s)`;
  if (res.pages_without_ocr && sources.includes("ocr")) html += ` · OCR not run on ${res.pages_without_ocr} page(s)`;
  html += "</div>";
  for (const pg of res.pages) {
    state.searchHits[pg.page] = {
      count: pg.hits.reduce((a, h) => a + h.count, 0),
      native: pg.hits.filter((h) => h.source === "native").flatMap((h) => h.boxes),
      ocr: pg.hits.filter((h) => h.source === "ocr").flatMap((h) => h.boxes),
    };
    html += `<div class="sr-page" data-page="${pg.page}"><b>Page ${pg.page + 1}</b>`;
    for (const h of pg.hits) {
      const s = h.snippets[0];
      const snippet = s ? `${esc(s.before)}<mark>${esc(s.match)}</mark>${esc(s.after)}` : "";
      html += `<div class="sr-hit"><span class="src">${esc(h.label)} · ${h.count}</span><div>${snippet}</div></div>`;
    }
    html += "</div>";
  }
  box.innerHTML = html;
  box.hidden = false;
  $$("#search-results .sr-page").forEach((el) => el.addEventListener("click", () => gotoPage(+el.dataset.page)));
  state.pages.forEach((_, n) => refreshPageBadges(n));
  drawHighlights();
  rerenderTexts();
  if (res.pages.length && !state.searchHits[state.page]) gotoPage(res.pages[0].page);
}

function rerenderTexts() {
  if (!state.native) return;
  renderNative();
  renderOcr();
  showLlmResult($("#llm-result-select").value);
}

// ------------------------------------------------------------------ compare
function renderCompareOptions() {
  const opts = [["native", "Native text layer"]];
  if (state.ocr) opts.push(["ocr", "Tesseract OCR"]);
  for (const r of state.llmResults) opts.push([`llm:${r.key}`, `LLM: ${r.provider} / ${r.model} (${r.prompt_mode})`]);
  const ref = $("#cmp-ref"), hyp = $("#cmp-hyp");
  const prev = { ref: ref.value, hyp: hyp.value };
  for (const sel of [ref, hyp]) sel.innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  const valid = (v) => opts.some(([o]) => o === v);
  // Defaults: newest LLM transcription as the reference, OCR (or the native
  // text if OCR hasn't run) as the candidate. User choices are kept while valid.
  ref.value = valid(prev.ref) ? prev.ref : (state.llmResults.length ? `llm:${state.llmResults[0].key}` : "native");
  const fallback = state.ocr ? "ocr" : "native";
  hyp.value = valid(prev.hyp) && prev.hyp !== ref.value ? prev.hyp : (fallback !== ref.value ? fallback : "native");
}

const textRef = (v) => (v.startsWith("llm:") ? { source: "llm", key: v.slice(4) } : { source: v });

async function runCompare() {
  const refV = $("#cmp-ref").value, hypV = $("#cmp-hyp").value;
  if (refV === hypV) { toast("Pick two different sources to compare.", true); return; }
  const res = await postJSON(pageUrl(state.page, "/compare"), {
    reference: textRef(refV), hypothesis: textRef(hypV),
    ignore_case: $("#cmp-case").checked, ignore_punctuation: $("#cmp-punct").checked,
  });
  renderMetrics($("#cmp-metrics"), res.metrics);
  $("#cmp-legend").hidden = false;
  $("#cmp-diff").innerHTML = renderDiff(res.diff);
}

function renderMetrics(el, m) {
  const metric = (k, v, cls = "") => `<div class="metric"><div class="v ${cls}">${v}</div><div class="k">${k}</div></div>`;
  el.innerHTML =
    metric("Char error rate", pct(m.cer), errorRateClass(m.cer)) +
    metric("Word error rate", pct(m.wer), errorRateClass(m.wer)) +
    metric("Char accuracy", pct(m.accuracy)) +
    metric("Ref / cand chars", `${fmtInt(m.reference_chars)} / ${fmtInt(m.hypothesis_chars)}`) +
    metric("Ref / cand words", `${fmtInt(m.reference_words)} / ${fmtInt(m.hypothesis_words)}`);
}

// ------------------------------------------------------------------ wiring
function wire() {
  $("#prev-page").addEventListener("click", () => gotoPage(state.page - 1));
  $("#next-page").addEventListener("click", () => gotoPage(state.page + 1));
  $("#zoom-in").addEventListener("click", () => { state.fit = false; state.zoom = Math.min(4, state.zoom * 1.2); applyZoom(); });
  $("#zoom-out").addEventListener("click", () => { state.fit = false; state.zoom = Math.max(0.2, state.zoom / 1.2); applyZoom(); });
  $("#zoom-fit").addEventListener("click", () => { state.fit = true; applyZoom(); });
  window.addEventListener("resize", () => state.fit && applyZoom());
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  $$("[data-copy]").forEach((b) => b.addEventListener("click", () => copyText($(b.dataset.copy).textContent)));

  $("#run-ocr").addEventListener("click", guard(() => runOcr(true)));
  $("#run-llm").addEventListener("click", guard(() => runLlm(false)));
  $("#rerun-llm").addEventListener("click", guard(() => runLlm(true)));
  $("#delete-llm").addEventListener("click", guard(deleteLlm));
  $("#llm-result-select").addEventListener("change", (e) => showLlmResult(e.target.value));
  $("#run-compare").addEventListener("click", guard(runCompare));
  $("#search-form").addEventListener("submit", guard(runSearch));
  // the (x) clear button fires "search" with an empty value (Enter is handled by submit)
  $("#search-input").addEventListener("search", guard(() => !$("#search-input").value && runSearch()));
  $$(".search-sources input").forEach((i) => i.addEventListener("change", guard(() => state.query && runSearch())));

  // scroll the side-by-side columns together (proportionally, since lengths differ)
  const cols = $$(".side-cols pre");
  let lead = null;
  cols.forEach((c) => {
    c.addEventListener("pointerenter", () => { lead = c; });
    c.addEventListener("scroll", () => {
      if (lead !== c) return;
      const f = c.scrollTop / Math.max(1, c.scrollHeight - c.clientHeight);
      cols.forEach((o) => { if (o !== c) o.scrollTop = f * (o.scrollHeight - o.clientHeight); });
    });
  });

  document.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
    const tabs = { 1: "native", 2: "ocr", 3: "llm", 4: "compare", 5: "side" };
    if (e.key === "ArrowRight" || e.key === "j") gotoPage(state.page + 1);
    else if (e.key === "ArrowLeft" || e.key === "k") gotoPage(state.page - 1);
    else if (e.key === "/") { e.preventDefault(); $("#search-input").focus(); }
    else if (tabs[e.key]) switchTab(tabs[e.key]);
    else if (e.key === "t") { switchTab("llm"); guard(() => runLlm(false))(); }
    else if (e.key === "o") { switchTab("ocr"); guard(() => runOcr(false))(); }
  });
}

(async function main() {
  wire();
  try {
    state.config = await getConfig();
    state.picker = await initLlmPicker($("[data-llm-picker]"));
    state.pages = await api(docUrl("/pages"));
    renderPageList();
    if (location.hash === "#llm") switchTab("llm");
    if (location.hash === "#compare") switchTab("compare");
    else if (!location.hash && localStorage.getItem("pyocr.sideBySide") === "1") switchTab("side");
    await gotoPage(Math.min(state.page, state.pages.length - 1));
    if (state.query) { $("#search-input").value = state.query; await runSearch(); }
  } catch (e) {
    toast(e.message, true);
  }
})();
