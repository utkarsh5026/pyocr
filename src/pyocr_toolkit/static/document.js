/* Document overview: start background extractions, follow jobs, page grid. */
"use strict";

const docRoot = $("#doc-root");
const DOC_ID = docRoot.dataset.docId;
const PAGE_COUNT = +docRoot.dataset.pageCount;
let picker;

const kind = () => $("#extract-form input[name=kind]:checked").value;

const ocrSelect = $("#ocr-engine");

function updateForm() {
  $("#llm-options").hidden = kind() !== "llm";
  $("#ocr-options").hidden = kind() !== "ocr";
  const pages = $("#extract-form input[name=pages]").value.trim();
  const what = pages ? `pages ${pages}` : `all ${PAGE_COUNT} page${PAGE_COUNT === 1 ? "" : "s"}`;
  const engine = ocrEngineInfo(ocrSelect.value);
  if (engine) {
    $("#ocr-engine-logo").innerHTML = logoHtml(engine.brand);
    $("#ocr-engine-note").innerHTML = engine.available
      ? `<span class="muted">${esc(engine.description)}</span>`
      : `<span class="warn">${esc(engine.problem)}</span> <a href="/providers">Setup</a>`;
  }
  const pre = $("#extract-form input[name=preprocess]").value.trim();
  $("#extract-hint").textContent = kind() === "llm" && picker
    ? `Sends ${what} as images${pre ? " (preprocessed)" : ""} to ${picker.describe()}`
    : engine?.kind === "cloud"
      ? `Sends ${what} as images to ${engine.label}`
      : `Runs locally on ${what} with ${engine?.label || "OCR"}`;
}

async function loadPages() {
  const pages = await api(`/api/documents/${DOC_ID}/pages`);
  $("#page-grid").innerHTML = pages.map((p, n) => `
    <a class="page-tile" href="/documents/${DOC_ID}/pages/${n + 1}">
      <img loading="lazy" src="/api/documents/${DOC_ID}/pages/${n}/image?dpi=50" alt="Page ${n + 1}">
      <div class="tile-foot"><b>${n + 1}</b>
        ${p.looks_scanned ? '<span class="badge scan">scan</span>' : `<span class="badge">${p.native_chars} ch</span>`}
        ${p.ocr_engines?.length ? `<span class="badge ok" title="${esc(p.ocr_engines.map(ocrLabel).join(", "))}">OCR${p.ocr_engines.length > 1 ? "×" + p.ocr_engines.length : ""}</span>` : ""}
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
  const body = { kind: kind(), pages: form.pages.value.trim() || null, force: form.force.checked };
  if (body.kind === "ocr") {
    const engine = ocrEngineInfo(ocrSelect.value);
    if (!engine?.available) throw new Error(engine?.problem || "pick an OCR engine");
    body.engine = engine.id;
    localStorage.setItem("pyocr.ocrEngine", engine.id);
    if (engine.kind === "cloud" && !confirm(`Send ${body.pages ? `pages ${body.pages}` : `all ${PAGE_COUNT} pages`} to ${engine.label}?`)) return;
  }
  if (body.kind === "llm") {
    body.llm = picker.spec();
    body.llm.preprocess = form.preprocess.value.trim() || null;
    preprocessStore.set(DOC_ID, body.llm.preprocess);
    const n = body.pages ? `pages ${body.pages}` : `all ${PAGE_COUNT} pages`;
    const img = body.llm.preprocess ? `\nImage preprocessing: ${body.llm.preprocess}\n` : "";
    if (!confirm(`Send ${n} to ${picker.describe()}?\n${img}\nPages already transcribed with this model, prompt and image settings are ` +
      (body.force ? "RE-SENT (re-run is on)." : "skipped."))) return;
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
    $("#llm-options").addEventListener("change", updateForm);
    updateForm();
    const cfg = await getConfig();
    ocrSelect.innerHTML = ocrEngineOptions(cfg);
    ocrSelect.value = preferredOcrEngine(cfg);
    ocrSelect.addEventListener("change", updateForm);
    $("#pre-presets").innerHTML = cfg.preprocess_presets.filter((p) => p.spec)
      .map((p) => `<option value="${esc(p.spec)}">${esc(p.label)}: ${esc(p.hint)}</option>`).join("");
    $("#extract-form input[name=preprocess]").value = preprocessStore.get(DOC_ID);
    if (!cfg.ocr_engines.some((e) => e.available)) {
      const ocr = $("#extract-form input[value=ocr]");
      ocr.disabled = true;
      ocr.parentElement.title = "No OCR engine is installed or configured: see Providers";
    }
    updateForm();
    await loadPages();
    const jobs = await refreshJobs();
    jobs.filter((j) => ["running", "queued"].includes(j.status)).forEach((j) => follow(j.id));
  } catch (err) {
    toast(err.message, true);
  }
})();
