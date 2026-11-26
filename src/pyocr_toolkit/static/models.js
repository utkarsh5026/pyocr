/* Models page: search the rated catalog, list what a provider's API offers, pick a model. */
"use strict";

const qIn = $("#m-q"), provSel = $("#m-provider"), rateSel = $("#m-rating"), depBox = $("#m-deprecated");

/** Remember `model` for `providerId`; the LLM picker on every page reads the same keys. */
function useModel(providerId, model) {
  localStorage.setItem("pyocr.provider", providerId);
  localStorage.setItem(`pyocr.model.${providerId}`, model);
  toast(`${model} will be preselected for ${providerId} in the LLM picker`);
}

function modelCell(m) {
  const status = m.status && m.status !== "current" ? ` <span class="badge">${esc(m.status)}</span>` : "";
  const varies = m.name_varies ? ' <span class="muted small" title="Use the exact name your server lists (Ollama tag, OpenRouter slug…)">name varies</span>' : "";
  return `<b>${esc(m.label || m.name)}</b>${status}<br><code>${esc(m.id || m.name)}</code>${varies}
    <span class="muted small">· ${esc(m.vendor || "")}</span>`;
}

function useButton(m, providerId) {
  if (m.rating === "unsuitable" || m.name_varies) return "";
  const pid = providerId || (m.providers || [])[0];
  if (!pid) return "";
  return `<button class="btn small ghost" data-use="${esc(m.id || m.name)}" data-pid="${esc(pid)}">Use</button>`;
}

let searchTimer;
async function runSearch() {
  const params = new URLSearchParams({ q: qIn.value.trim(), include_deprecated: depBox.checked });
  if (provSel.value) params.set("provider", provSel.value);
  if (rateSel.value) params.set("rating", rateSel.value);
  history.replaceState(null, "", `/models?${new URLSearchParams({ q: qIn.value.trim(), provider: provSel.value })}`);
  const r = await api(`/api/models?${params}`);
  $("#m-results").innerHTML = r.models.length ? r.models.map((m) => `<tr>
      <td>${modelCell(m)}</td>
      <td class="nowrap">${ratingBadge(m)}<br><span class="muted small">${esc(m.evidence)}</span></td>
      <td class="nowrap small">${m.price_in == null ? '<span class="muted">–</span>' : `$${m.price_in} / $${m.price_out}`}</td>
      <td class="small">${esc(m.note)}</td>
      <td>${useButton(m, provSel.value)}</td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">No catalog model matches. You can still type any name in the model box;
       it will be marked “custom”.</td></tr>`;
  $("#m-count").textContent = `${r.models.length} model${r.models.length === 1 ? "" : "s"}`;
}
const scheduleSearch = () => { clearTimeout(searchTimer); searchTimer = setTimeout(guard(runSearch), 150); };
[qIn].forEach((el) => el.addEventListener("input", scheduleSearch));
[provSel, rateSel, depBox].forEach((el) => el.addEventListener("change", scheduleSearch));
guard(runSearch)();

// ---------------------------------------------------------------- live listing
const liveSel = $("#live-provider"), liveBtn = $("#live-load"), liveOut = $("#live-results"), liveQ = $("#live-q");
let liveRows = [];

function renderLive() {
  const words = liveQ.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const rows = liveRows.filter((m) => words.every((w) => `${m.name} ${m.rating_label}`.toLowerCase().includes(w)));
  liveOut.innerHTML = `<div class="table-wrap"><table class="table compact models-table">
    <thead><tr><th>Model id</th><th>For OCR</th><th>Notes</th><th></th></tr></thead><tbody>
    ${rows.map((m) => `<tr><td><code>${esc(m.name)}</code></td><td class="nowrap">${ratingBadge(m)}</td>
      <td class="small">${m.match && !m.exact ? `<span class="muted">looks like <code>${esc(m.match)}</code> ·</span> ` : ""}${esc(m.note || "")}</td>
      <td>${m.rating === "unsuitable" ? "" : `<button class="btn small ghost" data-use="${esc(m.name)}" data-pid="${esc(liveSel.value)}">Use</button>`}</td></tr>`).join("")}
    </tbody></table></div><p class="small muted">${rows.length} of ${liveRows.length} models</p>`;
}

if (liveSel && liveBtn) {
  liveBtn.addEventListener("click", guard(async () => {
    const restore = setBusy(liveBtn, "Asking…");
    try {
      const r = await api(`/api/providers/${encodeURIComponent(liveSel.value)}/models`);
      if (r.live_error) {
        liveOut.innerHTML = `<p class="err small">✗ ${esc(r.live_error)}</p>`;
        liveQ.hidden = true;
        return;
      }
      // Most useful first: suitable before unsuitable, then by rating.
      const order = { excellent: 0, good: 1, fair: 2, unrated: 3, unsuitable: 4 };
      liveRows = (r.live || []).sort((a, b) => (order[a.rating] - order[b.rating]) || a.name.localeCompare(b.name));
      liveQ.hidden = !liveRows.length;
      if (!liveRows.length) liveOut.innerHTML = '<p class="muted small">The provider returned no models.</p>';
      else renderLive();
    } finally {
      restore();
    }
  }));
  liveQ.addEventListener("input", renderLive);
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-use]");
  if (b) useModel(b.dataset.pid, b.dataset.use);
});
