from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable


PLACEHOLDER_NORMALIZED = {
    "",
    "select",
    "select one",
    "select an option",
    "choose",
    "choose one",
    "0 items selected",
    "no items selected",
    "mm",
    "dd",
    "yyyy",
    "month",
    "day",
    "year",
}
LOADING_RE = re.compile(r"\b(loading|please wait|fetching options)\b", re.IGNORECASE)
SENSITIVE_KEY_RE = re.compile(r"(password|token|cookie|session|otp|secret|authorization)", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_CANDIDATE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)")


@dataclass
class OptionCandidate:
    text: str
    locator: Any
    selector: str = ""
    index: int = 0
    value: str = ""
    metadata: dict[str, Any] | None = None


@dataclass
class MatchResult:
    candidate: OptionCandidate | None
    reason: str = ""
    ambiguous: bool = False
    matches: list[str] | None = None


def normalize_space(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_for_match(value: Any) -> str:
    text = normalize_space(value).lower()
    text = text.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def normalized_equals(left: Any, right: Any) -> bool:
    return normalize_for_match(left) == normalize_for_match(right)


def normalize_phone(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def is_placeholder_text(value: Any) -> bool:
    normalized = normalize_for_match(value)
    return normalized in PLACEHOLDER_NORMALIZED


def is_loading_text(value: Any) -> bool:
    return bool(LOADING_RE.search(normalize_space(value)))


def redact_sensitive_text(value: Any, limit: int = 180) -> str:
    text = normalize_space(value)
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)

    def redact_phone(match: re.Match[str]) -> str:
        digits = normalize_phone(match.group(0))
        if len(digits) >= 10:
            return "[REDACTED_PHONE]"
        return match.group(0)

    text = PHONE_CANDIDATE_RE.sub(redact_phone, text)
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def safe_count(locator: Any) -> int:
    try:
        return int(locator.count())
    except Exception:
        return 0


def safe_is_visible(locator: Any, timeout_ms: int = 150) -> bool:
    try:
        return bool(locator.is_visible(timeout=timeout_ms))
    except Exception:
        return False


def safe_is_enabled(locator: Any, timeout_ms: int = 150) -> bool:
    try:
        return bool(locator.is_enabled(timeout=timeout_ms))
    except Exception:
        return True


def safe_attr(locator: Any, name: str, timeout_ms: int = 150) -> str:
    try:
        return str(locator.get_attribute(name, timeout=timeout_ms) or "")
    except Exception:
        return ""


def safe_input_value(locator: Any, timeout_ms: int = 200) -> str:
    try:
        return str(locator.input_value(timeout=timeout_ms) or "")
    except Exception:
        return ""


def locator_tag(locator: Any) -> str:
    try:
        return str(locator.evaluate("el => el.tagName.toLowerCase()") or "")
    except Exception:
        return ""


def safe_text(locator: Any, timeout_ms: int = 250, *, include_input_value: bool = True) -> str:
    tag = locator_tag(locator)
    if include_input_value and tag in {"input", "textarea", "select"}:
        value = safe_input_value(locator, timeout_ms=timeout_ms)
        if value:
            return normalize_space(value)
    try:
        text = locator.inner_text(timeout=timeout_ms)
        if text:
            return normalize_space(text)
    except Exception:
        pass
    for attr in ("aria-label", "title", "placeholder", "value"):
        value = safe_attr(locator, attr, timeout_ms=timeout_ms)
        if value:
            return normalize_space(value)
    return ""


def first_visible_locator(root: Any, selectors: Iterable[str], *, max_count: int = 50, timeout_ms: int = 120) -> Any | None:
    for selector in selectors:
        locators = root.locator(selector)
        for index in range(min(safe_count(locators), max_count)):
            candidate = locators.nth(index)
            if safe_is_visible(candidate, timeout_ms=timeout_ms):
                return candidate
    return None


def wait_until(predicate: Callable[[], bool], *, timeout_ms: int = 2000, interval_ms: int = 80) -> bool:
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(max(interval_ms, 1) / 1000)
    return predicate()


def scoped_locator(page: Any, selector: str, scope_index: int = 0) -> Any | None:
    if not selector:
        return None
    locator = page.locator(selector)
    count = safe_count(locator)
    if count <= 0:
        return None
    index = max(0, min(scope_index, count - 1))
    return locator.nth(index)


def nearest_field_scope(locator: Any) -> Any:
    scope = locator.locator(
        "xpath=ancestor-or-self::*[self::fieldset or @role='group' or @role='radiogroup' "
        "or @data-field "
        "or contains(concat(' ', normalize-space(@class), ' '), ' field-container ') "
        "or contains(concat(' ', normalize-space(@class), ' '), ' field-wrapper ') "
        "or contains(concat(' ', normalize-space(@class), ' '), ' form-field ') "
        "or contains(concat(' ', normalize-space(@class), ' '), ' workday-field ') "
        "or contains(concat(' ', normalize-space(@class), ' '), ' wd-field ')][1]"
    )
    if safe_count(scope):
        return scope.first
    scope = locator.locator(
        "xpath=ancestor-or-self::*[@data-automation-id "
        "and not(self::input or self::select or self::button or self::textarea) "
        "and (.//input or .//select or .//button or .//textarea or .//*[@role='combobox' or @role='radio'])][1]"
    )
    if safe_count(scope):
        return scope.first
    parent = locator.locator("xpath=..")
    if safe_count(parent):
        return parent.first
    return locator


def _selected_option_text(select_locator: Any) -> str:
    try:
        return normalize_space(
            select_locator.evaluate(
                """el => {
                    const option = el.selectedOptions && el.selectedOptions[0];
                    return option ? option.textContent : "";
                }"""
            )
        )
    except Exception:
        return ""


def native_select_options(select_locator: Any) -> list[OptionCandidate]:
    options: list[OptionCandidate] = []
    option_locators = select_locator.locator("option")
    for index in range(safe_count(option_locators)):
        option = option_locators.nth(index)
        text = safe_text(option, include_input_value=False)
        value = safe_attr(option, "value") or text
        if text:
            options.append(OptionCandidate(text=text, locator=option, selector="option", index=index, value=value))
    return options


def _dom_identity(locator: Any) -> str:
    try:
        return str(
            locator.evaluate(
                """el => {
                    const win = el.ownerDocument.defaultView || window;
                    if (!win.__workdayWidgetElementIds) {
                        win.__workdayWidgetElementIds = new WeakMap();
                        win.__workdayWidgetNextElementId = 1;
                    }
                    let id = win.__workdayWidgetElementIds.get(el);
                    if (!id) {
                        id = String(win.__workdayWidgetNextElementId++);
                        win.__workdayWidgetElementIds.set(el, id);
                    }
                    return id;
                }"""
            )
        )
    except Exception:
        return ""


def collect_visible_option_candidates(page_or_locator: Any, *, max_count: int = 80) -> list[OptionCandidate]:
    selectors = [
        '[role="option"]',
        '[data-automation-id*="promptOption" i]',
        '[data-automation-id*="prompt-option" i]',
        '[data-testid*="prompt-option" i]',
        '[data-testid*="option" i]',
        ".wd-option",
        ".fixture-option",
        "[data-option]",
    ]
    candidates: list[OptionCandidate] = []
    seen_elements: set[str] = set()
    fallback_seen: set[tuple[str, int]] = set()
    for selector in selectors:
        locators = page_or_locator.locator(selector)
        for index in range(min(safe_count(locators), max_count)):
            locator = locators.nth(index)
            if not safe_is_visible(locator):
                continue
            text = safe_text(locator, include_input_value=False)
            if not text or is_loading_text(text):
                continue
            element_id = _dom_identity(locator)
            if element_id:
                if element_id in seen_elements:
                    continue
                seen_elements.add(element_id)
            elif (selector, index) in fallback_seen:
                continue
            fallback_seen.add((selector, index))
            candidates.append(OptionCandidate(text=text, locator=locator, selector=selector, index=index))
    return candidates


def match_expected_option(
    candidates: Iterable[OptionCandidate],
    expected_value: Any,
    aliases: Iterable[Any] | None = None,
) -> MatchResult:
    candidate_list = [candidate for candidate in candidates if normalize_for_match(candidate.text)]
    expected_norm = normalize_for_match(expected_value)
    alias_norms = [normalize_for_match(item) for item in aliases or [] if normalize_for_match(item)]

    if expected_norm:
        exact = [candidate for candidate in candidate_list if normalize_for_match(candidate.text) == expected_norm]
        if len(exact) == 1:
            return MatchResult(exact[0], "exact")
        if len(exact) > 1:
            return MatchResult(None, "ambiguous_exact", True, [item.text for item in exact])

    if alias_norms:
        alias_matches = [
            candidate
            for candidate in candidate_list
            if normalize_for_match(candidate.text) in alias_norms
        ]
        if len(alias_matches) == 1:
            return MatchResult(alias_matches[0], "alias")
        if len(alias_matches) > 1:
            return MatchResult(None, "ambiguous_alias", True, [item.text for item in alias_matches])

    containment_needles = [item for item in [expected_norm, *alias_norms] if item]
    containment = []
    for candidate in candidate_list:
        text_norm = normalize_for_match(candidate.text)
        if any(needle in text_norm or text_norm in needle for needle in containment_needles):
            containment.append(candidate)
    if len(containment) == 1:
        return MatchResult(containment[0], "unique_containment")
    if len(containment) > 1:
        return MatchResult(None, "ambiguous_containment", True, [item.text for item in containment])
    return MatchResult(None, "no_match", False, [item.text for item in candidate_list])


def extract_validation_messages(scope: Any) -> list[str]:
    selectors = [
        '[role="alert"]',
        '[aria-live="assertive"]',
        '[data-automation-id*="error" i]',
        '[data-automation-id*="validation" i]',
        ".error",
        ".validation",
        '[id*="error" i]',
    ]
    messages: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        locators = scope.locator(selector)
        for index in range(min(safe_count(locators), 25)):
            locator = locators.nth(index)
            if not safe_is_visible(locator):
                continue
            text = safe_text(locator, include_input_value=False)
            if text and text not in seen:
                seen.add(text)
                messages.append(text)
    return messages


def extract_hidden_values(scope: Any) -> list[str]:
    values: list[str] = []
    locators = scope.locator('input[type="hidden"], input[aria-hidden="true"], [data-committed-value]')
    for index in range(min(safe_count(locators), 25)):
        locator = locators.nth(index)
        value = safe_input_value(locator) or safe_attr(locator, "value") or safe_attr(locator, "data-committed-value")
        if value:
            values.append(value)
    return values


COMMITTED_HIDDEN_RE = re.compile(r"(selected|selection|chip|committed)", re.IGNORECASE)
GENERIC_HIDDEN_TOKEN_RE = re.compile(r"\b(csrf|access|request)[_-]?token\b", re.IGNORECASE)


def extract_associated_hidden_values(scope: Any, control: Any | None = None) -> list[str]:
    values: list[str] = []
    locators = scope.locator('input[type="hidden"], input[aria-hidden="true"], [data-committed-value]')
    for index in range(min(safe_count(locators), 25)):
        locator = locators.nth(index)
        marker_text = " ".join(
            safe_attr(locator, attr)
            for attr in (
                "id",
                "name",
                "class",
                "role",
                "aria-label",
                "aria-labelledby",
                "aria-describedby",
                "data-automation-id",
                "data-testid",
                "data-for",
                "data-prompt-id",
                "data-control-id",
            )
        )
        if GENERIC_HIDDEN_TOKEN_RE.search(marker_text):
            continue
        explicit_value = safe_attr(locator, "data-committed-value")
        has_committed_semantics = bool(explicit_value or COMMITTED_HIDDEN_RE.search(marker_text))
        if not has_committed_semantics:
            continue
        if control is not None and not _hidden_value_associated_with_control(locator, control):
            continue
        value = explicit_value or safe_input_value(locator) or safe_attr(locator, "value")
        if value:
            values.append(value)
    return values


def _hidden_value_associated_with_control(locator: Any, control: Any) -> bool:
    try:
        control_handle = control.element_handle(timeout=150)
        if control_handle is None:
            return False
        return bool(
            locator.evaluate(
                """(el, control) => {
                    if (!control) return true;
                    const controlTerms = [
                        control.id,
                        control.getAttribute("name"),
                        control.getAttribute("aria-controls"),
                        control.getAttribute("aria-labelledby"),
                        control.getAttribute("data-automation-id"),
                        control.getAttribute("data-testid"),
                    ].filter(Boolean).map(item => String(item).toLowerCase());
                    const hiddenTerms = [
                        el.id,
                        el.getAttribute("name"),
                        el.getAttribute("aria-controls"),
                        el.getAttribute("aria-labelledby"),
                        el.getAttribute("aria-describedby"),
                        el.getAttribute("data-for"),
                        el.getAttribute("data-prompt-id"),
                        el.getAttribute("data-control-id"),
                        el.getAttribute("data-automation-id"),
                        el.getAttribute("data-testid"),
                    ].filter(Boolean).map(item => String(item).toLowerCase());
                    if (controlTerms.length && hiddenTerms.some(hidden => controlTerms.some(term => hidden.includes(term) || term.includes(hidden)))) {
                        return true;
                    }
                    const fieldSelector = "fieldset,[role='group'],[data-field],[data-automation-id],.field";
                    const controlField = control.closest(fieldSelector);
                    return Boolean(controlField && controlField.contains(el));
                }""",
                control_handle,
            )
        )
    except Exception:
        return False


def extract_committed_tokens(scope: Any, control: Any | None = None) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()

    def add_token(value: Any) -> None:
        text = normalize_space(value)
        if not text or is_placeholder_text(text) or is_loading_text(text):
            return
        normalized = normalize_for_match(text)
        if normalized in seen:
            return
        seen.add(normalized)
        tokens.append(text)

    if control is not None:
        tag = locator_tag(control)
        if tag == "select":
            add_token(_selected_option_text(control))
        elif tag in {"button", "div", "span"}:
            add_token(safe_text(control, include_input_value=False))

    selectors = [
        '[data-automation-id*="selected" i]',
        '[data-automation-id*="token" i]',
        '[data-testid*="selected" i]',
        '[data-testid*="token" i]',
        '[aria-selected="true"]',
        '[data-selected="true"]',
        ".wd-token",
        ".token",
        ".selected",
        '[data-attachment-tile]',
    ]
    for selector in selectors:
        locators = scope.locator(selector)
        for index in range(min(safe_count(locators), 40)):
            locator = locators.nth(index)
            if not safe_is_visible(locator):
                continue
            add_token(safe_text(locator, include_input_value=False))
    for hidden_value in extract_associated_hidden_values(scope, control):
        add_token(hidden_value)
    return tokens


def visible_text_contains(root: Any, expected: str) -> bool:
    expected_norm = normalize_for_match(expected)
    if not expected_norm:
        return False
    try:
        text = root.locator("body").inner_text(timeout=250)
    except Exception:
        try:
            text = root.inner_text(timeout=250)
        except Exception:
            text = ""
    return expected_norm in normalize_for_match(text)


def upload_success_markers(root: Any, filename: str = "") -> list[str]:
    markers: list[str] = []
    marker_selectors = [
        '[data-automation-id*="upload" i]',
        '[data-automation-id*="attachment" i]',
        '[data-testid*="upload" i]',
        '[data-testid*="attachment" i]',
        '[data-attachment-tile]',
        ".upload-success",
        ".attachment-tile",
    ]
    marker_re = re.compile(r"\b(successfully uploaded|upload complete|uploaded|attached)\b", re.IGNORECASE)
    for selector in marker_selectors:
        locators = root.locator(selector)
        for index in range(min(safe_count(locators), 40)):
            locator = locators.nth(index)
            if not safe_is_visible(locator):
                continue
            text = safe_text(locator, include_input_value=False)
            if text and (marker_re.search(text) or (filename and normalized_equals(text, filename))):
                markers.append(text)
    if filename and visible_text_contains(root, Path(filename).name):
        markers.append(Path(filename).name)
    try:
        body_text = root.locator("body").inner_text(timeout=250)
    except Exception:
        try:
            body_text = root.inner_text(timeout=250)
        except Exception:
            body_text = ""
    if marker_re.search(body_text):
        markers.append(marker_re.search(body_text).group(0))  # type: ignore[union-attr]
    return list(dict.fromkeys(markers))


def is_forbidden_submit_control(locator: Any) -> bool:
    text = normalize_for_match(
        " ".join(
            item
            for item in [
                safe_text(locator, include_input_value=False),
                safe_attr(locator, "aria-label"),
                safe_attr(locator, "title"),
                safe_attr(locator, "type"),
            ]
            if item
        )
    )
    return bool(re.search(r"\b(submit|finish|complete|done)\b", text))


def safe_locator_metadata(locator: Any, context: Any | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "tag": locator_tag(locator),
        "visible": safe_is_visible(locator),
        "enabled": safe_is_enabled(locator),
    }
    for attr in (
        "id",
        "name",
        "type",
        "role",
        "aria-label",
        "aria-haspopup",
        "aria-expanded",
        "aria-busy",
        "placeholder",
        "data-automation-id",
        "data-testid",
        "data-field",
    ):
        if SENSITIVE_KEY_RE.search(attr):
            continue
        value = safe_attr(locator, attr)
        if value:
            metadata[attr] = redact_sensitive_text(value, limit=120)
    tag = metadata.get("tag")
    if tag not in {"input", "textarea"}:
        text = safe_text(locator, include_input_value=False)
        if text:
            metadata["text_excerpt"] = redact_sensitive_text(text, limit=140)
    if context:
        for key in ("canonical_key", "selector", "scope_index", "current_stage"):
            value = context_get(context, key)
            if value not in (None, "") and not SENSITIVE_KEY_RE.search(str(key)):
                metadata[key] = value
    return metadata


def context_get(context: Any | None, key: str, default: Any = None) -> Any:
    if context is None:
        return default
    if isinstance(context, dict):
        return context.get(key, default)
    return getattr(context, key, default)


def context_list(context: Any | None, key: str) -> list[Any]:
    value = context_get(context, key, [])
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple) or isinstance(value, set):
        return list(value)
    return [value]
