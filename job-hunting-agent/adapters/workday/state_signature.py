from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from .contracts import FieldState, FieldStatus, GroupState, GroupStatus, StageSnapshot, WorkdayStage


_STAGE_ORDER = [
    WorkdayStage.AUTH.value,
    WorkdayStage.NAVIGATION.value,
    WorkdayStage.START_APPLICATION.value,
    WorkdayStage.MY_INFORMATION.value,
    WorkdayStage.MY_EXPERIENCE.value,
    WorkdayStage.APPLICATION_QUESTIONS.value,
    WorkdayStage.VOLUNTARY_DISCLOSURES.value,
    WorkdayStage.SELF_IDENTIFY.value,
    WorkdayStage.REVIEW.value,
    WorkdayStage.READY_TO_SUBMIT.value,
    WorkdayStage.SUBMITTED.value,
]
_STAGE_INDEX = {stage: index for index, stage in enumerate(_STAGE_ORDER)}
_SENSITIVE_KEY_RE = re.compile(r"(password|token|cookie|session|otp|secret)", re.IGNORECASE)


def _stage_token(stage: WorkdayStage | str) -> str:
    raw = stage.value if isinstance(stage, WorkdayStage) else str(stage or "")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", raw).strip("_").upper()
    aliases = {
        "AUTH": WorkdayStage.AUTH.value,
        "NAVIGATION": WorkdayStage.NAVIGATION.value,
        "START_APPLICATION": WorkdayStage.START_APPLICATION.value,
        "MY_INFORMATION": WorkdayStage.MY_INFORMATION.value,
        "MY_EXPERIENCE": WorkdayStage.MY_EXPERIENCE.value,
        "APPLICATION_QUESTIONS": WorkdayStage.APPLICATION_QUESTIONS.value,
        "VOLUNTARY_DISCLOSURES": WorkdayStage.VOLUNTARY_DISCLOSURES.value,
        "SELF_IDENTIFY": WorkdayStage.SELF_IDENTIFY.value,
        "REVIEW": WorkdayStage.REVIEW.value,
        "READY_TO_SUBMIT": WorkdayStage.READY_TO_SUBMIT.value,
        "SUBMITTED": WorkdayStage.SUBMITTED.value,
    }
    return aliases.get(normalized, normalized or WorkdayStage.UNKNOWN.value)


def _url_path(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url)
    return parsed.path or "/"


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return re.sub(r"\s+", " ", str(value)).strip()


def _stable_key(value: Any) -> str:
    if isinstance(value, FieldState):
        return value.canonical_key or value.name or value.label
    if isinstance(value, GroupState):
        return value.canonical_key
    if isinstance(value, dict):
        return str(
            value.get("canonical_key")
            or value.get("field")
            or value.get("name")
            or value.get("group")
            or value.get("label")
            or ""
        )
    return str(value or "")


def _all_fields(snapshot: StageSnapshot) -> list[FieldState]:
    return snapshot.all_fields()


def _required_keys(snapshot: StageSnapshot) -> list[str]:
    keys = {_stable_key(item) for item in snapshot.required_fields}
    for field in _all_fields(snapshot):
        if field.required:
            keys.add(_stable_key(field))
    for group in snapshot.groups:
        if group.required:
            keys.add(_stable_key(group))
        for field in group.fields:
            if group.required or field.required:
                keys.add(_stable_key(field))
    return sorted(key for key in keys if key)


def _unresolved_keys(snapshot: StageSnapshot) -> list[str]:
    keys = {_stable_key(item) for item in snapshot.unresolved_required_fields}
    for group in snapshot.groups:
        keys.update(str(item) for item in group.unresolved_fields)
        if group.required and group.normalized_status() in {
            GroupStatus.INCOMPLETE.value,
            GroupStatus.BLOCKED.value,
            GroupStatus.TECHNICAL_REVIEW.value,
        }:
            keys.add(group.canonical_key)
    for field in _all_fields(snapshot):
        if field.required and field.normalized_status() in {
            FieldStatus.MISSING.value,
            FieldStatus.BLOCKED.value,
            FieldStatus.TECHNICAL_REVIEW.value,
        }:
            keys.add(_stable_key(field))
    return sorted(key for key in keys if key)


def _committed_values(snapshot: StageSnapshot) -> list[tuple[str, str]]:
    values: dict[str, str] = {}
    for field in _all_fields(snapshot):
        key = _stable_key(field)
        if not key or _SENSITIVE_KEY_RE.search(key):
            continue
        current_value = field.visible_value if field.visible_value is not None else field.value
        values[key] = _normalize_text(current_value)
    return sorted(values.items())


def _blocking_errors(snapshot: StageSnapshot) -> list[str]:
    errors = {_normalize_text(item) for item in snapshot.validation_errors}
    for group in snapshot.groups:
        errors.update(_normalize_text(item) for item in group.validation_messages)
        for field in group.fields:
            errors.update(_normalize_text(item) for item in field.validation_messages)
    for field in snapshot.fields:
        errors.update(_normalize_text(item) for item in field.validation_messages)
    return sorted(item for item in errors if item)


def _loading_indicators(snapshot: StageSnapshot) -> list[str]:
    indicators = {_normalize_text(item) for item in snapshot.loading_indicators}
    for group in snapshot.groups:
        if group.normalized_status() == GroupStatus.LOADING.value:
            indicators.add(group.canonical_key)
        for field in group.fields:
            if field.normalized_status() == FieldStatus.LOADING.value:
                indicators.add(_stable_key(field))
    for field in snapshot.fields:
        if field.normalized_status() == FieldStatus.LOADING.value:
            indicators.add(_stable_key(field))
    return sorted(item for item in indicators if item)


def _verified_filled_required_count(snapshot: StageSnapshot) -> int:
    return sum(
        1
        for field in _all_fields(snapshot)
        if field.required and field.normalized_status() == FieldStatus.FILLED.value
    )


def _progress_score(snapshot: StageSnapshot) -> tuple[int, int, int, int]:
    return (
        -len(_unresolved_keys(snapshot)),
        -len(_blocking_errors(snapshot)),
        -len(_loading_indicators(snapshot)),
        _verified_filled_required_count(snapshot),
    )


def _signature_payload(snapshot: StageSnapshot) -> dict[str, Any]:
    return {
        "stage": _stage_token(snapshot.stage),
        "url_path": _url_path(snapshot.url),
        "required_keys": _required_keys(snapshot),
        "committed_values": _committed_values(snapshot),
        "unresolved_required_fields": _unresolved_keys(snapshot),
        "blocking_validation_errors": _blocking_errors(snapshot),
        "loading_indicators": _loading_indicators(snapshot),
    }


def build_stage_signature(snapshot: StageSnapshot) -> str:
    canonical = json.dumps(_signature_payload(snapshot), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def has_meaningful_progress(previous_snapshot: StageSnapshot, current_snapshot: StageSnapshot) -> bool:
    previous_stage = _stage_token(previous_snapshot.stage)
    current_stage = _stage_token(current_snapshot.stage)
    if _STAGE_INDEX.get(current_stage, -1) > _STAGE_INDEX.get(previous_stage, -1):
        return True

    previous_score = _progress_score(previous_snapshot)
    current_score = _progress_score(current_snapshot)
    return all(current >= previous for current, previous in zip(current_score, previous_score)) and current_score != previous_score


def should_stop_for_unchanged_state(signatures: list[str], threshold: int = 2) -> bool:
    if threshold <= 1:
        return bool(signatures)
    if len(signatures) < threshold:
        return False
    current = signatures[-1]
    if not current:
        return False
    recent_prior = signatures[max(0, len(signatures) - 5): len(signatures) - 1]
    return current in recent_prior
