/* Library: filter and delete documents. */
"use strict";

$("#lib-filter")?.addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  $$("#lib-table tbody tr").forEach((tr) => { tr.hidden = q && !tr.dataset.name.includes(q); });
});

$$("[data-delete]").forEach((btn) => btn.addEventListener("click", guard(async () => {
  if (!confirm(`Delete ${btn.dataset.name} and all of its OCR/LLM results?`)) return;
  await api(`/api/documents/${btn.dataset.delete}`, { method: "DELETE" });
  btn.closest("tr").hidden = true;
  toast(`Deleted ${btn.dataset.name}`);
})));
