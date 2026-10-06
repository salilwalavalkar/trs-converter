"""Background conversion jobs with progress reporting."""

import logging
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import analytics
import converter

log = logging.getLogger("trs.jobs")

# LibreOffice is memory hungry, so run one conversion at a time and queue the rest.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="convert")
_jobs: dict[str, "Job"] = {}
_lock = threading.Lock()

JOB_TTL_SECONDS = 60 * 60


@dataclass
class Job:
    owner: str
    filename: str
    workdir: Path
    excel_path: Path
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: float = field(default_factory=time.time)
    status: str = "queued"      # queued | running | done | error
    progress: int = 0
    message: str = "Waiting for the converter..."
    error: str | None = None
    jpeg_path: Path | None = None

    def update(self, progress: int, message: str) -> None:
        self.progress, self.message = progress, message

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "filename": self.filename,
            "jpeg_name": self.jpeg_path.name if self.jpeg_path else None,
        }


def submit(owner: str, filename: str, stem: str, data: bytes, suffix: str) -> Job:
    cleanup_expired()
    workdir = Path(tempfile.mkdtemp(prefix="trs_"))
    excel_path = workdir / f"{stem}{suffix}"
    excel_path.write_bytes(data)

    job = Job(owner=owner, filename=filename, workdir=workdir, excel_path=excel_path)
    with _lock:
        _jobs[job.id] = job
    _executor.submit(_run, job)
    return job


def get(job_id: str, owner: str) -> Job | None:
    with _lock:
        job = _jobs.get(job_id)
    # Users can only see their own jobs.
    return job if job and job.owner == owner else None


def _run(job: Job) -> None:
    started = time.time()
    job.status = "running"
    try:
        job.update(20, "Converting spreadsheet to PDF...")
        pdf_path = converter.excel_to_pdf(job.excel_path, job.workdir)

        job.update(65, "Rendering high-resolution JPEG...")
        jpeg_path = job.workdir / f"{job.excel_path.stem}.jpg"
        job.jpeg_path = converter.pdf_first_page_to_jpeg(pdf_path, jpeg_path)

        job.update(100, "Done! Your JPEG is ready.")
        job.status = "done"
        analytics.track(
            "conversion_succeeded", job.owner,
            file=job.filename, seconds=round(time.time() - started, 1),
        )
    except converter.ConversionError as e:
        _fail(job, str(e))
    except Exception:
        log.exception("Unexpected error converting job %s", job.id)
        _fail(job, "Something went wrong while converting. Please try again.")


def _fail(job: Job, message: str) -> None:
    job.status = "error"
    job.error = message
    job.message = message
    analytics.track("conversion_failed", job.owner, file=job.filename, reason=message)


def cleanup_expired() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with _lock:
        expired = [j for j in _jobs.values() if j.created < cutoff and j.status in ("done", "error")]
        for job in expired:
            del _jobs[job.id]
    for job in expired:
        shutil.rmtree(job.workdir, ignore_errors=True)
