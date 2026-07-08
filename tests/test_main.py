"""Tests for FastAPI routes — uses TestClient with mocked worker/verifier."""

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.models import Job, JobStatus, LookupResult, LookupStatus

# Set required env var before importing the app
import os
os.environ.setdefault("VERIFY_FROM_ADDR", "test@example.com")

from backend.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


VERIFIED_RESULT = LookupResult(
    first="John", last="Smith", domain="acme.com",
    email="john.smith@acme.com", status=LookupStatus.verified,
    permutations_tried=1,
)

NOT_FOUND_RESULT = LookupResult(
    first="John", last="Smith", domain="acme.com",
    status=LookupStatus.not_found, permutations_tried=20,
)


# ---------------------------------------------------------------------------
# POST /lookup
# ---------------------------------------------------------------------------

class TestLookup:
    def test_valid_returns_job_id(self, client):
        with patch("backend.worker.run_batch", new_callable=AsyncMock):
            resp = client.post("/lookup", json={"first": "John", "last": "Smith", "domain": "acme.com"})
        assert resp.status_code == 200
        assert "job_id" in resp.json()

    def test_missing_first_returns_422(self, client):
        resp = client.post("/lookup", json={"last": "Smith", "domain": "acme.com"})
        assert resp.status_code == 422

    def test_missing_domain_returns_422(self, client):
        resp = client.post("/lookup", json={"first": "John", "last": "Smith"})
        assert resp.status_code == 422

    def test_empty_first_returns_422(self, client):
        resp = client.post("/lookup", json={"first": "", "last": "Smith", "domain": "acme.com"})
        assert resp.status_code == 422

    def test_domain_stripped_and_lowercased(self, client):
        with patch("backend.worker.run_batch", new_callable=AsyncMock) as m:
            client.post("/lookup", json={"first": "John", "last": "Smith", "domain": " ACME.COM "})
        contacts = m.call_args[0][1]  # (job_id, contacts, from_addr)
        assert contacts[0]["domain"] == "acme.com"


# ---------------------------------------------------------------------------
# POST /batch
# ---------------------------------------------------------------------------

VALID_CSV = b"first_name,last_name,domain\nJohn,Smith,acme.com\nJane,Doe,beta.com\n"


class TestBatch:
    def test_valid_csv_returns_job_id(self, client):
        with patch("backend.worker.run_batch", new_callable=AsyncMock):
            resp = client.post("/batch", files={"file": ("test.csv", VALID_CSV, "text/csv")})
        assert resp.status_code == 200
        assert "job_id" in resp.json()

    def test_missing_column_returns_400(self, client):
        bad_csv = b"first_name,domain\nJohn,acme.com\n"
        resp = client.post("/batch", files={"file": ("test.csv", bad_csv, "text/csv")})
        assert resp.status_code == 400
        assert "last_name" in resp.json()["detail"]

    def test_empty_data_returns_400(self, client):
        header_only = b"first_name,last_name,domain\n"
        resp = client.post("/batch", files={"file": ("test.csv", header_only, "text/csv")})
        assert resp.status_code == 400

    def test_bom_csv_accepted(self, client):
        bom_csv = b"\xef\xbb\xbffirst_name,last_name,domain\nJohn,Smith,acme.com\n"
        with patch("backend.worker.run_batch", new_callable=AsyncMock):
            resp = client.post("/batch", files={"file": ("test.csv", bom_csv, "text/csv")})
        assert resp.status_code == 200

    def test_oversized_csv_returns_400(self, client):
        rows = "first_name,last_name,domain\n" + "John,Smith,acme.com\n" * 1001
        with patch("backend.main.MAX_BATCH_SIZE", 1000):
            resp = client.post("/batch", files={"file": ("big.csv", rows.encode(), "text/csv")})
        assert resp.status_code == 400
        assert "too large" in resp.json()["detail"].lower()

    def test_columns_case_insensitive(self, client):
        mixed_case = b"First_Name,Last_Name,Domain\nJohn,Smith,acme.com\n"
        with patch("backend.worker.run_batch", new_callable=AsyncMock):
            resp = client.post("/batch", files={"file": ("test.csv", mixed_case, "text/csv")})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /jobs/{id}
# ---------------------------------------------------------------------------

class TestJobStatus:
    def test_known_job_returns_200(self, client):
        with patch("backend.main.worker.get_job") as m:
            m.return_value = Job(job_id="abc", total=2)
            resp = client.get("/jobs/abc")
        assert resp.status_code == 200
        assert resp.json()["job_id"] == "abc"

    def test_unknown_job_returns_404(self, client):
        with patch("backend.main.worker.get_job", return_value=None):
            resp = client.get("/jobs/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /jobs/{id}/cancel
# ---------------------------------------------------------------------------

class TestCancel:
    def test_unknown_job_returns_404(self, client):
        with patch("backend.main.worker.get_job", return_value=None):
            resp = client.post("/jobs/nonexistent/cancel")
        assert resp.status_code == 404

    def test_already_finished_job_returns_409(self, client):
        job = Job(job_id="abc", status=JobStatus.completed, total=1, done=1)
        with patch("backend.main.worker.get_job", return_value=job), \
             patch("backend.main.worker.cancel_job", return_value=False):
            resp = client.post("/jobs/abc/cancel")
        assert resp.status_code == 409

    def test_running_job_returns_200(self, client):
        job = Job(job_id="abc", status=JobStatus.running, total=5, done=2)
        with patch("backend.main.worker.get_job", return_value=job), \
             patch("backend.main.worker.cancel_job", return_value=True) as m:
            resp = client.post("/jobs/abc/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"job_id": "abc", "status": "cancelled"}
        m.assert_called_once_with("abc")


# ---------------------------------------------------------------------------
# GET /jobs/{id}/csv
# ---------------------------------------------------------------------------

class TestJobCSV:
    def test_completed_job_returns_csv(self, client):
        job = Job(
            job_id="abc", status=JobStatus.completed, total=1, done=1,
            results=[VERIFIED_RESULT],
        )
        with patch("backend.main.worker.get_job", return_value=job):
            resp = client.get("/jobs/abc/csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "john.smith@acme.com" in resp.text

    def test_cancelled_job_returns_csv(self, client):
        job = Job(
            job_id="abc", status=JobStatus.cancelled, total=5, done=2,
            results=[VERIFIED_RESULT, NOT_FOUND_RESULT],
        )
        with patch("backend.main.worker.get_job", return_value=job):
            resp = client.get("/jobs/abc/csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "john.smith@acme.com" in resp.text

    def test_cancelled_job_with_no_results_still_returns_header(self, client):
        job = Job(job_id="abc", status=JobStatus.cancelled, total=5, done=0, results=[])
        with patch("backend.main.worker.get_job", return_value=job):
            resp = client.get("/jobs/abc/csv")
        assert resp.status_code == 200
        assert resp.text.strip() == "first_name,last_name,domain,email,status,permutations_tried,catch_all"

    def test_pending_job_returns_202(self, client):
        job = Job(job_id="abc", status=JobStatus.running, total=5)
        with patch("backend.main.worker.get_job", return_value=job):
            resp = client.get("/jobs/abc/csv")
        assert resp.status_code == 202

    def test_unknown_job_returns_404(self, client):
        with patch("backend.main.worker.get_job", return_value=None):
            resp = client.get("/jobs/nonexistent/csv")
        assert resp.status_code == 404
