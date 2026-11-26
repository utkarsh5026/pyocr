/* All background jobs, refreshed while any is running. */
"use strict";

const list = $("#all-jobs");
const names = JSON.parse(list.dataset.names || "{}");

async function refresh() {
  const jobs = await api("/api/jobs");
  if (jobs.length) list.innerHTML = jobs.map((j) => jobHtml(j, names[j.doc_id] || j.doc_id)).join("");
  if (jobs.some((j) => ["running", "queued"].includes(j.status))) setTimeout(guard(refresh), 1500);
}

guard(refresh)();
