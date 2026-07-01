from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .contracts import (
    ActionResult,
    FieldState,
    FieldStatus,
    GroupState,
    GroupStatus,
    OutcomeType,
    StageResult,
    StageSnapshot,
    WorkdayStage,
    normalize_required_field_keys,
    normalize_unresolved_required_fields,
    validate_stage_result,
)
from .controllers.base import BaseStageController
from .widgets.date import WorkdayDateGroupWidget
from .widgets.file_upload import WorkdayFileUploadWidget
from .widgets.loading import LoadingStateDetector
from .widgets.prompt import WorkdayPromptWidget
from .widgets.radio import WorkdayRadioGroupWidget
from .widgets.text import WorkdayTextInputWidget
from .widgets.utilities import is_placeholder_text, normalize_for_match


COUNTRY_ALIASES = ["United States", "United States of America"]
COUNTRY_PHONE_CODE_ALIASES = [
    "United States of America (+1)",
    "United States (+1)",
    "+1",
]
PHONE_DEVICE_PREFERRED = [
    "Business Mobile",
    "Mobile/Android",
    "Mobile/Apple iOS",
    "Mobile",
    "Cell Phone",
    "Cell",
]
HOW_HEARD_PREFERRED = ["Advertisement", "LinkedIn", "Company Website"]
MY_INFORMATION_STAGE = WorkdayStage.MY_INFORMATION
MY_EXPERIENCE_STAGE = WorkdayStage.MY_EXPERIENCE
UIUC_SCHOOL_PREFERRED = "University of Illinois Urbana-Champaign"
UIUC_SCHOOL_ALIASES = ["University of Illinois at Urbana-Champaign"]
POLLUTED_FIELD_NORMALIZED = {"to be reviewed by applicant"}
POLLUTED_FIELD_COMPACT = {"tobereviewedbyapplicant"}
DEGREE_ALIASES = {
    "bachelor": ["Bachelor's Degree", "Bachelors Degree", "Bachelor of Science", "Bachelor of Arts"],
    "master": ["Master's Degree", "Masters Degree", "Master of Science", "Master of Arts"],
    "doctor": ["Doctorate Degree", "Doctoral Degree", "PhD", "Doctor of Philosophy"],
    "associate": ["Associate Degree", "Associate's Degree"],
}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _context_from_args(page: Any = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    if context is not None:
        return context
    if isinstance(page, dict):
        return page
    return {}


def _snapshot_and_actions_from_verify_args(page: Any, previous_snapshot: Any) -> tuple[StageSnapshot, list[ActionResult]]:
    if isinstance(page, StageSnapshot):
        return page, [item for item in _as_list(previous_snapshot) if isinstance(item, ActionResult)]
    if isinstance(previous_snapshot, StageSnapshot):
        return previous_snapshot, []
    raise TypeError("legacy Workday controller verify requires a StageSnapshot")


def _field_state_from_unresolved(item: dict[str, Any], group: str = "") -> FieldState:
    name = str(item.get("field") or item.get("name") or item.get("canonical_key") or group or "required_field")
    status = item.get("status") or FieldStatus.BLOCKED
    if str(status) not in {status.value for status in FieldStatus}:
        status = FieldStatus.BLOCKED
    return FieldState(
        name=name,
        status=status,
        value=item.get("value"),
        required=True,
        group=str(item.get("group") or group or ""),
        last_attempt=str(item.get("last_attempt") or item.get("reason") or ""),
        evidence=str(item.get("validation_message") or item.get("raw_text") or item.get("label") or ""),
        metadata={key: value for key, value in item.items() if key not in {"field", "name", "value"}},
    )


def _fields_from_fixture(stage: str, payload: dict[str, Any]) -> list[FieldState]:
    fields: list[FieldState] = []
    state_snapshot = payload.get("state_snapshot") or {}
    if isinstance(state_snapshot, dict):
        for key, value in state_snapshot.items():
            if isinstance(value, dict):
                fields.append(
                    FieldState(
                        name=key,
                        status=value.get("status") or FieldStatus.MISSING,
                        value=value.get("value"),
                        source=value.get("source") or "",
                        required=bool(value.get("required")),
                        group=value.get("group") or "",
                        last_attempt=value.get("last_attempt") or "",
                        metadata={k: v for k, v in value.items() if k not in {"status", "value", "source", "required", "group", "last_attempt"}},
                    )
                )
    for item in normalize_unresolved_required_fields(payload.get("unresolved_required_fields")):
        fields.append(_field_state_from_unresolved(item))
    if stage == "my_experience":
        for group in normalize_unresolved_required_fields(payload.get("unresolved_groups")):
            group_key = str(group.get("canonical_key") or group.get("group") or "required_group")
            fields.append(
                FieldState(
                    name=group_key,
                    status=FieldStatus.BLOCKED,
                    required=True,
                    group=str(group.get("group") or group_key),
                    evidence="fixture unresolved group",
                )
                )
    return fields


def _page_like(page: Any) -> bool:
    return page is not None and callable(getattr(page, "locator", None))


def _clean_label(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"\s*\*\s*$", "", text).strip()
    return text


def _canonicalize_my_information_field(label: str, raw: dict[str, Any] | None = None) -> str:
    raw = raw or {}
    label_norm = normalize_for_match(label)
    identity_norm = normalize_for_match(
        " ".join(
            str(raw.get(key) or "")
            for key in ("id", "name", "data_field", "aria_label", "selector")
        )
    )
    combined = " ".join(item for item in [label_norm, identity_norm] if item)
    compact = combined.replace(" ", "")

    if "country phone code" in combined or "countryphonecode" in compact or ("country" in combined and "phone code" in combined):
        return "country_phone_code"
    if "phone device type" in combined or "phone type" in combined or "phonetype" in compact or (
        "device type" in combined and "phone" in combined
    ):
        return "phone_device_type"
    if "phone extension" in combined or ("extension" in combined and ("phone" in combined or "phonenumber" in compact)):
        return "phone_extension"
    if ("phone number" in combined or "phonenumber" in compact) and "extension" not in combined and "code" not in combined:
        return "phone_number"
    if "how did you hear" in combined or "hear about us" in combined:
        return "how_heard"
    if any(term in combined for term in ("previously employed", "previous employment")):
        return "previously_employed"
    if any(term in combined for term in ("former employee", "formerly employed", "prior employee", "former worker")):
        return "former_employee"
    if any(term in combined for term in ("previously worked", "previous worker", "worked here")):
        return "previously_worked"
    if label_norm in {"country", "country region"} or "country region" in label_norm or "country region" in identity_norm:
        if "region" in label_norm and "country" not in label_norm:
            return ""
        return "country"
    return ""


def _field_group(canonical_key: str) -> str:
    if _is_unknown_required_key(canonical_key):
        return "unknown_required_group"
    if canonical_key in {"country"}:
        return "address_group"
    if canonical_key in {"country_phone_code", "phone_number", "phone_device_type", "phone_extension"}:
        return "phone_group"
    if canonical_key == "how_heard":
        return "how_heard_group"
    if canonical_key in {"previously_worked", "previously_employed", "former_employee"}:
        return "employment_history_group"
    return ""


def _is_previous_worker_key(canonical_key: str) -> bool:
    return canonical_key in {"previously_worked", "previously_employed", "former_employee"}


def _unknown_required_key(label: str, selector: str) -> str:
    fallback = _clean_label(label) or _clean_label(selector) or "control"
    return f"unknown_required::{fallback}"


def _is_unknown_required_key(canonical_key: str) -> bool:
    return canonical_key.startswith("unknown_required::")


def _normalized_options(options: list[Any]) -> list[str]:
    normalized: list[str] = []
    for option in options:
        text = _clean_label(option)
        if text:
            normalized.append(text)
    return list(dict.fromkeys(normalized))


def _is_filled(field: FieldState) -> bool:
    return field.normalized_status() == FieldStatus.FILLED.value


def _is_loading(field: FieldState) -> bool:
    return field.normalized_status() == FieldStatus.LOADING.value


def _is_us_country_value(value: Any) -> bool:
    return normalize_for_match(value) in {normalize_for_match(item) for item in COUNTRY_ALIASES}


def _is_us_phone_code_value(value: Any) -> bool:
    return normalize_for_match(value) in {normalize_for_match(item) for item in COUNTRY_PHONE_CODE_ALIASES}


def _trusted_answer_sources(context: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for key in (
        "trusted_profile",
        "profile",
        "candidate_profile",
        "user_data",
        "answer_library",
        "approved_answers",
        "saved_answers",
    ):
        value = context.get(key)
        if isinstance(value, dict):
            sources.append(value)
    return sources


def _lookup_profile_value(context: dict[str, Any], keys: list[str]) -> Any:
    for source in _trusted_answer_sources(context):
        for key in keys:
            if key in source and source[key] not in (None, ""):
                return source[key]
    for key in keys:
        if key in context and context[key] not in (None, ""):
            return context[key]
    return None


def _yes_no_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    normalized = normalize_for_match(value)
    if normalized in {"yes", "y", "true", "1"}:
        return "Yes"
    if normalized in {"no", "n", "false", "0"}:
        return "No"
    return ""


def _trusted_previous_worker_answer(context: dict[str, Any], canonical_key: str) -> str:
    keys = [
        canonical_key,
        "previous_worker",
        "previously_worked",
        "previously_employed",
        "former_employee",
        "prior_employee",
        "worked_here_before",
        "formerly_employed",
    ]
    for source in _trusted_answer_sources(context):
        for key in keys:
            answer = _yes_no_value(source.get(key))
            if answer:
                return answer
    return ""


def _profile_phone_number(context: dict[str, Any]) -> str:
    value = _lookup_profile_value(context, ["phone_number", "phone", "mobile_phone", "telephone"])
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def _profile_phone_extension(context: dict[str, Any]) -> str:
    value = _lookup_profile_value(context, ["phone_extension", "extension", "phone_ext"])
    return str(value or "").strip()


def _profile_how_heard(context: dict[str, Any]) -> str:
    value = _lookup_profile_value(context, ["how_heard", "how_did_you_hear", "source"])
    return str(value or "").strip()


def _field_context(field: FieldState) -> dict[str, Any]:
    return {
        "canonical_key": field.canonical_key,
        "selector": str(field.metadata.get("selector") or ""),
        "locator_hints": field.locator_hints,
        "expected_value": field.expected_value,
        "required": field.required,
        "group_text": field.group,
        "validation_message": "\n".join(field.validation_messages),
        "current_stage": MY_INFORMATION_STAGE.value,
    }


def _action_for_field(action: str, field: FieldState, value: Any = None, **metadata: Any) -> ActionResult:
    return ActionResult(
        action=action,
        target=field.canonical_key,
        field=field.canonical_key,
        value=value,
        outcome_type=OutcomeType.RETRYABLE,
        metadata={
            "field": field.to_dict(),
            "selector": field.metadata.get("selector") or "",
            **metadata,
        },
    )


def _dedupe_fields(fields: list[FieldState]) -> list[FieldState]:
    deduped: dict[str, FieldState] = {}
    for field in fields:
        key = field.canonical_key or field.name
        if not key:
            continue
        current = deduped.get(key)
        if current is None or (not _is_filled(current) and _is_filled(field)):
            deduped[key] = field
    return list(deduped.values())


def _page_heading(page: Any) -> str:
    try:
        return str(
            page.evaluate(
                """() => {
                    const heading = document.querySelector("h1,h2,h3,[role='heading']");
                    return heading ? heading.innerText.replace(/\\s+/g, " ").trim() : "";
                }"""
            )
            or ""
        )
    except Exception:
        return ""


def _extract_my_information_controls(page: Any) -> list[dict[str, Any]]:
    try:
        controls = page.evaluate(
            """() => {
                let nextId = 0;
                const normalize = value => String(value || "").replace(/\\s+/g, " ").trim();
                const isHidden = el => {
                    if (!el || el.hidden) return true;
                    const style = window.getComputedStyle(el);
                    return style.display === "none" || style.visibility === "hidden";
                };
                const selectorFor = el => {
                    if (!el) return "";
                    if (!el.dataset.workdayControllerId) {
                        nextId += 1;
                        el.dataset.workdayControllerId = `wdc-${Date.now()}-${nextId}`;
                    }
                    return `[data-workday-controller-id="${el.dataset.workdayControllerId}"]`;
                };
                const textFromIds = ids => ids
                    .split(/\\s+/)
                    .map(id => document.getElementById(id))
                    .filter(Boolean)
                    .map(el => normalize(el.innerText || el.textContent || ""))
                    .filter(Boolean)
                    .join(" ");
                const labelFor = el => {
                    const labelledBy = el.getAttribute("aria-labelledby") || "";
                    let label = labelledBy ? textFromIds(labelledBy) : "";
                    if (!label && el.id) {
                        const explicit = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                        if (explicit) label = normalize(explicit.innerText || explicit.textContent || "");
                    }
                    if (!label) {
                        const wrapping = el.closest("label");
                        if (wrapping) label = normalize(wrapping.innerText || wrapping.textContent || "");
                    }
                    if (!label) label = normalize(el.getAttribute("aria-label") || "");
                    if (!label) label = normalize(el.getAttribute("placeholder") || "");
                    if (!label) {
                        const parent = el.closest("section, div, p, li, td, fieldset") || el.parentElement;
                        const candidate = parent ? parent.querySelector("label, legend, [data-automation-id*='label' i]") : null;
                        if (candidate) label = normalize(candidate.innerText || candidate.textContent || "");
                    }
                    return label;
                };
                const requiredFor = (el, label) => {
                    if (el.required || (el.getAttribute("aria-required") || "").toLowerCase() === "true") return true;
                    if (/\\*\\s*$/.test(label || "")) return true;
                    const scope = el.closest("section, fieldset, [role='group'], div");
                    if (scope && /\\*\\s*(indicates|required)?/i.test(scope.innerText || "")) {
                        return /\\*/.test(label || "");
                    }
                    return false;
                };
                const optionsFor = el => {
                    if (el.tagName.toLowerCase() === "select") {
                        return Array.from(el.options || []).map(option => normalize(option.textContent || option.value)).filter(Boolean);
                    }
                    const controlled = (el.getAttribute("aria-controls") || "")
                        .split(/\\s+/)
                        .map(id => document.getElementById(id))
                        .filter(Boolean);
                    const roots = controlled.length ? controlled : [el.closest("section, fieldset, [role='group'], div") || document];
                    const options = [];
                    for (const root of roots) {
                        for (const option of root.querySelectorAll("[role='option'], option, [data-option]")) {
                            options.push(normalize(option.innerText || option.textContent || option.value || ""));
                        }
                    }
                    return Array.from(new Set(options.filter(Boolean)));
                };
                const rows = [];
                const radioGroups = Array.from(document.querySelectorAll("fieldset, [role='radiogroup']"));
                const radioInputs = new Set();
                for (const group of radioGroups) {
                    if (isHidden(group)) continue;
                    const inputs = Array.from(group.querySelectorAll("input[type='radio'], [role='radio']"));
                    if (!inputs.length) continue;
                    inputs.forEach(input => radioInputs.add(input));
                    const legend = group.querySelector("legend");
                    const label = normalize(group.getAttribute("aria-label") || (legend ? legend.innerText || legend.textContent : "") || "");
                    rows.push({
                        kind: "radio",
                        selector: selectorFor(group),
                        label,
                        required: inputs.some(input => input.required || (input.getAttribute("aria-required") || "").toLowerCase() === "true") || /\\*\\s*$/.test(label),
                        id: group.id || "",
                        name: inputs.map(input => input.name || input.getAttribute("name") || "").filter(Boolean)[0] || "",
                        data_field: group.getAttribute("data-field") || "",
                        aria_label: group.getAttribute("aria-label") || "",
                        options: inputs.map(input => normalize(input.closest("label") ? input.closest("label").innerText : input.getAttribute("aria-label") || input.value)).filter(Boolean),
                    });
                }
                const controlSelector = [
                    "select",
                    "textarea",
                    "input:not([type='hidden']):not([type='radio']):not([type='checkbox'])",
                    "button[aria-haspopup='listbox']",
                    "[role='combobox']"
                ].join(",");
                for (const el of document.querySelectorAll(controlSelector)) {
                    if (isHidden(el) || radioInputs.has(el)) continue;
                    const tag = el.tagName.toLowerCase();
                    const role = (el.getAttribute("role") || "").toLowerCase();
                    const label = labelFor(el);
                    let kind = "text";
                    if (tag === "select" || tag === "button" || role === "combobox" || el.getAttribute("aria-haspopup") === "listbox") {
                        kind = "prompt";
                    }
                    rows.push({
                        kind,
                        selector: selectorFor(el),
                        label,
                        required: requiredFor(el, label),
                        id: el.id || "",
                        name: el.name || el.getAttribute("name") || "",
                        data_field: el.getAttribute("data-field") || "",
                        aria_label: el.getAttribute("aria-label") || "",
                        options: optionsFor(el),
                    });
                }
                return rows;
            }"""
        )
    except Exception:
        return []
    if not isinstance(controls, list):
        return []
    recognized: list[dict[str, Any]] = []
    for raw in controls:
        if not isinstance(raw, dict):
            continue
        label = _clean_label(raw.get("label") or raw.get("aria_label") or "")
        canonical_key = _canonicalize_my_information_field(label, raw)
        if not canonical_key:
            if not bool(raw.get("required")):
                continue
            canonical_key = _unknown_required_key(label, str(raw.get("selector") or ""))
        raw = dict(raw)
        raw["label"] = label or canonical_key.replace("_", " ").title()
        raw["canonical_key"] = canonical_key
        raw["unsupported_required"] = _is_unknown_required_key(canonical_key)
        raw["options"] = _normalized_options(_as_list(raw.get("options")))
        recognized.append(raw)
    return recognized


def _extract_page_messages(page: Any) -> list[str]:
    try:
        messages = page.evaluate(
            """() => {
                const selectors = [
                    "[role='alert']",
                    "[aria-live='assertive']",
                    "[data-automation-id*='error' i]",
                    "[data-automation-id*='alert' i]",
                    ".error",
                    ".validation"
                ];
                const values = [];
                const seen = new Set();
                for (const selector of selectors) {
                    for (const el of document.querySelectorAll(selector)) {
                        const style = window.getComputedStyle(el);
                        if (el.hidden || style.display === "none" || style.visibility === "hidden") continue;
                        const text = String(el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
                        if (text && !seen.has(text)) {
                            seen.add(text);
                            values.push(text);
                        }
                    }
                }
                return values;
            }"""
        )
    except Exception:
        return []
    return [str(item) for item in _as_list(messages) if str(item)]


def _hard_validation_message(text: str) -> bool:
    normalized = normalize_for_match(text)
    if "alerts found" in normalized and "errors found" not in normalized:
        return False
    if "capitalization" in normalized and "errors found" not in normalized:
        return False
    if "please select a value if applicable" in normalized:
        return False
    return any(
        phrase in normalized
        for phrase in (
            "errors found",
            "validation error",
            "required and must have a value",
            "required profile data is missing",
        )
    )


def _alert_message(text: str) -> bool:
    normalized = normalize_for_match(text)
    return "alerts found" in normalized or ("capitalization" in normalized and "errors found" not in normalized)


def _parse_required_label_from_message(text: str) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    compact = re.sub(r"(?i)\b(errors found|error|validation error)\b[:\s-]*", " ", compact).strip()
    patterns = [
        r"(?i)\bfield\s+(.+?)\s+is\s+required\b",
        r"(?i)^(.+?)\s+is\s+required\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            return _clean_label(match.group(1))
    return ""


def _validation_and_alerts(
    messages: list[str],
    fields: list[FieldState],
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    validation_errors: list[str] = []
    alerts: list[str] = []
    unresolved: list[dict[str, Any]] = []
    fields_by_key = {field.canonical_key: field for field in fields}
    for message in messages:
        if _alert_message(message):
            alerts.append(message)
            if not _hard_validation_message(message):
                continue
        if not _hard_validation_message(message):
            continue
        label = _parse_required_label_from_message(message)
        canonical_key = _canonicalize_my_information_field(label)
        if canonical_key and _is_filled(fields_by_key.get(canonical_key, FieldState(canonical_key=canonical_key))):
            continue
        validation_errors.append(message)
        unresolved_key = canonical_key or "workday_errors_found"
        unresolved.append(
            {
                "canonical_key": unresolved_key,
                "field": label or "Workday Errors Found",
                "group": _field_group(unresolved_key),
                "reason": "workday_required_validation_error",
                "validation_message": message,
            }
        )
    return list(dict.fromkeys(validation_errors)), list(dict.fromkeys(alerts)), unresolved


def _build_groups(fields: list[FieldState], unresolved: list[dict[str, Any]]) -> list[GroupState]:
    by_group: dict[str, list[FieldState]] = {}
    unresolved_by_group: dict[str, list[str]] = {}
    for field in fields:
        group = field.group or _field_group(field.canonical_key)
        if group:
            by_group.setdefault(group, []).append(field)
    for item in normalize_unresolved_required_fields(unresolved):
        group = str(item.get("group") or _field_group(str(item.get("canonical_key") or "")))
        key = str(item.get("canonical_key") or "")
        if group and key:
            unresolved_by_group.setdefault(group, []).append(key)
    groups: list[GroupState] = []
    for group_key, group_fields in by_group.items():
        required = any(field.required for field in group_fields) or bool(unresolved_by_group.get(group_key))
        missing = [
            field.canonical_key
            for field in group_fields
            if field.required and not _is_filled(field)
        ]
        missing.extend(unresolved_by_group.get(group_key, []))
        missing = list(dict.fromkeys(item for item in missing if item))
        groups.append(
            GroupState(
                canonical_key=group_key,
                required=required,
                status=GroupStatus.COMPLETE if not missing else GroupStatus.INCOMPLETE,
                fields=group_fields,
                unresolved_fields=missing,
            )
        )
    return groups


def _unresolved_for_fields(fields: list[FieldState]) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    for field in fields:
        if not field.required:
            continue
        if _is_filled(field) and not _is_unknown_required_key(field.canonical_key):
            continue
        reason = "unsupported_required_control" if _is_unknown_required_key(field.canonical_key) else ("loading" if _is_loading(field) else "required_field_missing")
        unresolved.append(
            {
                "canonical_key": field.canonical_key,
                "field": field.label or field.name or field.canonical_key,
                "group": field.group or _field_group(field.canonical_key),
                "reason": reason,
                "validation_message": "; ".join(field.validation_messages),
                "label": field.label,
                "selector": field.metadata.get("selector") or "",
                "kind": field.metadata.get("kind") or "",
                "options": field.options,
            }
        )
    return unresolved


def _my_information_root_loading_indicators(page: Any) -> list[str]:
    try:
        indicators = page.evaluate(
            """() => {
                const normalize = value => String(value || "").replace(/\\s+/g, " ").trim();
                const heading = Array.from(document.querySelectorAll("h1,h2,h3,[role='heading']"))
                    .find(el => /my information/i.test(normalize(el.innerText || el.textContent || "")));
                const root = heading
                    ? heading.closest("main, form, section, [role='main'], [data-automation-id*='page' i]")
                    : document.querySelector("main, form, [role='main']");
                if (!root) return [];
                const ownBits = [
                    root.getAttribute("aria-busy") === "true" ? "aria-busy" : "",
                    root.getAttribute("role") === "progressbar" ? "progressbar" : "",
                    /loading|spinner|skeleton/i.test(root.getAttribute("class") || "") ? "loading-root-class" : "",
                    /loading|spinner|skeleton/i.test(root.getAttribute("data-automation-id") || "") ? "loading-root-automation" : "",
                    /loading|spinner|skeleton/i.test(root.getAttribute("data-testid") || "") ? "loading-root-testid" : "",
                ].filter(Boolean);
                if (ownBits.length) return ownBits;
                const directText = Array.from(root.childNodes)
                    .filter(node => node.nodeType === Node.TEXT_NODE)
                    .map(node => normalize(node.textContent || ""))
                    .filter(Boolean)
                    .join(" ");
                return /^(loading|please wait|fetching options)$/i.test(directText) ? [directText] : [];
            }"""
        )
    except Exception:
        return []
    return [str(item) for item in _as_list(indicators) if str(item)]


class MyInformationController(BaseStageController):
    stage = "my_information"
    loading_stuck_after_attempts = 2

    def observe(self, page: Any = None, context: dict[str, Any] | None = None) -> StageSnapshot:
        context = _context_from_args(page, context)
        legacy_result = context.get("legacy_result") or context.get("fixture") or {}
        if _page_like(page) and not legacy_result:
            return self._observe_page(page, context)
        unresolved = normalize_unresolved_required_fields(legacy_result.get("unresolved_required_fields"))
        return StageSnapshot(
            stage=self.stage,
            url=str(context.get("url") or legacy_result.get("current_url") or legacy_result.get("url") or ""),
            fields=_fields_from_fixture(self.stage, legacy_result),
            required_fields=normalize_required_field_keys(unresolved),
            unresolved_required_fields=unresolved,
            validation_errors=[str(item) for item in _as_list(legacy_result.get("validation_errors"))],
            alerts=[str(item) for item in _as_list(legacy_result.get("alerts"))],
            metadata={
                "legacy_outcome_type": legacy_result.get("outcome_type"),
                "field_groups": legacy_result.get("field_groups") or {},
            },
        )

    def plan(self, snapshot: StageSnapshot, context: dict[str, Any] | None = None) -> list[ActionResult]:
        context = {} if context is None else context
        if snapshot.loading_indicators or any(_is_loading(field) for field in snapshot.all_fields()):
            return [
                ActionResult(
                    action="wait_for_loading",
                    target="my_information",
                    retryable=True,
                    outcome_type=OutcomeType.RETRYABLE,
                    metadata={"loading_indicators": list(snapshot.loading_indicators)},
                )
            ]
        if snapshot.unresolved_required_fields and "legacy_outcome_type" in snapshot.metadata:
            return [
                ActionResult(
                    action="return_structured_my_information_blocker",
                    outcome_type=OutcomeType.MY_INFORMATION_BLOCKED,
                    postcondition_verified=True,
                )
            ]

        fields = {field.canonical_key: field for field in snapshot.all_fields() if field.canonical_key}
        actions: list[ActionResult] = []

        country = fields.get("country")
        if country is not None and not _is_us_country_value(country.visible_value):
            actions.append(_action_for_field("select_country", country, "United States"))

        phone_code = fields.get("country_phone_code")
        if phone_code is not None and not _is_us_phone_code_value(phone_code.visible_value):
            actions.append(_action_for_field("select_country_phone_code", phone_code, "United States of America (+1)"))

        phone_number = fields.get("phone_number")
        profile_phone = _profile_phone_number(context)
        if phone_number is not None and profile_phone:
            current_digits = re.sub(r"\D+", "", str(phone_number.visible_value or ""))
            if len(current_digits) == 11 and current_digits.startswith("1"):
                current_digits = current_digits[1:]
            if current_digits != profile_phone:
                actions.append(_action_for_field("fill_phone_number", phone_number, profile_phone))

        phone_extension = fields.get("phone_extension")
        profile_extension = _profile_phone_extension(context)
        if phone_extension is not None and phone_extension.required and profile_extension:
            if str(phone_extension.visible_value or "") != profile_extension:
                actions.append(_action_for_field("fill_phone_extension", phone_extension, profile_extension))

        phone_device = fields.get("phone_device_type")
        if phone_device is not None and not _is_filled(phone_device):
            actions.append(_action_for_field("select_phone_device_type", phone_device))

        how_heard = fields.get("how_heard")
        if how_heard is not None and not _is_filled(how_heard):
            actions.append(_action_for_field("select_how_heard", how_heard, _profile_how_heard(context)))

        for field in fields.values():
            if not _is_previous_worker_key(field.canonical_key):
                continue
            if _is_filled(field):
                continue
            trusted_answer = _trusted_previous_worker_answer(context, field.canonical_key)
            if trusted_answer:
                actions.append(_action_for_field("select_previous_worker_answer", field, trusted_answer))

        if actions:
            return actions
        if snapshot.unresolved_required_fields:
            return [
                ActionResult(
                    action="return_structured_my_information_blocker",
                    outcome_type=OutcomeType.MY_INFORMATION_BLOCKED,
                    postcondition_verified=True,
                )
            ]
        return [ActionResult(action="complete_my_information", outcome_type=OutcomeType.COMPLETE, postcondition_verified=True)]

    def execute(self, page: Any, action: Any, context: dict[str, Any] | None = None) -> ActionResult | list[ActionResult]:
        context = {} if context is None else context
        if isinstance(action, list):
            return action
        if not _page_like(page):
            return action if isinstance(action, ActionResult) else ActionResult(action=str(action))
        if not isinstance(action, ActionResult):
            action = ActionResult(action=str(action))
        result = self._execute_action(page, action, context)
        context.setdefault("my_information_action_results", []).append(result)
        return result

    def verify(self, page: Any, previous_snapshot: Any, context: dict[str, Any] | None = None) -> StageResult:
        context = {} if context is None else context
        if _page_like(page) and isinstance(previous_snapshot, StageSnapshot):
            snapshot = self._observe_page(page, context)
            actions = [item for item in _as_list(context.get("my_information_action_results")) if isinstance(item, ActionResult)]
        else:
            snapshot, actions = _snapshot_and_actions_from_verify_args(page, previous_snapshot)

        if snapshot.loading_indicators or any(_is_loading(field) for field in snapshot.all_fields()):
            attempts = int(context.get("my_information_loading_attempts") or 0)
            unresolved = snapshot.unresolved_required_fields or _unresolved_for_fields(snapshot.all_fields())
            if attempts >= self.loading_stuck_after_attempts:
                result = StageResult(
                    stage=MY_INFORMATION_STAGE,
                    outcome_type=OutcomeType.WORKDAY_LOADING_STUCK,
                    status="WORKDAY_LOADING_STUCK",
                    snapshot=snapshot,
                    fields=snapshot.fields,
                    unresolved_required_fields=unresolved,
                    validation_errors=snapshot.validation_errors,
                    alerts=snapshot.alerts,
                    actions=actions,
                    blocked_reason="workday_loading_stuck",
                    message="workday_loading_stuck",
                )
            else:
                result = StageResult(
                    stage=MY_INFORMATION_STAGE,
                    outcome_type=OutcomeType.RETRYABLE,
                    status="NEEDS_RETRY",
                    snapshot=snapshot,
                    fields=snapshot.fields,
                    unresolved_required_fields=unresolved,
                    validation_errors=snapshot.validation_errors,
                    alerts=snapshot.alerts,
                    actions=actions,
                    blocked_reason="loading",
                    message="workday_loading",
                    terminal=False,
                )
            validate_stage_result(result)
            return result

        if snapshot.unresolved_required_fields:
            result = StageResult(
                stage=MY_INFORMATION_STAGE,
                outcome_type=OutcomeType.MY_INFORMATION_BLOCKED,
                status="BLOCKED_ON_QUESTIONS",
                snapshot=snapshot,
                fields=snapshot.fields,
                unresolved_required_fields=snapshot.unresolved_required_fields,
                validation_errors=snapshot.validation_errors,
                alerts=snapshot.alerts,
                actions=actions,
                blocked_reason="my_information_required_fields_unresolved",
                message="my_information_required_fields_unresolved",
            )
        else:
            result = StageResult(
                stage=MY_INFORMATION_STAGE,
                outcome_type=OutcomeType.COMPLETE,
                complete=True,
                status="COMPLETE",
                snapshot=snapshot,
                fields=snapshot.fields,
                alerts=snapshot.alerts,
                actions=actions,
            )
        validate_stage_result(result)
        return result

    def _observe_page(self, page: Any, context: dict[str, Any]) -> StageSnapshot:
        raw_controls = _extract_my_information_controls(page)
        fields: list[FieldState] = []
        for raw in raw_controls:
            field = self._observe_control(page, raw)
            if field is not None:
                fields.append(field)
        fields = _dedupe_fields(fields)

        loading_indicators: list[str] = []
        loading_indicators.extend(_my_information_root_loading_indicators(page))
        for field in fields:
            if _is_loading(field):
                loading_indicators.append(str(field.visible_value or field.label or field.canonical_key))

        validation_errors, alerts, validation_unresolved = _validation_and_alerts(_extract_page_messages(page), fields)
        unresolved = _unresolved_for_fields(fields)
        unresolved.extend(validation_unresolved)
        unresolved = normalize_unresolved_required_fields(unresolved)
        required_fields = [field.canonical_key for field in fields if field.required and field.canonical_key]
        required_fields.extend(str(item.get("canonical_key") or "") for item in unresolved if item.get("canonical_key"))

        return StageSnapshot(
            stage=MY_INFORMATION_STAGE,
            url=str(getattr(page, "url", "") or context.get("url") or ""),
            page_heading=_page_heading(page),
            groups=_build_groups(fields, unresolved),
            required_fields=required_fields,
            validation_errors=validation_errors,
            alerts=alerts,
            loading_indicators=list(dict.fromkeys(item for item in loading_indicators if item)),
            fields=fields,
            unresolved_required_fields=unresolved,
            metadata={"controller": self.__class__.__name__},
        )

    def _observe_control(self, page: Any, raw: dict[str, Any]) -> FieldState | None:
        canonical_key = str(raw.get("canonical_key") or "")
        if not canonical_key:
            return None
        required = bool(raw.get("required"))
        context = {
            "canonical_key": canonical_key,
            "selector": str(raw.get("selector") or ""),
            "required": required,
            "current_stage": MY_INFORMATION_STAGE.value,
        }
        kind = str(raw.get("kind") or "")
        if kind == "radio":
            field = WorkdayRadioGroupWidget().observe(page, context)
        elif canonical_key == "phone_number":
            field = WorkdayTextInputWidget(mode=WorkdayTextInputWidget.MODE_NORMALIZED_PHONE).observe(page, context)
        elif canonical_key == "phone_extension":
            field = WorkdayTextInputWidget(mode=WorkdayTextInputWidget.MODE_OPTIONAL_EMPTY).observe(page, context)
        elif kind == "text":
            field = WorkdayTextInputWidget().observe(page, context)
        else:
            field = WorkdayPromptWidget().observe(page, context)

        field.canonical_key = canonical_key
        field.name = canonical_key
        field.label = str(raw.get("label") or field.label or canonical_key)
        field.required = required
        field.group = _field_group(canonical_key)
        field.options = _normalized_options([*field.options, *_as_list(raw.get("options"))])
        field.locator_hints = list(dict.fromkeys([str(raw.get("selector") or ""), *field.locator_hints]))
        field.metadata = {
            **field.metadata,
            "selector": raw.get("selector") or "",
            "kind": kind,
            "label": raw.get("label") or "",
            "options": raw.get("options") or [],
            "unsupported_required": bool(raw.get("unsupported_required")),
            "id": raw.get("id") or "",
            "name": raw.get("name") or "",
            "data_field": raw.get("data_field") or "",
        }
        if canonical_key == "country":
            field.expected_value = "United States"
        elif canonical_key == "country_phone_code":
            field.expected_value = "United States of America (+1)"
        return field

    def _execute_action(self, page: Any, action: ActionResult, context: dict[str, Any]) -> ActionResult:
        field_payload = action.metadata.get("field") if isinstance(action.metadata, dict) else {}
        field = FieldState(**field_payload) if isinstance(field_payload, dict) else FieldState(canonical_key=action.target)
        if action.action == "wait_for_loading":
            context["my_information_loading_attempts"] = int(context.get("my_information_loading_attempts") or 0) + 1
            result = LoadingStateDetector(page).wait_until_resolved(800)
            result.action = "wait_for_loading"
            result.target = "my_information"
            result.field = "my_information"
            return result
        if action.action == "select_country":
            return self._select_prompt_exact(page, field, COUNTRY_ALIASES, "select_country")
        if action.action == "select_country_phone_code":
            return self._select_prompt_exact(page, field, COUNTRY_PHONE_CODE_ALIASES, "select_country_phone_code")
        if action.action == "select_phone_device_type":
            return self._select_prompt_preferred(page, field, PHONE_DEVICE_PREFERRED, "select_phone_device_type")
        if action.action == "select_how_heard":
            explicit = str(action.value or "").strip()
            preferred = [explicit] if explicit else []
            preferred.extend(HOW_HEARD_PREFERRED)
            return self._select_prompt_preferred(page, field, preferred, "select_how_heard", allow_first_valid=True)
        if action.action == "select_previous_worker_answer":
            return WorkdayRadioGroupWidget().select_value(
                page,
                action.value,
                aliases=[str(action.value)],
                context=_field_context(field),
            )
        if action.action == "fill_phone_number":
            return WorkdayTextInputWidget(mode=WorkdayTextInputWidget.MODE_NORMALIZED_PHONE).act(
                page,
                action.value,
                _field_context(field),
            )
        if action.action == "fill_phone_extension":
            return WorkdayTextInputWidget(mode=WorkdayTextInputWidget.MODE_OPTIONAL_EMPTY).act(
                page,
                action.value,
                _field_context(field),
            )
        return action

    def _select_prompt_exact(
        self,
        page: Any,
        field: FieldState,
        allowed_values: list[str],
        action_name: str,
    ) -> ActionResult:
        widget = WorkdayPromptWidget()
        ctx = _field_context(field)
        before = widget.observe(page, ctx)
        current_values = widget.read_committed_values(page, ctx)
        for value in current_values or _as_list(before.visible_value):
            if any(normalize_for_match(value) == normalize_for_match(allowed) for allowed in allowed_values):
                return ActionResult(
                    action=action_name,
                    target=field.canonical_key,
                    field=field.canonical_key,
                    value=value,
                    before=before.to_dict(),
                    after=before.to_dict(),
                    acted=False,
                    verified=True,
                    postcondition_verified=True,
                    outcome_type=OutcomeType.COMPLETE,
                    reason="already_committed",
                )
        widget.open(page, ctx)
        options = widget.read_visible_options(page, ctx)
        selected = self._first_exact_option(options, allowed_values)
        if not selected:
            after = widget.observe(page, ctx)
            return ActionResult(
                action=action_name,
                target=field.canonical_key,
                field=field.canonical_key,
                value=allowed_values[0] if allowed_values else "",
                before=before.to_dict(),
                after=after.to_dict(),
                verified=False,
                outcome_type=OutcomeType.RETRYABLE,
                reason="exact_option_not_found",
                error="exact_option_not_found",
                metadata={"options": options, "allowed_values": allowed_values},
            )
        result = widget.select_exact_or_alias(page, selected, aliases=[], context=ctx)
        result.action = action_name
        return result

    def _select_prompt_preferred(
        self,
        page: Any,
        field: FieldState,
        preferred_values: list[str],
        action_name: str,
        *,
        allow_first_valid: bool = False,
    ) -> ActionResult:
        widget = WorkdayPromptWidget()
        ctx = _field_context(field)
        before = widget.observe(page, ctx)
        if before.normalized_status() == FieldStatus.FILLED.value:
            return widget.verify_committed_value(page, before.visible_value, context=ctx)
        widget.open(page, ctx)
        options = [option for option in widget.read_visible_options(page, ctx) if not is_placeholder_text(option)]
        selected = self._first_exact_option(options, preferred_values)
        if selected is None and allow_first_valid and options:
            selected = options[0]
        if not selected:
            after = widget.observe(page, ctx)
            return ActionResult(
                action=action_name,
                target=field.canonical_key,
                field=field.canonical_key,
                before=before.to_dict(),
                after=after.to_dict(),
                verified=False,
                outcome_type=OutcomeType.RETRYABLE,
                reason="preferred_option_not_found",
                error="preferred_option_not_found",
                metadata={"options": options, "preferred_values": preferred_values},
            )
        result = widget.select_exact_or_alias(page, selected, aliases=[], context=ctx)
        result.action = action_name
        return result

    def _first_exact_option(self, options: list[str], allowed_values: list[str]) -> str | None:
        allowed = [normalize_for_match(item) for item in allowed_values if normalize_for_match(item)]
        for allowed_norm in allowed:
            for option in options:
                if normalize_for_match(option) == allowed_norm:
                    return option
        return None


def _my_experience_group(canonical_key: str) -> str:
    if canonical_key.startswith("education."):
        return "education"
    if canonical_key.startswith("experience."):
        return "work_experience"
    if canonical_key == "resume_upload":
        return "resume_upload"
    if _is_unknown_required_key(canonical_key):
        return "my_experience_unknown_group"
    return "my_experience"


def _canonicalize_my_experience_field(label: str, raw: dict[str, Any] | None = None) -> str:
    raw = raw or {}
    label_norm = normalize_for_match(label)
    section_norm = normalize_for_match(raw.get("section") or raw.get("section_label") or raw.get("group_label") or "")
    identity_norm = normalize_for_match(
        " ".join(
            str(raw.get(key) or "")
            for key in ("id", "name", "data_field", "aria_label", "selector", "placeholder")
        )
    )
    combined = " ".join(item for item in [label_norm, section_norm, identity_norm] if item)
    compact = combined.replace(" ", "")
    in_education = "education" in combined or "school" in combined or "degree" in combined
    in_experience = (
        "work experience" in combined
        or "employment" in combined
        or "experience" in combined
        or "employer" in combined
        or "company" in combined
        or "job title" in combined
    )

    if any(term in combined for term in ("resume", "cv", "curriculum vitae")) and any(
        term in combined for term in ("upload", "attach", "attachment", "file", "resume", "cv")
    ):
        return "resume_upload"

    if "school" in combined or "university" in combined or "institution" in combined:
        return "education.school"
    if "degree" in combined:
        return "education.degree"
    if "field of study" in combined or "major" in combined or "area of study" in combined:
        return "education.field"

    date_prefix = "education" if in_education and not in_experience else "experience"
    if ("expected" in combined or "end" in combined or "completion" in combined or "graduation" in combined) and (
        "year" in combined or "yyyy" in combined or "endyear" in compact
    ):
        return f"{date_prefix}.end_year"
    if ("start" in combined or "from" in combined) and ("year" in combined or "yyyy" in combined or "startyear" in compact):
        return f"{date_prefix}.start_year"
    if ("expected" in combined or "end" in combined or "completion" in combined or "graduation" in combined) and (
        "month" in combined or "mm" in combined or "endmonth" in compact
    ):
        return f"{date_prefix}.end_month"
    if ("start" in combined or "from" in combined) and ("month" in combined or "mm" in combined or "startmonth" in compact):
        return f"{date_prefix}.start_month"
    if ("expected" in combined or "end" in combined or "completion" in combined or "graduation" in combined) and "date" in combined:
        return f"{date_prefix}.end_date"
    if ("start" in combined or "from" in combined) and "date" in combined:
        return f"{date_prefix}.start_date"

    if "company" in combined or "employer" in combined or "organization" in combined:
        return "experience.company"
    if "job title" in combined or "position title" in combined or "position" in combined or label_norm == "title":
        return "experience.title"
    if "location" in combined and not in_education:
        return "experience.location"
    if "description" in combined or "responsibilities" in combined or "summary" in combined:
        return "experience.description"
    return ""


def _extract_my_experience_controls(page: Any) -> list[dict[str, Any]]:
    try:
        controls = page.evaluate(
            """() => {
                let nextId = 0;
                const normalize = value => String(value || "").replace(/\\s+/g, " ").trim();
                const isHidden = el => {
                    if (!el || el.hidden || el.type === "hidden") return true;
                    const style = window.getComputedStyle(el);
                    return style.display === "none" || style.visibility === "hidden";
                };
                const selectorFor = el => {
                    if (!el.dataset.workdayControllerId) {
                        nextId += 1;
                        el.dataset.workdayControllerId = `wdc-exp-${Date.now()}-${nextId}`;
                    }
                    return `[data-workday-controller-id="${el.dataset.workdayControllerId}"]`;
                };
                const textFromIds = ids => ids
                    .split(/\\s+/)
                    .map(id => document.getElementById(id))
                    .filter(Boolean)
                    .map(el => normalize(el.innerText || el.textContent || ""))
                    .filter(Boolean)
                    .join(" ");
                const labelFor = el => {
                    const labelledBy = el.getAttribute("aria-labelledby") || "";
                    let label = labelledBy ? textFromIds(labelledBy) : "";
                    if (!label && el.id) {
                        const explicit = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                        if (explicit) label = normalize(explicit.innerText || explicit.textContent || "");
                    }
                    if (!label) {
                        const wrapping = el.closest("label");
                        if (wrapping) label = normalize(wrapping.innerText || wrapping.textContent || "");
                    }
                    if (!label) label = normalize(el.getAttribute("aria-label") || "");
                    if (!label) label = normalize(el.getAttribute("placeholder") || "");
                    if (!label && /button/i.test(el.tagName)) label = normalize(el.innerText || el.textContent || "");
                    if (!label) {
                        const parent = el.closest("section, fieldset, [role='group'], div, td, li") || el.parentElement;
                        const candidate = parent ? parent.querySelector("label, legend, [data-automation-id*='label' i]") : null;
                        if (candidate) label = normalize(candidate.innerText || candidate.textContent || "");
                    }
                    return label;
                };
                const sectionFor = el => {
                    const scope = el.closest("[data-row], [data-section], section, fieldset, [role='group'], [data-automation-id]");
                    if (!scope) return "";
                    const heading = scope.querySelector("h1,h2,h3,h4,legend,[role='heading']");
                    return normalize(
                        scope.getAttribute("data-section")
                        || scope.getAttribute("aria-label")
                        || (heading ? heading.innerText || heading.textContent : "")
                        || ""
                    );
                };
                const requiredFor = (el, label) => {
                    if (el.required || (el.getAttribute("aria-required") || "").toLowerCase() === "true") return true;
                    if (/\\*\\s*$/.test(label || "")) return true;
                    const scope = el.closest("section, fieldset, [role='group'], div");
                    if (scope && /\\*\\s*(indicates|required)?/i.test(scope.innerText || "")) {
                        return /\\*/.test(label || "");
                    }
                    return false;
                };
                const optionsFor = el => {
                    if (el.tagName.toLowerCase() === "select") {
                        return Array.from(el.options || []).map(option => normalize(option.textContent || option.value)).filter(Boolean);
                    }
                    const controlled = (el.getAttribute("aria-controls") || "")
                        .split(/\\s+/)
                        .map(id => document.getElementById(id))
                        .filter(Boolean);
                    const roots = controlled.length ? controlled : [el.closest("section, fieldset, [role='group'], div") || document];
                    const options = [];
                    for (const root of roots) {
                        for (const option of root.querySelectorAll("[role='option'], option, [data-option], .fixture-option")) {
                            options.push(normalize(option.innerText || option.textContent || option.value || ""));
                        }
                    }
                    return Array.from(new Set(options.filter(Boolean)));
                };
                const rows = [];
                const controlSelector = [
                    "input:not([type='hidden']):not([type='radio']):not([type='checkbox'])",
                    "textarea",
                    "select",
                    "button[aria-haspopup='listbox']",
                    "[role='combobox']",
                    "[data-automation-id*='prompt' i]",
                    "[data-automation-id*='upload' i]",
                    "[data-automation-id*='attachment' i]"
                ].join(",");
                for (const el of document.querySelectorAll(controlSelector)) {
                    if (isHidden(el)) continue;
                    const tag = el.tagName.toLowerCase();
                    const role = (el.getAttribute("role") || "").toLowerCase();
                    const type = (el.getAttribute("type") || "").toLowerCase();
                    const automation = (el.getAttribute("data-automation-id") || "").toLowerCase();
                    const label = labelFor(el);
                    let kind = "text";
                    if (type === "file" || /upload|attachment/.test(automation)) {
                        kind = "file";
                    } else if (tag === "select" || tag === "button" || role === "combobox" || el.getAttribute("aria-haspopup") === "listbox" || el.getAttribute("aria-autocomplete") === "list") {
                        kind = "prompt";
                    }
                    rows.push({
                        kind,
                        selector: selectorFor(el),
                        label,
                        required: requiredFor(el, label),
                        id: el.id || "",
                        name: el.name || el.getAttribute("name") || "",
                        data_field: el.getAttribute("data-field") || "",
                        aria_label: el.getAttribute("aria-label") || "",
                        placeholder: el.getAttribute("placeholder") || "",
                        section: sectionFor(el),
                        options: optionsFor(el),
                    });
                }
                return rows;
            }"""
        )
    except Exception:
        return []
    recognized: list[dict[str, Any]] = []
    for raw in _as_list(controls):
        if not isinstance(raw, dict):
            continue
        label = _clean_label(raw.get("label") or raw.get("aria_label") or raw.get("placeholder") or "")
        canonical_key = _canonicalize_my_experience_field(label, raw)
        if not canonical_key:
            if not bool(raw.get("required")):
                continue
            canonical_key = _unknown_required_key(label, str(raw.get("selector") or ""))
        raw = dict(raw)
        raw["label"] = label or canonical_key.replace("_", " ").replace(".", " ").title()
        raw["canonical_key"] = canonical_key
        raw["unsupported_required"] = _is_unknown_required_key(canonical_key)
        raw["options"] = _normalized_options(_as_list(raw.get("options")))
        recognized.append(raw)
    return recognized


def _trusted_context_sources(context: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [context]
    sources.extend(_trusted_answer_sources(context))
    for key in ("stage_data", "workday_stage_data", "application_profile"):
        value = context.get(key)
        if isinstance(value, dict):
            sources.append(value)
    deduped: list[dict[str, Any]] = []
    seen: set[int] = set()
    for source in sources:
        if id(source) not in seen:
            seen.add(id(source))
            deduped.append(source)
    return deduped


def _first_section_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def _my_experience_section_records(context: dict[str, Any], section_names: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source in _trusted_context_sources(context):
        for section_name in section_names:
            record = _first_section_record(source.get(section_name))
            if record:
                records.append(record)
    return records


def _lookup_my_experience_profile_value(context: dict[str, Any], canonical_key: str) -> Any:
    section_names = ["education", "educations"] if canonical_key.startswith("education.") else ["experience", "experiences", "work_experience", "work_experiences"]
    leaf = canonical_key.split(".", 1)[1] if "." in canonical_key else canonical_key
    aliases_by_leaf = {
        "school": ["school", "school_name", "university", "institution"],
        "degree": ["degree", "degree_type", "highest_degree"],
        "field": ["field", "field_of_study", "major", "area_of_study"],
        "title": ["title", "job_title", "position", "role"],
        "company": ["company", "company_name", "employer", "organization"],
        "location": ["location", "city_state"],
        "description": ["description", "summary", "responsibilities"],
        "start_year": ["start_year", "from_year"],
        "end_year": ["end_year", "expected_end_year", "graduation_year", "to_year"],
        "start_month": ["start_month", "from_month"],
        "end_month": ["end_month", "expected_end_month", "graduation_month", "to_month"],
        "start_date": ["start_date", "from_date"],
        "end_date": ["end_date", "expected_end_date", "graduation_date", "to_date"],
    }
    keys = aliases_by_leaf.get(leaf, [leaf])
    for record in _my_experience_section_records(context, section_names):
        for key in keys:
            if key in record and record[key] not in (None, ""):
                return record[key]
    direct_keys = [canonical_key.replace(".", "_"), leaf]
    direct_keys.extend(keys)
    for source in _trusted_context_sources(context):
        for key in direct_keys:
            if key in source and source[key] not in (None, ""):
                return source[key]
    if leaf.endswith("_year"):
        date_value = _lookup_my_experience_profile_value(context, canonical_key.rsplit("_", 1)[0] + "_date")
        year = _year_from_value(date_value)
        if year:
            return year
    if leaf.endswith("_month"):
        date_value = _lookup_my_experience_profile_value(context, canonical_key.rsplit("_", 1)[0] + "_date")
        month = _month_from_value(date_value)
        if month:
            return month
    return None


def _resume_path_from_context(context: dict[str, Any]) -> str:
    for source in _trusted_context_sources(context):
        for key in ("resume_path", "resume_file", "resume_pdf", "cv_path"):
            value = source.get(key)
            if value not in (None, ""):
                return str(value)
        resume = source.get("resume")
        if isinstance(resume, dict):
            for key in ("path", "file", "filename"):
                value = resume.get(key)
                if value not in (None, ""):
                    return str(value)
    return ""


def _year_from_value(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"\b(19|20)\d{2}\b", text)
    if not match:
        return ""
    year = match.group(0)
    return year if _is_valid_year(year) else ""


def _month_from_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    month_name = _month_number_from_text(text)
    if month_name:
        return month_name
    match = re.match(r"^\s*(?:\d{4})-(\d{1,2})(?:-\d{1,2})?\s*$", text)
    if match:
        return f"{int(match.group(1)):02d}"
    match = re.match(r"^\s*(\d{1,2})/\d{1,2}/\d{4}\s*$", text)
    if match:
        return f"{int(match.group(1)):02d}"
    match = re.match(r"^\s*(\d{1,2})/\d{4}\s*$", text)
    if match:
        return f"{int(match.group(1)):02d}"
    if re.fullmatch(r"\d{1,2}", text):
        month = int(text)
        if 1 <= month <= 12:
            return f"{month:02d}"
    return text


def _month_number_from_text(value: Any) -> str:
    normalized = normalize_for_match(value)
    if not normalized:
        return ""
    month_aliases = {
        "01": {"january", "jan"},
        "02": {"february", "feb"},
        "03": {"march", "mar"},
        "04": {"april", "apr"},
        "05": {"may"},
        "06": {"june", "jun"},
        "07": {"july", "jul"},
        "08": {"august", "aug"},
        "09": {"september", "sep", "sept"},
        "10": {"october", "oct"},
        "11": {"november", "nov"},
        "12": {"december", "dec"},
    }
    tokens = set(normalized.split())
    for number, aliases in month_aliases.items():
        if normalized in aliases or tokens.intersection(aliases):
            return number
    return ""


def _is_valid_year(value: Any) -> bool:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}", text):
        return False
    year = int(text)
    return 1900 <= year <= 2100


def _expected_my_experience_value(canonical_key: str, context: dict[str, Any]) -> Any:
    if canonical_key == "resume_upload":
        return _resume_path_from_context(context)
    value = _lookup_my_experience_profile_value(context, canonical_key)
    if canonical_key == "education.school" and _is_uiuc_school(value):
        return UIUC_SCHOOL_PREFERRED
    if canonical_key.endswith("_year"):
        return _year_from_value(value)
    if canonical_key.endswith("_month"):
        return _month_from_value(value)
    return value


def _is_uiuc_school(value: Any) -> bool:
    normalized = normalize_for_match(value)
    allowed = [UIUC_SCHOOL_PREFERRED, *UIUC_SCHOOL_ALIASES]
    return normalized in {normalize_for_match(item) for item in allowed}


def _degree_aliases(expected_value: Any) -> list[str]:
    normalized = normalize_for_match(expected_value)
    aliases: list[str] = []
    for needle, values in DEGREE_ALIASES.items():
        if needle in normalized:
            aliases.extend(values)
    return list(dict.fromkeys(aliases))


def _my_experience_aliases(canonical_key: str, expected_value: Any) -> list[str]:
    if canonical_key == "education.school" and _is_uiuc_school(expected_value):
        return [*UIUC_SCHOOL_ALIASES, UIUC_SCHOOL_PREFERRED]
    if canonical_key == "education.degree":
        return _degree_aliases(expected_value)
    return []


def _flatten_visible_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(_flatten_visible_values(item))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_flatten_visible_values(item))
        return values
    text = str(value or "").strip()
    return [text] if text else []


def _is_polluted_value(value: Any) -> bool:
    for item in _flatten_visible_values(value):
        normalized = normalize_for_match(item)
        compact = normalized.replace(" ", "")
        if normalized in POLLUTED_FIELD_NORMALIZED or compact in POLLUTED_FIELD_COMPACT:
            return True
    return False


def _is_placeholder_value(value: Any) -> bool:
    values = _flatten_visible_values(value)
    return not values or all(is_placeholder_text(item) for item in values)


def _invalid_my_experience_reason(field: FieldState) -> str:
    if _is_loading(field):
        return "loading"
    if _is_polluted_value(field.visible_value):
        return "autofill_pollution"
    if _is_placeholder_value(field.visible_value):
        return "placeholder"
    if field.canonical_key.endswith("_year"):
        year = _year_from_value(field.visible_value)
        if not _is_valid_year(year):
            return "invalid_year"
    if field.canonical_key in {"education.school", "education.degree"} and field.normalized_status() != FieldStatus.FILLED.value:
        return "prompt_not_committed"
    if field.normalized_status() not in {FieldStatus.FILLED.value, FieldStatus.OPTIONAL.value}:
        return "required_field_missing"
    return ""


def _apply_my_experience_value_policy(field: FieldState) -> FieldState:
    reason = _invalid_my_experience_reason(field)
    if reason and reason != "loading":
        field.status = FieldStatus.MISSING if field.required else FieldStatus.OPTIONAL
        field.metadata = {**field.metadata, "invalid_reason": reason}
    return field


def _my_experience_field_context(field: FieldState) -> dict[str, Any]:
    return {
        "canonical_key": field.canonical_key,
        "selector": str(field.metadata.get("selector") or ""),
        "locator_hints": field.locator_hints,
        "expected_value": field.expected_value,
        "aliases": field.metadata.get("aliases") or [],
        "required": field.required,
        "group_text": field.group,
        "validation_message": "\n".join(field.validation_messages),
        "current_stage": MY_EXPERIENCE_STAGE.value,
    }


def _my_experience_action_for_field(action: str, field: FieldState, value: Any = None, **metadata: Any) -> ActionResult:
    return ActionResult(
        action=action,
        target=field.canonical_key,
        field=field.canonical_key,
        value=value,
        outcome_type=OutcomeType.RETRYABLE,
        metadata={
            "field": field.to_dict(),
            "selector": field.metadata.get("selector") or "",
            **metadata,
        },
    )


def _field_matches_expected(field: FieldState, expected_value: Any, aliases: list[str] | None = None) -> bool:
    if expected_value in (None, ""):
        return _is_filled(field) and not _invalid_my_experience_reason(field)
    expected_norms = [normalize_for_match(expected_value), *[normalize_for_match(item) for item in aliases or []]]
    expected_norms = [item for item in expected_norms if item]
    if field.canonical_key.endswith("_year"):
        return _year_from_value(field.visible_value) == str(expected_value)
    for value in [
        *_flatten_visible_values(field.visible_value),
        *_flatten_visible_values(field.metadata.get("committed_values") or []),
    ]:
        value_norm = normalize_for_match(value)
        if value_norm and value_norm in expected_norms:
            return True
    return False


def _unresolved_for_my_experience_fields(fields: list[FieldState]) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    for field in fields:
        if not field.required:
            continue
        reason = field.metadata.get("invalid_reason") or _invalid_my_experience_reason(field)
        if _is_filled(field) and not reason and not _is_unknown_required_key(field.canonical_key):
            continue
        if not reason:
            reason = "unsupported_required_control" if _is_unknown_required_key(field.canonical_key) else "required_field_missing"
        unresolved.append(
            {
                "canonical_key": field.canonical_key,
                "field": field.label or field.name or field.canonical_key,
                "group": field.group or _my_experience_group(field.canonical_key),
                "reason": reason,
                "validation_message": "; ".join(field.validation_messages),
                "label": field.label,
                "selector": field.metadata.get("selector") or "",
                "locator_hints": field.locator_hints,
                "kind": field.metadata.get("kind") or "",
                "current_value": field.visible_value,
                "expected_value": field.expected_value,
                "options": field.options,
            }
        )
    return unresolved


def _validation_and_alerts_for_my_experience(
    messages: list[str],
    fields: list[FieldState],
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    validation_errors: list[str] = []
    alerts: list[str] = []
    unresolved: list[dict[str, Any]] = []
    fields_by_key = {field.canonical_key: field for field in fields}
    for message in messages:
        if _alert_message(message):
            alerts.append(message)
            if not _hard_validation_message(message):
                continue
        if not _hard_validation_message(message):
            continue
        label = _parse_required_label_from_message(message)
        canonical_key = _canonicalize_my_experience_field(label)
        field = fields_by_key.get(canonical_key)
        if field is not None and _is_filled(field) and not _invalid_my_experience_reason(field):
            continue
        validation_errors.append(message)
        unresolved_key = canonical_key or "workday_errors_found"
        unresolved.append(
            {
                "canonical_key": unresolved_key,
                "field": label or "Workday Errors Found",
                "group": _my_experience_group(unresolved_key),
                "reason": "workday_required_validation_error",
                "validation_message": message,
                "current_value": field.visible_value if field is not None else "",
                "selector": field.metadata.get("selector") if field is not None else "",
                "options": field.options if field is not None else [],
            }
        )
    return list(dict.fromkeys(validation_errors)), list(dict.fromkeys(alerts)), unresolved


def _build_my_experience_groups(fields: list[FieldState], unresolved: list[dict[str, Any]]) -> list[GroupState]:
    by_group: dict[str, list[FieldState]] = {}
    unresolved_by_group: dict[str, list[str]] = {}
    for field in fields:
        group = field.group or _my_experience_group(field.canonical_key)
        if group:
            by_group.setdefault(group, []).append(field)
    for item in normalize_unresolved_required_fields(unresolved):
        key = str(item.get("canonical_key") or "")
        group = str(item.get("group") or _my_experience_group(key))
        if group and key:
            unresolved_by_group.setdefault(group, []).append(key)
    groups: list[GroupState] = []
    for group_key, group_fields in by_group.items():
        missing = [
            field.canonical_key
            for field in group_fields
            if field.required and (not _is_filled(field) or bool(_invalid_my_experience_reason(field)))
        ]
        missing.extend(unresolved_by_group.get(group_key, []))
        missing = list(dict.fromkeys(item for item in missing if item))
        required = any(field.required for field in group_fields) or bool(missing)
        groups.append(
            GroupState(
                canonical_key=group_key,
                required=required,
                status=GroupStatus.COMPLETE if not missing else GroupStatus.INCOMPLETE,
                fields=group_fields,
                unresolved_fields=missing,
            )
        )
    for group_key, missing in unresolved_by_group.items():
        if group_key not in by_group:
            groups.append(
                GroupState(
                    canonical_key=group_key,
                    required=True,
                    status=GroupStatus.INCOMPLETE,
                    unresolved_fields=list(dict.fromkeys(missing)),
                )
            )
    return groups


def _dedupe_my_experience_fields(fields: list[FieldState]) -> list[FieldState]:
    deduped: dict[tuple[str, str, str], FieldState] = {}
    for index, field in enumerate(fields):
        selector = str(field.metadata.get("selector") or (field.locator_hints[0] if field.locator_hints else ""))
        key = (
            field.canonical_key or field.name,
            selector,
            field.group or _my_experience_group(field.canonical_key),
        )
        if not key[0]:
            continue
        current = deduped.get(key)
        if current is None or (not _is_filled(current) and _is_filled(field)):
            deduped[key] = field
        field.metadata = {**field.metadata, "control_index": index}
    return list(deduped.values())


def _my_experience_root_loading_indicators(page: Any) -> list[str]:
    try:
        indicators = page.evaluate(
            """() => {
                const normalize = value => String(value || "").replace(/\\s+/g, " ").trim();
                const heading = Array.from(document.querySelectorAll("h1,h2,h3,[role='heading']"))
                    .find(el => /my experience/i.test(normalize(el.innerText || el.textContent || "")));
                const root = heading
                    ? heading.closest("main, form, section, [role='main'], [data-automation-id*='page' i]")
                    : document.querySelector("main, form, [role='main']");
                if (!root) return [];
                const ownBits = [
                    root.getAttribute("aria-busy") === "true" ? "aria-busy" : "",
                    root.getAttribute("role") === "progressbar" ? "progressbar" : "",
                    /loading|spinner|skeleton/i.test(root.getAttribute("class") || "") ? "loading-root-class" : "",
                    /loading|spinner|skeleton/i.test(root.getAttribute("data-automation-id") || "") ? "loading-root-automation" : "",
                    /loading|spinner|skeleton/i.test(root.getAttribute("data-testid") || "") ? "loading-root-testid" : "",
                ].filter(Boolean);
                if (ownBits.length) return ownBits;
                const directText = Array.from(root.childNodes)
                    .filter(node => node.nodeType === Node.TEXT_NODE)
                    .map(node => normalize(node.textContent || ""))
                    .filter(Boolean)
                    .join(" ");
                return /^(loading|please wait|fetching options)$/i.test(directText) ? [directText] : [];
            }"""
        )
    except Exception:
        return []
    return [str(item) for item in _as_list(indicators) if str(item)]


class MyExperienceController(BaseStageController):
    stage = "my_experience"
    loading_stuck_after_attempts = 2

    def observe(self, page: Any = None, context: dict[str, Any] | None = None) -> StageSnapshot:
        context = _context_from_args(page, context)
        fixture = context.get("fixture") or {}
        if _page_like(page) and not fixture:
            return self._observe_page(page, context)
        unresolved = []
        for group in normalize_unresolved_required_fields(fixture.get("unresolved_groups")):
            group_key = str(group.get("canonical_key") or group.get("group") or "")
            group_record = dict(group)
            if group_key and not group_record.get("group"):
                group_record["group"] = group_key
            group_record.setdefault("reason", "fixture_unresolved_group")
            unresolved.append(group_record)
        unresolved.extend(normalize_unresolved_required_fields(fixture.get("unresolved_required_fields")))
        return StageSnapshot(
            stage=self.stage,
            url=str(fixture.get("url") or context.get("url") or ""),
            fields=_fields_from_fixture(self.stage, fixture),
            required_fields=normalize_required_field_keys(unresolved),
            unresolved_required_fields=unresolved,
            validation_errors=[str(item) for item in _as_list(fixture.get("validation_errors"))],
            metadata={"legacy_outcome_type": fixture.get("outcome_type")},
        )

    def plan(self, snapshot: StageSnapshot, context: dict[str, Any] | None = None) -> list[ActionResult]:
        context = {} if context is None else context
        if snapshot.loading_indicators or any(_is_loading(field) for field in snapshot.all_fields()):
            return [
                ActionResult(
                    action="wait_for_loading",
                    target="my_experience",
                    retryable=True,
                    outcome_type=OutcomeType.RETRYABLE,
                    metadata={"loading_indicators": list(snapshot.loading_indicators)},
                )
            ]
        if snapshot.unresolved_required_fields and "legacy_outcome_type" in snapshot.metadata:
            return [
                ActionResult(
                    action="return_structured_my_experience_blocker",
                    outcome_type=OutcomeType.MY_EXPERIENCE_BLOCKED,
                    postcondition_verified=True,
                )
            ]

        actions: list[ActionResult] = []
        for field in snapshot.all_fields():
            if not field.canonical_key:
                continue
            expected = _expected_my_experience_value(field.canonical_key, context)
            invalid_reason = _invalid_my_experience_reason(field)
            if field.canonical_key == "resume_upload":
                if expected and (field.required or invalid_reason or not _is_filled(field)):
                    actions.append(_my_experience_action_for_field("upload_resume", field, expected, widget="file_upload"))
                continue

            aliases = _my_experience_aliases(field.canonical_key, expected)
            if field.canonical_key in {"education.school", "education.degree", "education.field"}:
                if expected and (invalid_reason or not _field_matches_expected(field, expected, aliases)):
                    actions.append(
                        _my_experience_action_for_field(
                            f"select_{field.canonical_key.replace('.', '_')}",
                            field,
                            expected,
                            aliases=aliases,
                            widget="prompt",
                        )
                    )
                continue

            if field.canonical_key.endswith(("_year", "_month", "_date")):
                if expected and (invalid_reason or not _is_filled(field)):
                    widget = "prompt" if field.metadata.get("kind") == "prompt" else "date"
                    actions.append(
                        _my_experience_action_for_field(
                            f"fill_{field.canonical_key.replace('.', '_')}",
                            field,
                            expected,
                            aliases=aliases,
                            widget=widget,
                        )
                    )
                continue

            if field.canonical_key.startswith("experience.") or field.canonical_key.startswith("education."):
                if expected and (invalid_reason or not _is_filled(field)):
                    actions.append(
                        _my_experience_action_for_field(
                            f"fill_{field.canonical_key.replace('.', '_')}",
                            field,
                            expected,
                            widget="text",
                        )
                    )

        if actions:
            return actions
        if snapshot.unresolved_required_fields:
            return [
                ActionResult(
                    action="return_structured_my_experience_blocker",
                    outcome_type=OutcomeType.MY_EXPERIENCE_BLOCKED,
                    postcondition_verified=True,
                )
            ]
        return [ActionResult(action="complete_my_experience", outcome_type=OutcomeType.COMPLETE, postcondition_verified=True)]

    def execute(self, page: Any, action: Any, context: dict[str, Any] | None = None) -> ActionResult | list[ActionResult]:
        context = {} if context is None else context
        if isinstance(action, list):
            return action
        if not _page_like(page):
            return action if isinstance(action, ActionResult) else ActionResult(action=str(action))
        if not isinstance(action, ActionResult):
            action = ActionResult(action=str(action))
        result = self._execute_action(page, action, context)
        context.setdefault("my_experience_action_results", []).append(result)
        return result

    def verify(self, page: Any, previous_snapshot: Any, context: dict[str, Any] | None = None) -> StageResult:
        context = {} if context is None else context
        if _page_like(page) and isinstance(previous_snapshot, StageSnapshot):
            snapshot = self._observe_page(page, context)
            actions = [item for item in _as_list(context.get("my_experience_action_results")) if isinstance(item, ActionResult)]
        else:
            snapshot, actions = _snapshot_and_actions_from_verify_args(page, previous_snapshot)

        if snapshot.loading_indicators or any(_is_loading(field) for field in snapshot.all_fields()):
            attempts = int(context.get("my_experience_loading_attempts") or 0)
            unresolved = snapshot.unresolved_required_fields or _unresolved_for_my_experience_fields(snapshot.all_fields())
            if attempts >= self.loading_stuck_after_attempts:
                result = StageResult(
                    stage=MY_EXPERIENCE_STAGE,
                    outcome_type=OutcomeType.LOADING_STUCK,
                    status="WORKDAY_LOADING_STUCK",
                    snapshot=snapshot,
                    fields=snapshot.fields,
                    unresolved_required_fields=unresolved,
                    validation_errors=snapshot.validation_errors,
                    alerts=snapshot.alerts,
                    actions=actions,
                    blocked_reason="workday_loading_stuck",
                    message="workday_loading_stuck",
                )
            else:
                result = StageResult(
                    stage=MY_EXPERIENCE_STAGE,
                    outcome_type=OutcomeType.RETRYABLE,
                    status="NEEDS_RETRY",
                    snapshot=snapshot,
                    fields=snapshot.fields,
                    unresolved_required_fields=unresolved,
                    validation_errors=snapshot.validation_errors,
                    alerts=snapshot.alerts,
                    actions=actions,
                    blocked_reason="loading",
                    message="workday_loading",
                    terminal=False,
                )
            validate_stage_result(result)
            return result

        if snapshot.unresolved_required_fields:
            result = StageResult(
                stage=MY_EXPERIENCE_STAGE,
                outcome_type=OutcomeType.MY_EXPERIENCE_BLOCKED,
                status="BLOCKED_ON_QUESTIONS",
                snapshot=snapshot,
                fields=snapshot.fields,
                unresolved_required_fields=snapshot.unresolved_required_fields,
                unresolved_required_groups=[
                    group.canonical_key for group in snapshot.groups if group.required and not group.is_satisfied()
                ],
                validation_errors=snapshot.validation_errors,
                alerts=snapshot.alerts,
                actions=actions,
                blocked_reason="my_experience_required_groups_unresolved",
                message="my_experience_required_groups_unresolved",
            )
        else:
            result = StageResult(
                stage=MY_EXPERIENCE_STAGE,
                outcome_type=OutcomeType.COMPLETE,
                complete=True,
                status="COMPLETE",
                snapshot=snapshot,
                fields=snapshot.fields,
                alerts=snapshot.alerts,
                actions=actions,
            )
        validate_stage_result(result)
        return result

    def _observe_page(self, page: Any, context: dict[str, Any]) -> StageSnapshot:
        raw_controls = _extract_my_experience_controls(page)
        fields: list[FieldState] = []
        for raw in raw_controls:
            field = self._observe_control(page, raw, context)
            if field is not None:
                fields.append(field)
        fields = _dedupe_my_experience_fields(fields)

        validation_errors, alerts, validation_unresolved = _validation_and_alerts_for_my_experience(_extract_page_messages(page), fields)
        validation_required = {str(item.get("canonical_key") or "") for item in validation_unresolved}
        for field in fields:
            if field.canonical_key in validation_required:
                field.required = True
            _apply_my_experience_value_policy(field)

        unresolved = _unresolved_for_my_experience_fields(fields)
        unresolved.extend(validation_unresolved)
        unresolved = normalize_unresolved_required_fields(unresolved)
        required_fields = [field.canonical_key for field in fields if field.required and field.canonical_key]
        required_fields.extend(str(item.get("canonical_key") or "") for item in unresolved if item.get("canonical_key"))

        loading_indicators: list[str] = []
        loading_indicators.extend(_my_experience_root_loading_indicators(page))
        for field in fields:
            if _is_loading(field):
                loading_indicators.append(str(field.visible_value or field.label or field.canonical_key))

        return StageSnapshot(
            stage=MY_EXPERIENCE_STAGE,
            url=str(getattr(page, "url", "") or context.get("url") or ""),
            page_heading=_page_heading(page),
            groups=_build_my_experience_groups(fields, unresolved),
            required_fields=list(dict.fromkeys(item for item in required_fields if item)),
            validation_errors=validation_errors,
            alerts=alerts,
            loading_indicators=list(dict.fromkeys(item for item in loading_indicators if item)),
            fields=fields,
            unresolved_required_fields=unresolved,
            metadata={"controller": self.__class__.__name__},
        )

    def _observe_control(self, page: Any, raw: dict[str, Any], context: dict[str, Any]) -> FieldState | None:
        canonical_key = str(raw.get("canonical_key") or "")
        if not canonical_key:
            return None
        expected = _expected_my_experience_value(canonical_key, context)
        aliases = _my_experience_aliases(canonical_key, expected)
        required = bool(raw.get("required"))
        widget_context = {
            "canonical_key": canonical_key,
            "selector": str(raw.get("selector") or ""),
            "expected_value": expected,
            "aliases": aliases,
            "required": required,
            "current_stage": MY_EXPERIENCE_STAGE.value,
        }
        kind = str(raw.get("kind") or "")
        if kind == "file" or canonical_key == "resume_upload":
            field = WorkdayFileUploadWidget().observe(page, widget_context)
            if expected:
                verify = WorkdayFileUploadWidget().verify_upload(page, Path(str(expected)).name, widget_context)
                if verify.verified:
                    field.status = FieldStatus.FILLED
                    field.visible_value = verify.metadata.get("markers") or field.visible_value
                elif required:
                    field.status = FieldStatus.MISSING
                field.metadata = {**field.metadata, "exact_upload_verified": verify.verified, "upload_evidence": verify.metadata}
        elif kind == "prompt" or canonical_key in {"education.school", "education.degree", "education.field"}:
            field = WorkdayPromptWidget().observe(page, widget_context)
        elif canonical_key.endswith(("_year", "_month", "_date")):
            field = WorkdayDateGroupWidget().observe(page, widget_context)
        else:
            field = WorkdayTextInputWidget().observe(page, widget_context)

        field.canonical_key = canonical_key
        field.name = canonical_key
        field.label = str(raw.get("label") or field.label or canonical_key)
        field.required = required
        field.group = _my_experience_group(canonical_key)
        field.expected_value = expected
        field.options = _normalized_options([*field.options, *_as_list(raw.get("options"))])
        field.locator_hints = list(dict.fromkeys([str(raw.get("selector") or ""), *field.locator_hints]))
        field.metadata = {
            **field.metadata,
            "selector": raw.get("selector") or "",
            "kind": kind,
            "label": raw.get("label") or "",
            "section": raw.get("section") or "",
            "options": raw.get("options") or [],
            "unsupported_required": bool(raw.get("unsupported_required")),
            "id": raw.get("id") or "",
            "name": raw.get("name") or "",
            "data_field": raw.get("data_field") or "",
            "aliases": aliases,
        }
        return _apply_my_experience_value_policy(field)

    def _execute_action(self, page: Any, action: ActionResult, context: dict[str, Any]) -> ActionResult:
        field_payload = action.metadata.get("field") if isinstance(action.metadata, dict) else {}
        field = FieldState(**field_payload) if isinstance(field_payload, dict) else FieldState(canonical_key=action.target)
        if action.action == "wait_for_loading":
            context["my_experience_loading_attempts"] = int(context.get("my_experience_loading_attempts") or 0) + 1
            result = LoadingStateDetector(page).wait_until_resolved(800)
            result.action = "wait_for_loading"
            result.target = "my_experience"
            result.field = "my_experience"
            return result
        if action.action == "upload_resume":
            result = WorkdayFileUploadWidget().upload(page, str(action.value), _my_experience_field_context(field))
            result.action = action.action
            return result
        widget_kind = action.metadata.get("widget") if isinstance(action.metadata, dict) else ""
        if widget_kind == "prompt":
            result = WorkdayPromptWidget().select_exact_or_alias(
                page,
                action.value,
                aliases=action.metadata.get("aliases") or field.metadata.get("aliases") or [],
                context=_my_experience_field_context(field),
            )
            result.action = action.action
            return result
        if widget_kind == "date":
            result = WorkdayDateGroupWidget().act(page, action.value, _my_experience_field_context(field))
            result.action = action.action
            return result
        result = WorkdayTextInputWidget().act(page, action.value, _my_experience_field_context(field))
        result.action = action.action
        return result


class NavigationController(BaseStageController):
    stage = "navigation"

    def observe(self, page: Any = None, context: dict[str, Any] | None = None) -> StageSnapshot:
        context = _context_from_args(page, context)
        fixture = context.get("fixture") or {}
        unresolved = []
        if not fixture.get("manual_apply_available") and not fixture.get("recovered"):
            unresolved.append(
                {
                    "field": "application_start_action",
                    "reason": "no_safe_apply_action",
                    "current_url": fixture.get("current_url"),
                    "original_job_url": fixture.get("original_job_url"),
                }
            )
        return StageSnapshot(
            stage=self.stage,
            url=str(fixture.get("current_url") or context.get("url") or ""),
            fields=_fields_from_fixture(self.stage, fixture),
            required_fields=["application_start_action"] if unresolved else [],
            unresolved_required_fields=unresolved,
            metadata={
                "legacy_outcome_type": fixture.get("outcome_type"),
                "original_job_url": fixture.get("original_job_url"),
                "available_actions": fixture.get("available_actions") or [],
            },
        )

    def plan(self, snapshot: StageSnapshot, context: dict[str, Any] | None = None) -> list[ActionResult]:
        if snapshot.unresolved_required_fields:
            return [
                ActionResult(
                    action="return_navigation_blocker",
                    outcome_type=OutcomeType.NAVIGATION_BLOCKED,
                    postcondition_verified=True,
                )
            ]
        return [
            ActionResult(
                action="recover_or_continue_apply_manually",
                changed=True,
                outcome_type=OutcomeType.COMPLETE,
                postcondition_verified=True,
            )
        ]

    def execute(self, page: Any, action: Any, context: dict[str, Any] | None = None) -> ActionResult | list[ActionResult]:
        if isinstance(action, list):
            return action
        return action if isinstance(action, ActionResult) else ActionResult(action=str(action))

    def verify(self, page: Any, previous_snapshot: Any, context: dict[str, Any] | None = None) -> StageResult:
        snapshot, actions = _snapshot_and_actions_from_verify_args(page, previous_snapshot)
        if snapshot.unresolved_required_fields:
            result = StageResult(
                stage=self.stage,
                outcome_type=OutcomeType.NAVIGATION_BLOCKED,
                status="BLOCKED",
                snapshot=snapshot,
                fields=snapshot.fields,
                unresolved_required_fields=snapshot.unresolved_required_fields,
                actions=actions,
                message="navigation_no_safe_apply_action",
            )
        else:
            result = StageResult(
                stage=self.stage,
                outcome_type=OutcomeType.COMPLETE,
                complete=True,
                status="COMPLETE",
                snapshot=snapshot,
                fields=snapshot.fields,
                actions=actions,
            )
        validate_stage_result(result)
        return result


ShadowMyInformationController = MyInformationController
ShadowMyExperienceController = MyExperienceController
ShadowNavigationController = NavigationController
LegacyMyInformationController = MyInformationController
LegacyMyExperienceController = MyExperienceController
LegacyNavigationController = NavigationController


def replay_fixture(fixture: dict[str, Any]) -> StageResult:
    stage = str(fixture.get("stage") or "").lower()
    if stage == "my_information":
        return MyInformationController().run_once({"fixture": fixture})
    if stage == "my_experience":
        return MyExperienceController().run_once({"fixture": fixture})
    if stage == "navigation":
        return NavigationController().run_once({"fixture": fixture})
    if stage == "application_questions":
        unresolved = normalize_unresolved_required_fields(fixture.get("unresolved_required_fields"))
        result = StageResult(
            stage=stage,
            outcome_type=OutcomeType.BLOCKED_ON_QUESTIONS,
            status="BLOCKED_ON_QUESTIONS",
            unresolved_required_fields=unresolved,
            blockers=[{"stage": stage, "canonical_key": item.get("canonical_key")} for item in unresolved],
            message="application_questions_require_review",
            metadata={"legacy_outcome_type": fixture.get("outcome_type")},
        )
        validate_stage_result(result)
        return result
    if stage == "auth":
        result = StageResult(
            stage=stage,
            outcome_type=OutcomeType.AUTH_BLOCKED,
            status="NEEDS_TECHNICAL_REVIEW",
            message=str(fixture.get("blocked_reason") or "auth_blocked"),
            metadata={
                "tenant": fixture.get("tenant"),
                "host": fixture.get("host"),
                "auth_strategy": fixture.get("auth_strategy"),
                "password_source": fixture.get("password_source"),
            },
        )
        validate_stage_result(result)
        return result
    raise ValueError(f"unsupported Workday replay fixture stage: {stage}")
