from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any

from ..contracts import ActionResult, FieldState, FieldStatus
from .base import BaseWorkdayWidget, WorkdayWidgetContext
from .utilities import (
    is_placeholder_text,
    locator_tag,
    normalize_for_match,
    safe_attr,
    safe_count,
    safe_input_value,
    safe_text,
    scoped_locator,
)


MONTH_NAMES = {
    "01": ["january", "jan", "1", "01"],
    "02": ["february", "feb", "2", "02"],
    "03": ["march", "mar", "3", "03"],
    "04": ["april", "apr", "4", "04"],
    "05": ["may", "5", "05"],
    "06": ["june", "jun", "6", "06"],
    "07": ["july", "jul", "7", "07"],
    "08": ["august", "aug", "8", "08"],
    "09": ["september", "sep", "sept", "9", "09"],
    "10": ["october", "oct", "10"],
    "11": ["november", "nov", "11"],
    "12": ["december", "dec", "12"],
}


@dataclass
class DatePart:
    kind: str
    locator: Any
    required: bool = True


class WorkdayDateGroupWidget(BaseWorkdayWidget):
    def locate(self, page: Any, context: Any | None = None) -> Any:
        ctx = WorkdayWidgetContext.from_any(context)
        locator = scoped_locator(page, ctx.selector, ctx.scope_index) if ctx.selector else None
        if locator is not None:
            return locator
        for selector in ctx.locator_hints:
            locator = scoped_locator(page, selector, ctx.scope_index)
            if locator is not None:
                return locator
        return page.locator("body")

    def locate_date_parts(self, page: Any, context: Any | None = None) -> dict[str, DatePart]:
        root = self.locate(page, context)
        parts: dict[str, DatePart] = {}
        if locator_tag(root) in {"input", "select"}:
            kind = self._part_kind(root)
            if kind:
                parts[kind] = DatePart(kind=kind, locator=root)
                return parts
        locators = root.locator("input:not([type='hidden']), select")
        for index in range(min(safe_count(locators), 25)):
            locator = locators.nth(index)
            kind = self._part_kind(locator)
            if kind and kind not in parts:
                parts[kind] = DatePart(kind=kind, locator=locator)
        if len(parts) == 1 and next(iter(parts)) in {"month", "day", "year"}:
            return parts
        return parts

    def parse_explicit_date(self, value: Any) -> dict[str, str]:
        if isinstance(value, date):
            return {
                "year": f"{value.year:04d}",
                "month": f"{value.month:02d}",
                "day": f"{value.day:02d}",
                "single": value.strftime("%m/%d/%Y"),
                "iso": value.isoformat(),
            }
        text = str(value or "").strip()
        if not text:
            raise ValueError("date must be explicit numeric date-like text")
        if re.fullmatch(r"\d{4}", text):
            return {"year": text}
        match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text)
        if match:
            month_text, day, year = match.groups()
            month = self._month_from_text(month_text)
            if not month:
                raise ValueError("month out of range")
            parsed = date(int(year), int(month), int(day))
            return {
                "year": f"{parsed.year:04d}",
                "month": f"{parsed.month:02d}",
                "day": f"{parsed.day:02d}",
                "single": parsed.strftime("%m/%d/%Y"),
                "iso": parsed.isoformat(),
            }
        match = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
        if match:
            day, month_text, year = match.groups()
            month = self._month_from_text(month_text)
            if not month:
                raise ValueError("month out of range")
            parsed = date(int(year), int(month), int(day))
            return {
                "year": f"{parsed.year:04d}",
                "month": f"{parsed.month:02d}",
                "day": f"{parsed.day:02d}",
                "single": parsed.strftime("%m/%d/%Y"),
                "iso": parsed.isoformat(),
            }
        match = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", text)
        if match:
            month_text, year = match.groups()
            month = self._month_from_text(month_text)
            if not month:
                raise ValueError("month out of range")
            return {"year": year, "month": month, "month_year": f"{month}/{year}"}
        month_name = self._month_from_text(text)
        if month_name:
            return {"month": month_name}
        if re.search(r"[A-Za-z]", text):
            raise ValueError("date must be explicit numeric date-like text")
        if re.fullmatch(r"\d{1,2}", text):
            month_i = int(text)
            if not 1 <= month_i <= 12:
                raise ValueError("month out of range")
            return {"month": f"{month_i:02d}"}
        match = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", text)
        if match:
            year, month, day = match.groups()
            month = f"{int(month):02d}"
            result = {"year": year, "month": month, "month_year": f"{month}/{year}"}
            if day:
                parsed = date(int(year), int(month), int(day))
                result = {
                    "year": f"{parsed.year:04d}",
                    "month": f"{parsed.month:02d}",
                    "day": f"{parsed.day:02d}",
                    "single": parsed.strftime("%m/%d/%Y"),
                    "iso": parsed.isoformat(),
                }
            return result
        match = re.fullmatch(r"(\d{1,2})/(\d{4})", text)
        if match:
            month, year = match.groups()
            month_i = int(month)
            if not 1 <= month_i <= 12:
                raise ValueError("month out of range")
            month = f"{month_i:02d}"
            return {"year": year, "month": month, "month_year": f"{month}/{year}"}
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if match:
            month, day, year = match.groups()
            parsed = date(int(year), int(month), int(day))
            return {
                "year": f"{parsed.year:04d}",
                "month": f"{parsed.month:02d}",
                "day": f"{parsed.day:02d}",
                "single": parsed.strftime("%m/%d/%Y"),
                "iso": parsed.isoformat(),
            }
        raise ValueError("date must be explicit numeric date-like text")

    def fill_parts(self, page: Any, value: Any, context: Any | None = None) -> ActionResult:
        ctx = WorkdayWidgetContext.from_any(context)
        before = self.observe(page, ctx)
        try:
            parsed = self.parse_explicit_date(value)
        except ValueError as exc:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="fill_date",
                target=ctx.canonical_key,
                value=value,
                before=before,
                after=after,
                reason=str(exc),
            )
        try:
            parts = self.locate_date_parts(page, ctx)
            if "single" in parts:
                single_value = self._single_value_for_input(parts["single"].locator, parsed)
                if not single_value:
                    raise ValueError("single date input requires explicit date value")
                self._set_value(parts["single"].locator, single_value)
            else:
                for kind, part in parts.items():
                    if kind not in parsed:
                        continue
                    self._set_part(part.locator, kind, parsed[kind])
        except Exception as exc:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="fill_date",
                target=ctx.canonical_key,
                value=value,
                before=before,
                after=after,
                reason=f"date_fill_failed:{exc}",
                retryable=True,
            )
        self.wait_until_stable(page, ctx, self.default_timeout_ms)
        after = self.observe(page, ctx)
        verify = self.verify_date(page, value, ctx)
        return self.result(
            acted=True,
            verified=verify.verified,
            action="fill_date",
            target=ctx.canonical_key,
            value=value,
            before=before,
            after=after,
            reason="verified" if verify.verified else verify.reason,
            retryable=not verify.verified,
        )

    def verify_date(self, page: Any, expected_value: Any, context: Any | None = None) -> ActionResult:
        ctx = WorkdayWidgetContext.from_any(context)
        state = self.observe(page, ctx)
        try:
            parsed = self.parse_explicit_date(expected_value)
            parts = self.locate_date_parts(page, ctx)
        except Exception as exc:
            return self.result(
                acted=False,
                verified=False,
                action="verify_date",
                target=ctx.canonical_key,
                value=expected_value,
                after=state,
                reason=str(exc),
            )
        required = self._required_components(parsed, parts)
        for kind in required:
            if kind not in parts:
                return self.result(
                    acted=False,
                    verified=False,
                    action="verify_date",
                    target=ctx.canonical_key,
                    value=expected_value,
                    after=state,
                    reason=f"missing_date_component:{kind}",
                    retryable=True,
                    metadata={"expected": parsed, "located_parts": sorted(parts)},
                )
        actual = self._read_parts(parts)
        if any(is_placeholder_text(actual.get(kind, "")) for kind in required):
            return self.result(
                acted=False,
                verified=False,
                action="verify_date",
                target=ctx.canonical_key,
                value=expected_value,
                after=state,
                reason="placeholder_component",
            )
        verified = bool(required) and all(self._component_matches(kind, actual.get(kind, ""), parsed) for kind in required)
        return self.result(
            acted=False,
            verified=verified,
            action="verify_date",
            target=ctx.canonical_key,
            value=expected_value,
            after=state,
            reason="verified" if verified else "date_component_mismatch",
            retryable=not verified,
            metadata={"actual": actual, "expected": parsed},
        )

    def observe(self, page: Any, context: Any | None = None) -> FieldState:
        ctx = WorkdayWidgetContext.from_any(context)
        try:
            parts = self.locate_date_parts(page, ctx)
            actual = self._read_parts(parts)
        except Exception as exc:
            return FieldState(
                canonical_key=ctx.canonical_key,
                required=ctx.required,
                status=FieldStatus.MISSING if ctx.required else FieldStatus.OPTIONAL,
                expected_value=ctx.expected_value,
                source=self.__class__.__name__,
                metadata={"reason": str(exc)},
            )
        filled_parts = {kind: value for kind, value in actual.items() if value and not is_placeholder_text(value)}
        required_kinds = [kind for kind in parts if kind != "single"] or list(parts)
        metadata = {"parts": sorted(parts)}
        if ctx.expected_value not in (None, ""):
            try:
                parsed = self.parse_explicit_date(ctx.expected_value)
                required_kinds = self._required_components(parsed, parts)
                metadata["expected_components"] = required_kinds
                metadata["expected_granularity"] = self._expected_granularity(parsed)
            except ValueError as exc:
                required_kinds = []
                metadata["expected_parse_error"] = str(exc)
        complete = bool(parts) and bool(required_kinds) and all(
            kind in parts and kind in filled_parts
            for kind in required_kinds
        )
        status = FieldStatus.FILLED if complete else (FieldStatus.MISSING if ctx.required else FieldStatus.OPTIONAL)
        return FieldState(
            canonical_key=ctx.canonical_key,
            required=ctx.required,
            status=status,
            visible_value=actual,
            expected_value=ctx.expected_value,
            source=self.__class__.__name__,
            locator_hints=[ctx.selector, *ctx.locator_hints],
            metadata=metadata,
        )

    def act(self, page: Any, value: Any, context: Any | None = None) -> ActionResult:
        return self.fill_parts(page, value, context)

    def verify(self, page: Any, expected_value: Any, context: Any | None = None) -> ActionResult:
        return self.verify_date(page, expected_value, context)

    def _part_kind(self, locator: Any) -> str:
        raw_haystack = " ".join(
            [
                safe_attr(locator, "placeholder"),
                safe_attr(locator, "aria-label"),
                safe_attr(locator, "name"),
                safe_attr(locator, "id"),
                safe_attr(locator, "data-automation-id"),
                safe_text(locator, include_input_value=False),
            ]
        )
        expanded_haystack = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw_haystack)
        haystack = normalize_for_match(expanded_haystack)
        placeholder = normalize_for_match(safe_attr(locator, "placeholder"))
        if placeholder in {"mm dd yyyy", "m d yyyy", "mm yyyy", "yyyy mm dd", "yyyy mm"} and locator_tag(locator) == "input":
            return "single"
        if "yyyy" in haystack or re.search(r"\byear\b", haystack):
            return "year"
        if "month" in haystack or re.search(r"\bmm\b", haystack):
            return "month"
        if re.search(r"\bdd\b", haystack) or re.search(r"\bday\b", haystack):
            return "day"
        if safe_attr(locator, "type") == "date" or "date" in haystack and "update" not in haystack:
            return "single"
        return ""

    def _set_value(self, locator: Any, value: str) -> None:
        if locator_tag(locator) == "select":
            locator.select_option(value=value, timeout=1200)
            return
        locator.evaluate(
            """(el, value) => {
                const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
                if (descriptor && descriptor.set) descriptor.set.call(el, value);
                else el.value = value;
                el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
                el.blur();
            }""",
            value,
        )

    def _set_part(self, locator: Any, kind: str, value: str) -> None:
        if locator_tag(locator) == "select":
            wanted = [value]
            if kind == "month":
                wanted.extend(MONTH_NAMES.get(value, []))
            options = locator.locator("option")
            for index in range(safe_count(options)):
                option = options.nth(index)
                option_text = safe_text(option, include_input_value=False)
                option_value = safe_attr(option, "value") or option_text
                if normalize_for_match(option_text) in {normalize_for_match(item) for item in wanted} or normalize_for_match(option_value) in {normalize_for_match(item) for item in wanted}:
                    locator.select_option(value=option_value, timeout=1200)
                    return
            raise ValueError(f"{kind}_option_not_found")
        self._set_value(locator, value)

    def _read_parts(self, parts: dict[str, DatePart]) -> dict[str, str]:
        values: dict[str, str] = {}
        for kind, part in parts.items():
            values[kind] = safe_input_value(part.locator) or safe_text(part.locator)
        return values

    def _required_components(self, parsed: dict[str, str], parts: dict[str, DatePart]) -> list[str]:
        granularity = self._expected_granularity(parsed)
        if "single" in parts:
            return ["single"]
        if granularity == "full":
            return ["month", "day", "year"]
        if granularity == "month_year":
            return ["month", "year"]
        if granularity == "month":
            return ["month"]
        if granularity == "year":
            return ["year"]
        return []

    def _component_matches(self, kind: str, actual: str, parsed: dict[str, str]) -> bool:
        if kind == "single":
            allowed_by_granularity = {
                "full": ("single", "iso"),
                "month_year": ("month_year",),
                "month": ("month",),
                "year": ("year",),
            }
            allowed = allowed_by_granularity.get(self._expected_granularity(parsed), ())
            return any(self._part_matches(kind, actual, parsed[item]) for item in allowed if item in parsed)
        return self._part_matches(kind, actual, parsed[kind])

    def _expected_granularity(self, parsed: dict[str, str]) -> str:
        if parsed.get("day") and (parsed.get("single") or parsed.get("iso")):
            return "full"
        if parsed.get("month_year") and parsed.get("month") and parsed.get("year"):
            return "month_year"
        if parsed.get("month") and not parsed.get("year"):
            return "month"
        if parsed.get("year") and not parsed.get("month"):
            return "year"
        return ""

    def _single_value_for_input(self, locator: Any, parsed: dict[str, str]) -> str:
        granularity = self._expected_granularity(parsed)
        if safe_attr(locator, "type") == "date":
            return parsed.get("iso", "") if granularity == "full" else ""
        if granularity == "full":
            return parsed.get("single", "")
        if granularity == "month_year":
            return parsed.get("month_year", "")
        if granularity == "month":
            return parsed.get("month", "")
        if granularity == "year":
            return parsed.get("year", "")
        return ""

    def _part_matches(self, kind: str, actual: str, expected: str) -> bool:
        if kind == "month":
            actual_norm = normalize_for_match(actual)
            expected_values = {normalize_for_match(item) for item in [expected, *MONTH_NAMES.get(expected, [])]}
            return actual_norm in expected_values
        return normalize_for_match(actual) == normalize_for_match(expected)

    def _month_from_text(self, value: str) -> str:
        normalized = normalize_for_match(value)
        if not normalized:
            return ""
        for number, aliases in MONTH_NAMES.items():
            if normalized in {normalize_for_match(item) for item in aliases}:
                return number
        return ""
