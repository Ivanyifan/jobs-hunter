from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from adapters.workday.contracts import (
    FieldState,
    FieldStatus,
    OutcomeType,
    StageResult,
    StageSnapshot,
    WorkdayStage,
    validate_stage_result,
)


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "workday"
REQUIRED_FIXTURE_KEYS = {
    "fixture_name",
    "tenant",
    "source_stage",
    "expected_stage",
    "source_url",
    "page_heading",
    "dom_excerpt",
    "accessibility_excerpt",
    "extracted_fields",
    "validation_errors",
    "alerts",
    "visible_actions",
    "expected_outcome",
    "expected_unresolved_fields",
    "metadata",
}
FORBIDDEN_TERMINAL_OUTCOMES = {"stage_timeout", "unknown", "no_action_found"}
SENSITIVE_KEY_RE = re.compile(r"(password|token|cookie|session|otp|secret|authorization)", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_CANDIDATE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)")
STREET_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9 .'-]+\s+"
    r"(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd|court|ct|way)\b",
    re.IGNORECASE,
)


def sanitize_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def sanitize_text(text: str) -> str:
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = STREET_ADDRESS_RE.sub("[REDACTED_ADDRESS]", text)

    def redact_phone(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) >= 10:
            return "[REDACTED_PHONE]"
        return match.group(0)

    return PHONE_CANDIDATE_RE.sub(redact_phone, text)


def _iter_keys_and_values(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _iter_keys_and_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_keys_and_values(item)


def _assert_sanitized(payload: dict[str, Any], path: Path | None = None) -> None:
    label = str(path) if path else payload.get("fixture_name", "fixture")
    for key, value in _iter_keys_and_values(payload):
        if SENSITIVE_KEY_RE.search(key):
            raise ValueError(f"{label}: sensitive key is not allowed in fixture: {key}")
        if isinstance(value, str):
            sanitized = sanitize_text(value)
            if sanitized != value:
                raise ValueError(f"{label}: unsanitized personal data found near key {key}")


def _required_non_filled_field_keys(payload: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in payload.get("extracted_fields", []):
        if not isinstance(field, dict):
            continue
        if not field.get("required"):
            continue
        if str(field.get("status") or "").lower() == FieldStatus.FILLED.value:
            continue
        canonical_key = str(field.get("canonical_key") or "")
        if canonical_key:
            keys.append(canonical_key)
    return keys


def validate_fixture(payload: dict[str, Any], path: Path | None = None) -> None:
    missing = sorted(REQUIRED_FIXTURE_KEYS - set(payload))
    if missing:
        label = str(path) if path else payload.get("fixture_name", "fixture")
        raise ValueError(f"{label}: missing required fixture keys: {', '.join(missing)}")

    if payload["expected_outcome"].lower() in FORBIDDEN_TERMINAL_OUTCOMES:
        raise ValueError(f"{payload['fixture_name']}: generic expected outcome is forbidden")
    if payload["expected_outcome"] not in {item.value for item in OutcomeType}:
        raise ValueError(f"{payload['fixture_name']}: expected_outcome is not typed")
    for stage_key in ("source_stage", "expected_stage"):
        if payload[stage_key] not in {item.value for item in WorkdayStage}:
            raise ValueError(f"{payload['fixture_name']}: {stage_key} is not a WorkdayStage")
    if sanitize_url(payload["source_url"]) != payload["source_url"]:
        raise ValueError(f"{payload['fixture_name']}: source_url must not include query parameters")
    if not isinstance(payload["extracted_fields"], list):
        raise ValueError(f"{payload['fixture_name']}: extracted_fields must be a list")
    if not isinstance(payload["expected_unresolved_fields"], list):
        raise ValueError(f"{payload['fixture_name']}: expected_unresolved_fields must be a list")
    unresolved = {str(item) for item in payload["expected_unresolved_fields"]}
    missing_unresolved = sorted(key for key in _required_non_filled_field_keys(payload) if key not in unresolved)
    if missing_unresolved:
        raise ValueError(
            f"{payload['fixture_name']}: required non-filled fields missing from expected_unresolved_fields: "
            + ", ".join(missing_unresolved)
        )
    _assert_sanitized(payload, path)


def load_fixture(path: str | Path) -> dict[str, Any]:
    fixture_path = Path(path)
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    validate_fixture(payload, fixture_path)
    return payload


def load_workday_fixtures(root: str | Path = FIXTURE_ROOT) -> list[dict[str, Any]]:
    root_path = Path(root)
    fixtures = [load_fixture(path) for path in sorted(root_path.glob("*/fixture.json"))]
    if not fixtures:
        raise ValueError(f"no Workday fixtures found under {root_path}")
    return fixtures


def fixture_to_stage_result(fixture: dict[str, Any]) -> StageResult:
    fields = [
        FieldState(
            canonical_key=str(field.get("canonical_key") or ""),
            label=str(field.get("label") or ""),
            required=bool(field.get("required")),
            status=str(field.get("status") or FieldStatus.UNKNOWN.value),
            visible_value=field.get("visible_value"),
            expected_value=field.get("expected_value"),
            options=field.get("options") or [],
            source=str(field.get("source") or ""),
            group=str(field.get("group") or ""),
        )
        for field in fixture.get("extracted_fields", [])
        if isinstance(field, dict)
    ]
    expected_unresolved = [{"canonical_key": str(item)} for item in fixture["expected_unresolved_fields"]]
    required_fields = [
        field.canonical_key
        for field in fields
        if field.required and field.canonical_key
    ]
    required_fields.extend(str(item) for item in fixture["expected_unresolved_fields"])
    snapshot = StageSnapshot(
        stage=fixture["expected_stage"],
        url=fixture["source_url"],
        page_heading=fixture["page_heading"],
        required_fields=required_fields,
        validation_errors=fixture["validation_errors"],
        alerts=fixture["alerts"],
        visible_actions=fixture["visible_actions"],
        dom_excerpt=fixture["dom_excerpt"],
        accessibility_excerpt=fixture["accessibility_excerpt"],
        metadata=fixture["metadata"],
        fields=fields,
        unresolved_required_fields=expected_unresolved,
    )
    result = StageResult(
        outcome_type=fixture["expected_outcome"],
        stage=fixture["expected_stage"],
        complete=fixture["expected_outcome"] == OutcomeType.COMPLETE.value,
        snapshot=snapshot,
        unresolved_required_fields=expected_unresolved,
        validation_errors=fixture["validation_errors"],
        alerts=fixture["alerts"],
        metadata={"fixture_name": fixture["fixture_name"]},
    )
    validate_stage_result(result)
    return result


def field_by_key(fixture: dict[str, Any], canonical_key: str) -> dict[str, Any]:
    for field in fixture.get("extracted_fields", []):
        if field.get("canonical_key") == canonical_key:
            return field
    raise KeyError(canonical_key)
