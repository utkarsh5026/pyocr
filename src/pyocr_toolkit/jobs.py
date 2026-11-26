"""Background extraction jobs ("OCR / transcribe the whole document").

A job processes a list of pages with one engine and records progress, so
the browser can start it, navigate elsewhere, and come back to a progress
bar. Results are written to the normal per-page caches as each page finishes,
which means a job that dies half way loses nothing that already completed,
and re-running it only processes the missing pages.

Jobs live in memory: restarting the server forgets the job list (not the
results).

Progress is reported in pages. ``current_pages`` are pages the engine is working
on right now; LLM pages that are still waiting for an ``LLM_CONCURRENCY`` slot
(another job or a workspace run holds it) are in ``waiting_pages`` instead, so
the UI doesn't claim the model is reading a page it hasn't been sent yet.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .documents import DEFAULT_OCR_ENGINE, DocumentStore
from .llm import catalog
from .preprocess import Preprocess
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
    #: OCR engine id for ``ocr`` jobs
    engine: str | None = None
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
    waiting_pages: list[int] = field(default_factory=list)
    #: catalog list price for LLM jobs: {"in", "out" (USD per 1M tokens), "model", "usd_inr_rate"}; None if unknown
    price: dict[str, Any] | None = None

    @property
    def total(self) -> int:
        return len(self.pages)

    def timing(self, now: float | None = None) -> tuple[float | None, float | None]:
        """``(seconds_per_page, eta_seconds)`` from the pages processed so far.

        Cache hits are near-instant, so they're left out of the rate: the rate is
        elapsed time over pages that really ran (failures included, since they
        cost time too). With N workers this is wall time per page, i.e. it already
        accounts for concurrency. Both are ``None`` until a page has really run;
        the ETA is only given while the job is running.
        """
        if self.started_at is None:
            return None, None
        processed = self.done - self.cached
        if processed <= 0:
            return None, None
        end = self.finished_at or (now if now is not None else time.time())
        per_page = (end - self.started_at) / processed
        eta = per_page * (self.total - self.done) if self.status == "running" else None
        return per_page, eta

    def cost(self) -> dict[str, Any] | None:
        """Spend so far at list price, plus a projection for the whole job while it runs.

        The projection assumes remaining pages cost what the paid pages so far did on
        average; pages that turn out to be cached make the real total lower.
        """
        if not self.price:
            return None
        usd = (self.input_tokens * self.price["in"] + self.output_tokens * self.price["out"]) / 1_000_000
        rate = self.price["usd_inr_rate"]
        paid = self.done - self.cached - self.failed
        projected = usd / paid * (paid + self.total - self.done) if self.status == "running" and paid > 0 else None
        return {"usd": usd, "inr": usd * rate, "projected_usd": projected,
                "projected_inr": projected * rate if projected is not None else None,
                "usd_inr_rate": rate, "price_in": self.price["in"], "price_out": self.price["out"],
                "price_model": self.price["model"]}

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["total"] = self.total
        data["pages"] = len(self.pages)
        data["first_page"] = self.pages[0] if self.pages else None
        data["errors"] = self.errors[-20:]
        data["seconds_per_page"], data["eta_seconds"] = self.timing()
        data["cost"] = self.cost()
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
              force: bool = False, engine: str = DEFAULT_OCR_ENGINE) -> Job:
        doc = self.store.get(doc_id)  # validates the id
        price: dict[str, Any] | None = None
        if kind == "llm":
            if llm is None:
                raise ValueError("LLM job needs a provider")
            provider, model, _ = self.services.resolve_spec(llm)
            if not provider.configured:
                raise ValueError(f"{provider.label} is not configured: set {', '.join(provider.missing_settings())}")
            label = f"LLM · {provider.label} · {model} · {llm.prompt_mode}"
            pre = Preprocess.parse(llm.preprocess)
            if not pre.is_identity:
                label += f" · image: {pre.describe()}"
            found = catalog.price(model, provider.id)
            if found:
                price = {"in": found[0], "out": found[1], "model": found[2],
                         "usd_inr_rate": self.services.settings.usd_inr_rate}
        else:
            try:
                ocr = self.services.ocr_engine(engine)
            except Exception as exc:  # DocumentError -> ValueError for the API layer
                raise ValueError(str(exc)) from exc
            if problem := ocr.problem():
                raise ValueError(problem)
            summary = ocr.settings_summary()
            label = f"{ocr.label}{f' ({summary})' if summary else ''}"
        job = Job(id=uuid.uuid4().hex[:12], doc_id=doc.meta.id, kind=kind, pages=pages, label=label,
                  llm=asdict(llm) if llm else None, engine=engine if kind == "ocr" else None, force=force,
                  price=price)
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
                # LLM pages wait for a semaphore slot before the model sees them.
                (job.waiting_pages if job.kind == "llm" else job.current_pages).append(page)

                def started(page: int = page) -> None:
                    job.waiting_pages.remove(page)
                    job.current_pages.append(page)

                try:
                    if job.kind == "ocr":
                        engine = job.engine or DEFAULT_OCR_ENGINE
                        if not job.force and doc.cached_ocr_text(page, engine) is not None:
                            job.cached += 1
                        else:
                            await asyncio.to_thread(self.services.run_ocr_page, doc, page, job.force, engine)
                    else:
                        res = await self.services.run_llm_page(doc, page, spec, force=job.force, on_start=started)
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
                    for active in (job.current_pages, job.waiting_pages):
                        if page in active:
                            active.remove(page)
                    job.done += 1

        if job.kind == "ocr":
            # local engines are CPU-bound and serialised anyway; cloud ones can overlap
            ocr = self.services.ocr_engines.get(job.engine or DEFAULT_OCR_ENGINE)
            concurrency = max(self.ocr_concurrency, ocr.concurrency if ocr else 1)
        else:
            concurrency = self.services.settings.llm_concurrency
        try:
            await asyncio.gather(*(worker() for _ in range(max(1, min(concurrency, job.total)))))
            job.status = "failed" if job.failed == job.total else "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
        finally:
            job.finished_at = time.time()
            job.current_pages.clear()
            job.waiting_pages.clear()
