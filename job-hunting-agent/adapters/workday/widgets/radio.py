from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..contracts import ActionResult, FieldState, FieldStatus
from .base import BaseWorkdayWidget, WorkdayWidgetContext
from .utilities import (
    context_list,
    match_expected_option,
    nearest_field_scope,
    locator_tag,
    normalize_for_match,
    safe_attr,
    safe_count,
    safe_is_visible,
    safe_text,
    scoped_locator,
    wait_until,
    OptionCandidate,
)


@dataclass
class RadioOption:
    text: str
    locator: Any
    checked: bool = False
    state_locator: Any | None = None


class WorkdayRadioGroupWidget(BaseWorkdayWidget):
    def locate(self, page: Any, context: Any | None = None) -> Any:
        ctx = WorkdayWidgetContext.from_any(context)
        locator = scoped_locator(page, ctx.selector, ctx.scope_index) if ctx.selector else None
        if locator is None:
            for selector in ctx.locator_hints:
                locator = scoped_locator(page, selector, ctx.scope_index)
                if locator is not None:
                    break
        if locator is None:
            locator = page.locator("fieldset, [role='radiogroup']").nth(ctx.scope_index)
        role = safe_attr(locator, "role")
        tag = ""
        try:
            tag = locator.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            pass
        if tag == "input" or role == "radio":
            return nearest_field_scope(locator)
        return locator

    def read_options(self, page: Any, context: Any | None = None) -> list[str]:
        return [option.text for option in self._radio_options(page, context)]

    def read_selected_value(self, page: Any, context: Any | None = None) -> str:
        for option in self._radio_options(page, context):
            if option.checked:
                return option.text
        try:
            group = self.locate(page, context)
            selected = safe_attr(group, "data-selected-value") or safe_attr(group, "aria-valuetext")
            return selected
        except Exception:
            return ""

    def observe(self, page: Any, context: Any | None = None) -> FieldState:
        ctx = WorkdayWidgetContext.from_any(context)
        try:
            selected = self.read_selected_value(page, ctx)
            options = self.read_options(page, ctx)
            group = self.locate(page, ctx)
        except Exception as exc:
            return FieldState(
                canonical_key=ctx.canonical_key,
                required=ctx.required,
                status=FieldStatus.MISSING if ctx.required else FieldStatus.OPTIONAL,
                expected_value=ctx.expected_value,
                source=self.__class__.__name__,
                metadata={"reason": str(exc)},
            )
        status = FieldStatus.FILLED if selected else (FieldStatus.MISSING if ctx.required else FieldStatus.OPTIONAL)
        return FieldState(
            canonical_key=ctx.canonical_key,
            required=ctx.required,
            status=status,
            visible_value=selected,
            expected_value=ctx.expected_value,
            options=options,
            source=self.__class__.__name__,
            locator_hints=[ctx.selector, *ctx.locator_hints],
            metadata=self.snapshot_locator(group, ctx),
        )

    def select_value(
        self,
        page: Any,
        expected_value: Any,
        aliases: Iterable[Any] | None = None,
        context: Any | None = None,
    ) -> ActionResult:
        ctx = WorkdayWidgetContext.from_any(context)
        aliases = list(aliases if aliases is not None else ctx.aliases)
        before = self.observe(page, ctx)
        options = self._radio_options(page, ctx)
        candidates = [OptionCandidate(text=option.text, locator=option.locator) for option in options]
        match = match_expected_option(candidates, expected_value, aliases)
        if match.candidate is None:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="select_radio",
                target=ctx.canonical_key,
                value=expected_value,
                before=before,
                after=after,
                reason=match.reason or "option_not_found",
                metadata={"options": match.matches or [option.text for option in options]},
            )
        try:
            match.candidate.locator.click(timeout=1500)
        except Exception as exc:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="select_radio",
                target=ctx.canonical_key,
                value=expected_value,
                before=before,
                after=after,
                reason=f"radio_click_failed:{exc}",
                retryable=True,
            )
        wait_until(lambda: self._selected_matches(page, expected_value, aliases, ctx), timeout_ms=1200)
        after = self.observe(page, ctx)
        verified = self._selected_matches(page, expected_value, aliases, ctx)
        return self.result(
            acted=True,
            verified=verified,
            action="select_radio",
            target=ctx.canonical_key,
            value=expected_value,
            before=before,
            after=after,
            reason="verified" if verified else "checked_state_not_changed",
            retryable=not verified,
            metadata={"matched_option": match.candidate.text},
        )

    def verify_selected_value(
        self,
        page: Any,
        expected_value: Any,
        aliases: Iterable[Any] | None = None,
        context: Any | None = None,
    ) -> ActionResult:
        ctx = WorkdayWidgetContext.from_any(context)
        state = self.observe(page, ctx)
        verified = self._selected_matches(page, expected_value, aliases or ctx.aliases, ctx)
        return self.result(
            acted=False,
            verified=verified,
            action="verify_radio",
            target=ctx.canonical_key,
            value=expected_value,
            after=state,
            reason="verified" if verified else "selected_value_mismatch",
            retryable=not verified,
        )

    def act(self, page: Any, value: Any, context: Any | None = None) -> ActionResult:
        return self.select_value(page, value, context_list(context, "aliases"), context)

    def verify(self, page: Any, expected_value: Any, context: Any | None = None) -> ActionResult:
        return self.verify_selected_value(page, expected_value, context_list(context, "aliases"), context)

    def _selected_matches(
        self,
        page: Any,
        expected_value: Any,
        aliases: Iterable[Any] | None,
        context: Any | None,
    ) -> bool:
        selected = self.read_selected_value(page, context)
        selected_norm = normalize_for_match(selected)
        expected_norms = [normalize_for_match(expected_value), *[normalize_for_match(item) for item in aliases or []]]
        expected_norms = [item for item in expected_norms if item]
        return bool(selected_norm and selected_norm in expected_norms)

    def _radio_options(self, page: Any, context: Any | None = None) -> list[RadioOption]:
        group = self.locate(page, context)
        options: list[RadioOption] = []
        inputs = group.locator('input[type="radio"], [role="radio"]')
        for index in range(min(safe_count(inputs), 40)):
            state_locator = inputs.nth(index)
            click_locator = self._radio_click_target(group, state_locator)
            if click_locator is None:
                continue
            text = self._radio_label_text(state_locator)
            checked = self._is_checked(state_locator)
            if text:
                options.append(RadioOption(text=text, locator=click_locator, checked=checked, state_locator=state_locator))
        return options

    def _is_checked(self, locator: Any) -> bool:
        try:
            return bool(locator.is_checked(timeout=150))
        except Exception:
            return safe_attr(locator, "aria-checked").lower() == "true"

    def _radio_label_text(self, locator: Any) -> str:
        if locator_tag(locator) != "input":
            text = safe_text(locator, include_input_value=False)
            if text:
                return text
        try:
            text = locator.evaluate(
                """el => {
                    if (el.labels && el.labels.length) return el.labels[0].innerText;
                    const id = el.getAttribute("id");
                    if (id) {
                        const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                        if (label) return label.innerText;
                    }
                    const parentLabel = el.closest("label");
                    if (parentLabel) return parentLabel.innerText;
                    const wrapper = el.closest("[role='radio'], .radio, .radio-option");
                    return wrapper ? wrapper.innerText : "";
                }"""
            )
            text = safe_text_value(text)
            if text:
                return text
        except Exception:
            pass
        return safe_text(locator, include_input_value=False)

    def _radio_click_target(self, group: Any, locator: Any) -> Any | None:
        if safe_is_visible(locator):
            return locator
        if locator_tag(locator) != "input":
            return None
        id_value = safe_attr(locator, "id")
        if id_value:
            label = group.locator(f"xpath=.//label[@for={self._xpath_literal(id_value)}]").first
            if safe_count(label) and safe_is_visible(label):
                return label
        for selector in (
            "xpath=ancestor::label[1]",
            "xpath=ancestor::*[@role='radio' or contains(concat(' ', normalize-space(@class), ' '), ' radio ') or contains(concat(' ', normalize-space(@class), ' '), ' radio-option ')][1]",
            "xpath=..",
        ):
            candidate = locator.locator(selector).first
            if safe_count(candidate) and safe_is_visible(candidate):
                return candidate
        return None

    def _xpath_literal(self, value: str) -> str:
        if '"' not in value:
            return f'"{value}"'
        if "'" not in value:
            return f"'{value}'"
        return "concat(" + ', "\"", '.join(f'"{part}"' for part in value.split('"')) + ")"


def safe_text_value(value: Any) -> str:
    return " ".join(str(value or "").split())
