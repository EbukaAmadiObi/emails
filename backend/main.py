"""
FastAPI application entry point.
"""

import asyncio
import csv
import io
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .models import LookupRequest, LookupResult, LookupStatus
from . import verifier, worker
from .permutations import generate_permutations

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = int(os.environ.get("MAX_BATCH_SIZE", "1000"))


def _get_from_addr() -> str:
    addr = os.environ.get("VERIFY_FROM_ADDR", "").strip()
    if not addr:
        raise RuntimeError(
            "VERIFY_FROM_ADDR environment variable is required. "
            "Example: export VERIFY_FROM_ADDR=you@yourdomain.com"
        )
    return addr


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    _get_from_addr()  # fails fast if not configured

    worker._init_semaphores()

    # Port 25 connectivity probe (warning only — don't crash, allows UI testing)
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("gmail-smtp-in.l.google.com", 25),
            timeout=5,
        )
        writer.close()
        await writer.wait_closed()
        logger.info("Port 25 reachable — SMTP verification ready")
    except Exception:
        logger.warning(
            "Port 25 blocked on this machine — SMTP verification will fail. "
            "Run on a home network or a VPS at Hetzner/OVH/Linode."
        )

    yield
    # --- shutdown (nothing to clean up for v1) ---


app = FastAPI(title="Email Finder", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Single lookup
# ---------------------------------------------------------------------------

@app.post("/lookup", response_model=LookupResult)
async def lookup(req: LookupRequest):
    from_addr = _get_from_addr()

    try:
        perms = generate_permutations(req.first, req.last)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    result = await asyncio.to_thread(
        worker._verify_contact_sync,
        req.first, req.last, req.domain, perms, from_addr,
    )
    return result


# ---------------------------------------------------------------------------
# Batch CSV
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = {"first_name", "last_name", "domain"}


@app.post("/batch")
async def batch(file: UploadFile = File(...)):
    from_addr = _get_from_addr()

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # strips BOM if present
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text))

    # Normalise header names
    if reader.fieldnames is None:
        raise HTTPException(status_code=400, detail="CSV has no header row")

    lower_fields = {f.strip().lower() for f in reader.fieldnames}
    missing = REQUIRED_COLUMNS - lower_fields
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"CSV missing required columns: {', '.join(sorted(missing))}",
        )

    contacts: list[dict] = []
    for row in reader:
        normalised = {k.strip().lower(): v.strip() for k, v in row.items()}
        contacts.append({
            "first": normalised["first_name"],
            "last": normalised["last_name"],
            "domain": normalised["domain"],
        })

    if not contacts:
        raise HTTPException(status_code=400, detail="CSV has no data rows")

    if len(contacts) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"CSV too large — max {MAX_BATCH_SIZE} rows",
        )

    job_id = worker.create_job(total=len(contacts))
    asyncio.create_task(worker.run_batch(job_id, contacts, from_addr))
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# Job status & results
# ---------------------------------------------------------------------------

@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    job = worker.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs/{job_id}/csv")
async def job_csv(job_id: str):
    job = worker.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status.value not in ("completed", "failed"):
        return JSONResponse(
            status_code=202,
            content={"message": "Job not complete yet", "status": job.status.value},
        )

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            ["first_name", "last_name", "domain", "email", "status",
             "permutations_tried", "catch_all"]
        )
        for r in job.results:
            writer.writerow([
                r.first, r.last, r.domain,
                r.email or "",
                r.status.value,
                r.permutations_tried,
                str(r.catch_all).lower(),
            ])
            output.seek(0)
            yield output.read()
            output.seek(0)
            output.truncate(0)

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="results-{job_id[:8]}.csv"'},
    )


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
