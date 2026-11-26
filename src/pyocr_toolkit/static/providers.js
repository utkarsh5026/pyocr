/* Providers page: edit credentials, rate the model being tested, "Test connection" buttons. */
"use strict";

$$(".provider").forEach((card) => {
  const pid = card.dataset.providerId;
  const btn = $("[data-test]", card);
  const out = $("[data-test-result]", card);
  const modelIn = $("[data-test-model]", card);
  bindModelVerdict(modelIn, $("[data-model-verdict]", card), pid);

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    out.innerHTML = "Sending a small test image…";
    try {
      const r = await postJSON(`/api/providers/${pid}/test`, { model: modelIn.value.trim() || null });
      out.innerHTML = `${r.ok ? '<span class="good">✓ Works.</span>' : '<span class="warn">Responded, but the text did not match.</span>'}
        Model <b>${esc(r.model)}</b> read <code>${esc(r.text)}</code> (expected <code>${esc(r.expected)}</code>)
        in ${(r.latency_ms / 1000).toFixed(1)}s.`;
    } catch (e) {
      out.innerHTML = `<span class="err">✗ ${esc(e.message)}</span>`;
    } finally {
      btn.disabled = false;
    }
  });

  const form = $("[data-cred-form]", card);
  if (!form) return;
  // Nothing configured anywhere yet: open the first card's form so the next step is obvious.
  if (!$("[data-provider-ready]") && card === $(".provider")) form.closest("details").open = true;
  form.addEventListener("submit", guard(async (ev) => {
    ev.preventDefault();
    const values = {};
    $$("input[name]", form).forEach((i) => { if (i.value.trim()) values[i.name] = i.value.trim(); });
    const clear = $$("input[data-clear]:checked", form).map((i) => i.dataset.clear);
    if (!Object.keys(values).length && !clear.length) {
      $("[data-cred-result]", form).innerHTML = '<span class="muted">Nothing to save.</span>';
      return;
    }
    const r = await postJSON(`/api/providers/${pid}/credentials`, { values, clear });
    $$("input[type=password]", form).forEach((i) => { i.value = ""; });  // don't keep keys in the DOM
    toast(r.provider.configured ? `${r.provider.label} saved and ready` : `Saved. Still missing: ${r.provider.missing.join(", ")}`);
    setTimeout(() => location.reload(), 400);  // badges and tables are server-rendered
  }));
});
