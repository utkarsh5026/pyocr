/* Library: filter by name and status, delete documents. */
"use strict";

function applyFilter() {
  const q = ($("#lib-filter")?.value || "").trim().toLowerCase();
  const status = $("input[name=lib-status]:checked")?.value || "all";
  let shown = 0;
  $$("#lib-grid .lib-card").forEach((card) => {
    card.hidden = (q && !card.dataset.name.includes(q)) || (status !== "all" && card.dataset.status !== status);
    if (!card.hidden) shown++;
  });
  const none = $("#lib-none");
  if (none) none.hidden = shown > 0;
}

$("#lib-filter")?.addEventListener("input", applyFilter);
$$("input[name=lib-status]").forEach((r) => r.addEventListener("change", applyFilter));

$$("[data-delete]").forEach((btn) => btn.addEventListener("click", guard(async () => {
  if (!confirm(`Delete ${btn.dataset.name} and all of its OCR/LLM results?`)) return;
  await api(`/api/documents/${btn.dataset.delete}`, { method: "DELETE" });
  btn.closest(".lib-card").remove();
  applyFilter();
  toast(`Deleted ${btn.dataset.name}`);
})));
