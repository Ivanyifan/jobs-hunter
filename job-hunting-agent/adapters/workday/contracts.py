from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import Enum
import hashlib
import json
import re
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
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)")
_STREET_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9 .'-]+\s+"
    r"(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd|court|ct|way)\b",
    re.IGNORECASE,
)
_PASSWORD_RE = re.compile(r"(?i)\b(password\s*[:=]\s*)[^\s;]+")
_OTP_RE = re.compile(r"(?i)\b((?:otp|code)\s*[:=]?\s*)\d{4,8}\b")
_TOKEN_RE = re.compile(r"(?i)\b((?:cookie|token|session(?:id)?)\s*[:=]\s*)[^\s;]+")


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


def _stable_unresolved_key(item: dict[str, Any]) -> str:
    return str(
        item.get("canonical_key")
        or item.get("field")
        or item.get("name")
        or item.get("group")
        or item.get("label")
        or ""
    )


def normalize_unresolved_required_fields(items: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in _as_list(items):
        if item is None:
            continue
        if isinstance(item, str):
            normalized.append({"canonical_key": item})
            continue
        if isinstance(item, dict):
            copied = dict(item)
            if not copied.get("canonical_key"):
                copied["canonical_key"] = _stable_unresolved_key(copied)
            if not copied.get("canonical_key") and len(copied) == 1:
                only_value = next(iter(copied.values()))
                copied["canonical_key"] = str(only_value)
            normalized.append(copied)
            continue
        normalized.append({"canonical_key": str(item)})
    return normalized


def _stable_required_field_key(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return _stable_unresolved_key(item)
    for attr in ("canonical_key", "field", "name", "group", "label"):
        value = getattr(item, attr, "")
        if value:
            return str(value)
    return str(item)


def normalize_required_field_keys(items: Any) -> list[str]:
    normalized: list[str] = []
    for item in _as_list(items):
        key = _stable_required_field_key(item)
        if key:
            normalized.append(key)
    return normalized


def _redact_text(value: str) -> str:
    value = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    value = _STREET_RE.sub("[REDACTED_ADDRESS]", value)

    def redact_phone(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        return "[REDACTED_PHONE]" if len(digits) >= 10 else match.group(0)

    value = _PHONE_RE.sub(redact_phone, value)
    value = _PASSWORD_RE.sub(r"\1[REDACTED_SECRET]", value)
    value = _OTP_RE.sub(r"\1[REDACTED_OTP]", value)
    value = _TOKEN_RE.sub(r"\1[REDACTED_SECRET]", value)
    return value


def safe_diagnostic_value(value: Any, key: str = "") -> Any:
    if key and _is_sensitive_key(key):
        return "[REDACTED_SECRET]"
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_safe_diagnostics") and callable(value.to_safe_diagnostics):
        return value.to_safe_diagnostics()
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return safe_diagnostic_value(value.to_dict(), key=key)
    if isinstance(value, dict):
        return {str(item_key): safe_diagnostic_value(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe_diagnostic_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return _redact_text(str(value))


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

    def is_satisfied(self) -> bool:
        status = self.normalized_status()
        return status == FieldStatus.FILLED.value or (status == FieldStatus.OPTIONAL.value and not self.required)

    def is_terminally_accounted_for(self) -> bool:
        return self.normalized_status() in {
            FieldStatus.FILLED.value,
            FieldStatus.OPTIONAL.value,
            FieldStatus.BLOCKED.value,
            FieldStatus.TECHNICAL_REVIEW.value,
        }

    def is_resolved(self) -> bool:
        return self.is_terminally_accounted_for()

    def to_dict(self) -> dict[str, Any]:
        return field_to_dict(self)

    def to_safe_diagnostics(self) -> dict[str, Any]:
        return safe_diagnostic_value(self.to_dict())


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

    def is_satisfied(self) -> bool:
        return self.normalized_status() == GroupStatus.COMPLETE.value

    def is_terminally_accounted_for(self) -> bool:
        return self.normalized_status() in {
            GroupStatus.COMPLETE.value,
            GroupStatus.BLOCKED.value,
            GroupStatus.TECHNICAL_REVIEW.value,
        }

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

    def to_safe_diagnostics(self) -> dict[str, Any]:
        return safe_diagnostic_value(self.to_dict())


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
    unresolved_required_fields: list[Any] = dataclass_field(default_factory=list)

    def __post_init__(self) -> None:
        self.groups = [
            item if isinstance(item, GroupState) else GroupState(**item)
            for item in _as_list(self.groups)
        ]
        self.fields = [
            item if isinstance(item, FieldState) else FieldState(**item)
            for item in _as_list(self.fields)
        ]
        self.required_fields = normalize_required_field_keys(self.required_fields)
        self.validation_errors = [str(item) for item in _as_list(self.validation_errors)]
        self.alerts = [str(item) for item in _as_list(self.alerts)]
        self.visible_actions = [str(item) for item in _as_list(self.visible_actions)]
        self.loading_indicators = [str(item) for item in _as_list(self.loading_indicators)]
        self.unresolved_required_fields = normalize_unresolved_required_fields(self.unresolved_required_fields)

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

    def to_safe_diagnostics(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["dom_excerpt"] = safe_diagnostic_value(self.dom_excerpt)
        payload["accessibility_excerpt"] = safe_diagnostic_value(self.accessibility_excerpt)
        return safe_diagnostic_value(payload)


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
        self.acted = bool(self.acted or self.changed)
        self.changed = self.acted
        self.verified = bool(self.verified or self.postcondition_verified)
        self.postcondition_verified = self.verified
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

    def to_safe_diagnostics(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["before"] = safe_diagnostic_value(self.before)
        payload["after"] = safe_diagnostic_value(self.after)
        payload["value"] = safe_diagnostic_value(self.value)
        return safe_diagnostic_value(payload)


@dataclass
class StageResult:
    outcome_type: OutcomeType | str
    stage: WorkdayStage | str
    complete: bool = False
    snapshot: StageSnapshot | None = None
    unresolved_required_groups: list[str] = dataclass_field(default_factory=list)
    unresolved_required_fields: list[Any] = dataclass_field(default_factory=list)
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
        self.unresolved_required_fields = normalize_unresolved_required_fields(self.unresolved_required_fields)
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
            merged_unresolved = list(self.unresolved_required_fields)
            merged_unresolved.extend(normalize_unresolved_required_fields(self.snapshot.unresolved_required_fields))
            for group in self.snapshot.groups:
                merged_unresolved.extend({"canonical_key": item, "group": group.canonical_key} for item in group.unresolved_fields)
            self.unresolved_required_fields = _dedupe_unresolved_fields(merged_unresolved)
            self.validation_errors = _dedupe_strings([*self.validation_errors, *_snapshot_validation_messages(self.snapshot)])
            self.alerts = _dedupe_strings([*self.alerts, *self.snapshot.alerts])
            if not self.screenshot_path:
                self.screenshot_path = self.snapshot.screenshot_path
            if not self.dom_excerpt:
                self.dom_excerpt = self.snapshot.dom_excerpt
            if not self.fields:
                self.fields = list(self.snapshot.fields)

    def normalized_outcome(self) -> str:
        return _normalized_enum_value(self.outcome_type)

    def to_dict(self) -> dict[str, Any]:
        validation_errors = _result_validation_errors(self)
        alerts = _dedupe_strings([
            *self.alerts,
            *((self.snapshot.alerts if self.snapshot is not None else [])),
        ])
        result = {
            "outcome_type": self.normalized_outcome(),
            "stage": _normalized_enum_value(self.stage),
            "complete": self.complete,
            "unresolved_required_groups": _dedupe_strings([*self.unresolved_required_groups, *_result_unresolved_group_keys(self)]),
            "unresolved_required_fields": _result_unresolved_fields(self),
            "validation_errors": validation_errors,
            "alerts": alerts,
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

    def to_safe_diagnostics(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["dom_excerpt"] = safe_diagnostic_value(self.dom_excerpt)
        payload["actions"] = [action.to_safe_diagnostics() for action in self.actions]
        if self.snapshot is not None:
            payload["snapshot"] = self.snapshot.to_safe_diagnostics()
        return safe_diagnostic_value(payload)


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
    canonical = json.dumps(
        _sanitize_for_json(_dedupe_unresolved_fields(items or [])),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _dedupe_strings(items: Any) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in _as_list(items):
        text = str(item)
        if text and text not in seen:
            seen.add(text)
            deduped.append(text)
    return deduped


def _is_blank_unresolved_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge_jsonish_values(existing: Any, incoming: Any) -> Any:
    if _is_blank_unresolved_value(existing):
        return incoming
    if _is_blank_unresolved_value(incoming):
        return existing
    if isinstance(existing, list) and isinstance(incoming, list):
        merged = list(existing)
        seen = {json.dumps(_sanitize_for_json(item), sort_keys=True, default=str) for item in merged}
        for item in incoming:
            key = json.dumps(_sanitize_for_json(item), sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                merged.append(item)
        return merged
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = dict(existing)
        for key, value in incoming.items():
            merged[key] = _merge_jsonish_values(merged.get(key), value) if key in merged else value
        return merged
    return existing


def _merge_unresolved_record(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key in merged:
            merged[key] = _merge_jsonish_values(merged[key], value)
        else:
            merged[key] = value
    return merged


def _dedupe_unresolved_fields(items: Any) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for item in normalize_unresolved_required_fields(items):
        key = item.get("canonical_key") or json.dumps(item, sort_keys=True, default=str)
        key = str(key)
        deduped[key] = _merge_unresolved_record(deduped[key], item) if key in deduped else item
    return list(deduped.values())


def _unresolved_field_keys(items: Any) -> set[str]:
    return {item.get("canonical_key", "") for item in normalize_unresolved_required_fields(items) if item.get("canonical_key")}


def _result_unresolved_fields(result: StageResult) -> list[dict[str, Any]]:
    unresolved = normalize_unresolved_required_fields(result.unresolved_required_fields)
    if result.snapshot is not None:
        unresolved.extend(normalize_unresolved_required_fields(result.snapshot.unresolved_required_fields))
        for group in result.snapshot.groups:
            unresolved.extend({"canonical_key": item, "group": group.canonical_key} for item in group.unresolved_fields)
    return _dedupe_unresolved_fields(unresolved)


def _result_unresolved_group_keys(result: StageResult) -> set[str]:
    keys = {str(item) for item in _as_list(result.unresolved_required_groups) if str(item)}
    if result.snapshot is not None:
        for group in result.snapshot.groups:
            if group.required and not group.is_satisfied():
                keys.add(group.canonical_key)
    return keys


def _result_fields(result: StageResult) -> list[FieldState]:
    fields = list(result.fields)
    if result.snapshot is not None:
        fields.extend(result.snapshot.fields)
        for group in result.snapshot.groups:
            fields.extend(group.fields)
    return fields


def _field_key(field: FieldState) -> str:
    return field.canonical_key or field.name


def _result_field_by_key(result: StageResult) -> dict[str, FieldState]:
    fields: dict[str, FieldState] = {}
    for field in _result_fields(result):
        key = _field_key(field)
        if not key:
            continue
        current = fields.get(key)
        if current is None or (not current.is_satisfied() and field.is_satisfied()):
            fields[key] = field
    return fields


def _result_declared_required_field_keys(result: StageResult) -> list[str]:
    if result.snapshot is None:
        return []
    return _dedupe_strings(result.snapshot.required_fields)


def _field_satisfied_for_required_key(field: FieldState) -> bool:
    return field.normalized_status() == FieldStatus.FILLED.value


def _snapshot_validation_messages(snapshot: StageSnapshot | None) -> list[str]:
    if snapshot is None:
        return []
    messages: list[str] = list(snapshot.validation_errors)
    for field in snapshot.fields:
        messages.extend(field.validation_messages)
    for group in snapshot.groups:
        messages.extend(group.validation_messages)
        for field in group.fields:
            messages.extend(field.validation_messages)
    return _dedupe_strings(messages)


def _result_validation_errors(result: StageResult) -> list[str]:
    return _dedupe_strings([*result.validation_errors, *_snapshot_validation_messages(result.snapshot)])


def _result_required_groups(result: StageResult) -> list[GroupState]:
    if result.snapshot is None:
        return []
    return [group for group in result.snapshot.groups if group.required]


def _group_explicitly_represented(group: GroupState, result: StageResult, unresolved_field_keys: set[str]) -> bool:
    if group.canonical_key in result.unresolved_required_groups:
        return True
    if group.canonical_key in unresolved_field_keys:
        return True
    if group.unresolved_fields:
        return True
    return any(field.group == group.canonical_key and (field.canonical_key in unresolved_field_keys) for field in _result_fields(result))


def validate_stage_result(result: StageResult, *, confirm_submit: bool = False) -> None:
    outcome = result.normalized_outcome()
    if outcome.lower() in GENERIC_TERMINAL_OUTCOMES:
        raise ValueError(f"generic Workday terminal outcome is not allowed: {outcome}")
    if outcome not in ALLOWED_OUTCOME_VALUES:
        raise ValueError(f"unsupported Workday outcome type: {outcome}")

    if outcome == OutcomeType.COMPLETE.value:
        if not result.complete:
            raise ValueError("complete outcome requires complete=true")
        if not result.terminal:
            raise ValueError("complete outcome requires terminal=true")
    elif outcome == OutcomeType.READY_TO_SUBMIT.value:
        if result.complete:
            raise ValueError("ready-to-submit must keep complete=false while waiting for confirmation")
        if not result.terminal:
            raise ValueError("ready-to-submit requires terminal=true")
    elif outcome == OutcomeType.SUBMITTED.value:
        if not confirm_submit:
            raise ValueError("submitted requires confirm_submit=true")
        if not result.complete:
            raise ValueError("submitted requires complete=true")
        if not result.terminal:
            raise ValueError("submitted requires terminal=true")
    elif outcome == OutcomeType.RETRYABLE.value:
        if result.complete:
            raise ValueError("retryable outcome requires complete=false")
        if result.terminal:
            raise ValueError("retryable outcome requires terminal=false")
    else:
        if result.complete:
            raise ValueError(f"{outcome} outcome requires complete=false")
        if not result.terminal:
            raise ValueError(f"{outcome} outcome requires terminal=true")

    unresolved = _result_unresolved_fields(result)
    unresolved_keys = _unresolved_field_keys(unresolved)
    unresolved_groups = _result_unresolved_group_keys(result)
    required_fields = [field for field in _result_fields(result) if field.required]
    field_by_key = _result_field_by_key(result)
    declared_required_keys = _result_declared_required_field_keys(result)
    required_groups = _result_required_groups(result)
    submit_ready_outcomes = {
        OutcomeType.COMPLETE.value,
        OutcomeType.READY_TO_SUBMIT.value,
        OutcomeType.SUBMITTED.value,
    }

    if outcome in submit_ready_outcomes:
        if unresolved:
            outcome_label = "complete" if outcome == OutcomeType.COMPLETE.value else outcome
            raise ValueError(f"{outcome_label} Workday stage cannot have unresolved required fields")
        if result.unresolved_required_groups:
            outcome_label = "complete" if outcome == OutcomeType.COMPLETE.value else outcome
            raise ValueError(f"{outcome_label} Workday stage cannot have unresolved required groups")
        for group in required_groups:
            if not group.is_satisfied():
                raise ValueError(f"required group is not complete: {group.canonical_key}")
        for key in declared_required_keys:
            field = field_by_key.get(key)
            if field is None:
                raise ValueError(f"required field state missing: {key}")
            if not _field_satisfied_for_required_key(field):
                raise ValueError(f"required field is not satisfied: {key}")
        for field in required_fields:
            if not field.is_satisfied():
                raise ValueError(f"required field is not satisfied: {field.canonical_key or field.name}")
        return

    for key in declared_required_keys:
        field = field_by_key.get(key)
        if field is not None and _field_satisfied_for_required_key(field):
            continue
        if key not in unresolved_keys:
            raise ValueError(f"required field is not satisfied or explicitly unresolved: {key}")

    for field in required_fields:
        key = field.canonical_key or field.name
        if not field.is_terminally_accounted_for() and key not in unresolved_keys:
            raise ValueError(f"required field is not resolved or explicitly unresolved: {key}")
    if outcome != OutcomeType.RETRYABLE.value:
        for group in required_groups:
            if not group.is_satisfied() and not _group_explicitly_represented(group, result, unresolved_keys):
                raise ValueError(f"required group is not explicitly represented: {group.canonical_key}")


def validate_llm_classification(payload: dict[str, Any]) -> None:
    forbidden = {"answer", "action", "click", "selector", "value_to_fill"}
    present = sorted(forbidden.intersection(payload))
    if present:
        raise ValueError(f"LLM classifier returned forbidden keys: {', '.join(present)}")
    required = {"canonical_key", "risk_level", "confidence"}
    missing = sorted(key for key in required if key not in payload)
    if missing:
        raise ValueError(f"LLM classifier missing required keys: {', '.join(missing)}")


def __getattr__(name: str) -> Any:
    if name == "BaseStageController":
        from .controllers.base import BaseStageController

        return BaseStageController
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
