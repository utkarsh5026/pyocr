/* pyocr UI - plain JS, no build step. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  config: null,
  doc: null,          // document meta
  pages: [],          // page_info list
  page: 0,
  zoom: 1,
  fit: true,
  query: "",
  searchHits: {},     // page -> {native: boxes[], ocr: boxes[]}
  llmResults: [],     // stored results for current page
  native: null,       // current page native layer
  ocr: null,          // current page OCR layer (or null)
  batchCancel: false,
};

// ------------------------------------------------------------------ helpers
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { const body = await res.json(); detail = body.detail || detail; } catch (_) { /* not json */ }
    if (typeof detail !== "string") detail = JSON.stringify(detail);
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function postJSON(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 3000);
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null ? "–" : (x * 100).toFixed(1) + "%");
const docUrl = (suffix = "") => `/api/documents/${state.doc.id}${suffix}`;
const pageUrl = (n, suffix = "") => docUrl(`/pages/${n}${suffix}`);

function queryRegex(q) {
  const terms = q.trim().split(/\s+/).filter(Boolean).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return terms.length ? new RegExp(terms.join("\\s+"), "gi") : null;
}

/** Put `text` into a <pre>, highlighting the current search query. */
function setText(el, text, emptyMsg) {
  el.dataset.empty = emptyMsg || "";
  const re = state.query ? queryRegex(state.query) : null;
  if (!text) { el.innerHTML = ""; return; }
  if (!re) { el.textContent = text; return; }
  let html = "", last = 0;
  for (const m of text.matchAll(re)) {
    html += esc(text.slice(last, m.index)) + "<mark>" + esc(m[0]) + "</mark>";
    last = m.index + m[0].length;
  }
  el.innerHTML = html + esc(text.slice(last));
  const first = el.querySelector("mark");
  if (first) first.scrollIntoView({ block: "center" });
}

function errorRateClass(cer) {
  if (cer == null) return "";
  return cer <= 0.02 ? "good" : cer <= 0.1 ? "warn" : "bad";
}

// ------------------------------------------------------------------ config
async function loadConfig() {
  state.config = await api("/api/config");
  const sel = $("#provider-select");
  sel.innerHTML = "";
  const providers = [...state.config.providers].sort((a, b) => b.configured - a.configured);
  for (const p of providers) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.configured ? p.label : `${p.label} (not configured)`;
    opt.title = p.configured ? p.model_hint : `Set ${p.missing.join(", ")} in the server environment`;
    sel.appendChild(opt);
  }
  const saved = localStorage.getItem("pyocr.provider");
  if (saved && providers.some((p) => p.id === saved)) sel.value = saved;
  onProviderChange(true);

  const prompt = $("#prompt-select");
  prompt.innerHTML = "";
  for (const m of state.config.prompt_modes) prompt.add(new Option(m.label, m.id));
  prompt.add(new Option("Custom prompt…", "custom"));
  prompt.value = localStorage.getItem("pyocr.prompt") || "verbatim";
  $("#custom-prompt").value = localStorage.getItem("pyocr.customPrompt") || state.config.prompt_modes[0].prompt;
  onPromptChange();
}

function currentProvider() {
  return state.config.providers.find((p) => p.id === $("#provider-select").value);
}

function onProviderChange(initial = false) {
  const p = currentProvider();
  if (!p) return;
  localStorage.setItem("pyocr.provider", p.id);
  const savedModel = localStorage.getItem(`pyocr.model.${p.id}`);
  const input = $("#model-input");
  input.value = (initial && savedModel) || savedModel || p.default_model || "";
  input.placeholder = p.model_hint;
  input.title = p.model_hint;
  $("#model-list").innerHTML = p.suggested_models.map((m) => `<option value="${esc(m)}">`).join("");
  if (!p.configured && !initial) toast(`${p.label} is not configured: set ${p.missing.join(", ")}`, true);
}

function onPromptChange() {
  const mode = $("#prompt-select").value;
  localStorage.setItem("pyocr.prompt", mode);
  $("#custom-prompt-box").hidden = mode !== "custom";
  if (mode === "custom") $("#custom-prompt-box").open = true;
}

function llmRequestBody(force = false) {
  const body = {
    provider: $("#provider-select").value,
    model: $("#model-input").value.trim() || null,
    prompt_mode: $("#prompt-select").value,
    force,
  };
  if (body.prompt_mode === "custom") body.custom_prompt = $("#custom-prompt").value;
  return body;
}

// ------------------------------------------------------------------ documents
async function loadDocList(selectId) {
  const docs = await api("/api/documents");
  const sel = $("#doc-select");
  sel.innerHTML = '<option value="">— select document —</option>';
  for (const d of docs) {
    const date = new Date(d.created_at * 1000).toLocaleString();
    sel.add(new Option(`${d.filename} (${d.page_count}p · ${date})`, d.id));
  }
  if (selectId) sel.value = selectId;
}

async function uploadFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  toast(`Uploading ${file.name}…`);
  try {
    const meta = await api("/api/documents", { method: "POST", body: fd });
    await loadDocList(meta.id);
    await openDoc(meta.id);
    toast(`Loaded ${meta.filename}: ${meta.page_count} page(s)`);
  } catch (e) {
    toast(`Upload failed: ${e.message}`, true);
  }
}

async function openDoc(id, page = 0) {
  if (!id) return;
  state.doc = await api(`/api/documents/${id}`);
  state.pages = await api(docUrl("/pages"));
  state.searchHits = {};
  state.query = "";
  $("#search-input").value = "";
  $("#search-results").hidden = true;
  $("#doc-select").value = id;
  $("#empty-state").hidden = true;
  $("#page-frame").hidden = false;
  for (const el of ["#search-input", "#batch-ocr", "#batch-llm", "#report-btn"]) $(el).disabled = false;
  $("#delete-doc").hidden = false;
  renderPageList();
  await gotoPage(Math.min(page, state.pages.length - 1));
}

function renderPageList() {
  const list = $("#page-list");
  list.innerHTML = "";
  state.pages.forEach((p, n) => {
    const li = document.createElement("li");
    li.dataset.page = n;
    li.innerHTML = `
      <img loading="lazy" src="${pageUrl(n, "/image?dpi=36")}" alt="">
      <div class="pinfo"><b>Page ${n + 1}</b><div class="badges">${pageBadges(p, n)}</div></div>`;
    li.addEventListener("click", () => gotoPage(n));
    list.appendChild(li);
  });
  highlightActivePage();
}

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

function refreshPageBadges(n) {
  const li = $(`#page-list li[data-page="${n}"] .badges`);
  if (li) li.innerHTML = pageBadges(state.pages[n], n);
  if (n === state.page) $("#page-badges").innerHTML = pageBadges(state.pages[n], n);
}

function highlightActivePage() {
  $$("#page-list li").forEach((li) => li.classList.toggle("active", +li.dataset.page === state.page));
  const active = $(`#page-list li[data-page="${state.page}"]`);
  if (active) active.scrollIntoView({ block: "nearest" });
}

// ------------------------------------------------------------------ page view
async function gotoPage(n) {
  if (!state.doc || n < 0 || n >= state.pages.length) return;
  state.page = n;
  history.replaceState(null, "", `#doc=${state.doc.id}&page=${n + 1}`);
  $("#page-label").textContent = `${n + 1} / ${state.pages.length}`;
  $("#prev-page").disabled = n === 0;
  $("#next-page").disabled = n === state.pages.length - 1;
  $("#page-badges").innerHTML = pageBadges(state.pages[n], n);
  highlightActivePage();

  const img = $("#page-img");
  img.onload = () => applyZoom();
  img.src = pageUrl(n, `/image?dpi=${state.config.preview_dpi}`);
  applyZoom();

  // reset panels
  $("#cmp-metrics").innerHTML = "";
  $("#cmp-diff").innerHTML = "";
  $("#cmp-legend").hidden = true;
  $("#cmp-ref").innerHTML = $("#cmp-hyp").innerHTML = ""; // new page -> fresh defaults

  const [native, ocr, llm] = await Promise.all([
    api(pageUrl(n, "/text/native")),
    api(pageUrl(n, "/text/ocr?cached_only=true")),
    api(pageUrl(n, "/llm")),
  ]);
  if (n !== state.page) return; // user moved on while loading
  state.native = native;
  state.ocr = ocr;
  state.llmResults = llm;
  renderNative();
  renderOcr();
  renderLlm();
  renderCompareOptions();
  drawHighlights();
}

function renderNative() {
  const layer = state.native;
  const p = state.pages[state.page];
  const words = layer.words.length;
  $("#native-meta").innerHTML = state.doc.is_pdf
    ? `${layer.text.trim().length} characters · ${words} words embedded in the file` +
      (p.looks_scanned ? ' · <span class="warn">looks like a scanned page - try OCR / LLM</span>' : "")
    : `<span class="warn">Image file - there is no embedded text layer. Use OCR or an LLM.</span>`;
  setText($("#native-text"), layer.text, "No embedded text on this page.");
}

function renderOcr() {
  const layer = state.ocr;
  const meta = $("#ocr-meta");
  if (!state.config.tesseract_available) {
    meta.innerHTML = '<span class="err">Tesseract is not installed on the server.</span>';
  } else if (layer) {
    meta.textContent = `${layer.text.trim().length} characters · ${layer.words.length} words · ` +
      `${(layer.elapsed_ms / 1000).toFixed(1)}s · lang ${state.config.tesseract_language}`;
  } else {
    meta.textContent = "Not run yet for this page.";
  }
  $("#run-ocr").textContent = layer ? "Re-run OCR" : "Run OCR";
  $("#run-ocr").disabled = !state.config.tesseract_available;
  setText($("#ocr-text"), layer ? layer.text : "", "Click “Run OCR” to OCR this page with Tesseract.");
}

function renderLlm(selectKey) {
  const sel = $("#llm-result-select");
  sel.innerHTML = "";
  for (const r of state.llmResults) {
    const when = new Date(r.created_at * 1000).toLocaleTimeString();
    sel.add(new Option(`${r.provider} · ${r.model} · ${r.prompt_mode} · ${when}`, r.key));
  }
  sel.hidden = state.llmResults.length === 0;
  if (selectKey) sel.value = selectKey;
  showLlmResult(sel.value);
}

function showLlmResult(key) {
  const r = state.llmResults.find((x) => x.key === key);
  const meta = $("#llm-meta");
  if (!r) {
    meta.innerHTML = "";
    setText($("#llm-text"), "", "No transcription yet. Pick a provider/model at the top and click “Transcribe page”.");
    return;
  }
  const tokens = r.input_tokens != null ? ` · ${r.input_tokens} in / ${r.output_tokens} out tokens` : "";
  const warns = (r.warnings || []).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join("");
  meta.innerHTML = `${esc(r.provider_label || r.provider)} · <b>${esc(r.model)}</b> · ` +
    `${(r.latency_ms / 1000).toFixed(1)}s${tokens} · ${r.text.length} chars${warns}`;
  setText($("#llm-text"), r.text);
}

async function runOcr(n = state.page, force = true) {
  const btn = $("#run-ocr");
  if (n === state.page) { btn.disabled = true; btn.textContent = "Running…"; }
  try {
    const layer = await api(pageUrl(n, `/text/ocr?force=${force}`));
    state.pages[n].has_ocr_cache = true;
    refreshPageBadges(n);
    if (n === state.page) { state.ocr = layer; renderOcr(); renderCompareOptions(); }
    return layer;
  } finally {
    if (n === state.page) btn.disabled = false;
  }
}

async function runLlm(n = state.page, force = false) {
  const btn = $("#run-llm");
  const p = currentProvider();
  if (!p.configured) throw new Error(`${p.label} is not configured: set ${p.missing.join(", ")}`);
  localStorage.setItem(`pyocr.model.${p.id}`, $("#model-input").value.trim());
  if ($("#prompt-select").value === "custom") localStorage.setItem("pyocr.customPrompt", $("#custom-prompt").value);
  if (n === state.page) { btn.disabled = true; btn.textContent = "Transcribing…"; }
  try {
    const result = await postJSON(pageUrl(n, "/llm"), llmRequestBody(force));
    if (!result.cached) state.pages[n].llm_results += 1;
    refreshPageBadges(n);
    if (n === state.page) {
      state.llmResults = await api(pageUrl(n, "/llm"));
      renderLlm(result.key);
      renderCompareOptions();
      if (result.cached) toast("Showing cached result - use “re-run” to call the model again.");
    }
    return result;
  } finally {
    if (n === state.page) { btn.disabled = false; btn.textContent = "Transcribe page"; }
  }
}

// ------------------------------------------------------------------ zoom & highlights
function applyZoom() {
  const frame = $("#page-frame");
  const p = state.pages[state.page];
  if (!p) return;
  const scroll = $("#page-scroll");
  const naturalWidth = p.width * (state.config.preview_dpi / 72);
  if (state.fit) {
    const avail = scroll.clientWidth - 32;
    state.zoom = Math.max(0.2, Math.min(4, avail / naturalWidth));
  }
  frame.style.width = `${naturalWidth * state.zoom}px`;
  $("#zoom-label").textContent = `${Math.round(state.zoom * 100)}%`;
}

function drawHighlights() {
  const layer = $("#highlight-layer");
  layer.innerHTML = "";
  const hits = state.searchHits[state.page];
  const p = state.pages[state.page];
  if (!hits || !p) return;
  const add = (boxes, cls) => {
    for (const [x0, y0, x1, y1] of boxes) {
      const d = document.createElement("div");
      d.className = cls;
      d.style.left = `${(x0 / p.width) * 100}%`;
      d.style.top = `${(y0 / p.height) * 100}%`;
      d.style.width = `${((x1 - x0) / p.width) * 100}%`;
      d.style.height = `${((y1 - y0) / p.height) * 100}%`;
      layer.appendChild(d);
    }
  };
  add(hits.native || [], "native");
  add(hits.ocr || [], "ocr");
}

// ------------------------------------------------------------------ search
async function runSearch(ev) {
  ev?.preventDefault();
  const q = $("#search-input").value.trim();
  state.query = q;
  state.searchHits = {};
  const box = $("#search-results");
  if (!q) {
    box.hidden = true;
    state.pages.forEach((_, n) => refreshPageBadges(n));
    drawHighlights();
    rerenderTexts();
    return;
  }
  const sources = $$(".search-sources input:checked").map((i) => i.value).join(",");
  const res = await api(docUrl(`/search?q=${encodeURIComponent(q)}&sources=${sources}`));
  const re = queryRegex(q);
  let html = `<div class="sr-summary">${res.total} match${res.total === 1 ? "" : "es"} on ${res.pages.length} page(s)`;
  if (res.pages_without_ocr && sources.includes("ocr")) {
    html += ` · OCR not run on ${res.pages_without_ocr} page(s)`;
  }
  html += "</div>";
  for (const pg of res.pages) {
    const count = pg.hits.reduce((a, h) => a + h.count, 0);
    state.searchHits[pg.page] = {
      count,
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
  if (re && res.pages.length && !state.searchHits[state.page]) gotoPage(res.pages[0].page);
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
  for (const sel of [ref, hyp]) {
    sel.innerHTML = "";
    for (const [v, l] of opts) sel.add(new Option(l, v));
  }
  const valid = (v) => opts.some(([o]) => o === v);
  // Defaults: newest LLM transcription as the reference, OCR (or the native
  // text layer if OCR hasn't run) as the candidate. A choice the user made
  // is kept as long as it still exists on this page.
  ref.value = valid(prev.ref) ? prev.ref : (state.llmResults.length ? `llm:${state.llmResults[0].key}` : "native");
  const fallback = state.ocr ? "ocr" : "native";
  hyp.value = valid(prev.hyp) && prev.hyp !== ref.value ? prev.hyp : (fallback !== ref.value ? fallback : "native");
}

function textRef(value) {
  return value.startsWith("llm:") ? { source: "llm", key: value.slice(4) } : { source: value };
}

async function runCompare() {
  const refV = $("#cmp-ref").value, hypV = $("#cmp-hyp").value;
  if (refV === hypV) { toast("Pick two different sources to compare.", true); return; }
  const res = await postJSON(pageUrl(state.page, "/compare"), {
    reference: textRef(refV),
    hypothesis: textRef(hypV),
    ignore_case: $("#cmp-case").checked,
    ignore_punctuation: $("#cmp-punct").checked,
  });
  const m = res.metrics;
  const metric = (k, v, cls = "") => `<div class="metric"><div class="v ${cls}">${v}</div><div class="k">${k}</div></div>`;
  $("#cmp-metrics").innerHTML =
    metric("Char error rate", pct(m.cer), errorRateClass(m.cer)) +
    metric("Word error rate", pct(m.wer), errorRateClass(m.wer)) +
    metric("Char accuracy", pct(m.accuracy)) +
    metric("Ref / cand chars", `${m.reference_chars} / ${m.hypothesis_chars}`) +
    metric("Ref / cand words", `${m.reference_words} / ${m.hypothesis_words}`);
  $("#cmp-legend").hidden = false;
  // Keep leading line breaks outside the coloured span so they don't leave
  // a highlighted sliver at the end of the previous line.
  const wrap = (text, render) => {
    const lead = text.match(/^\n*/)[0];
    return lead + render(esc(text.slice(lead.length)));
  };
  $("#cmp-diff").innerHTML = res.diff.map((s) => {
    switch (s.op) {
      case "equal": return esc(s.ref);
      case "delete": return wrap(s.ref, (t) => ` <span class="del" title="in reference only">${t}</span> `);
      case "insert": return wrap(s.hyp, (t) => ` <span class="ins" title="in candidate only">${t}</span> `);
      default: {
        const lead = (s.ref.match(/^\n*/)[0].length >= s.hyp.match(/^\n*/)[0].length ? s.ref : s.hyp).match(/^\n*/)[0];
        return `${lead} <span class="rep"><s>${esc(s.ref.trimStart())}</s>→<ins>${esc(s.hyp.trimStart())}</ins></span> `;
      }
    }
  }).join(" ");
}

// ------------------------------------------------------------------ batch
async function runBatch(kind) {
  const pages = state.pages.map((_, i) => i).filter((n) =>
    kind === "ocr" ? !state.pages[n].has_ocr_cache : true);
  if (!pages.length) { toast("All pages already done."); return; }
  if (kind === "llm") {
    const p = currentProvider();
    if (!p.configured) { toast(`${p.label} is not configured: set ${p.missing.join(", ")}`, true); return; }
    if (!confirm(`Send ${pages.length} page image(s) to ${p.label} (${$("#model-input").value || p.default_model})? ` +
      "Pages with a cached result for this model + prompt are not re-sent.")) return;
  }
  const concurrency = kind === "ocr" ? 2 : 3;
  const prog = $("#batch-progress");
  const bar = prog.querySelector("progress"), label = prog.querySelector("span");
  prog.hidden = false;
  bar.max = pages.length; bar.value = 0;
  state.batchCancel = false;
  $("#batch-ocr").disabled = $("#batch-llm").disabled = true;
  let done = 0, failed = 0;
  const errors = [];
  const queue = [...pages];
  label.textContent = `0 / ${pages.length}`;
  async function worker() {
    while (queue.length && !state.batchCancel) {
      const n = queue.shift();
      try {
        if (kind === "ocr") await runOcr(n, false); else await runLlm(n, false);
      } catch (e) {
        failed++; errors.push(`p${n + 1}: ${e.message}`);
      }
      done++;
      bar.value = done;
      label.textContent = `${done} / ${pages.length}${failed ? ` (${failed} failed)` : ""}`;
    }
  }
  await Promise.all(Array.from({ length: concurrency }, worker));
  $("#batch-ocr").disabled = $("#batch-llm").disabled = false;
  setTimeout(() => { prog.hidden = true; }, 2500);
  if (errors.length) toast(`${failed} page(s) failed. First error: ${errors[0]}`, true);
  else toast(state.batchCancel ? "Batch cancelled." : `Done: ${done} page(s).`);
}

// ------------------------------------------------------------------ report
async function showReport() {
  const dlg = $("#report-dialog");
  $("#report-body").innerHTML = "Loading…";
  dlg.showModal();
  for (const [id, q] of [["#export-native", "source=native"], ["#export-ocr", "source=ocr"],
                         ["#export-llm", "source=llm"], ["#export-llm-json", "source=llm&fmt=json"]]) {
    $(id).href = docUrl(`/export?${q}`);
  }
  const res = await api(docUrl("/report"));
  const avg = (key) => {
    const vals = res.pages.map((r) => r[key]?.cer).filter((v) => v != null);
    return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  };
  const cell = (s) => s ? `<td><span class="v ${errorRateClass(s.cer)}">${pct(s.cer)}</span></td>` : "<td>–</td>";
  let html = `<table class="report"><thead><tr>
    <th>Page</th><th>Native chars</th><th>OCR chars</th><th>LLM runs</th><th class="l">Latest LLM</th>
    <th title="CER of the native text layer vs the LLM transcription">Native vs LLM</th>
    <th title="CER of Tesseract vs the LLM transcription">OCR vs LLM</th>
    <th title="CER of Tesseract vs the native text layer">OCR vs native</th></tr></thead><tbody>`;
  for (const r of res.pages) {
    html += `<tr data-page="${r.page}"><td>${r.page + 1}</td><td>${r.native_chars}</td>
      <td>${r.ocr_chars ?? "–"}</td><td>${r.llm_runs}</td><td class="l">${esc(r.llm_model ?? "–")}</td>
      ${cell(r.native_vs_llm)}${cell(r.ocr_vs_llm)}${cell(r.native_vs_ocr)}</tr>`;
  }
  const foot = (k) => { const a = avg(k); return a == null ? "<td>–</td>" : `<td>${pct(a)}</td>`; };
  html += `</tbody><tfoot><tr><td colspan="5" class="l">Average CER</td>${foot("native_vs_llm")}${foot("ocr_vs_llm")}${foot("native_vs_ocr")}</tr></tfoot></table>`;
  $("#report-body").innerHTML = html;
  $$("#report-body tbody tr").forEach((tr) => tr.addEventListener("click", () => {
    dlg.close();
    gotoPage(+tr.dataset.page);
  }));
}

// ------------------------------------------------------------------ wiring
function wire() {
  $("#file-input").addEventListener("change", (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]); e.target.value = ""; });
  $("#doc-select").addEventListener("change", (e) => openDoc(e.target.value));
  $("#delete-doc").addEventListener("click", async () => {
    if (!state.doc || !confirm(`Delete ${state.doc.filename} and all cached OCR/LLM results?`)) return;
    await api(docUrl(), { method: "DELETE" });
    location.hash = "";
    location.reload();
  });
  $("#provider-select").addEventListener("change", () => onProviderChange());
  $("#prompt-select").addEventListener("change", onPromptChange);
  $("#prev-page").addEventListener("click", () => gotoPage(state.page - 1));
  $("#next-page").addEventListener("click", () => gotoPage(state.page + 1));
  $("#zoom-in").addEventListener("click", () => { state.fit = false; state.zoom = Math.min(4, state.zoom * 1.2); applyZoom(); });
  $("#zoom-out").addEventListener("click", () => { state.fit = false; state.zoom = Math.max(0.2, state.zoom / 1.2); applyZoom(); });
  $("#zoom-fit").addEventListener("click", () => { state.fit = true; applyZoom(); });
  window.addEventListener("resize", () => state.fit && applyZoom());

  $$(".tabs button").forEach((b) => b.addEventListener("click", () => {
    $$(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab-body").forEach((t) => { t.hidden = t.dataset.tab !== b.dataset.tab; });
  }));

  const guard = (fn) => async (...args) => { try { await fn(...args); } catch (e) { toast(e.message, true); } };
  $("#run-ocr").addEventListener("click", guard(() => runOcr(state.page, true)));
  $("#run-llm").addEventListener("click", guard(() => runLlm(state.page, false)));
  $("#rerun-llm").addEventListener("click", guard(() => runLlm(state.page, true)));
  $("#llm-result-select").addEventListener("change", (e) => showLlmResult(e.target.value));
  $("#run-compare").addEventListener("click", guard(runCompare));
  $("#search-form").addEventListener("submit", guard(runSearch));
  // the (x) clear button fires "search" with an empty value (Enter is handled by submit)
  $("#search-input").addEventListener("search", guard(() => !$("#search-input").value && runSearch()));
  $$(".search-sources input").forEach((i) => i.addEventListener("change", guard(() => state.query && runSearch())));
  $("#batch-ocr").addEventListener("click", guard(() => runBatch("ocr")));
  $("#batch-llm").addEventListener("click", guard(() => runBatch("llm")));
  $("#batch-cancel").addEventListener("click", () => { state.batchCancel = true; });
  $("#report-btn").addEventListener("click", guard(showReport));
  $("#report-close").addEventListener("click", () => $("#report-dialog").close());

  document.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName) || !state.doc) return;
    if (e.key === "ArrowRight" || e.key === "j") gotoPage(state.page + 1);
    if (e.key === "ArrowLeft" || e.key === "k") gotoPage(state.page - 1);
    if (e.key === "/") { e.preventDefault(); $("#search-input").focus(); }
  });

  // drag & drop upload
  let dragDepth = 0;
  window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; $("#drop-overlay").hidden = false; });
  window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#drop-overlay").hidden = true; } });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    e.preventDefault(); dragDepth = 0; $("#drop-overlay").hidden = true;
    const f = e.dataTransfer.files[0];
    if (f) uploadFile(f);
  });
}

(async function main() {
  wire();
  try {
    await loadConfig();
    const params = new URLSearchParams(location.hash.slice(1));
    const docId = params.get("doc");
    await loadDocList(docId);
    if (docId) await openDoc(docId, Math.max(0, (+params.get("page") || 1) - 1));
  } catch (e) {
    toast(e.message, true);
  }
})();
