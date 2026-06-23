from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import Enum
import hashlib
import json
from typing import Any


class OutcomeType(str, Enum):
    COMPLETE = "COMPLETE"
    BLOCKED_ON_QUESTIONS = "BLOCKED_ON_QUESTIONS"
    NEEDS_TECHNICAL_REVIEW = "NEEDS_TECHNICAL_REVIEW"
    NEEDS_RETRY = "NEEDS_RETRY"
    MY_INFORMATION_BLOCKED = "MY_INFORMATION_BLOCKED"
    MY_EXPERIENCE_BLOCKED = "MY_EXPERIENCE_BLOCKED"
    NAVIGATION_BLOCKED = "NAVIGATION_BLOCKED"
    AUTH_BLOCKED = "AUTH_BLOCKED"
    AUTOFILL_RESUME_STUCK = "AUTOFILL_RESUME_STUCK"
    WORKDAY_LOADING_STUCK = "WORKDAY_LOADING_STUCK"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"


class FieldStatus(str, Enum):
    MISSING = "missing"
    FILLED = "filled"
    BLOCKED = "blocked"
    TECHNICAL_REVIEW = "technical_review"


TERMINAL_OUTCOMES = {
    OutcomeType.COMPLETE,
    OutcomeType.BLOCKED_ON_QUESTIONS,
    OutcomeType.NEEDS_TECHNICAL_REVIEW,
    OutcomeType.MY_INFORMATION_BLOCKED,
    OutcomeType.MY_EXPERIENCE_BLOCKED,
    OutcomeType.NAVIGATION_BLOCKED,
    OutcomeType.AUTH_BLOCKED,
    OutcomeType.AUTOFILL_RESUME_STUCK,
    OutcomeType.WORKDAY_LOADING_STUCK,
    OutcomeType.READY_TO_SUBMIT,
}

GENERIC_TERMINAL_OUTCOMES = {"stage_timeout", "unknown", "no_action_found", "timeout"}


@dataclass
class FieldState:
    name: str
    status: FieldStatus | str
    value: Any = None
    source: str = ""
    required: bool = False
    group: str = ""
    last_attempt: str = ""
    evidence: str = ""
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    def normalized_status(self) -> str:
        return self.status.value if isinstance(self.status, FieldStatus) else str(self.status or "")

    def is_resolved(self) -> bool:
        return self.normalized_status() in {
            FieldStatus.FILLED.value,
            FieldStatus.BLOCKED.value,
            FieldStatus.TECHNICAL_REVIEW.value,
        }


@dataclass
class StageSnapshot:
    stage: str
    url: str = ""
    fields: list[FieldState] = dataclass_field(default_factory=list)
    required_fields: list[str] = dataclass_field(default_factory=list)
    unresolved_required_fields: list[dict[str, Any]] = dataclass_field(default_factory=list)
    validation_errors: list[str] = dataclass_field(default_factory=list)
    alerts: list[str] = dataclass_field(default_factory=list)
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    @property
    def unresolved_signature(self) -> str:
        return unresolved_signature(self.unresolved_required_fields)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "url": self.url,
            "fields": [field_to_dict(item) for item in self.fields],
            "required_fields": list(self.required_fields),
            "unresolved_required_fields": list(self.unresolved_required_fields),
            "validation_errors": list(self.validation_errors),
            "alerts": list(self.alerts),
            "unresolved_signature": self.unresolved_signature,
            "metadata": dict(self.metadata),
        }


@dataclass
class ActionResult:
    action: str
    changed: bool = False
    outcome_type: OutcomeType | str = OutcomeType.NEEDS_RETRY
    postcondition_verified: bool = False
    field: str = ""
    details: dict[str, Any] = dataclass_field(default_factory=dict)
    error: str = ""

    def normalized_outcome(self) -> str:
        return self.outcome_type.value if isinstance(self.outcome_type, OutcomeType) else str(self.outcome_type or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "changed": self.changed,
            "outcome_type": self.normalized_outcome(),
            "postcondition_verified": self.postcondition_verified,
            "field": self.field,
            "details": dict(self.details),
            "error": self.error,
        }


@dataclass
class StageResult:
    stage: str
    outcome_type: OutcomeType | str
    status: str = ""
    snapshot: StageSnapshot | None = None
    fields: list[FieldState] = dataclass_field(default_factory=list)
    unresolved_required_fields: list[dict[str, Any]] = dataclass_field(default_factory=list)
    validation_errors: list[str] = dataclass_field(default_factory=list)
    alerts: list[str] = dataclass_field(default_factory=list)
    blockers: list[dict[str, Any]] = dataclass_field(default_factory=list)
    actions: list[ActionResult] = dataclass_field(default_factory=list)
    message: str = ""
    terminal: bool = True
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    def normalized_outcome(self) -> str:
        return self.outcome_type.value if isinstance(self.outcome_type, OutcomeType) else str(self.outcome_type or "")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "stage": self.stage,
            "outcome_type": self.normalized_outcome(),
            "status": self.status,
            "fields": [field_to_dict(item) for item in self.fields],
            "unresolved_required_fields": list(self.unresolved_required_fields),
            "validation_errors": list(self.validation_errors),
            "alerts": list(self.alerts),
            "blockers": list(self.blockers),
            "actions": [item.to_dict() for item in self.actions],
            "message": self.message,
            "terminal": self.terminal,
            "metadata": dict(self.metadata),
        }
        if self.snapshot is not None:
            result["snapshot"] = self.snapshot.to_dict()
        return result


class BaseStageController:
    stage = "workday"

    def observe(self, context: dict[str, Any]) -> StageSnapshot:
        raise NotImplementedError

    def plan(self, snapshot: StageSnapshot) -> list[ActionResult]:
        raise NotImplementedError

    def execute(self, context: dict[str, Any], actions: list[ActionResult]) -> list[ActionResult]:
        raise NotImplementedError

    def verify(self, snapshot: StageSnapshot, actions: list[ActionResult]) -> StageResult:
        raise NotImplementedError

    def run_once(self, context: dict[str, Any]) -> StageResult:
        snapshot = self.observe(context)
        actions = self.plan(snapshot)
        executed = self.execute(context, actions)
        result = self.verify(snapshot, executed)
        validate_stage_result(result)
        return result


def field_to_dict(item: FieldState) -> dict[str, Any]:
    return {
        "name": item.name,
        "status": item.normalized_status(),
        "value": item.value,
        "source": item.source,
        "required": item.required,
        "group": item.group,
        "last_attempt": item.last_attempt,
        "evidence": item.evidence,
        "metadata": dict(item.metadata),
    }


def unresolved_signature(items: list[dict[str, Any]]) -> str:
    canonical = json.dumps(items or [], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def validate_stage_result(result: StageResult, *, confirm_submit: bool = False) -> None:
    outcome = result.normalized_outcome()
    if outcome.lower() in GENERIC_TERMINAL_OUTCOMES:
        raise ValueError(f"generic Workday terminal outcome is not allowed: {outcome}")
    unresolved = result.unresolved_required_fields
    if result.snapshot is not None and not unresolved:
        unresolved = result.snapshot.unresolved_required_fields
    if outcome == OutcomeType.COMPLETE.value and unresolved:
        raise ValueError("complete Workday stage cannot have unresolved required fields")
    for item in result.fields:
        if item.required and not item.is_resolved():
            raise ValueError(f"required field is not resolved: {item.name}")
    if outcome == OutcomeType.READY_TO_SUBMIT.value and (not confirm_submit or unresolved):
        raise ValueError("ready-to-submit requires confirm_submit=true and zero unresolved blockers")


def validate_llm_classification(payload: dict[str, Any]) -> None:
    forbidden = {"answer", "action", "click", "selector", "value_to_fill"}
    present = sorted(forbidden.intersection(payload))
    if present:
        raise ValueError(f"LLM classifier returned forbidden keys: {', '.join(present)}")
    required = {"canonical_key", "risk_level", "confidence"}
    missing = sorted(key for key in required if key not in payload)
    if missing:
        raise ValueError(f"LLM classifier missing required keys: {', '.join(missing)}")
