/* Page inspector: one page image + native / OCR / LLM text + per-page diff,
   plus "Adjust image": preprocessing applied to the image an LLM receives. */
"use strict";

const root = $("#workspace");
const DOC_ID = root.dataset.docId;
const IS_PDF = root.dataset.isPdf === "true";

const state = {
  config: null,
  picker: null,
  showJson: false,    // structured extraction: raw JSON instead of the field table
  pages: [],
  page: +root.dataset.page,
  zoom: 1,
  fit: true,
  query: new URLSearchParams(location.search).get("q") || "",
  searchHits: {},     // page -> {count, native: boxes[], ocr: boxes[]}
  llmResults: [],
  native: null,
  nativeLibrary: "pymupdf",  // which PDF library reads the text layer (see pdftext.py)
  ocr: null,          // cached layer of the selected OCR engine on this page
  ocrEngine: "tesseract",
  // image preprocessing (see preprocess.py); "" = send the page as rendered
  pre: preprocessStore.get(DOC_ID),
  preLabel: "",
  adjustOpen: false,
  peek: false,           // "hold to compare" is showing the original
  editedUrl: null,       // object URL of the adjusted image currently shown
  editedInfo: null,      // {bytes, mime, w, h} of that image
  origInfo: {},          // page -> {bytes, mime, w, h} of the unadjusted LLM image
  stripKey: null,        // page + geometry the filter thumbnails were rendered for
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
  const ocrDone = p.ocr_engines || (p.has_ocr_cache ? ["tesseract"] : []);
  if (ocrDone.length) {
    b.push(`<span class="badge ok" title="OCR done: ${esc(ocrDone.map(ocrLabel).join(", "))}">` +
      `OCR${ocrDone.length > 1 ? "×" + ocrDone.length : ""}</span>`);
  }
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

  guard(loadPageImage)();
  if (state.adjustOpen) renderPresets();
  applyZoom();

  $("#cmp-metrics").innerHTML = "";
  $("#cmp-diff").innerHTML = "";
  if ($("#native-libraries")) $("#native-libraries").hidden = true;
  $("#cmp-legend").hidden = true;
  $("#ab-summary").hidden = true;
  $("#cmp-ref").innerHTML = $("#cmp-hyp").innerHTML = "";

  const [native, ocr, llm] = await Promise.all([
    api(nativeUrl(n)),
    api(pageUrl(n, `/text/ocr?cached_only=true&engine=${encodeURIComponent(state.ocrEngine)}`)),
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
const nativeUrl = (n, lib = state.nativeLibrary) =>
  pageUrl(n, lib === "pymupdf" ? "/text/native" : `/text/native?library=${encodeURIComponent(lib)}`);
const libraryInfo = (id) => (state.config?.text_libraries || []).find((l) => l.id === id);
const nativeSourceId = (lib) => (lib === "pymupdf" ? "native" : `native:${lib}`);

function renderNative() {
  const layer = state.native, p = state.pages[state.page];
  const lib = libraryInfo(state.nativeLibrary);
  const by = lib ? ` · read by <b>${esc(lib.label)}</b>${lib.version ? ` ${esc(lib.version)}` : ""}` +
    ` in ${((layer.elapsed_ms || 0) / 1000).toFixed(2)}s` : "";
  const words = lib && !lib.has_boxes ? `${layer.text.split(/\s+/).filter(Boolean).length} words (no word boxes)`
    : `${layer.words.length} words`;
  $("#native-meta").innerHTML = IS_PDF
    ? `${layer.text.trim().length} characters · ${words} embedded in the file${by}` +
      (p.looks_scanned ? ' · <span class="warn">looks scanned: try OCR or an LLM</span>' : "")
    : '<span class="warn">Image file: there is no embedded text layer. Use OCR or an LLM.</span>';
  setText($("#native-text"), layer.text, "No embedded text on this page.");
  renderSide();
}

function renderLibraries() {
  const sel = $("#native-library");
  if (!sel) { state.nativeLibrary = "pymupdf"; return; }  // not a PDF: only PyMuPDF reads it
  const libs = state.config.text_libraries || [];
  sel.innerHTML = libs.map((l) => `<option value="${esc(l.id)}" ${l.available ? "" : "disabled"}>` +
    `${esc(l.label)}${l.available ? "" : " (not installed)"}</option>`).join("");
  if (!libraryInfo(state.nativeLibrary)?.available) state.nativeLibrary = "pymupdf";
  sel.value = state.nativeLibrary;
  const missing = libs.filter((l) => !l.available);
  sel.title = "Which Python PDF library reads the embedded text layer" +
    (missing.length ? `\nNot installed: ${missing.map((l) => l.label).join(", ")} (pip install "pyocr-toolkit[pdf]")` : "");
}

async function selectLibrary(id) {
  state.nativeLibrary = id;
  try { localStorage.setItem("pyocr.nativeLibrary", id); } catch (_) { /* private mode */ }
  const n = state.page;
  const layer = await api(nativeUrl(n, id));
  if (n !== state.page || id !== state.nativeLibrary) return;
  state.native = layer;
  renderNative();
}

/** Read the page with every PDF library and show which ones get text out. */
async function checkLibraries() {
  const box = $("#native-libraries"), n = state.page;
  box.hidden = false;
  box.innerHTML = '<p class="muted small">Reading this page with every PDF library…</p>';
  const rows = await api(pageUrl(n, "/text/native/libraries"));
  if (n !== state.page) return;
  const best = Math.max(0, ...rows.map((r) => r.chars || 0));
  const status = (r) => {
    if (!r.available) return `<span class="muted">not installed</span> <code>${esc(r.install || "")}</code>`;
    if (r.error) return `<span class="err">${esc(r.error)}</span>`;
    if (!r.chars) return '<span class="bad">no text</span>';
    return r.chars < best * 0.9 ? '<span class="warn">less text than the others</span>' : '<span class="good">text found</span>';
  };
  const num = (v) => (v != null ? fmtInt(v) : "–");
  box.innerHTML = `<table class="table compact"><thead><tr><th>Library</th><th>Result</th><th>Chars</th><th>Words</th>
    <th>Lines</th><th>Time</th><th></th></tr></thead><tbody>${rows.map((r) => `<tr>
    <td><b>${esc(r.label)}</b> <span class="muted small">${esc(r.version || "")}</span></td><td class="nowrap">${status(r)}</td>
    <td>${num(r.chars)}</td><td>${num(r.words)}</td><td>${num(r.lines)}</td>
    <td>${r.elapsed_ms != null ? `${(r.elapsed_ms / 1000).toFixed(2)}s` : "–"}</td>
    <td class="actions">${r.available && !r.error ? `<button class="btn small ghost" data-lib="${esc(r.id)}">View</button>` : ""}</td>
    </tr>`).join("")}</tbody></table>
    <p class="muted small">Results are cached per page. Compare two libraries word by word in the Compare tab,
    or across the whole document on the document's Compare page.</p>`;
  $$("#native-libraries [data-lib]").forEach((b) => b.addEventListener("click", guard(async () => {
    $("#native-library").value = b.dataset.lib;
    await selectLibrary(b.dataset.lib);
  })));
}

function renderOcrEngines() {
  const sel = $("#ocr-engine"), p = state.pages[state.page];
  sel.innerHTML = ocrEngineOptions(state.config, p?.ocr_engines || []);
  sel.value = state.ocrEngine;
  $("#ocr-engine-logo").innerHTML = logoHtml(ocrEngineInfo(state.ocrEngine)?.brand);
}

function renderOcr() {
  renderOcrEngines();
  const layer = state.ocr, engine = ocrEngineInfo(state.ocrEngine);
  const meta = $("#ocr-meta");
  if (layer) {
    const m = layer.meta || {};
    const details = m.settings || (state.ocrEngine === "tesseract" ? `lang ${state.config.tesseract_language}` : "");
    meta.innerHTML = `${layer.text.trim().length} characters · ${layer.words.length} words · ` +
      `${((layer.elapsed_ms || 0) / 1000).toFixed(1)}s` + (m.model ? ` · <b>${esc(m.model)}</b>` : "") +
      (details ? ` · ${esc(details)}` : "") +
      (engine && !engine.has_boxes ? ' · <span class="muted">text only, no word boxes</span>' : "") +
      (m.warnings || []).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join("");
  } else if (engine && !engine.available) {
    meta.innerHTML = `<span class="err">${esc(engine.problem)}</span> <a href="/providers">Setup</a>`;
  } else {
    meta.innerHTML = engine?.kind === "cloud"
      ? `Not run yet for this page. Running it sends the page image to ${esc(engine.label)}.`
      : "Not run yet for this page.";
  }
  $("#run-ocr .lbl").textContent = layer ? "Re-run OCR" : "Run OCR";
  $("#run-ocr").disabled = !engine?.available;
  setText($("#ocr-text"), layer ? layer.text : "",
    `Click “Run OCR” (or press o) to OCR this page with ${engine?.label || "the selected engine"}.`);
  renderSide();
}

async function selectOcrEngine(id) {
  state.ocrEngine = id;
  localStorage.setItem("pyocr.ocrEngine", id);
  const n = state.page;
  const layer = await api(pageUrl(n, `/text/ocr?cached_only=true&engine=${encodeURIComponent(id)}`));
  if (n !== state.page || id !== state.ocrEngine) return;
  state.ocr = layer;
  renderOcr();
  renderCompareOptions();
}

function renderLlm(selectKey) {
  const sel = $("#llm-result-select");
  sel.innerHTML = state.llmResults.map((r) =>
    `<option value="${esc(r.key)}">${esc(r.model)} · ${esc(r.prompt_mode)}${r.schema_label ? ` (${esc(r.schema_label)})` : ""}${r.preprocess ? " · adjusted image" : ""} · ${new Date(r.created_at * 1000).toLocaleString()}</option>`).join("");
  sel.disabled = $("#delete-llm").disabled = state.llmResults.length === 0;
  // With one stored result the meta line already says which it is; the picker only matters when there are several.
  $("#llm-result-row").hidden = state.llmResults.length < 2;
  if (selectKey) sel.value = selectKey;
  showLlmResult(sel.value);
}

function showLlmResult(key) {
  const r = state.llmResults.find((x) => x.key === key);
  renderExtraction(r);
  if (!r) {
    $("#llm-meta").innerHTML = "";
    setText($("#llm-text"), "", "No transcription yet. Click “Transcribe this page” (or press t).");
    renderSide();
    return;
  }
  const tokens = r.input_tokens != null ? ` · ${fmtInt(r.input_tokens)} in / ${fmtInt(r.output_tokens)} out tokens` : "";
  const warns = (r.warnings || []).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join("");
  const image = r.preprocess_label ? ` · image: <span class="badge accent" title="${esc(r.preprocess)}">${esc(r.preprocess_label)}</span>` : "";
  $("#llm-meta").innerHTML = `<span title="${esc(r.provider_label || r.provider)}">${logoHtml(providerBrand(r.provider), "sm")}</span> <b>${esc(r.model)}</b> · ` +
    `${(r.latency_ms / 1000).toFixed(1)}s${tokens}${r.cost ? ` · ${costHtml(r.cost)}` : ""} · ${fmtInt(r.text.length)} chars${image}${warns}`;
  setText($("#llm-text"), r.text);
  renderSide();
}

/** Structured extraction results get a field table; the raw JSON stays one click away. */
function renderExtraction(r) {
  const isExtract = !!(r && r.schema), view = $("#llm-data"), toggle = $("#llm-json-toggle");
  view.hidden = !isExtract || state.showJson;
  toggle.hidden = !isExtract;
  view.innerHTML = isExtract ? extractionHtml(r) : "";
  // a reply that isn't JSON has no table to show, so keep the raw text visible under the message
  $("#llm-text").hidden = isExtract && !state.showJson && r.data != null;
  $(".lbl", toggle).textContent = state.showJson ? "Fields" : "JSON";
}

/** The folded model setup's one-line summary: provider logo, model, prompt, and the image when it is adjusted. */
function renderLlmSetup() {
  const picker = state.picker;
  if (!picker) return;
  const p = picker.provider();
  const root = $("#llm-setup");
  const model = $("[data-model]", root).value.trim() || p.default_model || "no model";
  const promptSel = $("[data-prompt]", root);
  const prompt = (promptSel.selectedOptions[0]?.textContent || promptSel.value).replace(/\s*\(.*$|…$/g, "");
  $("#llm-setup-summary").innerHTML = `<span title="${esc(p.label)}">${logoHtml(p.brand, "sm")}</span>` +
    `<b>${esc(model)}</b> · ${esc(prompt)}` + (state.pre ? ` · <span class="badge accent">${esc(state.preLabel || "adjusted image")}</span>` : "") +
    (p.configured ? "" : ' · <span class="warn">not configured</span>');
  if (!p.configured) root.open = true;  // nothing can run until it is set up: show why
}

/** "Transcribe" or "Extract fields", following the prompt picked in the LLM picker. */
function syncLlmVerb() {
  const extract = state.picker?.isExtract();
  $("#run-llm").lastChild.textContent = extract ? "Extract fields" : "Transcribe this page";
  return extract ? "Extracting…" : "Transcribing…";
}

/** Side-by-side tab: the three sources in columns (LLM = the result picked in the LLM tab). */
function renderSide() {
  if (!state.native) return;
  const llm = state.llmResults.find((x) => x.key === $("#llm-result-select").value);
  $("#side-native-meta").textContent = `${state.native.text.trim().length.toLocaleString()} ch`;
  $("#side-ocr-title").innerHTML = `${logoHtml(ocrEngineInfo(state.ocrEngine)?.brand, "sm")} ${esc(ocrLabel(state.ocrEngine))}`;
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
  const n = state.page, engine = state.ocrEngine, btn = $("#run-ocr");
  const done = setBusy(btn, `Running ${ocrLabel(engine)}…`,
    [[$("#page-frame"), "is-reading"], [$("#ocr-text"), "is-thinking"]]);
  try {
    const layer = await api(pageUrl(n, `/text/ocr?force=${force}&engine=${encodeURIComponent(engine)}`));
    const p = state.pages[n];
    p.ocr_engines = [...new Set([...(p.ocr_engines || []), engine])];
    if (engine === "tesseract") p.has_ocr_cache = true;
    refreshPageBadges(n);
    if (n === state.page && engine === state.ocrEngine) { state.ocr = layer; renderCompareOptions(); }
  } finally {
    done();
    renderOcr();
    if (n === state.page) replayAnimation($("#ocr-text"), "reveal");
  }
}

async function runLlm(force = false) {
  const n = state.page, btn = $("#run-llm");
  const body = { ...state.picker.spec(force), preprocess: state.pre || null };
  const out = $("#llm-text"), emptyMsg = out.dataset.empty;
  out.dataset.empty = "Reading the page…";
  const verb = syncLlmVerb();
  const done = setBusy(btn, verb, [[$("#page-frame"), "is-reading"], [out, "is-thinking"]]);
  // One request, no progress events: an elapsed counter at least shows it's alive.
  const t0 = Date.now();
  const tick = setInterval(() => {
    const s = fmtDuration((Date.now() - t0) / 1000);
    btn.textContent = `${verb} ${s}`;
    out.dataset.empty = `Reading the page… ${s}`;
  }, 1000);
  try {
    const result = await postJSON(pageUrl(n, "/llm"), body);
    // A re-run replaces the stored result under the same key, so count what is stored rather than adding one.
    const stored = await api(pageUrl(n, "/llm"));
    state.pages[n].llm_results = stored.length;
    refreshPageBadges(n);
    if (n === state.page) {
      state.llmResults = stored;
      renderLlm(result.key);
      replayAnimation(out, "reveal");
      renderCompareOptions();
      if (result.cached) toast("Showing the cached result. Use ⋯ › Re-run to call the model again.");
    }
  } finally {
    clearInterval(tick);
    done();
    if (!out.textContent) out.dataset.empty = emptyMsg;  // failed: put the hint back
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
  // An adjusted image may be rotated or cropped: size the frame by what is actually shown.
  let widthPt = p.width;
  if (state.pre && !state.peek) {
    const o = parsePre(state.pre);
    if (o.rotate === 90 || o.rotate === 270) widthPt = p.height;
    widthPt *= (100 - o.crop[1] - o.crop[3]) / 100;
  }
  const naturalWidth = widthPt * (state.config.preview_dpi / 72);
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
      const brand = h.source === "ocr" ? ocrEngineInfo(h.engine)?.brand : h.source === "llm" ? providerBrand(h.provider) : null;
      html += `<div class="sr-hit"><span class="src">${brand ? logoHtml(brand, "sm") + " " : ""}${esc(h.label)} · ${h.count}</span><div>${snippet}</div></div>`;
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
  const opts = [["native", "Native text layer (PyMuPDF)"]];
  if (IS_PDF) {
    for (const l of state.config?.text_libraries || []) {
      if (l.id !== "pymupdf" && l.available) opts.push([nativeSourceId(l.id), `Native text · ${l.label}`]);
    }
  }
  const ocrDone = state.pages[state.page]?.ocr_engines || [];
  for (const id of ocrDone) opts.push([ocrSourceId(id), ocrLabel(id)]);
  for (const r of state.llmResults) {
    opts.push([`llm:${r.key}`, `LLM: ${r.provider} / ${r.model} (${r.prompt_mode})` +
      (r.preprocess_label ? ` · image: ${r.preprocess_label}` : " · original image")]);
  }
  const ref = $("#cmp-ref"), hyp = $("#cmp-hyp");
  const prev = { ref: ref.value, hyp: hyp.value };
  for (const sel of [ref, hyp]) sel.innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  const valid = (v) => opts.some(([o]) => o === v);
  // Defaults: newest LLM transcription as the reference, OCR (or the native
  // text if OCR hasn't run) as the candidate. User choices are kept while valid.
  ref.value = valid(prev.ref) ? prev.ref : (state.llmResults.length ? `llm:${state.llmResults[0].key}` : "native");
  const selected = ocrSourceId(state.ocrEngine);
  const fallback = ocrDone.includes(state.ocrEngine) ? selected : ocrDone.length ? ocrSourceId(ocrDone[0]) : "native";
  hyp.value = valid(prev.hyp) && prev.hyp !== ref.value ? prev.hyp : (fallback !== ref.value ? fallback : "native");
}

const ocrSourceId = (engine) => (engine === "tesseract" ? "ocr" : `ocr:${engine}`);
const textRef = (v) => (v.startsWith("llm:") ? { source: "llm", key: v.slice(4) }
  : v.startsWith("ocr:") ? { source: "ocr", engine: v.slice(4) }
  : v.startsWith("native:") ? { source: "native", library: v.slice(7) } : { source: v });

async function runCompare() {
  const refV = $("#cmp-ref").value, hypV = $("#cmp-hyp").value;
  if (refV === hypV) { toast("Pick two different sources to compare.", true); return; }
  $("#ab-summary").hidden = true;
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

// ------------------------------------------------------------------ adjust image (preprocessing)
// Spec format and step order mirror preprocess.py. The server re-normalises every spec anyway;
// building the same canonical string here lets the filter strip recognise a preset.
const PRE_DEFAULT = { rotate: 0, crop: [0, 0, 0, 0], dpi: null, gray: false, denoise: 0, auto: false,
  equalize: false, brightness: 1, contrast: 1, sharpness: 1, threshold: null, invert: false };
const fmtNum = (v) => String(+(+v).toFixed(2));

function parsePre(spec) {
  const o = { ...PRE_DEFAULT, crop: [...PRE_DEFAULT.crop] };
  for (const tok of (spec || "").split(";")) {
    const [k, v = ""] = tok.split("=").map((x) => x.trim());
    if (!k) continue;
    if (k === "rotate") o.rotate = ((+v % 360) + 360) % 360;
    else if (k === "crop") o.crop = v.split(",").map(Number);
    else if (k === "dpi") o.dpi = +v || null;
    else if (k === "denoise") o.denoise = +v || 3;
    else if (k === "threshold") o.threshold = !v || v === "otsu" || v === "auto" ? "otsu" : +v;
    else if (["brightness", "contrast", "sharpness"].includes(k)) o[k] = +v;
    else if (k in o) o[k] = !["0", "false", "no", "off"].includes(v.toLowerCase());
  }
  if (o.threshold != null) o.gray = true;  // binarizing implies grayscale (as on the server)
  return o;
}

function buildPre(o) {
  const p = [];
  if (o.rotate) p.push(`rotate=${o.rotate}`);
  if (o.crop.some(Boolean)) p.push(`crop=${o.crop.map((c) => fmtNum(Math.round(c * 10) / 10)).join(",")}`);
  if (o.dpi) p.push(`dpi=${o.dpi}`);
  if (o.gray || o.threshold != null) p.push("gray");
  if (o.denoise) p.push(`denoise=${o.denoise}`);
  if (o.auto) p.push("auto");
  if (o.equalize) p.push("equalize");
  for (const k of ["brightness", "contrast", "sharpness"]) if (+fmtNum(o[k]) !== 1) p.push(`${k}=${fmtNum(o[k])}`);
  if (o.threshold != null) p.push(`threshold=${o.threshold}`);
  if (o.invert) p.push("invert");
  return p.join(";");
}

/** The "look" part of a spec (no rotate / crop / resolution): what a filter preset decides. */
const toneOf = (o) => buildPre({ ...o, rotate: 0, crop: [0, 0, 0, 0], dpi: null });
/** A preset applied on top of the current geometry, so picking a filter keeps your crop and rotation. */
const withGeometry = (presetSpec, o) => buildPre({ ...parsePre(presetSpec), rotate: o.rotate, crop: o.crop, dpi: o.dpi });
const originalSrc = () => pageUrl(state.page, `/image?dpi=${state.config.preview_dpi}`);

async function imageInfo(blob) {
  const bmp = await createImageBitmap(blob);
  const info = { bytes: blob.size, mime: blob.type, w: bmp.width, h: bmp.height };
  bmp.close();
  return info;
}

let previewTimer = 0, previewSeq = 0;
function schedulePreview(delay = 220) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(guard(loadPageImage), delay);
}

/** Show the page: the plain preview, or (with a spec) exactly the image the LLM will receive. */
async function loadPageImage() {
  const n = state.page, img = $("#page-img"), seq = ++previewSeq;
  const edited = !!state.pre;
  $("#highlight-layer").hidden = edited;  // search boxes are in page coordinates; a crop/rotation breaks them
  img.classList.add("loading");           // dimmed until the new render arrives
  if (!edited) {
    if (state.editedUrl) URL.revokeObjectURL(state.editedUrl);
    Object.assign(state, { editedUrl: null, editedInfo: null, preLabel: "" });
    img.src = originalSrc();
    renderAdjustState();
    return;
  }
  const res = await fetch(pageUrl(n, `/llm-image?preprocess=${encodeURIComponent(state.pre)}`));
  if (!res.ok) {
    img.classList.remove("loading");
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || res.statusText);
  }
  const blob = await res.blob();
  const info = await imageInfo(blob);
  if (seq !== previewSeq) return;  // a newer preview superseded this one
  if (state.editedUrl) URL.revokeObjectURL(state.editedUrl);
  state.editedUrl = URL.createObjectURL(blob);
  state.editedInfo = info;
  state.preLabel = decodeURIComponent(res.headers.get("X-Preprocess-Label") || "");
  img.src = state.peek ? originalSrc() : state.editedUrl;
  renderAdjustState();
}

/** Everything that reflects the current spec: toolbar button, badge, LLM-tab line, editor footer. */
function renderAdjustState() {
  const on = !!state.pre;
  $("#adjust-toggle").classList.toggle("on", on);
  const badge = $("#edited-badge");
  badge.hidden = !on;
  badge.textContent = state.peek ? "Original" : "Adjusted · what the model sees";
  badge.classList.toggle("peek", state.peek);
  $("#ab-llm").hidden = !on;
  renderLlmSetup();
  $("#llm-image-line").innerHTML = on
    ? `Image: <span class="badge accent">${esc(state.preLabel || "adjusted")}</span> ` +
      '<a href="#" data-adjust="open">Change</a> · <a href="#" data-adjust="clear">Use the original</a>'
    : 'Image: the page as rendered. <a href="#" data-adjust="open">Adjust image</a> to preprocess it first.';
  if (!state.adjustOpen) return;
  $("#adj-spec").textContent = state.pre || "original";
  const o = state.origInfo[state.page], e = on ? state.editedInfo : o;
  const kind = (i) => i.mime.replace("image/", "").toUpperCase();
  $("#adj-info").textContent = !e ? "" :
    `Model receives ${e.w}×${e.h} px · ${kind(e)} · ${fmtSize(e.bytes)}` +
    (on && o ? ` (original ${o.w}×${o.h} px · ${fmtSize(o.bytes)})` : "");
}

/** Filter strip: one thumbnail per preset, rendered with the current crop / rotation. */
function renderPresets() {
  const o = parsePre(state.pre), tone = toneOf(o);
  const key = `${state.page}|${o.rotate}|${o.crop}|${o.dpi}`;
  if (key !== state.stripKey) {
    state.stripKey = key;
    $("#adj-presets").innerHTML = state.config.preprocess_presets.map((p) => {
      const spec = withGeometry(p.spec, o);
      const src = pageUrl(state.page, `/image?dpi=40${spec ? `&preprocess=${encodeURIComponent(spec)}` : ""}`);
      return `<button type="button" class="filter" role="radio" data-spec="${esc(p.spec)}" title="${esc(p.hint)}">
        <img alt="" src="${src}"><span>${esc(p.label)}</span></button>`;
    }).join("") + '<span class="filter custom" role="radio" title="Your own combination (Fine-tune)"><span class="custom-box">' +
      '<svg class="icon" viewBox="0 0 24 24"><path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/>' +
      '<circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/></svg></span><span>Custom</span></span>';
  }
  let matched = false;
  $$("#adj-presets .filter[data-spec]").forEach((b) => {
    const active = b.dataset.spec === tone;
    matched ||= active;
    b.classList.toggle("active", active);
    b.setAttribute("aria-checked", active);
  });
  const custom = $("#adj-presets .filter.custom");
  custom.classList.toggle("active", !matched);
  custom.hidden = matched;
}

/** Put the spec's values into the fine-tune controls. */
function syncAdjustControls() {
  const o = parsePre(state.pre);
  for (const el of $$("[data-adj]")) {
    const k = el.dataset.adj;
    if (k === "threshold-mode") el.value = o.threshold == null ? "" : o.threshold === "otsu" ? "otsu" : "fixed";
    else if (k === "threshold") el.value = typeof o.threshold === "number" ? o.threshold : 128;
    else if (k === "dpi") el.value = o.dpi || "";
    else if (el.type === "checkbox") el.checked = !!o[k];
    else el.value = o[k];
    const out = el.parentElement.querySelector("output");
    if (out) out.textContent = k === "threshold" ? el.value : `×${fmtNum(el.value)}`;
  }
  $('[data-adj="gray"]').disabled = o.threshold != null;  // black & white is always grayscale
  $("[data-threshold-level]").hidden = typeof o.threshold !== "number";
  $$("[data-crop]").forEach((el) => { el.value = o.crop[+el.dataset.crop] || 0; });
  $("[data-rotate-label]").textContent = o.rotate ? `${o.rotate}° clockwise` : "";
  renderPresets();
}

/** Build a spec from the fine-tune controls (rotation comes from the current spec). */
function readAdjustControls() {
  const o = parsePre(state.pre), v = (k) => $(`[data-adj="${k}"]`);
  for (const k of ["brightness", "contrast", "sharpness"]) o[k] = +v(k).value;
  for (const k of ["gray", "auto", "equalize", "invert"]) o[k] = v(k).checked;
  o.denoise = +v("denoise").value;
  o.dpi = +v("dpi").value || null;
  const mode = v("threshold-mode").value;
  o.threshold = mode === "" ? null : mode === "otsu" ? "otsu" : +v("threshold").value;
  const crop = $$("[data-crop]").map((el) => Math.max(0, Math.min(45, +el.value || 0)));
  // keep at least 20% of the page each way (the server enforces the same limit)
  if (crop[0] + crop[2] > 80) crop[2] = 80 - crop[0];
  if (crop[1] + crop[3] > 80) crop[3] = 80 - crop[1];
  o.crop = crop;
  return buildPre(o);
}

function setPre(spec, { immediate = false } = {}) {
  state.pre = spec;
  preprocessStore.set(DOC_ID, spec);
  if (state.adjustOpen) syncAdjustControls();
  if (!spec) state.peek = false;
  applyZoom();
  if (immediate) guard(loadPageImage)(); else schedulePreview();
}

async function loadOriginalInfo(n) {
  if (state.origInfo[n]) return;
  const res = await fetch(pageUrl(n, "/llm-image"));
  if (res.ok) state.origInfo[n] = await imageInfo(await res.blob());
  if (n === state.page) renderAdjustState();
}

function toggleAdjust(open = !state.adjustOpen) {
  state.adjustOpen = open;
  $("#adjust-panel").hidden = !open;
  $("#adjust-toggle").setAttribute("aria-expanded", String(open));
  if (open) {
    state.stripKey = null;
    syncAdjustControls();
    renderAdjustState();
    guard(loadOriginalInfo)(state.page);
  }
  applyZoom();
}

function peekOriginal(on) {
  if (!state.pre || state.peek === on) return;
  state.peek = on;
  if (on || state.editedUrl) $("#page-img").src = on ? originalSrc() : state.editedUrl;
  else schedulePreview(0);  // released before the adjusted render arrived
  renderAdjustState();
  applyZoom();
}

/** Transcribe the original and the adjusted image with the same model, then diff the two. */
async function runAB() {
  if (!state.pre) { toast("Pick a filter in Adjust image first.", true); return; }
  const n = state.page, base = state.picker.spec(false);
  const done = setBusy($("#ab-llm"), "Running A/B…", [[$("#page-frame"), "is-reading"], [$("#llm-text"), "is-thinking"]]);
  try {
    const [a, b] = await Promise.all([
      postJSON(pageUrl(n, "/llm"), { ...base, preprocess: null }),
      postJSON(pageUrl(n, "/llm"), { ...base, preprocess: state.pre }),
    ]);
    state.pages[n].llm_results += !a.cached + !b.cached;
    refreshPageBadges(n);
    if (n !== state.page) return;
    state.llmResults = await api(pageUrl(n, "/llm"));
    renderLlm(b.key);
    renderCompareOptions();
    $("#cmp-ref").value = `llm:${a.key}`;
    $("#cmp-hyp").value = `llm:${b.key}`;
    switchTab("compare");
    await runCompare();
    renderAB(a, b);
  } finally {
    done();
  }
}

function renderAB(a, b) {
  // each cell returns HTML; text goes through esc()
  const rows = [
    ["Image sent", (r) => esc(`${(r.image_mime || "").replace("image/", "").toUpperCase()} · ${fmtSize(r.image_bytes)}`)],
    ["Input tokens", (r) => esc(fmtInt(r.input_tokens))],
    ["Output tokens", (r) => esc(fmtInt(r.output_tokens))],
    ["Cost", (r) => (r.cost ? costHtml(r.cost) : '<span class="muted">unknown</span>')],
    ["Time", (r) => esc(`${(r.latency_ms / 1000).toFixed(1)} s${r.cached ? " (cached)" : ""}`)],
    ["Characters", (r) => esc(fmtInt(r.text.length))],
  ];
  const box = $("#ab-summary");
  box.innerHTML = `<table class="table compact ab-table"><thead><tr><th></th><th>A · original image</th>
      <th>B · adjusted <span class="small muted">${esc(b.preprocess_label || "")}</span></th></tr></thead><tbody>` +
    rows.map(([k, f]) => `<tr><td class="muted">${k}</td><td>${f(a)}</td><td>${f(b)}</td></tr>`).join("") +
    `</tbody></table><p class="small muted">The diff shows what the preprocessing changed in the transcription
    (A is the reference, B the candidate). Different isn't automatically better: to see which is <i>right</i>,
    compare each against text you trust, such as the native layer of a born-digital PDF or a page you've checked.</p>`;
  box.hidden = false;
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
  $("#ocr-engine").addEventListener("change", guard((e) => selectOcrEngine(e.target.value)));
  $("#native-library")?.addEventListener("change", guard((e) => selectLibrary(e.target.value)));
  $("#check-libraries")?.addEventListener("click", guard(checkLibraries));
  $("#run-llm").addEventListener("click", guard(() => runLlm(false)));
  $("#rerun-llm").addEventListener("click", guard(() => { $("#llm-more").open = false; return runLlm(true); }));
  $("#llm-json-toggle").addEventListener("click", () => {
    state.showJson = !state.showJson;
    renderExtraction(state.llmResults.find((x) => x.key === $("#llm-result-select").value));
  });
  $("#delete-llm").addEventListener("click", guard(() => { $("#llm-more").open = false; return deleteLlm(); }));
  $("#llm-result-select").addEventListener("change", (e) => showLlmResult(e.target.value));
  $("#run-compare").addEventListener("click", guard(runCompare));
  $("#ab-llm").addEventListener("click", guard(runAB));

  // adjust image
  const img = $("#page-img");
  img.addEventListener("load", () => { img.classList.remove("loading"); applyZoom(); });
  img.addEventListener("error", () => img.classList.remove("loading"));
  $("#adjust-toggle").addEventListener("click", () => toggleAdjust());
  $("#adj-close").addEventListener("click", () => toggleAdjust(false));
  $("#adj-reset").addEventListener("click", () => setPre("", { immediate: true }));
  $("#adj-copy").addEventListener("click", () => copyText(state.pre));
  $("#edited-badge").addEventListener("click", () => toggleAdjust(true));
  $("#adj-presets").addEventListener("click", (e) => {
    const btn = e.target.closest(".filter[data-spec]");
    if (btn) setPre(withGeometry(btn.dataset.spec, parsePre(state.pre)), { immediate: true });
  });
  $$("#adjust-panel [data-adj], #adjust-panel [data-crop]").forEach((el) => {
    el.addEventListener("input", () => setPre(readAdjustControls()));
  });
  $$("#adjust-panel [data-rotate]").forEach((b) => b.addEventListener("click", () => {
    const o = parsePre(state.pre);
    o.rotate = (o.rotate + +b.dataset.rotate + 360) % 360;
    setPre(buildPre(o), { immediate: true });
  }));
  const hold = $("#adj-compare");
  hold.addEventListener("pointerdown", () => peekOriginal(true));
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => hold.addEventListener(ev, () => peekOriginal(false)));
  hold.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); peekOriginal(true); } });
  hold.addEventListener("keyup", () => peekOriginal(false));
  $("#llm-image-line").addEventListener("click", (e) => {
    const act = e.target.closest("[data-adjust]")?.dataset.adjust;
    if (!act) return;
    e.preventDefault();
    if (act === "open") toggleAdjust(true); else setPre("", { immediate: true });
  });
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
    else if (e.key === "e") toggleAdjust();
  });
}

(async function main() {
  wire();
  try {
    state.config = await getConfig();
    state.ocrEngine = preferredOcrEngine(state.config);
    try { state.nativeLibrary = localStorage.getItem("pyocr.nativeLibrary") || "pymupdf"; } catch (_) { /* private mode */ }
    renderLibraries();
    const dpis = [100, 150, 300, 400].filter((d) => d !== state.config.llm_dpi);
    $('[data-adj="dpi"]').innerHTML = `<option value="">Default (${state.config.llm_dpi} dpi)</option>` +
      dpis.map((d) => `<option value="${d}">${d} dpi</option>`).join("");
    state.picker = await initLlmPicker($("[data-llm-picker]"), { page: () => ({ doc_id: DOC_ID, page: state.page }) });
    $("[data-llm-picker] [data-prompt]").addEventListener("change", syncLlmVerb);
    syncLlmVerb();
    $("#llm-setup").addEventListener("change", renderLlmSetup);
    $("#llm-setup").addEventListener("input", renderLlmSetup);
    renderLlmSetup();
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
