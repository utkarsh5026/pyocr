/* Document overview: start background extractions, follow jobs, page grid. */
"use strict";

const docRoot = $("#doc-root");
const DOC_ID = docRoot.dataset.docId;
const PAGE_COUNT = +docRoot.dataset.pageCount;
let picker;

const kind = () => $("#extract-form input[name=kind]:checked").value;

// ------------------------------------------------------------- page selection
/*
 * The "Pages" text box is the source of truth (it's what the API gets: 1-based ranges
 * like "1-3, 7"). The page map and the preset chips read and write it.
 */
const pagesIn = $("#ex-pages");
const pageMap = $("#page-map");
let pageInfo = [];      // from /api/documents/{id}/pages, once loaded
let lastClicked = null; // 0-based anchor for shift-click ranges

/** Same rules as service.parse_pages: '1-3, 7' -> 0-based pages; empty or 'all' -> every page. */
function parsePages(spec) {
  const all = Array.from({ length: PAGE_COUNT }, (_, i) => i);
  spec = spec.trim();
  if (!spec || spec.toLowerCase() === "all") return { pages: all, all: true };
  const set = new Set();
  let beyond = false;
  for (let part of spec.split(",")) {
    part = part.trim();
    if (!part) continue;
    const m = part.match(/^(\d+)\s*(?:-\s*(\d+))?$/);
    if (!m) return { error: `“${part}” isn't a page or range (use e.g. 1-3, 7)` };
    const a = +m[1], b = m[2] === undefined ? a : +m[2];
    if (a > b) return { error: `“${part}” runs backwards; did you mean ${b}-${a}?` };
    if (b > PAGE_COUNT) beyond = true;
    for (let i = Math.max(a, 1); i <= Math.min(b, PAGE_COUNT); i++) set.add(i - 1);
  }
  if (!set.size) return { error: `No pages selected; this document has ${plural(PAGE_COUNT, "page")}` };
  const pages = [...set].sort((x, y) => x - y);
  return { pages, all: pages.length === PAGE_COUNT, beyond };
}

/** 0-based pages -> '1-3, 7' (empty when every page is selected). */
function formatPages(pages) {
  if (pages.length === PAGE_COUNT) return "";
  const out = [];
  for (let i = 0; i < pages.length; i++) {
    let j = i;
    while (j + 1 < pages.length && pages[j + 1] === pages[j] + 1) j++;
    out.push(j > i ? `${pages[i] + 1}-${pages[j] + 1}` : `${pages[i] + 1}`);
    i = j;
  }
  return out.join(", ");
}

const selection = () => parsePages(pagesIn.value);
/** Page already has text from the chosen engine (any LLM result counts for LLM). */
const isDone = (n) => !!pageInfo[n] && (kind() === "ocr" ? pageInfo[n].has_ocr_cache : pageInfo[n].llm_results > 0);

function setPages(pages) {
  pagesIn.value = formatPages(pages);
  updateForm();
}

function renderPageMap() {
  const chosen = new Set(selection().pages || []);
  if (pageMap.childElementCount !== PAGE_COUNT) {
    pageMap.classList.toggle("compact", PAGE_COUNT > 120);
    pageMap.innerHTML = Array.from({ length: PAGE_COUNT }, (_, n) =>
      `<button type="button" tabindex="-1" data-n="${n}">${n + 1}</button>`).join("");
  }
  const engine = kind() === "ocr" ? "OCR" : "LLM";
  $("#legend-done").textContent = `has ${engine} text`;
  for (const btn of pageMap.children) {
    const n = +btn.dataset.n, info = pageInfo[n];
    btn.setAttribute("aria-pressed", String(chosen.has(n)));
    btn.classList.toggle("scan", !!info?.looks_scanned);
    btn.classList.toggle("done", isDone(n));
    btn.title = [`Page ${n + 1}`, info?.looks_scanned && "scan", isDone(n) && `has ${engine} text`]
      .filter(Boolean).join(" · ");
  }
}

function presetPages(name) {
  const all = Array.from({ length: PAGE_COUNT }, (_, i) => i);
  if (name === "first") return all.slice(0, 5);
  if (name === "scans") return all.filter((n) => pageInfo[n]?.looks_scanned);
  if (name === "missing") return all.filter((n) => !isDone(n));
  return all;
}

function renderPresets() {
  const current = (selection().pages || []).join(",");
  const engine = kind() === "ocr" ? "OCR" : "LLM text";
  for (const btn of $$("#page-presets button")) {
    const name = btn.dataset.preset, pages = presetPages(name);
    if (name === "first") btn.hidden = PAGE_COUNT <= 5;
    if (name === "scans") btn.hidden = !pages.length || pages.length === PAGE_COUNT;
    if (name === "missing") {
      btn.hidden = !pageInfo.length || !pages.length || pages.length === PAGE_COUNT;
      btn.textContent = `No ${engine} yet (${pages.length})`;
    }
    btn.classList.toggle("on", pages.length > 0 && pages.join(",") === current);
  }
}

pageMap.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-n]");
  if (!btn) return;
  const n = +btn.dataset.n, sel = selection();
  // an invalid box starts from nothing, so a click always does something visible
  const chosen = new Set(sel.error ? [] : sel.pages);
  const on = !chosen.has(n);
  const [from, to] = e.shiftKey && lastClicked !== null ? [Math.min(lastClicked, n), Math.max(lastClicked, n)] : [n, n];
  for (let i = from; i <= to; i++) on ? chosen.add(i) : chosen.delete(i);
  lastClicked = n;
  if (!chosen.size) return toast("Select at least one page", true);
  setPages([...chosen].sort((x, y) => x - y));
});
$("#page-presets").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-preset]");
  if (btn) setPages(presetPages(btn.dataset.preset));
});

// ------------------------------------------------------- pages per request
// The preset buttons and the "Other" box both set one value; "Other" stays empty while a preset is chosen.
const perRequestIn = $("#extract-form input[name=per_request]");
let perRequestN = 1;
/** Pages per LLM request (1 = one page per request). */
const perRequest = () => perRequestN;

function setPerRequest(value, typed = false) {
  perRequestN = Math.max(1, Math.min(+perRequestIn.max || 20, Math.floor(+value) || 1));
  let preset = false;
  for (const r of $$("#per-request-presets input")) {
    r.checked = +r.value === perRequestN;
    preset ||= r.checked;
  }
  if (!typed) perRequestIn.value = preset ? "" : String(perRequestN);
  $(".per-request-other").classList.toggle("on", !preset);
  updateForm();
}
$("#per-request-presets").addEventListener("change", (e) => setPerRequest(e.target.value));
perRequestIn.addEventListener("input", () => {
  const typed = perRequestIn.value.trim();
  // clearing the box goes back to the highlighted preset (or 1)
  const preset = $("#per-request-presets input:checked");
  setPerRequest(typed || (preset ? preset.value : 1), !!typed);
});
perRequestIn.addEventListener("change", () => setPerRequest(perRequestN)); // tidy the box on blur

function updateForm() {
  $("#llm-options").hidden = kind() !== "llm";
  const sel = selection();
  const summary = $("#pages-summary");
  pagesIn.classList.toggle("invalid", !!sel.error);
  summary.classList.toggle("err", !!sel.error);
  if (sel.error) summary.textContent = sel.error;
  else summary.innerHTML = (sel.all ? `All <b>${PAGE_COUNT}</b> page${PAGE_COUNT === 1 ? "" : "s"}`
    : `<b>${sel.pages.length}</b> of ${plural(PAGE_COUNT, "page")}`) +
    (sel.beyond ? ` · pages past ${PAGE_COUNT} ignored` : "");
  renderPageMap();
  renderPresets();

  const count = sel.error ? 0 : sel.pages.length;
  const n = perRequest();
  const requests = Math.ceil(count / n);
  const last = count % n;
  const reqNote = $("#per-request-summary");
  reqNote.innerHTML = !count ? "" : n === 1 ? "one request per page"
    : `up to <b>${plural(requests, "request")}</b>` + (requests > 1 && last ? ` (last one ${plural(last, "page")})` : "");
  reqNote.title = n > 1 ? "Pages already transcribed with this setup are skipped first, so there may be fewer." : "";

  const what = sel.error ? "the selected pages" : sel.all ? `all ${plural(PAGE_COUNT, "page")}` : plural(count, "page");
  $("#extract-hint").textContent = kind() === "llm" && picker
    ? `Sends ${what} as images to ${picker.describe()}${n > 1 ? `, ${n} pages per request` : ""}`
    : `Runs locally on ${what}; about 1–5 s per page`;
}

async function loadPages() {
  const pages = await api(`/api/documents/${DOC_ID}/pages`);
  pageInfo = pages;
  updateForm();
  $("#page-grid").innerHTML = pages.map((p, n) => `
    <a class="page-tile" href="/documents/${DOC_ID}/pages/${n + 1}">
      <img loading="lazy" src="/api/documents/${DOC_ID}/pages/${n}/image?dpi=50" alt="Page ${n + 1}">
      <div class="tile-foot"><b>${n + 1}</b>
        ${p.looks_scanned ? '<span class="badge scan">scan</span>' : `<span class="badge">${p.native_chars} ch</span>`}
        ${p.has_ocr_cache ? '<span class="badge ok">OCR</span>' : ""}
        ${p.llm_results ? `<span class="badge ok">LLM${p.llm_results > 1 ? "×" + p.llm_results : ""}</span>` : ""}
      </div>
    </a>`).join("");
}

async function refreshJobs() {
  const jobs = await api(`/api/jobs?doc_id=${DOC_ID}`);
  if (jobs.length) $("#job-list").innerHTML = jobs.map((j) => jobHtml(j)).join("");
  return jobs;
}

async function follow(jobId) {
  const job = await watchJob(jobId, async (j) => {
    const el = $(`.job[data-job="${j.id}"]`);
    if (el) el.outerHTML = jobHtml(j); else await refreshJobs();
  });
  toast(`Extraction ${job.status}: ${job.done - job.failed}/${job.total} pages ok`, job.failed > 0);
  // reload so the sources table and page badges show the new results
  setTimeout(() => location.reload(), 1500);
}

$("#extract-form").addEventListener("submit", guard(async (e) => {
  e.preventDefault();
  const form = e.target;
  const sel = selection();
  if (sel.error) {
    pagesIn.focus();
    throw new Error(sel.error);
  }
  const body = { kind: kind(), pages: form.pages.value.trim() || null, force: form.force.checked };
  if (body.kind === "llm") {
    body.llm = { ...picker.spec(), pages_per_request: perRequest() };
    localStorage.setItem("pyocr.perRequest", String(body.llm.pages_per_request));
    const n = sel.all ? `all ${plural(PAGE_COUNT, "page")}` : `${plural(sel.pages.length, "page")} (${body.pages})`;
    const batch = body.llm.pages_per_request > 1 ? `, ${body.llm.pages_per_request} pages per request` : "";
    if (!confirm(`Send ${n} to ${picker.describe()}${batch}?\n\nPages already transcribed with this model, prompt ` +
      "and pages per request are " + (body.force ? "RE-SENT (re-run is on)." : "skipped."))) return;
  }
  const job = await postJSON(`/api/documents/${DOC_ID}/jobs`, body);
  await refreshJobs();
  follow(job.id);
}));
$$("#extract-form input").forEach((i) => i.addEventListener("input", updateForm));
$$("#extract-form input[name=kind]").forEach((i) => i.addEventListener("change", updateForm));

$("#delete-doc").addEventListener("click", guard(async (e) => {
  if (!confirm(`Delete ${e.currentTarget.dataset.name} and all of its OCR/LLM results?`)) return;
  await api(`/api/documents/${DOC_ID}`, { method: "DELETE" });
  location.href = "/documents";
}));

(async function main() {
  try {
    picker = await initLlmPicker($("#llm-options [data-llm-picker]"));
    const cfg0 = await getConfig();
    perRequestIn.max = cfg0.max_pages_per_request || 20;
    setPerRequest(localStorage.getItem("pyocr.perRequest") || 1);
    $("#llm-options").addEventListener("change", updateForm);
    updateForm();
    const cfg = await getConfig();
    if (!cfg.tesseract_available) {
      const ocr = $("#extract-form input[value=ocr]");
      ocr.disabled = true;
      ocr.parentElement.title = "Tesseract is not installed on the server";
    }
    await loadPages();
    const jobs = await refreshJobs();
    jobs.filter((j) => ["running", "queued"].includes(j.status)).forEach((j) => follow(j.id));
  } catch (err) {
    toast(err.message, true);
  }
})();
