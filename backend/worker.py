"""
Async batch job runner.

Manages job state and concurrency for SMTP verification tasks.
All SMTP work runs via asyncio.to_thread() to keep the event loop free.
"""

import asyncio
import logging
import os
import uuid
from typing import Optional

from .models import Job, JobStatus, LookupResult, LookupStatus
from . import verifier
from .permutations import generate_permutations

logger = logging.getLogger(__name__)

# In-memory job store. State is lost on server restart (acceptable for v1).
_jobs: dict[str, Job] = {}

# Global SMTP concurrency limit (default: 10)
_MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "10"))
_MAX_CONCURRENT_PER_DOMAIN = int(os.environ.get("MAX_CONCURRENT_PER_DOMAIN", "2"))

_global_sem: asyncio.Semaphore
_domain_sems: dict[str, asyncio.Semaphore] = {}
_catch_all_cache: dict[str, Optional[bool]] = {}


def _init_semaphores() -> None:
    """Create asyncio semaphores. Called from the FastAPI startup event."""
    global _global_sem
    _global_sem = asyncio.Semaphore(_MAX_CONCURRENT)


def create_job(total: int) -> str:
    job_id = str(uuid.uuid4())
    _jobs[job_id] = Job(job_id=job_id, total=total)
    return job_id


def get_job(job_id: str) -> Optional[Job]:
    return _jobs.get(job_id)


async def _get_domain_sem(domain: str) -> asyncio.Semaphore:
    if domain not in _domain_sems:
        _domain_sems[domain] = asyncio.Semaphore(_MAX_CONCURRENT_PER_DOMAIN)
    return _domain_sems[domain]


async def _process_contact(
    first: str,
    last: str,
    domain: str,
    permutations: list[str],
    from_addr: str,
) -> LookupResult:
    """Verify one contact. Acquires per-domain then global semaphore."""
    domain_sem = await _get_domain_sem(domain)

    async with domain_sem:
        async with _global_sem:
            return await asyncio.to_thread(
                _verify_contact_sync, first, last, domain, permutations, from_addr
            )


def _verify_contact_sync(
    first: str,
    last: str,
    domain: str,
    permutations: list[str],
    from_addr: str,
) -> LookupResult:
    """Synchronous verification logic. Runs inside asyncio.to_thread()."""
    mx_host = verifier.get_mx_host(domain)
    if mx_host is None:
        return LookupResult(
            first=first, last=last, domain=domain,
            status=LookupStatus.inconclusive,
        )

    # Catch-all check (result may already be in cache from a sibling coroutine)
    if domain not in _catch_all_cache:
        _catch_all_cache[domain] = verifier.check_catch_all(mx_host, domain, from_addr)

    catch_all = _catch_all_cache[domain]

    if catch_all is True:
        return LookupResult(
            first=first, last=last, domain=domain,
            status=LookupStatus.unverifiable, catch_all=True,
        )

    if catch_all is None:
        logger.warning("Cannot verify catch-all status for %s — MAIL FROM rejected", domain)
        return LookupResult(
            first=first, last=last, domain=domain,
            status=LookupStatus.inconclusive,
        )

    # Try permutations in frequency order
    for i, local_part in enumerate(permutations):
        address = f"{local_part}@{domain}"
        try:
            status = verifier.verify_address(mx_host, address, from_addr)
        except verifier.SMTPSenderRejected:
            logger.warning("MAIL FROM rejected during permutation check at %s", mx_host)
            return LookupResult(
                first=first, last=last, domain=domain,
                status=LookupStatus.inconclusive, permutations_tried=i,
            )

        if status == LookupStatus.verified:
            return LookupResult(
                first=first, last=last, domain=domain,
                email=address, status=LookupStatus.verified,
                permutations_tried=i + 1,
            )

    return LookupResult(
        first=first, last=last, domain=domain,
        status=LookupStatus.not_found,
        permutations_tried=len(permutations),
    )


async def run_batch(job_id: str, contacts: list[dict], from_addr: str) -> None:
    """Launch a batch job as an asyncio background task.

    contacts: list of {"first": str, "last": str, "domain": str}
    Updates job state in-place as results arrive.
    """
    job = _jobs.get(job_id)
    if not job:
        logger.error("run_batch called with unknown job_id: %s", job_id)
        return

    job.status = JobStatus.running

    async def process_one(contact: dict) -> None:
        first, last, domain = contact["first"], contact["last"], contact["domain"]
        try:
            perms = generate_permutations(first, last)
        except ValueError as e:
            result = LookupResult(
                first=first, last=last, domain=domain,
                status=LookupStatus.inconclusive,
            )
            logger.warning("Skipping contact %s %s: %s", first, last, e)
        else:
            result = await _process_contact(first, last, domain, perms, from_addr)

        job.results.append(result)
        job.done += 1
        job.progress = job.done / job.total if job.total > 0 else 1.0

    tasks = [asyncio.create_task(process_one(c)) for c in contacts]

    try:
        await asyncio.gather(*tasks)
        job.status = JobStatus.completed
        job.progress = 1.0
    except Exception as e:
        logger.exception("Batch job %s failed: %s", job_id, e)
        job.status = JobStatus.failed
