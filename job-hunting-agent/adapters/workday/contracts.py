from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import Enum
import hashlib
import json
from typing import Any


class WorkdayStage(str, Enum):
    AUTH = "AUTH"
    NAVIGATION = "NAVIGATION"
    START_APPLICATION = "START_APPLICATION"
    MY_INFORMATION = "MY_INFORMATION"
    MY_EXPERIENCE = "MY_EXPERIENCE"
    APPLICATION_QUESTIONS = "APPLICATION_QUESTIONS"
    VOLUNTARY_DISCLOSURES = "VOLUNTARY_DISCLOSURES"
    SELF_IDENTIFY = "SELF_IDENTIFY"
    REVIEW = "REVIEW"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"


class OutcomeType(str, Enum):
    COMPLETE = "COMPLETE"
    AUTH_BLOCKED = "AUTH_BLOCKED"
    NAVIGATION_BLOCKED = "NAVIGATION_BLOCKED"
    LOADING_STUCK = "LOADING_STUCK"
    AUTOFILL_RESUME_STUCK = "AUTOFILL_RESUME_STUCK"
    MY_INFORMATION_BLOCKED = "MY_INFORMATION_BLOCKED"
    MY_EXPERIENCE_BLOCKED = "MY_EXPERIENCE_BLOCKED"
    BLOCKED_ON_QUESTIONS = "BLOCKED_ON_QUESTIONS"
    NEEDS_TECHNICAL_REVIEW = "NEEDS_TECHNICAL_REVIEW"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTED = "SUBMITTED"
    RETRYABLE = "RETRYABLE"

    # Backward-compatible aliases for the previous shadow-controller contract.
    NEEDS_RETRY = "RETRYABLE"
    WORKDAY_LOADING_STUCK = "LOADING_STUCK"


class FieldStatus(str, Enum):
    UNKNOWN = "unknown"
    LOADING = "loading"
    MISSING = "missing"
    FILLED = "filled"
    OPTIONAL = "optional"
    BLOCKED = "blocked"
    TECHNICAL_REVIEW = "technical_review"


class GroupStatus(str, Enum):
    UNKNOWN = "unknown"
    LOADING = "loading"
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    TECHNICAL_REVIEW = "technical_review"


TERMINAL_OUTCOMES = {
    OutcomeType.COMPLETE,
    OutcomeType.AUTH_BLOCKED,
    OutcomeType.NAVIGATION_BLOCKED,
    OutcomeType.LOADING_STUCK,
    OutcomeType.AUTOFILL_RESUME_STUCK,
    OutcomeType.MY_INFORMATION_BLOCKED,
    OutcomeType.MY_EXPERIENCE_BLOCKED,
    OutcomeType.BLOCKED_ON_QUESTIONS,
    OutcomeType.NEEDS_TECHNICAL_REVIEW,
    OutcomeType.READY_TO_SUBMIT,
    OutcomeType.SUBMITTED,
}
RETRYABLE_OUTCOMES = {OutcomeType.RETRYABLE}
ALLOWED_OUTCOME_VALUES = {item.value for item in OutcomeType}
GENERIC_TERMINAL_OUTCOMES = {"stage_timeout", "unknown", "no_action_found", "timeout"}

_DROP = object()
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "otp",
    "password",
    "secret",
    "session",
    "storage_state",
    "token",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _normalized_enum_value(value: Any, default: str = "") -> str:
    value = _enum_value(value)
    return str(value if value is not None else default)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple) or isinstance(value, set):
        return list(value)
    return [value]


def _sanitize_for_json(value: Any, key: str = "") -> Any:
    if key and _is_sensitive_key(key):
        return _DROP
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _sanitize_for_json(value.to_dict(), key=key)
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            safe_key = str(raw_key)
            safe_value = _sanitize_for_json(raw_value, key=safe_key)
            if safe_value is not _DROP:
                cleaned[safe_key] = safe_value
        return cleaned
    if isinstance(value, (list, tuple, set)):
        cleaned_list = []
        for item in value:
            safe_item = _sanitize_for_json(item)
            if safe_item is not _DROP:
                cleaned_list.append(safe_item)
        return cleaned_list
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _clean_dict(payload: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_for_json(payload)


@dataclass
class FieldState:
    canonical_key: str = ""
    label: str = ""
    required: bool = False
    status: FieldStatus | str = FieldStatus.UNKNOWN
    visible_value: Any = None
    expected_value: Any = None
    options: list[Any] = dataclass_field(default_factory=list)
    source: str = ""
    locator_hints: list[str] = dataclass_field(default_factory=list)
    validation_messages: list[str] = dataclass_field(default_factory=list)
    last_action: str = ""
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    # Legacy names kept for the existing shadow replay contract.
    name: str = ""
    value: Any = None
    group: str = ""
    last_attempt: str = ""
    evidence: str = ""

    def __post_init__(self) -> None:
        if not self.canonical_key:
            self.canonical_key = self.name
        if not self.name:
            self.name = self.canonical_key
        if self.visible_value is None and self.value is not None:
            self.visible_value = self.value
        if self.value is None and self.visible_value is not None:
            self.value = self.visible_value
        if not self.last_action and self.last_attempt:
            self.last_action = self.last_attempt
        self.options = _as_list(self.options)
        self.locator_hints = [str(item) for item in _as_list(self.locator_hints)]
        self.validation_messages = [str(item) for item in _as_list(self.validation_messages)]

    def normalized_status(self) -> str:
        return _normalized_enum_value(self.status, FieldStatus.UNKNOWN.value)

    def is_resolved(self) -> bool:
        return self.normalized_status() in {
            FieldStatus.FILLED.value,
            FieldStatus.OPTIONAL.value,
            FieldStatus.BLOCKED.value,
            FieldStatus.TECHNICAL_REVIEW.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return field_to_dict(self)


@dataclass
class GroupState:
    canonical_key: str
    required: bool = False
    status: GroupStatus | str = GroupStatus.UNKNOWN
    fields: list[FieldState] = dataclass_field(default_factory=list)
    unresolved_fields: list[str] = dataclass_field(default_factory=list)
    validation_messages: list[str] = dataclass_field(default_factory=list)
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        self.fields = [
            item if isinstance(item, FieldState) else FieldState(**item)
            for item in _as_list(self.fields)
        ]
        self.unresolved_fields = [str(item) for item in _as_list(self.unresolved_fields)]
        self.validation_messages = [str(item) for item in _as_list(self.validation_messages)]

    def normalized_status(self) -> str:
        return _normalized_enum_value(self.status, GroupStatus.UNKNOWN.value)

    def to_dict(self) -> dict[str, Any]:
        return _clean_dict(
            {
                "canonical_key": self.canonical_key,
                "required": self.required,
                "status": self.normalized_status(),
                "fields": [item.to_dict() for item in self.fields],
                "unresolved_fields": list(self.unresolved_fields),
                "validation_messages": list(self.validation_messages),
                "metadata": dict(self.metadata),
            }
        )


@dataclass
class StageSnapshot:
    stage: WorkdayStage | str
    url: str = ""
    page_heading: str = ""
    groups: list[GroupState] = dataclass_field(default_factory=list)
    required_fields: list[Any] = dataclass_field(default_factory=list)
    validation_errors: list[str] = dataclass_field(default_factory=list)
    alerts: list[str] = dataclass_field(default_factory=list)
    visible_actions: list[str] = dataclass_field(default_factory=list)
    loading_indicators: list[str] = dataclass_field(default_factory=list)
    screenshot_path: str = ""
    dom_excerpt: str = ""
    accessibility_excerpt: str = ""
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    # Legacy names kept for existing fixture replay code.
    fields: list[FieldState] = dataclass_field(default_factory=list)
    unresolved_required_fields: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def __post_init__(self) -> None:
        self.groups = [
            item if isinstance(item, GroupState) else GroupState(**item)
            for item in _as_list(self.groups)
        ]
        self.fields = [
            item if isinstance(item, FieldState) else FieldState(**item)
            for item in _as_list(self.fields)
        ]
        self.required_fields = _as_list(self.required_fields)
        self.validation_errors = [str(item) for item in _as_list(self.validation_errors)]
        self.alerts = [str(item) for item in _as_list(self.alerts)]
        self.visible_actions = [str(item) for item in _as_list(self.visible_actions)]
        self.loading_indicators = [str(item) for item in _as_list(self.loading_indicators)]
        self.unresolved_required_fields = [
            item for item in _as_list(self.unresolved_required_fields) if isinstance(item, dict)
        ]

    @property
    def unresolved_signature(self) -> str:
        return unresolved_signature(self.unresolved_required_fields)

    def all_fields(self) -> list[FieldState]:
        grouped = [field for group in self.groups for field in group.fields]
        return list(self.fields) + grouped

    def to_dict(self) -> dict[str, Any]:
        return _clean_dict(
            {
                "stage": _normalized_enum_value(self.stage),
                "url": self.url,
                "page_heading": self.page_heading,
                "groups": [group.to_dict() for group in self.groups],
                "required_fields": list(self.required_fields),
                "validation_errors": list(self.validation_errors),
                "alerts": list(self.alerts),
                "visible_actions": list(self.visible_actions),
                "loading_indicators": list(self.loading_indicators),
                "screenshot_path": self.screenshot_path,
                "dom_excerpt": self.dom_excerpt,
                "accessibility_excerpt": self.accessibility_excerpt,
                "metadata": dict(self.metadata),
                "fields": [field_to_dict(item) for item in self.fields],
                "unresolved_required_fields": list(self.unresolved_required_fields),
                "unresolved_signature": self.unresolved_signature,
            }
        )


@dataclass
class ActionResult:
    acted: bool = False
    verified: bool = False
    action: str = ""
    target: str = ""
    value: Any = None
    before: Any = None
    after: Any = None
    reason: str = ""
    retryable: bool = False
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    # Legacy names kept for existing shadow replay code.
    changed: bool = False
    outcome_type: OutcomeType | str = OutcomeType.RETRYABLE
    postcondition_verified: bool = False
    field: str = ""
    details: dict[str, Any] = dataclass_field(default_factory=dict)
    error: str = ""

    def __post_init__(self) -> None:
        if self.changed:
            self.acted = True
        if self.postcondition_verified:
            self.verified = True
        if self.field and not self.target:
            self.target = self.field

    def normalized_outcome(self) -> str:
        return _normalized_enum_value(self.outcome_type)

    def to_dict(self) -> dict[str, Any]:
        return _clean_dict(
            {
                "acted": self.acted,
                "verified": self.verified,
                "action": self.action,
                "target": self.target,
                "value": self.value,
                "before": self.before,
                "after": self.after,
                "reason": self.reason,
                "retryable": self.retryable,
                "metadata": dict(self.metadata),
                "changed": self.changed,
                "outcome_type": self.normalized_outcome(),
                "postcondition_verified": self.postcondition_verified,
                "field": self.field,
                "details": dict(self.details),
                "error": self.error,
            }
        )


@dataclass
class StageResult:
    outcome_type: OutcomeType | str
    stage: WorkdayStage | str
    complete: bool = False
    snapshot: StageSnapshot | None = None
    unresolved_required_groups: list[str] = dataclass_field(default_factory=list)
    unresolved_required_fields: list[dict[str, Any]] = dataclass_field(default_factory=list)
    validation_errors: list[str] = dataclass_field(default_factory=list)
    alerts: list[str] = dataclass_field(default_factory=list)
    actions: list[ActionResult] = dataclass_field(default_factory=list)
    blocked_reason: str = ""
    needs_user_action: str = ""
    screenshot_path: str = ""
    dom_excerpt: str = ""
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    # Legacy names kept for existing shadow replay code.
    status: str = ""
    fields: list[FieldState] = dataclass_field(default_factory=list)
    blockers: list[dict[str, Any]] = dataclass_field(default_factory=list)
    message: str = ""
    terminal: bool = True

    def __post_init__(self) -> None:
        self.unresolved_required_groups = [str(item) for item in _as_list(self.unresolved_required_groups)]
        self.unresolved_required_fields = [
            item for item in _as_list(self.unresolved_required_fields) if isinstance(item, dict)
        ]
        self.validation_errors = [str(item) for item in _as_list(self.validation_errors)]
        self.alerts = [str(item) for item in _as_list(self.alerts)]
        self.actions = [
            item if isinstance(item, ActionResult) else ActionResult(**item)
            for item in _as_list(self.actions)
        ]
        self.fields = [
            item if isinstance(item, FieldState) else FieldState(**item)
            for item in _as_list(self.fields)
        ]
        if self.snapshot is not None:
            if not self.unresolved_required_fields:
                self.unresolved_required_fields = list(self.snapshot.unresolved_required_fields)
            if not self.validation_errors:
                self.validation_errors = list(self.snapshot.validation_errors)
            if not self.alerts:
                self.alerts = list(self.snapshot.alerts)
            if not self.screenshot_path:
                self.screenshot_path = self.snapshot.screenshot_path
            if not self.dom_excerpt:
                self.dom_excerpt = self.snapshot.dom_excerpt
            if not self.fields:
                self.fields = list(self.snapshot.fields)
        if self.normalized_outcome() in {OutcomeType.COMPLETE.value, OutcomeType.SUBMITTED.value}:
            self.complete = True

    def normalized_outcome(self) -> str:
        return _normalized_enum_value(self.outcome_type)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "outcome_type": self.normalized_outcome(),
            "stage": _normalized_enum_value(self.stage),
            "complete": self.complete,
            "unresolved_required_groups": list(self.unresolved_required_groups),
            "unresolved_required_fields": list(self.unresolved_required_fields),
            "validation_errors": list(self.validation_errors),
            "alerts": list(self.alerts),
            "actions": [item.to_dict() for item in self.actions],
            "blocked_reason": self.blocked_reason,
            "needs_user_action": self.needs_user_action,
            "screenshot_path": self.screenshot_path,
            "dom_excerpt": self.dom_excerpt,
            "metadata": dict(self.metadata),
            "status": self.status,
            "fields": [field_to_dict(item) for item in self.fields],
            "blockers": list(self.blockers),
            "message": self.message,
            "terminal": self.terminal,
        }
        if self.snapshot is not None:
            result["snapshot"] = self.snapshot.to_dict()
        return _clean_dict(result)


class BaseStageController:
    stage: WorkdayStage | str = WorkdayStage.UNKNOWN

    def observe(self, page: Any, context: dict[str, Any] | None = None) -> StageSnapshot:
        raise NotImplementedError

    def plan(self, snapshot: StageSnapshot, context: dict[str, Any] | None = None) -> list[Any]:
        raise NotImplementedError

    def execute(self, page: Any, action: Any, context: dict[str, Any] | None = None) -> ActionResult:
        raise NotImplementedError

    def verify(
        self,
        page: Any,
        previous_snapshot: StageSnapshot,
        context: dict[str, Any] | None = None,
    ) -> StageResult:
        raise NotImplementedError

    def run_pass(self, page: Any, context: dict[str, Any] | None = None) -> StageResult:
        context = context or {}
        previous_snapshot = self.observe(page, context)
        planned_actions = self.plan(previous_snapshot, context)
        action_results = [self.execute(page, action, context) for action in planned_actions]
        result = self.verify(page, previous_snapshot, context)
        if action_results and not result.actions:
            result.actions = action_results
        validate_stage_result(result)

        from .state_signature import has_meaningful_progress

        if result.snapshot is not None and has_meaningful_progress(previous_snapshot, result.snapshot):
            return result
        if result.normalized_outcome() in ALLOWED_OUTCOME_VALUES:
            return result
        raise ValueError("Workday controller pass returned without progress or typed outcome")

    def run_once(self, context: dict[str, Any]) -> StageResult:
        """Compatibility hook for the existing shadow fixture controllers."""
        snapshot = self.observe(context)  # type: ignore[misc]
        actions = self.plan(snapshot)  # type: ignore[misc]
        executed = self.execute(context, actions)  # type: ignore[misc]
        result = self.verify(snapshot, executed)  # type: ignore[misc]
        validate_stage_result(result)
        return result


def field_to_dict(item: FieldState) -> dict[str, Any]:
    return _clean_dict(
        {
            "canonical_key": item.canonical_key,
            "label": item.label,
            "required": item.required,
            "status": item.normalized_status(),
            "visible_value": item.visible_value,
            "expected_value": item.expected_value,
            "options": list(item.options),
            "source": item.source,
            "locator_hints": list(item.locator_hints),
            "validation_messages": list(item.validation_messages),
            "last_action": item.last_action,
            "metadata": dict(item.metadata),
            "name": item.name,
            "value": item.value,
            "group": item.group,
            "last_attempt": item.last_attempt,
            "evidence": item.evidence,
        }
    )


def unresolved_signature(items: list[dict[str, Any]]) -> str:
    canonical = json.dumps(_sanitize_for_json(items or []), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def validate_stage_result(result: StageResult, *, confirm_submit: bool = False) -> None:
    outcome = result.normalized_outcome()
    if outcome.lower() in GENERIC_TERMINAL_OUTCOMES:
        raise ValueError(f"generic Workday terminal outcome is not allowed: {outcome}")
    if outcome not in ALLOWED_OUTCOME_VALUES:
        raise ValueError(f"unsupported Workday outcome type: {outcome}")
    unresolved = result.unresolved_required_fields
    if result.snapshot is not None and not unresolved:
        unresolved = result.snapshot.unresolved_required_fields
    if outcome == OutcomeType.COMPLETE.value and unresolved:
        raise ValueError("complete Workday stage cannot have unresolved required fields")
    for item in result.fields:
        if item.required and not item.is_resolved():
            raise ValueError(f"required field is not resolved: {item.canonical_key or item.name}")
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
