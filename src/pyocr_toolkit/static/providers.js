/* Providers page: "Test connection" buttons for LLM providers and OCR engines. */
"use strict";

$$(".provider").forEach((card) => {
  const btn = $("[data-test]", card);
  const out = $("[data-test-result]", card);
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    out.innerHTML = "Sending a small test image…";
    try {
      const r = await postJSON(`/api/providers/${card.dataset.providerId}/test`, { model: $("[data-test-model]", card).value.trim() || null });
      out.innerHTML = `${r.ok ? '<span class="good">✓ Works.</span>' : '<span class="warn">Responded, but the text did not match.</span>'}
        Model <b>${esc(r.model)}</b> read <code>${esc(r.text)}</code> (expected <code>${esc(r.expected)}</code>)
        in ${(r.latency_ms / 1000).toFixed(1)}s.`;
    } catch (e) {
      out.innerHTML = `<span class="err">✗ ${esc(e.message)}</span>`;
    } finally {
      btn.disabled = false;
    }
  });
});

$$(".engine-card").forEach((card) => {
  const btn = $("[data-test-engine]", card);
  const out = $("[data-test-result]", card);
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    out.innerHTML = "OCRing a small test image… (local engines may download their model first)";
    try {
      const r = await postJSON(`/api/ocr-engines/${card.dataset.engineId}/test`, {});
      out.innerHTML = `${r.ok ? '<span class="good">✓ Works.</span>' : '<span class="warn">Ran, but the text did not match.</span>'}
        Read <code>${esc(r.text)}</code> (expected <code>${esc(r.expected)}</code>)${r.model ? ` with <b>${esc(r.model)}</b>` : ""}
        in ${(r.latency_ms / 1000).toFixed(1)}s.`;
    } catch (e) {
      out.innerHTML = `<span class="err">✗ ${esc(e.message)}</span>`;
    } finally {
      btn.disabled = false;
    }
  });
});
