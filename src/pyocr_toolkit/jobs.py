"""Background extraction jobs ("OCR / transcribe the whole document").

A job processes a list of pages with one engine and records progress, so
the browser can start it, navigate elsewhere, and come back to a progress
bar. Results are written to the normal per-page caches as each page finishes,
which means a job that dies half way loses nothing that already completed,
and re-running it only processes the missing pages.

Jobs live in memory: restarting the server forgets the job list (not the
results).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .documents import DocumentStore
from .service import LLMSpec, Services

JobKind = Literal["ocr", "llm"]


@dataclass
class Job:
    id: str
    doc_id: str
    kind: JobKind
    pages: list[int]
    label: str
    llm: dict[str, Any] | None = None
    force: bool = False
    status: str = "queued"  # queued | running | done | failed | cancelled
    done: int = 0
    cached: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    current_pages: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.pages)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["total"] = self.total
        data["pages"] = len(self.pages)
        data["first_page"] = self.pages[0] if self.pages else None
        data["errors"] = self.errors[-20:]
        return data


class JobManager:
    def __init__(self, store: DocumentStore, services: Services, ocr_concurrency: int = 1) -> None:
        self.store = store
        self.services = services
        self.ocr_concurrency = ocr_concurrency
        self.jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def list(self, doc_id: str | None = None) -> list[Job]:
        jobs = [j for j in self.jobs.values() if doc_id is None or j.doc_id == doc_id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def start(self, doc_id: str, kind: JobKind, pages: list[int], llm: LLMSpec | None = None,
              force: bool = False) -> Job:
        doc = self.store.get(doc_id)  # validates the id
        if kind == "llm":
            if llm is None:
                raise ValueError("LLM job needs a provider")
            provider, model, _ = self.services.resolve_spec(llm)
            if not provider.configured:
                raise ValueError(f"{provider.label} is not configured: set {', '.join(provider.missing_settings())}")
            label = f"LLM · {provider.label} · {model} · {llm.prompt_mode}"
        else:
            label = f"Tesseract OCR ({self.services.settings.tesseract_language})"
        job = Job(id=uuid.uuid4().hex[:12], doc_id=doc.meta.id, kind=kind, pages=pages, label=label,
                  llm=asdict(llm) if llm else None, force=force)
        self.jobs[job.id] = job
        self._tasks[job.id] = asyncio.get_running_loop().create_task(self._run(job))
        return job

    def cancel(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        task = self._tasks.get(job_id)
        if job and task and not task.done():
            task.cancel()
        return job

    async def _run(self, job: Job) -> None:
        doc = self.store.get(job.doc_id)
        job.status = "running"
        job.started_at = time.time()
        queue: asyncio.Queue[int] = asyncio.Queue()
        for p in job.pages:
            queue.put_nowait(p)
        spec = LLMSpec(**job.llm) if job.llm else None

        async def worker() -> None:
            while not queue.empty():
                page = queue.get_nowait()
                job.current_pages.append(page)
                try:
                    if job.kind == "ocr":
                        if not job.force and doc.cached_ocr_text(page) is not None:
                            job.cached += 1
                        else:
                            await asyncio.to_thread(self.services.run_ocr_page, doc, page, job.force)
                    else:
                        res = await self.services.run_llm_page(doc, page, spec, force=job.force)
                        if res.get("cached"):
                            job.cached += 1
                        else:
                            job.input_tokens += res.get("input_tokens") or 0
                            job.output_tokens += res.get("output_tokens") or 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # keep going; report per page
                    job.failed += 1
                    job.errors.append(f"page {page + 1}: {exc}")
                finally:
                    job.current_pages.remove(page)
                    job.done += 1

        concurrency = self.ocr_concurrency if job.kind == "ocr" else self.services.settings.llm_concurrency
        try:
            await asyncio.gather(*(worker() for _ in range(max(1, min(concurrency, job.total)))))
            job.status = "failed" if job.failed == job.total else "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
        finally:
            job.finished_at = time.time()
            job.current_pages.clear()
