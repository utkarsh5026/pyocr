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
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = label; btn.classList.add("is-busy");
  extras.forEach(([el, cls]) => el?.classList.add(cls));
  return () => {
    btn.disabled = false; btn.textContent = prev; btn.classList.remove("is-busy");
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
let _config = null; // set once /api/config has loaded (for sync helpers like logoHtml)
function getConfig() {
  _configPromise ??= api("/api/config").then((c) => { _config = c; return c; });
  return _configPromise;
}

// ------------------------------------------------------------------ brand logos
/** Logo tile for a provider / OCR engine brand key. Mirrors the _brand.html macro. */
function logoHtml(key, size = "") {
  const b = (_config?.brands || {})[key] || { name: "", color: "#64748b", text: "?", fg: "#ffffff" };
  const inner = b.icon ? `<i style="--icon:url(/static/logos/${esc(b.icon)})"></i>` : esc(b.text);
  return `<span class="logo${size ? " " + size : ""}${b.icon ? "" : " mono"}" ` +
    `style="--brand:${esc(b.color)};--brand-fg:${esc(b.fg)}" title="${esc(b.name)}" aria-hidden="true">${inner}</span>`;
}
const providerBrand = (id) => (_config?.providers || []).find((p) => p.id === id)?.brand || "";
const ocrEngineInfo = (id) => (_config?.ocr_engines || []).find((e) => e.id === (id || "tesseract"));
const ocrLabel = (id) => ocrEngineInfo(id)?.label || id;

// ------------------------------------------------------------------ OCR engines
/** The OCR engine to preselect: the last one used if it still works, else the first ready one. */
function preferredOcrEngine(cfg) {
  const saved = localStorage.getItem("pyocr.ocrEngine");
  const ready = cfg.ocr_engines.filter((e) => e.available);
  if (saved && cfg.ocr_engines.some((e) => e.id === saved && e.available)) return saved;
  return (ready[0] || cfg.ocr_engines[0]).id;
}

/** <option>s for an engine <select>; `done` = ids that already have a result (marked ✓). */
function ocrEngineOptions(cfg, done = []) {
  return cfg.ocr_engines.map((e) => {
    const note = done.includes(e.id) ? " ✓" : e.available ? "" : e.installed ? " (not configured)" : " (not installed)";
    return `<option value="${esc(e.id)}"${e.available || done.includes(e.id) ? "" : " disabled"}>${esc(e.label)}${note}</option>`;
  }).join("");
}

// ------------------------------------------------------------------ uploads
async function uploadFiles(files) {
  files = Array.from(files || []);
  if (!files.length) return;
  const uploaded = [];
  for (const [i, file] of files.entries()) {
    toast(`Uploading ${file.name}${files.length > 1 ? ` (${i + 1}/${files.length})` : ""}…`);
    const fd = new FormData();
    fd.append("file", file);
    try {
      uploaded.push(await api("/api/documents", { method: "POST", body: fd }));
    } catch (e) {
      toast(`${file.name}: ${e.message}`, true);
      await new Promise((r) => setTimeout(r, 1500));
    }
  }
  if (uploaded.length === 1) location.href = `/documents/${uploaded[0].id}`;
  else if (uploaded.length > 1) location.href = "/documents";
}

function wireUploads() {
  $$(".upload-input").forEach((input) => input.addEventListener("change", (e) => {
    uploadFiles(e.target.files);
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
async function initLlmPicker(root) {
  const cfg = await getConfig();
  const provSel = $("[data-provider]", root), modelIn = $("[data-model]", root), promptSel = $("[data-prompt]", root);
  const customBox = $("[data-custom-box]", root), customTa = $("[data-custom]", root), status = $("[data-status]", root);
  const datalist = document.getElementById(modelIn.getAttribute("list"));
  const logoEl = $("[data-provider-logo]", root);

  const providers = [...cfg.providers].sort((a, b) => b.configured - a.configured);
  provSel.innerHTML = providers.map((p) =>
    `<option value="${esc(p.id)}">${esc(p.label)}${p.configured ? "" : " (not configured)"}</option>`).join("");
  promptSel.innerHTML = cfg.prompt_modes.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("") +
    '<option value="custom">Custom prompt…</option>';

  const saved = localStorage.getItem("pyocr.provider");
  if (saved && providers.some((p) => p.id === saved)) provSel.value = saved;
  promptSel.value = localStorage.getItem("pyocr.prompt") || "verbatim";
  customTa.value = localStorage.getItem("pyocr.customPrompt") || cfg.prompt_modes[0].prompt;

  const provider = () => cfg.providers.find((p) => p.id === provSel.value);
  function syncProvider() {
    const p = provider();
    localStorage.setItem("pyocr.provider", p.id);
    if (logoEl) logoEl.innerHTML = logoHtml(p.brand);
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
  }
  // Rating of the chosen model (custom names are fine; they just get a heuristic verdict).
  const verdict = document.createElement("div");
  verdict.className = "picker-verdict small";
  status.before(verdict);
  const refreshVerdict = bindModelVerdict(modelIn, verdict, () => provSel.value);
  provSel.addEventListener("change", syncProvider);
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
      return body;
    },
    describe() {
      const p = provider();
      return `${p.label} · ${modelIn.value.trim() || p.default_model || "?"} · ${promptSel.value}`;
    },
  };
}

// ------------------------------------------------------------------ jobs
function jobHtml(job, docName) {
  const pctDone = job.total ? Math.round((job.done / job.total) * 100) : 0;
  const statusCls = { done: "ok", running: "run", queued: "run", failed: "scan", cancelled: "" }[job.status] || "";
  const dur = job.started_at ? (((job.finished_at || Date.now() / 1000) - job.started_at)).toFixed(0) + "s" : "";
  const tokens = job.kind === "llm" && (job.input_tokens || job.output_tokens)
    ? ` · ${fmtInt(job.input_tokens)} in / ${fmtInt(job.output_tokens)} out tokens` : "";
  const errors = job.errors.length
    ? `<details class="small"><summary class="err">${job.failed} page(s) failed</summary><ul>${job.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></details>` : "";
  const active = job.status === "running" || job.status === "queued";
  // Polling re-renders this markup; a negative delay tied to the clock keeps the
  // running animations (styles.css "Motion") in phase instead of restarting.
  const phase = active ? ` style="--phase:-${(Date.now() % 2800) / 1000}s"` : "";
  return `<div class="job${active ? " is-active" : ""}" data-job="${job.id}"${phase}>
    <div class="job-head">
      <span class="badge ${statusCls}">${job.status}</span>
      ${logoHtml(job.kind === "ocr" ? ocrEngineInfo(job.engine)?.brand : providerBrand(job.llm?.provider), "sm")}
      <b>${esc(job.label)}</b>
      ${docName ? `<a href="/documents/${job.doc_id}">${esc(docName)}</a>` : ""}
      <div class="spacer"></div>
      ${active ? `<button class="link" data-cancel="${job.id}">cancel</button>` : ""}
    </div>
    <progress max="${job.total}" value="${job.done}"></progress>
    <div class="small">${job.done}/${job.total} pages (${pctDone}%) · ${job.cached} from cache · ${job.failed} failed · ${dur}${tokens}
      ${job.current_pages.length ? ` · working on p${job.current_pages.map((p) => p + 1).join(", p")}` : ""}</div>
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
