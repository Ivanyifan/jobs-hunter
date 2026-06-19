from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
import re
import time
from typing import Any, Iterable


US_COUNTRY_LABELS = {
    "united states",
    "united states of america",
    "usa",
    "us",
    "u s",
    "u s a",
}

STATUS_OK = "ok"
STATUS_UNSUPPORTED = "unsupported"
STATUS_FAILED = "failed"
STATUS_HUMAN_REQUIRED = "human_required"

UNSUPPORTED_REASONS = {
    "add_not_found",
    "country_control_not_found",
    "field_not_found",
    "input_not_found",
    "section_not_found",
    "select_not_found",
    "unsupported_select",
}


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


@dataclass
class HandlerResult:
    ok: bool
    changed: bool = False
    attempts: int = 0
    field: str = ""
    reason: str = ""
    value: str = ""
    details: dict[str, Any] = dataclass_field(default_factory=dict)
    status: str = ""

    def __post_init__(self) -> None:
        if self.status:
            return
        if self.ok:
            self.status = STATUS_OK
        elif self.reason == "max_attempts":
            self.status = STATUS_HUMAN_REQUIRED
        elif self.reason in UNSUPPORTED_REASONS:
            self.status = STATUS_UNSUPPORTED
        else:
            self.status = STATUS_FAILED


@dataclass
class ExecutionState:
    field_locks: dict[str, str] = dataclass_field(default_factory=dict)
    section_add_attempts: dict[str, int] = dataclass_field(default_factory=dict)
    field_attempts: dict[str, int] = dataclass_field(default_factory=dict)

    def is_valid(self, key: str, value: str = "valid") -> bool:
        return self.field_locks.get(key) == value

    def mark_valid(self, key: str, value: str = "valid") -> None:
        self.field_locks[key] = value

    def can_attempt_field(self, key: str, max_attempts: int = 2) -> bool:
        return self.field_attempts.get(key, 0) < max_attempts

    def note_field_attempt(self, key: str) -> int:
        self.field_attempts[key] = self.field_attempts.get(key, 0) + 1
        return self.field_attempts[key]

    def can_add_section(self, section: str, max_attempts: int = 1) -> bool:
        return self.section_add_attempts.get(section, 0) < max_attempts

    def note_section_add(self, section: str) -> int:
        self.section_add_attempts[section] = self.section_add_attempts.get(section, 0) + 1
        return self.section_add_attempts[section]


def locator_text(locator) -> str:
    try:
        value = locator.input_value(timeout=200)
        if value:
            return value.strip()
    except Exception:
        pass
    try:
        return re.sub(r"\s+", " ", locator.inner_text(timeout=300)).strip()
    except Exception:
        pass
    try:
        return (locator.get_attribute("aria-label", timeout=200) or "").strip()
    except Exception:
        return ""


def first_visible(page_or_locator, selectors: Iterable[str]):
    for selector in selectors:
        loc = page_or_locator.locator(selector)
        try:
            count = min(loc.count(), 25)
        except Exception:
            count = 0
        for idx in range(count):
            item = loc.nth(idx)
            try:
                if item.is_visible(timeout=150):
                    return item
            except Exception:
                continue
    return None


def wait_until(predicate, timeout_ms: int = 2000, interval_ms: int = 80) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_ms / 1000)
    return predicate()


class SearchPromptHandler:
    def __init__(self, max_attempts: int = 2):
        self.max_attempts = max_attempts

    def selection_is_valid(self, page, input_selector: str, expected: str, hidden_id_selector: str | None = None) -> bool:
        input_box = page.locator(input_selector).first
        if input_box.count() == 0:
            return False
        expected_norm = norm(expected)
        current = locator_text(input_box)
        if expected_norm and expected_norm not in norm(current) and norm(current) not in expected_norm:
            return False
        if hidden_id_selector:
            hidden = page.locator(hidden_id_selector).first
            if hidden.count() == 0:
                return False
            try:
                return bool(hidden.input_value(timeout=200).strip())
            except Exception:
                try:
                    return bool((hidden.get_attribute("value", timeout=200) or "").strip())
                except Exception:
                    return False
        return True

    def select(self, page, input_selector: str, query: str, preferred: Iterable[str] | None = None, state: ExecutionState | None = None, key: str | None = None, hidden_id_selector: str | None = None) -> HandlerResult:
        state = state or ExecutionState()
        key = key or input_selector
        if state.is_valid(key):
            return HandlerResult(True, changed=False, field=key, reason="locked")
        existing_values = [query, *(preferred or [])]
        for existing in existing_values:
            if existing and self.selection_is_valid(page, input_selector, existing, hidden_id_selector=hidden_id_selector):
                state.mark_valid(key)
                return HandlerResult(True, changed=False, field=key, reason="already_valid", value=existing)
        if not state.can_attempt_field(key, self.max_attempts):
            return HandlerResult(False, attempts=state.field_attempts.get(key, 0), field=key, reason="max_attempts", status=STATUS_HUMAN_REQUIRED)

        attempts = state.note_field_attempt(key)
        input_box = page.locator(input_selector).first
        if input_box.count() == 0:
            return HandlerResult(False, attempts=attempts, field=key, reason="input_not_found")

        input_box.click(timeout=1500)
        input_box.fill("", timeout=1000)
        input_box.fill(query, timeout=1500)

        wanted = [query, *(preferred or [])]
        wanted_norms = [norm(item) for item in wanted if norm(item)]

        def option_matches(text: str) -> bool:
            text_norm = norm(text)
            return any(text_norm == item or item in text_norm or text_norm in item for item in wanted_norms)

        option_selectors = [
            '[role="option"]',
            '[data-automation-id*="promptOption" i]',
            '[data-testid="prompt-option"]',
            '.wd-option',
            '.fixture-option',
        ]

        def has_option() -> bool:
            for selector in option_selectors:
                options = page.locator(selector)
                for idx in range(min(options.count(), 50)):
                    opt = options.nth(idx)
                    try:
                        if opt.is_visible(timeout=100) and option_matches(locator_text(opt)):
                            return True
                    except Exception:
                        continue
            return False

        wait_until(has_option, timeout_ms=1800)
        for selector in option_selectors:
            options = page.locator(selector)
            for idx in range(min(options.count(), 50)):
                opt = options.nth(idx)
                try:
                    if not opt.is_visible(timeout=100):
                        continue
                    text = locator_text(opt)
                    if not option_matches(text):
                        continue
                    opt.click(timeout=1500)
                    if hidden_id_selector and not wait_until(
                        lambda: self.selection_is_valid(page, input_selector, text, hidden_id_selector=hidden_id_selector),
                        timeout_ms=1200,
                    ):
                        status = STATUS_HUMAN_REQUIRED if attempts >= self.max_attempts else STATUS_FAILED
                        return HandlerResult(False, attempts=attempts, field=key, reason="hidden_entity_id_missing", value=text, status=status)
                    state.mark_valid(key)
                    return HandlerResult(True, changed=True, attempts=attempts, field=key, value=text)
                except Exception:
                    continue
        status = STATUS_HUMAN_REQUIRED if attempts >= self.max_attempts else STATUS_FAILED
        return HandlerResult(False, attempts=attempts, field=key, reason="option_not_found", value=query, status=status)


class NativeSelectHandler:
    def __init__(self, max_attempts: int = 2):
        self.max_attempts = max_attempts

    def select(self, page, selector: str, values: Iterable[str], state: ExecutionState | None = None, key: str | None = None) -> HandlerResult:
        state = state or ExecutionState()
        key = key or selector
        if state.is_valid(key):
            return HandlerResult(True, field=key, reason="locked")
        loc = page.locator(selector).first
        if loc.count() == 0:
            return HandlerResult(False, field=key, reason="select_not_found")

        values_norm = [norm(value) for value in values if norm(value)]
        tag = ""
        try:
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            tag = ""

        if tag == "select":
            options = loc.locator("option")
            try:
                current_value = loc.input_value(timeout=200)
            except Exception:
                current_value = ""
            for idx in range(options.count()):
                opt = options.nth(idx)
                text = locator_text(opt)
                value = opt.get_attribute("value") or text
                option_norm = norm(f"{text} {value}")
                if any(item == norm(text) or item == norm(value) or item in option_norm for item in values_norm):
                    if current_value == value:
                        state.mark_valid(key)
                        return HandlerResult(True, changed=False, field=key, value=text, reason="already_valid")
                    if not state.can_attempt_field(key, self.max_attempts):
                        return HandlerResult(False, attempts=state.field_attempts.get(key, 0), field=key, reason="max_attempts", status=STATUS_HUMAN_REQUIRED)
                    attempts = state.note_field_attempt(key)
                    loc.select_option(value=value, timeout=1500)
                    state.mark_valid(key)
                    return HandlerResult(True, changed=True, attempts=attempts, field=key, value=text)
            if not state.can_attempt_field(key, self.max_attempts):
                return HandlerResult(False, attempts=state.field_attempts.get(key, 0), field=key, reason="max_attempts", status=STATUS_HUMAN_REQUIRED)
            attempts = state.note_field_attempt(key)
            status = STATUS_HUMAN_REQUIRED if attempts >= self.max_attempts else STATUS_FAILED
            return HandlerResult(False, attempts=attempts, field=key, reason="option_not_found", status=status)

        if not state.can_attempt_field(key, self.max_attempts):
            return HandlerResult(False, attempts=state.field_attempts.get(key, 0), field=key, reason="max_attempts", status=STATUS_HUMAN_REQUIRED)
        attempts = state.note_field_attempt(key)
        loc.click(timeout=1500)
        for value in values:
            terms = [value]
            option = SearchPromptHandler(max_attempts=1).select(page, selector, value, terms, state=ExecutionState(), key=f"{key}:popup")
            if option.ok:
                state.mark_valid(key)
                return HandlerResult(True, changed=True, attempts=attempts, field=key, value=option.value)
        return HandlerResult(False, attempts=attempts, field=key, reason="unsupported_select")


class CountrySelectorHandler:
    def __init__(self, max_attempts: int = 2):
        self.max_attempts = max_attempts

    def is_us(self, page) -> bool:
        loc = page.locator("#country--country, [data-field='country']").first
        if loc.count() == 0:
            return False
        return norm(locator_text(loc)) in US_COUNTRY_LABELS

    def ensure_united_states(self, page, state: ExecutionState | None = None) -> HandlerResult:
        state = state or ExecutionState()
        key = "country"
        if self.is_us(page):
            state.mark_valid(key)
            return HandlerResult(True, field=key, reason="already_valid", value=locator_text(page.locator("#country--country, [data-field='country']").first))
        if state.is_valid(key) and self.is_us(page):
            return HandlerResult(True, field=key, reason="locked")
        if not state.can_attempt_field(key, self.max_attempts):
            return HandlerResult(False, attempts=state.field_attempts.get(key, 0), field=key, reason="max_attempts")
        attempts = state.note_field_attempt(key)
        control = page.locator("#country--country, [data-field='country']").first
        if control.count() == 0:
            return HandlerResult(False, attempts=attempts, field=key, reason="country_control_not_found")
        control.click(timeout=1500)
        search = first_visible(page, ['[data-popup="country"] input', '[role="dialog"] input', 'input[placeholder="Search"]'])
        if search:
            search.fill("United States", timeout=1000)
        candidates = [
            r"^United States of America$",
            r"^United States$",
        ]
        for pattern in candidates:
            opt = page.get_by_role("option", name=re.compile(pattern, re.I)).first
            try:
                if opt.count() and opt.is_visible(timeout=500):
                    opt.click(timeout=1500)
                    if self.is_us(page):
                        state.mark_valid(key)
                        return HandlerResult(True, changed=True, attempts=attempts, field=key, value=locator_text(control))
            except Exception:
                pass
        status = STATUS_HUMAN_REQUIRED if attempts >= self.max_attempts else STATUS_FAILED
        return HandlerResult(False, attempts=attempts, field=key, reason="us_option_not_selected", status=status)


class RepeatableSection:
    section_key = ""
    add_button_name = re.compile(r"^Add$", re.I)

    def row_exists(self, page) -> bool:
        raise NotImplementedError

    def add_row_once(self, page, state: ExecutionState) -> HandlerResult:
        if self.row_exists(page):
            return HandlerResult(True, field=self.section_key, reason="row_exists")
        if not state.can_add_section(self.section_key, 1):
            return HandlerResult(False, attempts=state.section_add_attempts.get(self.section_key, 0), field=self.section_key, reason="section_add_already_attempted")
        attempts = state.note_section_add(self.section_key)
        group = page.get_by_role("group", name=re.compile(rf"^{re.escape(self.section_key.title())}$", re.I)).first
        if group.count() == 0:
            group = page.locator(f'[data-section="{self.section_key}"]').first
        if group.count() == 0:
            return HandlerResult(False, attempts=attempts, field=self.section_key, reason="section_not_found")
        button = group.get_by_role("button", name=self.add_button_name).first
        if button.count() == 0:
            button = group.locator('[data-automation-id="add-button"], button').first
        if button.count() == 0:
            return HandlerResult(False, attempts=attempts, field=self.section_key, reason="add_not_found")
        button.click(timeout=1500)
        opened = wait_until(lambda: self.row_exists(page), timeout_ms=2000)
        return HandlerResult(opened, changed=opened, attempts=attempts, field=self.section_key, reason="" if opened else "row_not_created")


class EducationRepeatableSectionHandler(RepeatableSection):
    section_key = "education"

    def row_exists(self, page) -> bool:
        return page.locator('input[id*="education-"][id*="school"], input[data-field="education-school"]').count() > 0

    def fill(self, page, data: dict[str, str], state: ExecutionState | None = None) -> HandlerResult:
        state = state or ExecutionState()
        add = self.add_row_once(page, state)
        if not add.ok:
            return add
        changed = add.changed

        prompt = SearchPromptHandler()
        school = prompt.select(
            page,
            'input[id*="education-"][id*="school"], input[data-field="education-school"]',
            data.get("school", ""),
            preferred=["University of Illinois at Urbana-Champaign", "University of Illinois Urbana-Champaign"],
            state=state,
            key="education.school",
            hidden_id_selector='input[data-field="education-school-id"], input[name="education-school-id"]',
        )
        if not school.ok:
            return HandlerResult(False, attempts=school.attempts, field="education.school", reason=school.reason, details={"add_attempts": state.section_add_attempts.get("education", 0)}, status=school.status)
        changed = changed or school.changed

        degree_values = [
            data.get("degree", ""),
            "Bachelor's Degree",
            "Bachelors Degree",
            "Bachelor Degree",
            "Bachelor of Science",
        ]
        degree = NativeSelectHandler().select(
            page,
            'select[id*="education-"][id*="degree"], select[data-field="education-degree"]',
            degree_values,
            state=state,
            key="education.degree",
        )
        if not degree.ok:
            return HandlerResult(False, attempts=degree.attempts, field="education.degree", reason=degree.reason, status=degree.status)
        changed = changed or degree.changed

        field_result = prompt.select(
            page,
            'input[id*="education-"][id*="field"], input[id*="education-"][id*="fieldOfStudy"], input[data-field="education-field"]',
            data.get("field", ""),
            preferred=[data.get("field", ""), "Computer Science"],
            state=state,
            key="education.field",
            hidden_id_selector='input[data-field="education-field-id"], input[name="education-field-id"]',
        )
        if not field_result.ok:
            return HandlerResult(False, attempts=field_result.attempts, field="education.field", reason=field_result.reason, status=field_result.status)
        changed = changed or field_result.changed
        return HandlerResult(True, changed=changed, field="education")


class ExperienceRepeatableSectionHandler(RepeatableSection):
    section_key = "experience"

    def row_exists(self, page) -> bool:
        return page.locator('input[id*="workExperience-"][id*="jobTitle"], input[data-field="experience-title"]').count() > 0

    def fill(self, page, data: dict[str, str], state: ExecutionState | None = None) -> HandlerResult:
        state = state or ExecutionState()
        add = self.add_row_once(page, state)
        if not add.ok:
            return add
        changed = add.changed
        fields = [
            ('input[id*="workExperience-"][id*="jobTitle"], input[data-field="experience-title"]', "title", "experience.title"),
            ('input[id*="workExperience-"][id*="companyName"], input[data-field="experience-company"]', "company", "experience.company"),
            ('input[id*="workExperience-"][id*="location"], input[data-field="experience-location"]', "location", "experience.location"),
            ('textarea[id*="workExperience-"], textarea[data-field="experience-description"]', "description", "experience.description"),
        ]
        for selector, data_key, lock_key in fields:
            value = data.get(data_key, "")
            if not value or state.is_valid(lock_key):
                continue
            if not state.can_attempt_field(lock_key, 2):
                return HandlerResult(False, field=lock_key, reason="max_attempts", status=STATUS_HUMAN_REQUIRED)
            attempts = state.note_field_attempt(lock_key)
            loc = page.locator(selector).first
            if loc.count() == 0:
                return HandlerResult(False, field=lock_key, attempts=attempts, reason="field_not_found")
            try:
                if locator_text(loc) == value:
                    state.mark_valid(lock_key)
                    continue
            except Exception:
                pass
            loc.fill(value, timeout=1200)
            state.mark_valid(lock_key)
            changed = True
        return HandlerResult(True, changed=changed, field="experience")
