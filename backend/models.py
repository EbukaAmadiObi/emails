from enum import Enum
from typing import Optional
from pydantic import BaseModel, field_validator


class LookupStatus(str, Enum):
    verified = "verified"
    not_found = "not_found"
    unverifiable = "unverifiable"   # catch-all domain
    inconclusive = "inconclusive"   # SMTP errors / sender rejected
    timed_out = "timed_out"         # per-contact deadline exceeded


class LookupRequest(BaseModel):
    first: str
    last: str
    domain: str

    @field_validator("first", "last")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("domain")
    @classmethod
    def domain_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("must not be empty")
        if " " in v:
            raise ValueError("must not contain spaces")
        return v


class LookupResult(BaseModel):
    first: str
    last: str
    domain: str
    email: Optional[str] = None
    status: LookupStatus
    permutations_tried: int = 0
    catch_all: bool = False


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class Job(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.pending
    progress: float = 0.0   # 0.0 – 1.0
    total: int = 0
    done: int = 0
    results: list[LookupResult] = []
