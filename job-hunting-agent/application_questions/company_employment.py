from __future__ import annotations

import re
from typing import Any, Iterable


COMPANY_REGISTRY_KEYS = (
    "company_employment_registry",
    "company_employment_assertions",
)
EXPERIENCE_KEYS = (
    "experiences",
    "experience",
    "work_experience_entries",
    "work_experiences",
)
LEGAL_SUFFIXES = {
    "co",
    "company",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "llc",
    "llp",
    "lp",
    "ltd",
    "limited",
    "plc",
}


def normalize_company_name(value: Any) -> str:
    text = str(value or "").casefold().replace("&", " and ")
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    if tokens and tokens[0] == "the":
        tokens.pop(0)
    return " ".join(tokens)


def _as_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in (value or []) if isinstance(item, dict)]


def _profile_sources(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(profile, dict):
        return []
    sources = [profile]
    for key in ("application_profile_library", "experience_library"):
        value = profile.get(key)
        if isinstance(value, dict):
            sources.append(value)
    return sources


def _aliases(entry: dict[str, Any]) -> list[str]:
    values: list[Any] = [
        entry.get("company"),
        entry.get("company_name"),
        entry.get("employer"),
    ]
    aliases = entry.get("aliases") or entry.get("company_aliases") or []
    if isinstance(aliases, str):
        aliases = re.split(r"[,;\n]", aliases)
    if isinstance(aliases, Iterable) and not isinstance(aliases, (str, bytes, dict)):
        values.extend(aliases)
    return [str(value).strip() for value in values if str(value or "").strip()]


def _matches_company(entry: dict[str, Any], target_company: str) -> bool:
    target = normalize_company_name(target_company)
    return bool(target) and target in {
        normalize_company_name(value)
        for value in _aliases(entry)
        if normalize_company_name(value)
    }


def _yes_no(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    normalized = str(value or "").strip().casefold()
    if normalized in {"yes", "y", "true", "1"}:
        return "Yes"
    if normalized in {"no", "n", "false", "0"}:
        return "No"
    return ""


def _is_user_confirmed(entry: dict[str, Any]) -> bool:
    return entry.get("confirmed") is True or entry.get("user_confirmed") is True


def _covers_subsidiaries(entry: dict[str, Any]) -> bool:
    return entry.get("includes_subsidiaries") is True or entry.get("covers_subsidiaries") is True


def _registry_entries(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for source in _profile_sources(profile):
        for key in COMPANY_REGISTRY_KEYS:
            entries.extend(_as_entries(source.get(key)))
    return entries


def _experience_entries(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for source in _profile_sources(profile):
        for key in EXPERIENCE_KEYS:
            entries.extend(_as_entries(source.get(key)))
    return entries


def resolve_company_employment(
    profile: dict[str, Any] | None,
    target_company: Any,
    *,
    subsidiaries_required: bool = False,
) -> dict[str, Any]:
    target = str(target_company or "").strip()
    result = {
        "answer": "",
        "target_company": target,
        "matched_company": "",
        "source": "none",
        "reason": "target_company_missing" if not target else "company_record_missing",
    }
    if not target:
        return result

    answers: list[tuple[str, str, str]] = []
    for entry in _registry_entries(profile):
        if not _matches_company(entry, target) or not _is_user_confirmed(entry):
            continue
        answer = _yes_no(entry.get("previously_employed"))
        if not answer:
            continue
        if answer == "No" and subsidiaries_required and not _covers_subsidiaries(entry):
            continue
        matched = next((value for value in _aliases(entry) if value), target)
        answers.append((answer, matched, "company_employment_registry"))

    for entry in _experience_entries(profile):
        if _matches_company(entry, target):
            matched = next((value for value in _aliases(entry) if value), target)
            answers.append(("Yes", matched, "work_experience"))

    distinct_answers = {answer for answer, _, _ in answers}
    if len(distinct_answers) > 1:
        result.update({"source": "conflict", "reason": "company_employment_records_conflict"})
        return result
    if not answers:
        if subsidiaries_required:
            matched_unscoped_no = any(
                _matches_company(entry, target)
                and _is_user_confirmed(entry)
                and _yes_no(entry.get("previously_employed")) == "No"
                for entry in _registry_entries(profile)
            )
            if matched_unscoped_no:
                result["reason"] = "subsidiary_scope_not_confirmed"
        return result

    answer, matched_company, source = answers[0]
    result.update({
        "answer": answer,
        "matched_company": matched_company,
        "source": source,
        "reason": "company_employment_record_matched",
    })
    return result
