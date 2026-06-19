from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


BLOCKED_ON_QUESTIONS = "BLOCKED_ON_QUESTIONS"
NEEDS_TECHNICAL_REVIEW = "NEEDS_TECHNICAL_REVIEW"
READY_TO_RESUME = "READY_TO_RESUME"
READY_TO_SUBMIT = "READY_TO_SUBMIT"
SUBMITTING = "SUBMITTING"
SUBMITTED = "SUBMITTED"
SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"

APPLICATION_WORKFLOW_STATUSES = {
    BLOCKED_ON_QUESTIONS,
    NEEDS_TECHNICAL_REVIEW,
    READY_TO_RESUME,
    READY_TO_SUBMIT,
    SUBMITTING,
    SUBMITTED,
    SUBMIT_UNKNOWN,
}

UNANSWERED = "UNANSWERED"
APPROVED = "APPROVED"
APPLIED = "APPLIED"
DISMISSED = "DISMISSED"
TECHNICAL_REVIEW = "TECHNICAL_REVIEW"

QUESTION_STATUSES = {
    UNANSWERED,
    APPROVED,
    APPLIED,
    DISMISSED,
    TECHNICAL_REVIEW,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class DetectedQuestion:
    raw_text: str
    normalized_text: str
    fingerprint: str
    required: bool
    control_type: str
    options: list[str] = field(default_factory=list)
    validation_message: str = ""
    locator_hints: dict[str, Any] = field(default_factory=dict)
    status: str = UNANSWERED
    canonical_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QuestionBlocker:
    id: str
    batch_id: str | None
    application_id: str
    ats: str
    tenant: str | None
    company: str | None
    role: str | None
    job_url: str | None
    page_name: str | None
    stage: str
    raw_text: str
    normalized_text: str
    fingerprint: str
    canonical_key: str | None
    required: bool
    control_type: str
    options: list[str]
    validation_message: str | None
    locator_hints: dict[str, Any]
    artifacts: list[dict[str, Any]]
    status: str = UNANSWERED
    approved_answer: Any = None
    approval_scope: str | None = None
    approved_by: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
