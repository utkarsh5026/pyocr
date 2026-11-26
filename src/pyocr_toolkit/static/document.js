/* Document overview: start background extractions, follow jobs, page grid. */
"use strict";

const docRoot = $("#doc-root");
const DOC_ID = docRoot.dataset.docId;
const PAGE_COUNT = +docRoot.dataset.pageCount;
let picker;

const kind = () => $("#extract-form input[name=kind]:checked").value;

function updateForm() {
  $("#llm-options").hidden = kind() !== "llm";
  const pages = $("#extract-form input[name=pages]").value.trim();
  const what = pages ? `pages ${pages}` : `all ${PAGE_COUNT} page${PAGE_COUNT === 1 ? "" : "s"}`;
  $("#extract-hint").textContent = kind() === "llm" && picker
    ? `Sends ${what} as images to ${picker.describe()}`
    : `Runs locally on ${what}; about 1–5 s per page`;
}

async function loadPages() {
  const pages = await api(`/api/documents/${DOC_ID}/pages`);
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
  const body = { kind: kind(), pages: form.pages.value.trim() || null, force: form.force.checked };
  if (body.kind === "llm") {
    body.llm = picker.spec();
    const n = body.pages ? `pages ${body.pages}` : `all ${PAGE_COUNT} pages`;
    if (!confirm(`Send ${n} to ${picker.describe()}?\n\nPages already transcribed with this model and prompt are ` +
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
