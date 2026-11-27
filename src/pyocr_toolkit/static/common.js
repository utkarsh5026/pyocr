/* Shared helpers for every page. Loaded before the page-specific script. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

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
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
}

let _toastTimer;
function toast(msg, isError = false) {
  const el = $("#toast");
  if (!el) return alert(msg);
  el.textContent = msg;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    el.classList.add("leaving");  // fade out (styles.css), then hide
    _toastTimer = setTimeout(() => { el.hidden = true; }, 180);
  }, isError ? 8000 : 3000);
}

/** Replay a one-shot CSS animation class (e.g. "reveal") on an element. */
function replayAnimation(el, cls) {
  el.classList.remove(cls);
  void el.offsetWidth;  // force a reflow so removing + re-adding restarts it
  el.classList.add(cls);
  el.addEventListener("animationend", () => el.classList.remove(cls), { once: true });
}

/**
 * Show that `btn` is working: spinner + label, disabled. Returns a function that
 * restores the button. Extra elements get `cls` toggled for the same duration
 * (the workspace uses this for the page scan line and the shimmering text box).
 */
function setBusy(btn, label, extras = []) {
  const prev = btn.innerHTML;  // innerHTML, not textContent, so a button's icon survives
  btn.disabled = true; btn.textContent = label; btn.classList.add("is-busy");
  extras.forEach(([el, cls]) => el?.classList.add(cls));
  return () => {
    btn.disabled = false; btn.innerHTML = prev; btn.classList.remove("is-busy");
    extras.forEach(([el, cls]) => el?.classList.remove(cls));
  };
}

/** Wrap an async handler so errors become toasts instead of silent failures. */
const guard = (fn) => async (...args) => { try { return await fn(...args); } catch (e) { toast(e.message, true); } };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null ? "–" : (x * 100).toFixed(1) + "%");
const fmtInt = (n) => (n == null ? "–" : Number(n).toLocaleString());
function errorRateClass(rate) {
  if (rate == null) return "";
  return rate <= 0.02 ? "good" : rate <= 0.1 ? "warn" : "bad";
}
function queryRegex(q) {
  const terms = (q || "").trim().split(/\s+/).filter(Boolean).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return terms.length ? new RegExp(terms.join("\\s+"), "gi") : null;
}
/** HTML for `text` with matches of `q` wrapped in <mark>. */
function highlight(text, q) {
  const re = queryRegex(q);
  if (!re || !text) return esc(text);
  let html = "", last = 0;
  for (const m of text.matchAll(re)) {
    html += esc(text.slice(last, m.index)) + "<mark>" + esc(m[0]) + "</mark>";
    last = m.index + m[0].length;
  }
  return html + esc(text.slice(last));
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied to clipboard"); }
  catch (_) { toast("Copy failed: the browser blocked clipboard access", true); }
}

// ------------------------------------------------------------------ config
let _configPromise;
function getConfig() {
  _configPromise ??= api("/api/config");
  return _configPromise;
}

// ------------------------------------------------------------------ uploads
const isZip = (file) => /\.zip$/i.test(file.name);
const fmtSize = (n) => (n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** Workspace name ideas from the server ("adjective-scientist", led by one taken from the file names). */
async function suggestNames(filenames = [], count = 4) {
  return (await postJSON("/api/workspaces/suggest-names", { filenames, count })).names;
}

/**
 * Upload files one request at a time (so each gets its own progress line) to
 * /api/uploads, which unpacks zips. `onFile(i, status, text)` reports progress.
 */
async function sendBatch(files, workspaceId, onFile = () => {}) {
  const documents = [], skipped = [];
  for (const [i, file] of files.entries()) {
    onFile(i, "run", isZip(file) ? "unpacking…" : "uploading…");
    const fd = new FormData();
    fd.append("files", file);
    if (workspaceId) fd.append("workspace_id", workspaceId);
    try {
      const res = await api("/api/uploads", { method: "POST", body: fd });
      documents.push(...res.documents);
      skipped.push(...res.skipped);
      const n = res.documents.length;
      if (!n) onFile(i, "err", res.skipped[0]?.reason || "nothing to add");
      else if (!isZip(file)) onFile(i, "ok", "added");
      else onFile(i, "ok", plural(n, "document") + (res.skipped.length ? `, ${res.skipped.length} skipped` : ""));
    } catch (e) {
      skipped.push({ name: file.name, reason: e.message });
      onFile(i, "err", e.message);
    }
  }
  return { documents, skipped };
}

function skippedMessage(skipped) {
  if (!skipped.length) return "";
  const first = skipped.slice(0, 3).map((s) => `${s.name}: ${s.reason}`).join("; ");
  return `Skipped ${plural(skipped.length, "file")} (${first}${skipped.length > 3 ? "; …" : ""})`;
}

async function uploadFiles(files, { workspaceId } = {}) {
  files = Array.from(files || []);
  if (!files.length) return;
  workspaceId ??= $("#ws-root")?.dataset.wsId;  // on a workspace page, uploads go into it
  if (workspaceId) {
    toast(`Adding ${plural(files.length, "file")}…`);
    const { documents, skipped } = await sendBatch(files, workspaceId);
    if (skipped.length) { toast(skippedMessage(skipped), true); await new Promise((r) => setTimeout(r, 2500)); }
    if (documents.length) location.href = `/workspaces/${workspaceId}`;
    return;
  }
  if (files.length > 1 || isZip(files[0])) return openUploadDialog(files);

  const file = files[0];
  toast(`Uploading ${file.name}…`);
  const fd = new FormData();
  fd.append("file", file);
  try {
    const doc = await api("/api/documents", { method: "POST", body: fd });
    location.href = `/documents/${doc.id}`;
  } catch (e) {
    toast(`${file.name}: ${e.message}`, true);
  }
}

/** Several files or a zip: ask which workspace they belong to (with name suggestions), then upload. */
async function openUploadDialog(files) {
  const dlg = $("#upload-dialog");
  if (!dlg?.showModal) {  // very old browser: upload without a workspace
    const { skipped } = await sendBatch(files, null);
    if (skipped.length) toast(skippedMessage(skipped), true);
    location.href = "/documents";
    return;
  }
  const nameIn = $("#upload-ws-name"), chips = $("#upload-ws-suggest"), existing = $("#upload-ws-existing");
  const okBtn = $("#upload-ok"), list = $("#upload-files");
  const radios = $$("input[name=ws-mode]", dlg);
  const mode = () => $("input[name=ws-mode]:checked", dlg).value;
  const filenames = files.map((f) => f.name);
  let done = null;  // set once uploads finish: where "Continue" goes

  const zips = files.filter(isZip).length;
  const total = files.reduce((n, f) => n + f.size, 0);
  $("#upload-summary").textContent = `${plural(files.length, "file")} · ${fmtSize(total)}` +
    (zips ? ` · ${zips === 1 ? "the zip" : `${zips} zips`} will be unpacked` : "");
  list.innerHTML = files.map((f) => `<li><span title="${esc(f.name)}">${esc(f.name)}</span><span>${esc(fmtSize(f.size))}</span></li>`).join("");
  okBtn.disabled = false;
  okBtn.textContent = "Upload";
  radios.forEach((r) => { r.disabled = false; });
  $("input[name=ws-mode][value=new]", dlg).checked = true;

  const markChips = () => $$("button", chips).forEach((b) => b.setAttribute("aria-pressed", String(b.textContent === nameIn.value.trim())));
  async function loadSuggestions(fill) {
    const names = await suggestNames(filenames);
    chips.innerHTML = names.map((n) => `<button type="button">${esc(n)}</button>`).join("");
    if (fill || !nameIn.value.trim()) nameIn.value = names[0] || "";
    markChips();
  }
  chips.onclick = (e) => {
    const b = e.target.closest("button");
    if (b) { nameIn.value = b.textContent; markChips(); nameIn.focus(); }
  };
  nameIn.oninput = markChips;
  $("#upload-ws-shuffle").onclick = guard(() => loadSuggestions(false));

  const syncMode = () => $$("[data-ws-mode]", dlg).forEach((el) => { el.hidden = el.dataset.wsMode !== mode(); });
  radios.forEach((r) => { r.onchange = syncMode; });
  syncMode();

  nameIn.value = "";
  guard(() => loadSuggestions(true))();
  guard(async () => {
    const spaces = await api("/api/workspaces");
    existing.innerHTML = spaces.map((w) =>
      `<option value="${esc(w.id)}">${esc(w.name)} (${esc(plural(w.document_count, "doc"))})</option>`).join("");
    const radio = $("input[name=ws-mode][value=existing]", dlg);
    radio.disabled = !spaces.length;
    radio.parentElement.title = spaces.length ? "" : "No workspaces yet";
  })();

  $("#upload-cancel").onclick = () => { if (done) location.href = done; else dlg.close(); };
  $("#upload-form").onsubmit = guard(async (e) => {
    e.preventDefault();
    if (done) { location.href = done; return; }
    let wsId = null;
    if (mode() === "new") {
      const name = nameIn.value.trim();
      if (!name) { nameIn.focus(); throw new Error("Give the workspace a name, or pick a suggestion"); }
      wsId = (await postJSON("/api/workspaces", { name })).id;
    } else if (mode() === "existing") {
      wsId = existing.value;
    }
    const restore = setBusy(okBtn, "Uploading…");
    radios.forEach((r) => { r.disabled = true; });
    const items = $$("li", list);
    const { documents, skipped } = await sendBatch(files, wsId, (i, status, text) => {
      items[i].className = status;
      items[i].lastElementChild.textContent = text;
      if (status === "run") items[i].scrollIntoView({ block: "nearest" });
    });
    restore();
    done = wsId ? `/workspaces/${wsId}` : documents.length === 1 ? `/documents/${documents[0].id}` : "/documents";
    if (!skipped.length) { location.href = done; return; }
    // stay open so the skipped files and their reasons can be read
    $("#upload-summary").textContent = `Added ${plural(documents.length, "document")}. ${skippedMessage(skipped)}`;
    okBtn.textContent = "Continue";
  });

  dlg.showModal();
}

function wireUploads() {
  $$(".upload-input").forEach((input) => input.addEventListener("change", (e) => {
    uploadFiles(e.target.files, { workspaceId: e.target.dataset.workspace });
    e.target.value = "";
  }));
  const overlay = $("#drop-overlay");
  let depth = 0;
  const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
  window.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth++; overlay.hidden = false; });
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; overlay.hidden = true; } });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; overlay.hidden = true;
    uploadFiles(e.dataTransfer.files);
  });
}

// ------------------------------------------------------------------ model ratings
const RATING_CLS = { excellent: "excellent", good: "ok", fair: "fair", unsuitable: "bad", unrated: "" };

/** Badge for a catalog rating (see llm/catalog.py). */
function ratingBadge(m) {
  return `<span class="badge ${RATING_CLS[m.rating] || ""}" title="${esc(m.note || "")}">${esc(m.rating_label)}</span>`;
}

function fmtPrice(m) {
  if (m.price_in == null) return "";
  return `$${m.price_in} / $${m.price_out} per 1M tokens`;
}

const _verdictCache = new Map();
/** Ask the server how well `name` suits transcription (works for custom names too). */
function lookupModel(providerId, name) {
  const key = `${providerId}|${name}`;
  if (!_verdictCache.has(key)) {
    const qs = `provider=${encodeURIComponent(providerId)}&name=${encodeURIComponent(name)}`;
    _verdictCache.set(key, api(`/api/models/lookup?${qs}`).catch(() => null));
  }
  return _verdictCache.get(key);
}

/** One-line HTML verdict for a model name, e.g. under a model input. */
function verdictHtml(m) {
  if (!m) return "";
  const which = m.match && !m.exact ? ` <span class="muted">(looks like <code>${esc(m.match)}</code>)</span>` : "";
  const status = m.status && m.status !== "current" ? ` <span class="badge">${esc(m.status)}</span>` : "";
  const custom = m.match ? "" : ' <span class="muted">custom name</span>';
  const price = fmtPrice(m) ? ` <span class="muted">· ${esc(fmtPrice(m))} ·</span>` : "";
  return `${ratingBadge(m)}${status}${which}${custom}${price} <span class="muted">${esc(m.note || "")}</span>`;
}

/** Keep `out` showing the verdict for whatever is typed into `input` (or its placeholder). */
function bindModelVerdict(input, out, providerId) {
  let timer;
  const current = () => input.value.trim() || input.placeholder.trim();
  const update = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const name = current();
      const pid = typeof providerId === "function" ? providerId() : providerId;
      if (!name || !pid) { out.innerHTML = ""; return; }
      const m = await lookupModel(pid, name);
      if (current() === name) out.innerHTML = verdictHtml(m);
    }, 200);
  };
  input.addEventListener("input", update);
  input.addEventListener("change", update);
  update();
  return update;
}

/** Fill a <datalist> with a provider's model options, labelled with their rating. */
function fillModelDatalist(datalist, options) {
  datalist.innerHTML = options.map((m) =>
    `<option value="${esc(m.name)}" label="${esc(m.rating_label + (m.label ? " · " + m.label : ""))}">`).join("");
}

// ------------------------------------------------------------------ LLM picker
/**
 * Turn a `[data-llm-picker]` block into a working provider/model/prompt picker.
 * The choice is remembered in localStorage and shared between pages.
 */
async function initLlmPicker(root, opts = {}) {
  const cfg = await getConfig();
  const provSel = $("[data-provider]", root), modelIn = $("[data-model]", root), promptSel = $("[data-prompt]", root);
  const customBox = $("[data-custom-box]", root), customTa = $("[data-custom]", root), status = $("[data-status]", root);
  const datalist = document.getElementById(modelIn.getAttribute("list"));

  const providers = [...cfg.providers].sort((a, b) => b.configured - a.configured);
  provSel.innerHTML = providers.map((p) =>
    `<option value="${esc(p.id)}">${esc(p.label)}${p.configured ? "" : " (not configured)"}</option>`).join("");
  promptSel.innerHTML = cfg.prompt_modes.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("") +
    '<option value="extract">Structured data (fields → JSON)…</option>' +
    '<option value="custom">Custom prompt…</option>';
  const schema = initSchemaPicker(root, cfg.extract_presets || [],
    () => ({ provider: provSel.value, model: modelIn.value.trim() || null }), opts.page);

  const saved = localStorage.getItem("pyocr.provider");
  if (saved && providers.some((p) => p.id === saved)) provSel.value = saved;
  promptSel.value = localStorage.getItem("pyocr.prompt") || "verbatim";
  customTa.value = localStorage.getItem("pyocr.customPrompt") || cfg.prompt_modes[0].prompt;

  const provider = () => cfg.providers.find((p) => p.id === provSel.value);
  function syncProvider() {
    const p = provider();
    localStorage.setItem("pyocr.provider", p.id);
    modelIn.value = localStorage.getItem(`pyocr.model.${p.id}`) || p.default_model || "";
    modelIn.placeholder = p.model_hint;
    fillModelDatalist(datalist, p.model_options || []);
    status.innerHTML = p.configured ? "" :
      `<span class="warn">Not configured: set ${esc(p.missing.join(", "))}. See <a href="/providers">Providers</a>.</span>`;
    refreshVerdict();
  }
  function syncPrompt() {
    localStorage.setItem("pyocr.prompt", promptSel.value);
    customBox.hidden = promptSel.value !== "custom";
    if (!customBox.hidden) customBox.open = true;
    schema.show(promptSel.value === "extract");
  }
  // Rating of the chosen model (custom names are fine; they just get a heuristic verdict).
  const verdict = document.createElement("div");
  verdict.className = "picker-verdict small";
  status.before(verdict);
  const refreshVerdict = bindModelVerdict(modelIn, verdict, () => provSel.value);
  provSel.addEventListener("change", () => { syncProvider(); schema.recheck(); });
  modelIn.addEventListener("change", () => schema.recheck());
  promptSel.addEventListener("change", syncPrompt);
  modelIn.addEventListener("change", () => localStorage.setItem(`pyocr.model.${provider().id}`, modelIn.value.trim()));
  customTa.addEventListener("change", () => localStorage.setItem("pyocr.customPrompt", customTa.value));
  syncProvider();
  syncPrompt();

  return {
    provider,
    /** Request body for the LLM endpoints; throws if the provider isn't usable. */
    spec(force = false) {
      const p = provider();
      if (!p.configured) throw new Error(`${p.label} is not configured: set ${p.missing.join(", ")}`);
      const body = { provider: p.id, model: modelIn.value.trim() || null, prompt_mode: promptSel.value, force };
      if (body.prompt_mode === "custom") body.custom_prompt = customTa.value;
      if (body.prompt_mode === "extract") body.extract_schema = schema.value();
      return body;
    },
    /** True when the prompt fills a schema (structured data) instead of transcribing. */
    isExtract: () => promptSel.value === "extract",
    describe() {
      const p = provider();
      const mode = promptSel.value === "extract" ? `extract: ${schema.label()}` : promptSel.value;
      return `${p.label} · ${modelIn.value.trim() || p.default_model || "?"} · ${mode}`;
    },
  };
}

// ------------------------------------------------------------------ schema editor
const _pretty = (v) => JSON.stringify(v, null, 2);
const _store = {
  get: (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (_) { /* private mode */ } },
};
/** One element from an HTML string (the caller escapes any data in it). */
function _h(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

/** JSON as HTML with token classes, for the editor's highlight layer. */
function highlightJson(text) {
  const re = /("(?:[^"\\\n]|\\.)*"?)(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b|([{}[\],:])/g;
  let out = "", last = 0, m;
  while ((m = re.exec(text))) {
    out += esc(text.slice(last, m.index));
    if (m[1] !== undefined) {
      out += m[2] !== undefined ? `<span class="tk-key">${esc(m[1])}</span>${esc(m[2])}` : `<span class="tk-str">${esc(m[1])}</span>`;
    } else if (m[3]) out += `<span class="tk-num">${m[3]}</span>`;
    else if (m[4]) out += `<span class="tk-lit">${m[4]}</span>`;
    else out += `<span class="tk-p">${esc(m[5])}</span>`;
    last = re.lastIndex;
  }
  return out + esc(text.slice(last));
}

/**
 * Turn a <textarea> into a small JSON editor: syntax colours, line numbers, a marked error
 * line, Tab to indent and Enter keeping the indentation. No dependencies: the textarea stays
 * the real input (transparent text) over a highlighted <pre>.
 */
function initJsonEditor(ta) {
  const wrap = _h('<div class="code-editor"><div class="ce-gutter" aria-hidden="true"></div>' +
    '<div class="ce-body"><pre class="ce-hl" aria-hidden="true"></pre><div class="ce-errline" hidden></div></div></div>');
  ta.before(wrap);
  $(".ce-body", wrap).append(ta);
  ta.classList.add("ce-input");
  const gutter = $(".ce-gutter", wrap), hl = $(".ce-hl", wrap), errline = $(".ce-errline", wrap);
  let errorLine = null, escaped = false;

  function sync() {
    hl.scrollTop = ta.scrollTop;
    hl.scrollLeft = ta.scrollLeft;
    gutter.scrollTop = ta.scrollTop;
    errline.hidden = !errorLine;
    if (errorLine) errline.style.top = `calc(var(--ce-pad) + ${errorLine - 1} * var(--ce-lh) - ${ta.scrollTop}px)`;
  }
  function render() {
    hl.innerHTML = highlightJson(ta.value) + "\n";  // the extra line keeps the last line's height
    const lines = ta.value.split("\n").length;
    gutter.innerHTML = Array.from({ length: lines }, (_, i) =>
      `<div${i + 1 === errorLine ? ' class="err"' : ""}>${i + 1}</div>`).join("") + "<div>&nbsp;</div>";
    sync();
  }
  function insert(text) {
    if (!document.execCommand("insertText", false, text)) {  // keeps undo where supported
      ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }
  ta.addEventListener("keydown", (e) => {
    const plain = !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey;
    if (e.key === "Escape") { escaped = true; return; }
    if (e.key === "Tab" && plain && !escaped) {
      e.preventDefault();
      insert("  ");
    } else if (e.key === "Enter" && plain) {
      const before = ta.value.slice(0, ta.selectionStart), after = ta.value.slice(ta.selectionEnd);
      const line = before.slice(before.lastIndexOf("\n") + 1);
      const indent = line.match(/^\s*/)[0], opens = /[[{]\s*$/.test(line);
      e.preventDefault();
      if (opens && /^\s*[\]}]/.test(after)) {  // {|} -> block with the cursor inside
        insert(`\n${indent}  \n${indent}`);
        ta.selectionStart = ta.selectionEnd = ta.selectionStart - indent.length - 1;
      } else {
        insert(`\n${indent}${opens ? "  " : ""}`);
      }
    }
    if (e.key !== "Tab") escaped = false;
  });
  ta.addEventListener("input", render);
  ta.addEventListener("scroll", sync);
  render();
  return {
    set(text) { ta.value = text; render(); },
    setError(line) { errorLine = line || null; render(); },
    /** Put the cursor at a 1-based line/column and scroll it into view. */
    goto(line, col) {
      const lines = ta.value.split("\n");
      const pos = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0) + Math.max(0, col - 1);
      ta.focus();
      ta.setSelectionRange(pos, pos);
      ta.scrollTop = Math.max(0, (line - 4) * parseFloat(getComputedStyle(ta).lineHeight || "19"));
      sync();
    },
  };
}

// --- the visual field builder. Its model: {title, description, fields: [{name, type, description,
// required, options, children}]}, converted to and from JSON Schema (the schema stays the source of truth).
const FIELD_TYPES = [
  ["string", "Text"], ["number", "Number"], ["integer", "Whole number"], ["boolean", "Yes / no"],
  ["enum", "One of a list"], ["list", "List of text"], ["table", "Table (rows of fields)"], ["object", "Group of fields"],
];
const _BUILDER_KEYS = new Set(["type", "description", "title", "default", "examples", "enum", "properties", "required",
  "items", "additionalProperties", "nullable", "anyOf"]);

function _unwrapField(node, path) {
  let n = node, nullable = false;
  if (Array.isArray(n.anyOf)) {
    const real = n.anyOf.filter((o) => !(o && o.type === "null"));
    if (real.length !== 1) throw new Error(`${path} can be one of several types`);
    nullable = real.length < n.anyOf.length;
    const { anyOf, ...rest } = n;  // eslint-disable-line no-unused-vars
    n = { ...real[0], ...rest };
  }
  if (Array.isArray(n.type)) {
    const types = n.type.filter((t) => t !== "null");
    if (types.length !== 1) throw new Error(`${path} has several types (${n.type.join(", ")})`);
    nullable = nullable || types.length < n.type.length;
    n = { ...n, type: types[0] };
  }
  if (n.nullable === true) nullable = true;
  const extra = Object.keys(n).find((k) => !_BUILDER_KEYS.has(k));
  if (extra) throw new Error(`${path || "the schema"} uses '${extra}'`);
  return { n, nullable };
}

function _fieldsFrom(obj, path) {
  const req = new Set(Array.isArray(obj.required) ? obj.required : []);
  return Object.entries(obj.properties || {}).map(([name, raw]) => {
    const p = path ? `${path}.${name}` : name;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error(`${p} isn't a field schema`);
    const { n, nullable } = _unwrapField(raw, p);
    const f = { name, type: "string", description: n.description || "", required: req.has(name) && !nullable,
      options: "", children: [] };
    if (Array.isArray(n.enum)) {
      if (!n.enum.every((v) => typeof v === "string")) throw new Error(`${p} is a choice of non-text values`);
      f.type = "enum";
      f.options = n.enum.join(", ");
    } else if (n.type === "object" || n.properties) {
      f.type = "object";
      f.children = _fieldsFrom(n, p);
    } else if (n.type === "array") {
      const items = n.items ? _unwrapField(n.items, `${p}[]`).n : { type: "string" };
      if (items.type === "object" || items.properties) {
        f.type = "table";
        f.children = _fieldsFrom(items, `${p}[]`);
      } else if (items.type === "string" && !items.enum) {
        f.type = "list";
      } else {
        throw new Error(`${p} is a list of ${items.type || "mixed values"}`);
      }
    } else if (["string", "number", "integer", "boolean"].includes(n.type)) {
      f.type = n.type;
    } else {
      throw new Error(`${p} has ${n.type ? `type '${n.type}'` : "no type"}`);
    }
    return f;
  });
}

/** Builder model from a schema; throws Error(reason) when the schema uses something the builder can't show. */
function builderFromSchema(schema) {
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) throw new Error("the schema isn't an object");
  const { n } = _unwrapField(schema, "");
  if (!(n.type === "object" || n.properties)) throw new Error("the root isn't an object");
  return { title: schema.title || "", description: schema.description || "", fields: _fieldsFrom(n, "") };
}

function builderToSchema(model) {
  const obj = (fields) => {
    const properties = {}, required = [];
    for (const f of fields) {
      const name = f.name.trim();
      if (!name || Object.hasOwn(properties, name)) continue;
      properties[name] = field(f);
      if (f.required) required.push(name);
    }
    return { type: "object", properties, ...(required.length ? { required } : {}) };
  };
  const field = (f) => {
    const d = f.description.trim() ? { description: f.description.trim() } : {};
    if (f.type === "enum") return { ...d, enum: f.options.split(",").map((s) => s.trim()).filter(Boolean) };
    if (f.type === "object") return { ...d, ...obj(f.children) };
    if (f.type === "list") return { ...d, type: "array", items: { type: "string" } };
    if (f.type === "table") return { ...d, type: "array", items: obj(f.children) };
    return { ...d, type: f.type };
  };
  return { ...(model.title.trim() ? { title: model.title.trim() } : {}),
    ...(model.description ? { description: model.description } : {}), ...obj(model.fields) };
}

const _newField = () => ({ name: "", type: "string", description: "", required: false, options: "", children: [] });

/** The "Fields" tab: rows of name / type / hint / required, nested for groups and tables. */
function initSchemaBuilder(host, onChange) {
  let model = { title: "", description: "", fields: [] };
  const changed = () => onChange(builderToSchema(model));
  // Enter in a builder input must not submit a surrounding form (the Overview's extraction form).
  host.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") e.preventDefault(); });

  function list(fields, depth, focusField) {
    const wrap = _h('<div class="sb-list"></div>');
    const rows = [];
    const validate = () => {
      const seen = new Map();
      rows.forEach(({ f, input, err }) => {
        const name = f.name.trim();
        const dup = name && seen.has(name);
        if (name && !dup) seen.set(name, f);
        err.textContent = !name ? "Give this field a name (it's left out until it has one)."
          : dup ? `Another field is already called “${name}”; only the first is used.` : "";
        err.hidden = !err.textContent;
        input.classList.toggle("invalid", !!err.textContent);
      });
    };
    if (!fields.length) wrap.append(_h(`<p class="small muted sb-empty">No ${depth ? "columns" : "fields"} yet.</p>`));
    fields.forEach((f, i) => {
      const row = _h(`<div class="sb-row">
        <div class="sb-line">
          <input class="sb-name" placeholder="${depth ? "column_name" : "field_name"}" aria-label="Field name" autocomplete="off" spellcheck="false">
          <select class="sb-type" aria-label="Type">${FIELD_TYPES.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>
          <label class="sb-req" title="Must always have a value: the model can't answer “not on page”, so it may guess. Leave off unless it's always printed.">
            <input type="checkbox"> Required</label>
          <span class="sb-actions">
            <button type="button" class="btn small ghost icon-only" data-act="up" title="Move up" aria-label="Move up">↑</button>
            <button type="button" class="btn small ghost icon-only" data-act="down" title="Move down" aria-label="Move down">↓</button>
            <button type="button" class="btn small ghost icon-only danger" data-act="del" title="Remove" aria-label="Remove">✕</button>
          </span>
        </div>
        <div class="sb-err small err" hidden></div>
        <input class="sb-desc" placeholder="Hint for the model (optional), e.g. “as printed, with currency symbol”" aria-label="Hint for the model">
        <input class="sb-options" placeholder="Allowed values, comma-separated: paid, unpaid, overdue" aria-label="Allowed values">
        <div class="sb-children"></div>
      </div>`);
      const [name, type, req, desc, opts] = ["sb-name", "sb-type", "sb-req input", "sb-desc", "sb-options"].map((c) => $(`.${c}`, row));
      name.value = f.name;
      type.value = f.type;
      req.checked = f.required;
      desc.value = f.description;
      opts.value = f.options;
      opts.hidden = f.type !== "enum";
      const nested = f.type === "object" || f.type === "table";
      if (nested) {
        const kids = $(".sb-children", row);
        kids.append(list(f.children, depth + 1, focusField));
        const add = _h(`<button type="button" class="btn small ghost sb-add">＋ Add ${f.type === "table" ? "column" : "field to the group"}</button>`);
        add.addEventListener("click", () => { const c = _newField(); f.children.push(c); render(c); changed(); });
        kids.append(add);
      } else {
        $(".sb-children", row).remove();
      }
      const err = $(".sb-err", row);
      rows.push({ f, input: name, err });
      name.addEventListener("input", () => { f.name = name.value; validate(); changed(); });
      desc.addEventListener("input", () => { f.description = desc.value; changed(); });
      opts.addEventListener("input", () => { f.options = opts.value; changed(); });
      req.addEventListener("change", () => { f.required = req.checked; changed(); });
      type.addEventListener("change", () => {
        f.type = type.value;
        if ((f.type === "object" || f.type === "table") && !f.children.length) f.children.push(_newField());
        render(f.type === "object" || f.type === "table" ? f.children[0] : null);
        changed();
      });
      row.addEventListener("click", (e) => {
        const act = e.target.closest("[data-act]")?.dataset.act;
        if (!act || e.target.closest(".sb-row") !== row) return;
        const j = act === "up" ? i - 1 : act === "down" ? i + 1 : i;
        if (act === "del") fields.splice(i, 1);
        else if (j >= 0 && j < fields.length) [fields[i], fields[j]] = [fields[j], fields[i]];
        else return;
        render();
        changed();
      });
      $('[data-act="up"]', row).disabled = i === 0;
      $('[data-act="down"]', row).disabled = i === fields.length - 1;
      if (f === focusField) requestAnimationFrame(() => name.focus());
      wrap.append(row);
    });
    validate();
    return wrap;
  }

  function render(focusField) {
    host.innerHTML = "";
    const title = _h(`<label class="sb-title"><span>Schema name <span class="muted">(optional)</span></span>
      <input placeholder="e.g. Purchase order" autocomplete="off"></label>`);
    const input = $("input", title);
    input.value = model.title;
    input.addEventListener("input", () => { model.title = input.value; changed(); });
    const add = _h('<button type="button" class="btn small sb-add">＋ Add field</button>');
    add.addEventListener("click", () => { const f = _newField(); model.fields.push(f); render(f); changed(); });
    host.append(title, list(model.fields, 0, focusField), add);
  }

  render();
  return {
    /** Show ``schema`` in the builder; throws Error(reason) if it can't be shown (the JSON tab still works). */
    load(schema) { model = builderFromSchema(schema); render(); },
  };
}

/**
 * The "Fields to extract" part of the picker: a preset, or your own schema built field by field
 * or written as JSON. Every change is checked by the server (mistakes with suggestions, what the
 * chosen provider does with the schema) and previewed as the table you'll get back.
 * ``context()`` gives the current {provider, model}; ``pageContext()`` (optional) the {doc_id, page}
 * the "describe it" drafter may show the model.
 */
function initSchemaPicker(root, presets, context, pageContext) {
  const box = $("[data-schema-box]", root), sel = $("[data-schema-preset]", root), hint = $("[data-schema-hint]", root);
  const details = $("[data-schema-edit]", root), ta = $("[data-schema]", root), summary = $("[data-schema-status]", root);
  const notesEl = $("[data-schema-notes]", root), syntax = $("[data-schema-syntax]", root);
  const fixBtn = $("[data-schema-fix]", root), formatBtn = $("[data-schema-format]", root);
  const prevTable = $("[data-preview-table]", root), prevJson = $("[data-preview-json]", root);
  const editor = initJsonEditor(ta);
  const builder = initSchemaBuilder($('[data-schema-pane="fields"]', root), (schema) => {
    editor.set(_pretty(schema));
    makeCustom();
    check();
  });
  let tab = "fields", preview = _store.get("pyocr.schemaPreview") || "table", last = null, timer, seq = 0;
  const customHint = "Your own schema. Build it field by field, or write JSON Schema.";

  sel.innerHTML = presets.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("") +
    '<option value="custom">My own fields…</option>';
  const saved = _store.get("pyocr.extractPreset");
  sel.value = saved && [...sel.options].some((o) => o.value === saved) ? saved : (presets[0]?.id || "custom");

  function makeCustom() {
    if (sel.value !== "custom") {
      sel.value = "custom";
      _store.set("pyocr.extractPreset", "custom");
      hint.textContent = customHint;
    }
    _store.set("pyocr.extractSchema", ta.value);
  }
  function showTab(name, quiet) {
    if (name === "fields") {
      let parsed;
      try { parsed = JSON.parse(ta.value); } catch (_) {
        if (!quiet) toast("The JSON doesn't parse yet: fix it (try “Fix syntax”) to edit it as fields.", true);
        name = "json";
      }
      if (parsed !== undefined) {
        try { builder.load(parsed); } catch (e) {
          if (!quiet) toast(`The field editor can't show this schema (${e.message}). Keep editing it as JSON.`, true);
          name = "json";
        }
      }
    }
    tab = name;
    _store.set("pyocr.schemaTab", name);
    $$("[data-schema-tab]", root).forEach((b) => b.classList.toggle("active", b.dataset.schemaTab === name));
    $$("[data-schema-pane]", root).forEach((p) => { p.hidden = p.dataset.schemaPane !== name; });
  }
  function sync() {
    _store.set("pyocr.extractPreset", sel.value);
    const p = presets.find((x) => x.id === sel.value);
    hint.textContent = p ? p.description : customHint;
    editor.set(p ? _pretty(p.schema) : (_store.get("pyocr.extractSchema") ||
      _pretty({ title: "", type: "object", properties: { field_name: { type: "string" } } })));
    showTab(_store.get("pyocr.schemaTab") === "json" ? "json" : "fields", true);
    if (!p) details.open = true;
    check(true);
  }
  function value() { return sel.value === "custom" ? ta.value : sel.value; }

  function check(now) {
    clearTimeout(timer);
    if (box.hidden) return;
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const r = await postJSON("/api/extract/check-schema", { extract_schema: value(), ...context() });
        if (mine === seq) show(r);
      } catch (e) {
        if (mine === seq) summary.innerHTML = `<span class="err">Couldn't check the schema: ${esc(e.message)}</span>`;
      }
    }, now ? 0 : 300);
  }
  const ICON = { ok: "✓", info: "ℹ", warn: "⚠", error: "✕" };
  function show(r) {
    last = r;
    editor.setError(r.line);
    if (r.line) {
      syntax.innerHTML = `<a href="#" data-goto class="err">Line ${r.line}, column ${r.column}</a>: ${esc(r.error)}`;
    } else {
      syntax.textContent = "";
    }
    fixBtn.hidden = !r.repaired;
    fixBtn.title = r.repaired ? `Will fix: ${r.repairs.join(", ")}` : "";
    summary.innerHTML = r.ok
      ? `<span class="good">✓</span> ${r.fields} field${r.fields === 1 ? "" : "s"} · ${esc(r.label)}` +
        (details.open ? "" : ' · <a href="#" data-open-editor>edit fields &amp; see an example</a>')
      : `<span class="err">✕ ${esc(r.line ? `The JSON has a syntax error (line ${r.line})` : r.error)}</span>` +
        (r.repaired ? " · it can be fixed automatically" : "");
    notesEl.innerHTML = (r.notes || []).map((n) =>
      `<li class="note-${esc(n.level)}"><span class="note-icon" aria-hidden="true">${ICON[n.level] || "•"}</span><span>` +
      (n.path && !n.path.startsWith("(") ? `<code>${esc(n.path)}</code> ` : "") + `${esc(n.message)}</span></li>`).join("");
    if (r.ok) {
      prevTable.innerHTML = extractionHtml({ schema: r.schema, data: r.example, schema_label: r.label }, { preview: true });
      prevJson.textContent = _pretty(r.example);
    } else {
      prevTable.innerHTML = '<p class="small muted">Fix the schema to see an example.</p>';
      prevJson.textContent = "";
    }
    showPreview(preview);
  }
  function showPreview(name) {
    preview = name;
    _store.set("pyocr.schemaPreview", name);
    $$("[data-preview]", root).forEach((b) => b.classList.toggle("active", b.dataset.preview === name));
    prevTable.hidden = name !== "table";
    prevJson.hidden = name !== "json" || !prevJson.textContent;
  }

  ta.addEventListener("input", () => { makeCustom(); check(); });
  sel.addEventListener("change", sync);
  initSchemaDrafter(root, {
    context, pageContext,
    current: () => ta.value,
    apply(schema) {
      editor.set(_pretty(schema));
      makeCustom();
      details.open = true;
      showTab("fields", true);
      check(true);
    },
  });
  $$("[data-schema-tab]", root).forEach((b) => b.addEventListener("click", () => showTab(b.dataset.schemaTab)));
  $$("[data-preview]", root).forEach((b) => b.addEventListener("click", () => showPreview(b.dataset.preview)));
  details.addEventListener("toggle", () => last && show(last));
  summary.addEventListener("click", (e) => {
    if (!e.target.closest("[data-open-editor]")) return;
    e.preventDefault();
    details.open = true;
  });
  syntax.addEventListener("click", (e) => {
    if (!e.target.closest("[data-goto]") || !last?.line) return;
    e.preventDefault();
    editor.goto(last.line, last.column);
  });
  fixBtn.addEventListener("click", () => {
    if (!last?.repaired) return;
    editor.set(last.repaired);
    makeCustom();
    toast(`Fixed: ${last.repairs.join(", ") || "formatting"}.`);
    check(true);
  });
  formatBtn.addEventListener("click", () => {
    try { editor.set(_pretty(JSON.parse(ta.value))); makeCustom(); } catch (_) {
      toast("Can't format: the JSON doesn't parse yet. Try “Fix syntax”.", true);
    }
  });
  sync();
  return {
    show(on) { box.hidden = !on; if (on) check(true); },
    /** Re-check after the provider or model changed (their notes depend on it). */
    recheck: () => check(),
    value,
    label: () => (last?.ok ? last.label : sel.options[sel.selectedIndex]?.text) || "schema",
  };
}

/**
 * "Describe what you want in plain words": a model drafts the fields (a paid call), which land in
 * the field editor for review. "Change my current fields" sends the current schema along, so a
 * draft can be refined step by step ("also add the due date").
 */
function initSchemaDrafter(root, { context, pageContext, current, apply }) {
  const box = $("[data-draft-box]", root), ta = $("[data-draft]", root), go = $("[data-draft-go]", root);
  const usePage = $("[data-draft-page]", root), keep = $("[data-draft-keep]", root), status = $("[data-draft-status]", root);
  ta.value = _store.get("pyocr.draftText") || "";
  usePage.closest("label").hidden = !pageContext;
  ta.addEventListener("input", () => _store.set("pyocr.draftText", ta.value));
  ta.addEventListener("keydown", (e) => {  // Ctrl/Cmd+Enter drafts; plain Enter must not submit a surrounding form
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go.click(); }
  });
  go.addEventListener("click", async () => {
    const description = ta.value.trim();
    if (!description) {
      status.innerHTML = '<span class="err">Describe what to extract first, e.g. “invoice number, date, and each line item with its amount”.</span>';
      ta.focus();
      return;
    }
    const body = { description, ...context(), ...(usePage.checked && pageContext ? pageContext() : {}) };
    if (keep.checked) body.current_schema = current();
    const done = setBusy(go, "Drafting…");
    const t0 = Date.now();
    const tick = setInterval(() => { go.textContent = `Drafting… ${fmtDuration((Date.now() - t0) / 1000)}`; }, 1000);
    status.innerHTML = '<span class="muted">Asking the model for fields…</span>';
    try {
      const r = await postJSON("/api/extract/draft-schema", body);
      apply(r.schema);
      keep.checked = true;  // the next description most likely refines this draft
      const n = Object.keys(r.schema.properties || {}).length;
      const tokens = r.input_tokens != null ? ` · ${fmtInt(r.input_tokens)} in / ${fmtInt(r.output_tokens)} out tokens` : "";
      status.innerHTML = `<span class="good">✓</span> Drafted ${n} field${n === 1 ? "" : "s"} with <b>${esc(r.model)}</b>` +
        `${r.used_page ? " (with this page)" : ""} in ${(r.latency_ms / 1000).toFixed(1)}s${tokens}` +
        (r.cost ? ` · ${costHtml(r.cost)}` : "") + ". Review them below, then extract." +
        (r.notes ? `<div class="muted">Model's note: ${esc(r.notes)}</div>` : "") +
        (r.warnings || []).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join("");
    } catch (e) {
      status.innerHTML = `<span class="err">${esc(e.message)}</span>`;
    } finally {
      clearInterval(tick);
      done();
    }
  });
  return { open: () => { box.open = true; ta.focus(); } };
}

// ------------------------------------------------------------------ structured data
const _fieldLabel = (k) => String(k).replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const _props = (node) => node?.properties || (node?.anyOf || []).find((o) => o.properties)?.properties;
const _items = (node) => node?.items || (node?.anyOf || []).find((o) => o.items)?.items;

function _fieldValue(v) {
  if (v === null || v === undefined || v === "") return '<span class="muted">not on page</span>';
  if (typeof v === "object") return `<code>${esc(JSON.stringify(v))}</code>`;
  return esc(String(v));
}

/**
 * HTML for one stored extraction result: a summary line, then every schema field with its value,
 * in schema order. Lists of objects become sub-tables; validation errors are shown on their field.
 */
function extractionHtml(r, opts = {}) {
  const errors = new Map();
  for (const e of r.data_errors || []) errors.set(e.path, [...(errors.get(e.path) || []), e.message]);
  const shown = new Set();
  const errHtml = (path) => {
    const list = errors.get(path);
    if (!list) return "";
    shown.add(path);
    return list.map((m) => `<div class="err small">⚠ ${esc(m)}</div>`).join("");
  };
  const rows = [];
  function walk(value, node, path, name) {
    const props = _props(node);
    if (props && (value == null || (typeof value === "object" && !Array.isArray(value)))) {
      if (path && errors.has(path)) rows.push(`<tr class="has-err"><td class="k">${esc(name)}</td><td class="v">${errHtml(path)}</td></tr>`);
      for (const [k, sub] of Object.entries(props)) {
        walk(value?.[k], sub, path ? `${path}.${k}` : k, name ? `${name} › ${_fieldLabel(k)}` : _fieldLabel(k));
      }
      return;
    }
    const itemProps = _props(_items(node));
    let cell;
    if (Array.isArray(value) && value.length && itemProps) {
      const cols = Object.keys(itemProps);
      const body = value.map((item, i) => "<tr>" + cols.map((c) => {
        const p = `${path}[${i}].${c}`;
        return `<td${errors.has(p) ? ' class="has-err"' : ""}>${_fieldValue(item?.[c])}${errHtml(p)}</td>`;
      }).join("") + "</tr>").join("");
      cell = `<table class="table compact sub"><thead><tr>${cols.map((c) => `<th>${esc(_fieldLabel(c))}</th>`).join("")}</tr></thead>` +
        `<tbody>${body}</tbody></table>`;
    } else if (Array.isArray(value)) {
      cell = value.length ? value.map((v) => `<div>${_fieldValue(v)}</div>`).join("") : '<span class="muted">none on page</span>';
    } else {
      cell = _fieldValue(value);
    }
    const bad = [...errors.keys()].some((p) => p === path || p.startsWith(`${path}[`));
    rows.push(`<tr${bad ? ' class="has-err"' : ""}><td class="k">${esc(name || "Value")}</td><td class="v">${cell}${errHtml(path)}</td></tr>`);
  }
  if (r.data != null) walk(r.data, r.schema || {}, "", "");
  const leftover = [...errors.entries()].filter(([p]) => !shown.has(p) && ![...shown].some((s) => s.startsWith(p)))
    .flatMap(([p, ms]) => ms.map((m) => `<div class="err small">⚠ ${p === "(root)" || p === "(reply)" ? "" : `<b>${esc(p)}</b>: `}${esc(m)}</div>`));
  const nErr = (r.data_errors || []).length;
  const verdict = r.data == null ? '<span class="badge bad">could not read the reply</span>'
    : nErr ? `<span class="badge bad">${nErr} schema problem${nErr === 1 ? "" : "s"}</span>`
      : '<span class="badge ok">matches the schema</span>';
  const head = opts.preview ? "" : `<div class="extract-head"><b>${esc(r.schema_label || "Structured data")}</b>` +
    (r.fields_total ? `<span class="muted">${r.fields_filled} of ${r.fields_total} fields found</span>` : "") + verdict + "</div>";
  return head + leftover.join("") +
    (rows.length ? `<table class="table compact extract-table"><tbody>${rows.join("")}</tbody></table>` : "");
}

// ------------------------------------------------------------------ image preprocessing
// The spec chosen in "Adjust image" is remembered per document, so the Pages view and the
// overview's batch extraction use the same one (and therefore the same cache key on every page).
const preprocessStore = {
  get: (docId) => { try { return localStorage.getItem(`pyocr.preprocess.${docId}`) || ""; } catch (_) { return ""; } },
  set: (docId, spec) => {
    try {
      if (spec) localStorage.setItem(`pyocr.preprocess.${docId}`, spec);
      else localStorage.removeItem(`pyocr.preprocess.${docId}`);
    } catch (_) { /* private mode */ }
  },
};

// ------------------------------------------------------------------ jobs
/** 7 → "7s", 95 → "1m 35s", 4000 → "1h 6m". */
function fmtDuration(sec) {
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** $0.0042 / $1.23: more digits for the sub-cent amounts a single page costs. */
function fmtUSD(x) {
  if (x === 0) return "$0";
  return x < 0.01 ? (x < 0.0001 ? "<$0.0001" : `$${x.toFixed(4)}`) : `$${x.toFixed(2)}`;
}
/** ₹0.39 / ₹1,23,456.78 (Indian digit grouping). */
function fmtINR(x) {
  if (x === 0) return "₹0";
  if (x < 0.01) return "<₹0.01";
  return "₹" + x.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
/** "$0.42 (₹38.85)" for a `cost` object from the API (Services.cost / Job.cost), with the basis in a tooltip. */
function costHtml(cost) {
  if (!cost) return "";
  const tip = `List price for ${cost.price_model}: $${cost.price_in} in / $${cost.price_out} out per 1M tokens, ` +
    `at ₹${cost.usd_inr_rate} per $ (USD_INR_RATE). Excludes taxes such as GST and batch/caching discounts; ` +
    `a self-hosted model costs nothing.`;
  return `<span class="cost" title="${esc(tip)}">${fmtUSD(cost.usd)} (${fmtINR(cost.inr)})</span>`;
}

function jobHtml(job, docName) {
  const pctDone = job.total ? Math.round((job.done / job.total) * 100) : 0;
  const ok = job.done - job.failed;
  // "done" with some failed pages is a partial success: amber, not green.
  const statusCls = job.status === "done" && job.failed ? "scan"
    : { done: "ok", running: "run", queued: "run", failed: "bad", cancelled: "" }[job.status] || "";
  const status = job.status === "done" && job.failed ? "done with errors" : job.status;
  const dur = job.started_at ? fmtDuration((job.finished_at || Date.now() / 1000) - job.started_at) : "";
  const rate = job.seconds_per_page != null ? ` · ~${fmtDuration(job.seconds_per_page)}/page` : "";
  const eta = job.eta_seconds != null ? ` · ~${fmtDuration(job.eta_seconds)} left` : "";
  const tokens = job.kind === "llm" && (job.input_tokens || job.output_tokens)
    ? ` · ${fmtInt(job.input_tokens)} in / ${fmtInt(job.output_tokens)} out tokens` : "";
  const c = job.cost;
  const projected = c && c.projected_usd != null
    ? ` · <span class="cost" title="Assumes the remaining pages cost what the pages so far did; cached pages make it lower.">` +
      `~${fmtUSD(c.projected_usd)} (${fmtINR(c.projected_inr)}) for the whole job</span>` : "";
  const cost = c ? ` · ${costHtml(c)}${projected}`
    : job.kind === "llm" ? ` · <span title="This model isn't in the catalog, so no price is known.">cost unknown</span>` : "";
  const failed = job.failed ? `<span class="err">${job.failed} failed</span>` : "0 failed";
  const pages = (ps) => "p" + ps.map((p) => p + 1).join(", p");
  const working = job.current_pages.length ? ` · working on ${pages(job.current_pages)}` : "";
  const waiting = (job.waiting_pages || []).length ? ` · waiting for a model slot: ${pages(job.waiting_pages)}` : "";
  const errors = job.errors.length
    ? `<details class="small"><summary class="err">${job.failed} page(s) failed</summary><ul>${job.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></details>` : "";
  const active = job.status === "running" || job.status === "queued";
  const w = (n) => (job.total ? (n / job.total) * 100 : 0).toFixed(2);
  // Polling re-renders this markup; a negative delay tied to the clock keeps the
  // running animations (styles.css "Motion") in phase instead of restarting.
  const phase = active ? ` style="--phase:-${(Date.now() % 2800) / 1000}s"` : "";
  return `<div class="job${active ? " is-active" : ""}" data-job="${job.id}"${phase}>
    <div class="job-head">
      <span class="badge ${statusCls}">${status}</span>
      <b>${esc(job.label)}</b>
      ${docName ? `<a href="/documents/${job.doc_id}">${esc(docName)}</a>` : ""}
      <div class="spacer"></div>
      ${active ? `<button class="link" data-cancel="${job.id}">cancel</button>` : ""}
    </div>
    <div class="jobbar" role="progressbar" aria-valuemin="0" aria-valuemax="${job.total}" aria-valuenow="${job.done}"
      aria-valuetext="${job.done} of ${job.total} pages, ${job.failed} failed">
      <span class="jobbar-ok" style="width:${w(ok)}%"></span><span class="jobbar-fail" style="width:${w(job.failed)}%"></span>
    </div>
    <div class="small">${job.done}/${job.total} pages (${pctDone}%) · ${job.cached} from cache · ${failed} · ${dur}${rate}${eta}${tokens}${cost}${working}${waiting}</div>
    ${errors}
  </div>`;
}

/** Poll a job until it finishes, calling onUpdate(job) each time. */
async function watchJob(jobId, onUpdate, intervalMs = 1000) {
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    onUpdate(job);
    if (!["running", "queued"].includes(job.status)) return job;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

document.addEventListener("click", guard(async (e) => {
  const btn = e.target.closest("[data-cancel]");
  if (btn) {
    await postJSON(`/api/jobs/${btn.dataset.cancel}/cancel`);
    toast("Cancelling…");
  }
}));

wireUploads();

// ------------------------------------------------------------------ theme
/* The inline script in base.html applies the saved theme before first paint;
 * this wires the toggle button (system -> light -> dark -> system ...) and keeps
 * "system" in sync if the OS switches between light and dark while the page is open. */
(function wireTheme() {
  const root = document.documentElement;
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const labels = { system: "Theme: match system", light: "Theme: light", dark: "Theme: dark" };
  function apply(pref, animate) {
    if (animate) {
      root.classList.add("theme-switching");
      setTimeout(() => root.classList.remove("theme-switching"), 250);
    }
    root.dataset.themePref = pref;
    root.dataset.theme = pref === "dark" || (pref === "system" && media.matches) ? "dark" : "light";
    const btn = $("#theme-toggle");
    if (btn) btn.title = `${labels[pref]} (click to change)`;
  }
  const order = ["system", "light", "dark"];
  $("#theme-toggle")?.addEventListener("click", () => {
    const next = order[(order.indexOf(root.dataset.themePref) + 1) % order.length];
    try { localStorage.setItem("pyocr.theme", next); } catch (_) { /* private mode */ }
    apply(next, true);
    toast(labels[next]);
  });
  media.addEventListener("change", () => { if (root.dataset.themePref === "system") apply("system", true); });
  apply(root.dataset.themePref || "system", false);
})();

// ------------------------------------------------------------------ diff
/** HTML for a word diff from the compare API. Leading line breaks stay outside
 *  the coloured spans so they don't leave a highlighted sliver at line ends. */
function renderDiff(segments) {
  const lead = (t) => t.match(/^\n*/)[0];
  const wrap = (text, cls, title) => `${lead(text)} <span class="${cls}" title="${title}">${esc(text.slice(lead(text).length))}</span> `;
  return segments.map((s) => {
    switch (s.op) {
      case "equal": return esc(s.ref);
      case "delete": return wrap(s.ref, "del", "in reference only");
      case "insert": return wrap(s.hyp, "ins", "in candidate only");
      default: {
        const l = lead(s.ref).length >= lead(s.hyp).length ? lead(s.ref) : lead(s.hyp);
        return `${l} <span class="rep"><s>${esc(s.ref.trimStart())}</s>→<ins>${esc(s.hyp.trimStart())}</ins></span> `;
      }
    }
  }).join(" ");
}
