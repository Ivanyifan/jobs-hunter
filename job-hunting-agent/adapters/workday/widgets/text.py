from __future__ import annotations

from typing import Any

from ..contracts import ActionResult, FieldState, FieldStatus
from .base import BaseWorkdayWidget, WorkdayWidgetContext
from .utilities import (
    locator_tag,
    normalize_phone,
    safe_count,
    safe_input_value,
    scoped_locator,
)


class WorkdayTextInputWidget(BaseWorkdayWidget):
    MODE_EXACT = "exact"
    MODE_NORMALIZED_PHONE = "normalized_phone"
    MODE_OPTIONAL_EMPTY = "optional-empty"

    def __init__(self, mode: str = MODE_EXACT):
        self.mode = mode

    def locate(self, page: Any, context: Any | None = None) -> Any:
        ctx = WorkdayWidgetContext.from_any(context)
        locator = scoped_locator(page, ctx.selector, ctx.scope_index) if ctx.selector else None
        if locator is None:
            for selector in ctx.locator_hints:
                locator = scoped_locator(page, selector, ctx.scope_index)
                if locator is not None:
                    break
        if locator is None:
            locator = page.locator("input:not([type='hidden']), textarea").nth(ctx.scope_index)
        if locator_tag(locator) not in {"input", "textarea"}:
            child = locator.locator("input:not([type='hidden']), textarea")
            if safe_count(child):
                locator = child.first
        return locator

    def observe(self, page: Any, context: Any | None = None) -> FieldState:
        ctx = WorkdayWidgetContext.from_any(context)
        try:
            locator = self.locate(page, ctx)
        except Exception as exc:
            return FieldState(
                canonical_key=ctx.canonical_key,
                required=ctx.required,
                status=FieldStatus.MISSING if ctx.required else FieldStatus.OPTIONAL,
                expected_value=ctx.expected_value,
                source=self.__class__.__name__,
                metadata={"reason": str(exc), "mode": self.mode},
            )
        value = safe_input_value(locator)
        if value:
            status = FieldStatus.FILLED
        elif ctx.required:
            status = FieldStatus.MISSING
        else:
            status = FieldStatus.OPTIONAL
        return FieldState(
            canonical_key=ctx.canonical_key,
            required=ctx.required,
            status=status,
            visible_value=value,
            expected_value=ctx.expected_value,
            source=self.__class__.__name__,
            locator_hints=[ctx.selector, *ctx.locator_hints],
            validation_messages=[ctx.validation_message] if ctx.validation_message else [],
            metadata={"mode": self.mode, **self.snapshot_locator(locator, ctx)},
        )

    def _set_native_value(self, locator: Any, value: str) -> None:
        locator.evaluate(
            """(el, value) => {
                const prototype = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
                if (descriptor && descriptor.set) {
                    descriptor.set.call(el, value);
                } else {
                    el.value = value;
                }
                el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
            }""",
            str(value or ""),
        )
        try:
            locator.evaluate("el => el.blur()")
        except Exception:
            pass

    def act(self, page: Any, value: Any, context: Any | None = None) -> ActionResult:
        ctx = WorkdayWidgetContext.from_any(context)
        before = self.observe(page, ctx)
        try:
            locator = self.locate(page, ctx)
            self._set_native_value(locator, str(value or ""))
        except Exception as exc:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="fill_text",
                target=ctx.canonical_key,
                value=value,
                before=before,
                after=after,
                reason=f"text_input_failed:{exc}",
                retryable=True,
            )
        self.wait_until_stable(page, ctx, self.default_timeout_ms)
        after = self.observe(page, ctx)
        verified = self._value_matches(after.visible_value, value, ctx)
        reason = "verified" if verified else "committed_value_mismatch"
        return self.result(
            acted=True,
            verified=verified,
            action="fill_text",
            target=ctx.canonical_key,
            value=value,
            before=before,
            after=after,
            reason=reason,
            metadata={"mode": self.mode, "field_status": after.normalized_status()},
        )

    def _value_matches(self, actual: Any, expected: Any, context: WorkdayWidgetContext) -> bool:
        actual_text = str(actual or "")
        expected_text = str(expected or "")
        mode = self.mode
        if mode == self.MODE_NORMALIZED_PHONE:
            return normalize_phone(actual_text) == normalize_phone(expected_text)
        if mode == self.MODE_OPTIONAL_EMPTY:
            if expected_text == "" and actual_text == "":
                return True
            return actual_text == expected_text
        return actual_text == expected_text

    def verify(self, page: Any, expected_value: Any, context: Any | None = None) -> ActionResult:
        ctx = WorkdayWidgetContext.from_any(context)
        state = self.observe(page, ctx)
        verified = self._value_matches(state.visible_value, expected_value, ctx)
        return self.result(
            acted=False,
            verified=verified,
            action="verify_text",
            target=ctx.canonical_key,
            value=expected_value,
            after=state,
            reason="verified" if verified else "committed_value_mismatch",
            metadata={"mode": self.mode, "field_status": state.normalized_status()},
        )
