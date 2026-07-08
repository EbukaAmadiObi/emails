"""Tests for backend.worker job lifecycle: creation, completion, cancellation."""

import asyncio
from unittest.mock import patch

import pytest

from backend import worker
from backend.models import JobStatus, LookupResult, LookupStatus

# Set required env var before importing (harmless if already set elsewhere)
import os
os.environ.setdefault("VERIFY_FROM_ADDR", "test@example.com")


@pytest.fixture(autouse=True)
def reset_worker_state():
    worker._jobs.clear()
    worker._job_tasks.clear()
    worker._domain_sems.clear()
    worker._catch_all_cache.clear()
    worker._init_semaphores()
    yield
    worker._jobs.clear()
    worker._job_tasks.clear()
    worker._domain_sems.clear()
    worker._catch_all_cache.clear()


FAST_RESULT = LookupResult(
    first="A", last="B", domain="x.com", status=LookupStatus.not_found,
)


async def _slow_verify(*args, **kwargs):
    """Stand-in for asyncio.to_thread(_verify_contact_sync, ...): a real
    asyncio.sleep so cancellation can actually interrupt it, unlike a real
    OS thread running SMTP I/O."""
    await asyncio.sleep(10)
    return FAST_RESULT


class TestCreateJob:
    def test_default_kind_is_batch(self):
        job_id = worker.create_job(total=1)
        assert worker.get_job(job_id).kind == "batch"

    def test_single_kind(self):
        job_id = worker.create_job(total=1, kind="single")
        assert worker.get_job(job_id).kind == "single"


class TestRunBatchCompletion:
    async def test_normal_completion(self):
        job_id = worker.create_job(total=2)
        contacts = [
            {"first": "A", "last": "B", "domain": "x.com"},
            {"first": "C", "last": "D", "domain": "y.com"},
        ]
        with patch("backend.worker._verify_contact_sync", return_value=FAST_RESULT):
            await worker.run_batch(job_id, contacts, "from@x.com")

        job = worker.get_job(job_id)
        assert job.status == JobStatus.completed
        assert job.progress == 1.0
        assert job.done == 2
        assert job_id not in worker._job_tasks


class TestCancelJob:
    def test_unknown_job_returns_false(self):
        assert worker.cancel_job("nonexistent") is False

    def test_terminal_job_returns_false(self):
        job_id = worker.create_job(total=1)
        worker.get_job(job_id).status = JobStatus.completed
        assert worker.cancel_job(job_id) is False

    async def test_cancel_releases_queued_tasks(self):
        job_id = worker.create_job(total=3)
        contacts = [
            {"first": "A", "last": "B", "domain": f"domain{i}.com"}
            for i in range(3)
        ]

        with patch("backend.worker.asyncio.to_thread", side_effect=_slow_verify):
            task = asyncio.create_task(worker.run_batch(job_id, contacts, "from@x.com"))
            # Let the process_one coroutines start and reach the
            # semaphore-protected slow call before cancelling.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            assert worker.cancel_job(job_id) is True

            # run_batch must finish cleanly (return_exceptions=True keeps
            # gather from raising on the cancelled children).
            await task

        job = worker.get_job(job_id)
        assert job.status == JobStatus.cancelled
        assert job_id not in worker._job_tasks
        assert worker._global_sem._value == worker._MAX_CONCURRENT
