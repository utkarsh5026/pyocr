/* Workspaces list and workspace page: create, rename (with name suggestions), delete, manage documents. */
"use strict";

// ----------------------------------------------------------- list page
$("#ws-new")?.addEventListener("click", guard(async () => {
  const [suggestion] = await suggestNames([], 1);
  const name = prompt("Name for the new workspace:", suggestion);
  if (name === null) return;
  const ws = await postJSON("/api/workspaces", { name: name.trim() || suggestion });
  location.href = `/workspaces/${ws.id}`;
}));

// ----------------------------------------------------------- workspace page
const root = $("#ws-root");
if (root) {
  const wsId = root.dataset.wsId;
  const form = $("#ws-rename-form"), input = $("#ws-rename-input"), title = $("#ws-name"), chips = $("#ws-rename-suggest");
  const filenames = $$("#lib-grid .lib-card").map((c) => c.dataset.name);

  const markChips = () => $$("button", chips).forEach((b) => b.setAttribute("aria-pressed", String(b.textContent === input.value.trim())));
  function showForm(show) {
    form.hidden = !show;
    title.parentElement.hidden = show;
    if (!show) return;
    input.value = root.dataset.wsName;
    input.focus();
    input.select();
    guard(async () => {
      const names = await suggestNames(filenames);
      chips.innerHTML = '<span class="small muted">Ideas:</span>' +
        names.map((n) => `<button type="button">${esc(n)}</button>`).join("");
      markChips();
    })();
  }
  $("#ws-rename").addEventListener("click", () => showForm(true));
  $("#ws-rename-cancel").addEventListener("click", () => showForm(false));
  title.addEventListener("dblclick", () => showForm(true));
  input.addEventListener("input", markChips);
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") showForm(false); });
  chips.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { input.value = b.textContent; markChips(); input.focus(); }
  });
  form.addEventListener("submit", guard(async (e) => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name || name === root.dataset.wsName) return showForm(false);
    const ws = await api(`/api/workspaces/${wsId}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
    });
    root.dataset.wsName = ws.name;
    title.textContent = ws.name;
    document.title = `${ws.name} · pyocr`;
    showForm(false);
    toast(`Renamed to ${ws.name}`);
  }));

  $("#ws-delete").addEventListener("click", guard(async () => {
    const name = root.dataset.wsName;
    const count = $$("#lib-grid .lib-card").length;
    if (!confirm(`Delete the workspace "${name}"?`)) return;
    let alsoDocs = false;
    if (count) alsoDocs = confirm(`Also delete its ${count} document${count === 1 ? "" : "s"} and all their OCR/LLM results?\n\n` +
      "OK = delete the documents too.  Cancel = keep them in the Library.");
    await api(`/api/workspaces/${wsId}?delete_documents=${alsoDocs}`, { method: "DELETE" });
    location.href = "/workspaces";
  }));

  $$("[data-ws-remove]").forEach((btn) => btn.addEventListener("click", guard(async () => {
    await api(`/api/workspaces/${wsId}/documents/${btn.dataset.wsRemove}`, { method: "DELETE" });
    btn.closest(".lib-card").remove();
    toast(`Removed ${btn.dataset.name} from ${root.dataset.wsName} (it is still in the Library)`);
  })));

  $$("[data-delete]").forEach((btn) => btn.addEventListener("click", guard(async () => {
    if (!confirm(`Delete ${btn.dataset.name} and all of its OCR/LLM results?`)) return;
    await api(`/api/documents/${btn.dataset.delete}`, { method: "DELETE" });
    btn.closest(".lib-card").remove();
    toast(`Deleted ${btn.dataset.name}`);
  })));
}
