from __future__ import annotations

from typing import Any, Iterable

from ..contracts import ActionResult, FieldState, FieldStatus
from .base import BaseWorkdayWidget, WorkdayWidgetContext
from .loading import LoadingStateDetector
from .utilities import (
    collect_visible_option_candidates,
    context_list,
    extract_committed_tokens,
    extract_validation_messages,
    first_visible_locator,
    is_placeholder_text,
    locator_tag,
    match_expected_option,
    native_select_options,
    normalize_for_match,
    normalize_space,
    OptionCandidate,
    safe_attr,
    safe_count,
    safe_is_visible,
    safe_input_value,
    safe_text,
    scoped_locator,
)


class WorkdayPromptWidget(BaseWorkdayWidget):
    prompt_selectors = [
        'button[aria-haspopup="listbox"]',
        '[role="combobox"]',
        'input[aria-autocomplete="list"]',
        '[data-automation-id*="prompt" i]',
        '[data-automation-id*="select" i]',
        "select",
    ]

    def locate(self, page: Any, context: Any | None = None) -> Any:
        ctx = WorkdayWidgetContext.from_any(context)
        locator = scoped_locator(page, ctx.selector, ctx.scope_index) if ctx.selector else None
        if locator is None:
            for selector in ctx.locator_hints:
                locator = scoped_locator(page, selector, ctx.scope_index)
                if locator is not None:
                    break
        if locator is None:
            for selector in self.prompt_selectors:
                locator = scoped_locator(page, selector, ctx.scope_index)
                if locator is not None:
                    break
        if locator is None:
            raise LookupError(f"prompt locator not found for {ctx.canonical_key or '<unknown>'}")
        tag = locator_tag(locator)
        if tag not in {"button", "input", "select"} and safe_attr(locator, "role") != "combobox":
            child = first_visible_locator(locator, self.prompt_selectors)
            if child is not None:
                return child
        return locator

    def open(self, page: Any, context: Any | None = None) -> ActionResult:
        ctx = WorkdayWidgetContext.from_any(context)
        before = self.observe(page, ctx)
        try:
            locator = self.locate(page, ctx)
            if locator_tag(locator) != "select":
                locator.click(timeout=1500)
        except Exception as exc:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="open_prompt",
                target=ctx.canonical_key,
                before=before,
                after=after,
                reason=f"open_failed:{exc}",
                retryable=True,
            )
        self.wait_until_stable(page, ctx, self.default_timeout_ms)
        options = self.read_visible_options(page, ctx)
        after = self.observe(page, ctx)
        if self.is_loading(page, ctx):
            return self._loading_result("open_prompt", ctx, before, after)
        return self.result(
            acted=True,
            verified=bool(options) or locator_tag(self.locate(page, ctx)) == "select",
            action="open_prompt",
            target=ctx.canonical_key,
            before=before,
            after=after,
            reason="opened" if options else "no_options_visible",
            retryable=not bool(options),
            metadata={"options": options},
        )

    def read_visible_options(self, page: Any, context: Any | None = None) -> list[str]:
        ctx = WorkdayWidgetContext.from_any(context)
        try:
            locator = self.locate(page, ctx)
            if locator_tag(locator) == "select":
                return [candidate.text for candidate in native_select_options(locator)]
        except Exception:
            pass
        return [candidate.text for candidate in self._visible_option_candidates(page, ctx)]

    def read_committed_values(self, page: Any, context: Any | None = None) -> list[str]:
        ctx = WorkdayWidgetContext.from_any(context)
        try:
            locator = self.locate(page, ctx)
            scope = self.field_scope(page, ctx)
        except Exception:
            return []
        values = extract_committed_tokens(scope, locator)
        tag = locator_tag(locator)
        role = safe_attr(locator, "role")
        if tag == "select":
            return values
        if tag == "button":
            text = safe_text(locator, include_input_value=False)
            if text and not is_placeholder_text(text):
                values.append(text)
        elif role == "combobox" and tag != "input":
            text = safe_text(locator, include_input_value=False)
            if text and not is_placeholder_text(text):
                values.append(text)
        return list(dict.fromkeys(normalize_space(item) for item in values if normalize_space(item)))

    def is_placeholder(self, page: Any, context: Any | None = None) -> bool:
        try:
            return is_placeholder_text(safe_text(self.locate(page, context)))
        except Exception:
            return True

    def is_loading(self, page: Any, context: Any | None = None) -> bool:
        ctx = WorkdayWidgetContext.from_any(context)
        return self._loading_state(page, ctx).normalized_status() == FieldStatus.LOADING.value

    def observe(self, page: Any, context: Any | None = None) -> FieldState:
        ctx = WorkdayWidgetContext.from_any(context)
        loading_state = self._loading_state(page, ctx)
        if loading_state.normalized_status() == FieldStatus.LOADING.value:
            return FieldState(
                canonical_key=ctx.canonical_key,
                required=ctx.required,
                status=FieldStatus.LOADING,
                visible_value=loading_state.visible_value,
                expected_value=ctx.expected_value,
                source=self.__class__.__name__,
                locator_hints=[ctx.selector, *ctx.locator_hints],
                metadata=loading_state.metadata,
            )
        try:
            locator = self.locate(page, ctx)
            scope = self.field_scope(page, ctx)
            visible_value = safe_text(locator)
            committed = self.read_committed_values(page, ctx)
            options = self.read_visible_options(page, ctx)
            validation = extract_validation_messages(scope)
        except Exception as exc:
            return FieldState(
                canonical_key=ctx.canonical_key,
                required=ctx.required,
                status=FieldStatus.MISSING if ctx.required else FieldStatus.OPTIONAL,
                expected_value=ctx.expected_value,
                source=self.__class__.__name__,
                locator_hints=[ctx.selector, *ctx.locator_hints],
                metadata={"reason": str(exc)},
            )
        status = FieldStatus.FILLED if committed else FieldStatus.OPTIONAL
        if not committed and ctx.required:
            status = FieldStatus.MISSING
        if not committed and is_placeholder_text(visible_value) and ctx.required:
            status = FieldStatus.MISSING
        return FieldState(
            canonical_key=ctx.canonical_key,
            required=ctx.required,
            status=status,
            visible_value=committed[0] if len(committed) == 1 else (committed or visible_value),
            expected_value=ctx.expected_value,
            options=options,
            source=self.__class__.__name__,
            locator_hints=[ctx.selector, *ctx.locator_hints],
            validation_messages=validation,
            metadata={
                "committed_values": committed,
                "placeholder": is_placeholder_text(visible_value),
                **self.snapshot_locator(locator, ctx),
            },
        )

    def select_exact_or_alias(
        self,
        page: Any,
        expected_value: Any,
        aliases: Iterable[Any] | None = None,
        context: Any | None = None,
    ) -> ActionResult:
        ctx = WorkdayWidgetContext.from_any(context)
        aliases = list(aliases if aliases is not None else ctx.aliases)
        before = self.observe(page, ctx)
        if not normalize_for_match(expected_value):
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="select_prompt_option",
                target=ctx.canonical_key,
                value=expected_value,
                before=before,
                after=after,
                reason="missing_expected_value",
            )
        already = self._committed_value_matches(page, expected_value, aliases, ctx)
        if already:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=True,
                action="select_prompt_option",
                target=ctx.canonical_key,
                value=expected_value,
                before=before,
                after=after,
                reason="already_committed",
            )

        try:
            locator = self.locate(page, ctx)
        except Exception as exc:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="select_prompt_option",
                target=ctx.canonical_key,
                value=expected_value,
                before=before,
                after=after,
                reason=f"prompt_not_found:{exc}",
                retryable=True,
            )

        if locator_tag(locator) == "select":
            return self._select_native_option(page, locator, expected_value, aliases, ctx, before)

        self.open(page, ctx)
        if self.is_loading(page, ctx):
            after = self.observe(page, ctx)
            return self._loading_result("select_prompt_option", ctx, before, after, expected_value)

        candidates = self._visible_option_candidates(page, ctx, expected_value, aliases)
        if not candidates and locator_tag(locator) == "input":
            self._set_search_text(locator, str(expected_value))
            self.wait_until_stable(page, ctx, self.default_timeout_ms)
            candidates = self._visible_option_candidates(page, ctx, expected_value, aliases)

        if not candidates and self.is_loading(page, ctx):
            after = self.observe(page, ctx)
            return self._loading_result("select_prompt_option", ctx, before, after, expected_value)

        match = match_expected_option(candidates, expected_value, aliases)
        if match.candidate is None:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="select_prompt_option",
                target=ctx.canonical_key,
                value=expected_value,
                before=before,
                after=after,
                reason=match.reason or "option_not_found",
                retryable=not match.ambiguous,
                metadata={"options": match.matches or [candidate.text for candidate in candidates]},
            )

        try:
            match.candidate.locator.click(timeout=1500)
        except Exception as exc:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="select_prompt_option",
                target=ctx.canonical_key,
                value=expected_value,
                before=before,
                after=after,
                reason=f"option_click_failed:{exc}",
                retryable=True,
                metadata={"matched_option": match.candidate.text},
            )

        self.wait_until_stable(page, ctx, self.default_timeout_ms)
        after = self.observe(page, ctx)
        verified = self._committed_value_matches(page, expected_value, aliases, ctx)
        reason = "verified" if verified else "committed_value_not_changed"
        return self.result(
            acted=True,
            verified=verified,
            action="select_prompt_option",
            target=ctx.canonical_key,
            value=expected_value,
            before=before,
            after=after,
            reason=reason,
            retryable=not verified,
            metadata={"matched_option": match.candidate.text, "match_reason": match.reason},
        )

    def _select_native_option(
        self,
        page: Any,
        locator: Any,
        expected_value: Any,
        aliases: Iterable[Any],
        ctx: WorkdayWidgetContext,
        before: FieldState,
    ) -> ActionResult:
        candidates = native_select_options(locator)
        match = match_expected_option(candidates, expected_value, aliases)
        if match.candidate is None:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="select_prompt_option",
                target=ctx.canonical_key,
                value=expected_value,
                before=before,
                after=after,
                reason=match.reason or "option_not_found",
                metadata={"options": match.matches or [candidate.text for candidate in candidates]},
            )
        try:
            locator.select_option(value=match.candidate.value, timeout=1500)
        except Exception:
            locator.select_option(label=match.candidate.text, timeout=1500)
        self.wait_until_stable(page, ctx, self.default_timeout_ms)
        after = self.observe(page, ctx)
        verified = self._committed_value_matches(page, expected_value, aliases, ctx)
        return self.result(
            acted=True,
            verified=verified,
            action="select_prompt_option",
            target=ctx.canonical_key,
            value=expected_value,
            before=before,
            after=after,
            reason="verified" if verified else "committed_value_not_changed",
            retryable=not verified,
            metadata={"matched_option": match.candidate.text, "match_reason": match.reason},
        )

    def _set_search_text(self, locator: Any, value: str) -> None:
        locator.evaluate(
            """(el, value) => {
                const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
                if (descriptor && descriptor.set) {
                    descriptor.set.call(el, value);
                } else {
                    el.value = value;
                }
                el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
            }""",
            value,
        )

    def _visible_option_candidates(
        self,
        page: Any,
        context: WorkdayWidgetContext,
        expected_value: Any | None = None,
        aliases: Iterable[Any] | None = None,
    ) -> list[OptionCandidate]:
        try:
            locator = self.locate(page, context)
            if locator_tag(locator) == "select":
                return native_select_options(locator)
        except Exception:
            locator = None

        if locator is not None:
            controlled_candidates = self._controlled_option_candidates(page, locator)
            if controlled_candidates:
                return controlled_candidates

        try:
            scope = self.field_scope(page, context)
            if locator_tag(scope) not in {"body", "html"}:
                scoped_candidates = collect_visible_option_candidates(scope)
                if scoped_candidates:
                    return scoped_candidates
        except Exception:
            pass

        page_candidates = collect_visible_option_candidates(page)
        if not page_candidates:
            return []
        if expected_value is None:
            return page_candidates if len(page_candidates) == 1 else []
        match = match_expected_option(page_candidates, expected_value, aliases or context.aliases)
        if match.candidate is not None:
            return [match.candidate]
        return page_candidates

    def _controlled_option_candidates(self, page: Any, locator: Any) -> list[OptionCandidate]:
        candidates: list[OptionCandidate] = []
        controlled_ids = [item for item in safe_attr(locator, "aria-controls").split() if item]
        for controlled_id in controlled_ids:
            popup = page.locator(f"xpath=//*[@id={self._xpath_literal(controlled_id)}]").first
            if safe_count(popup) and safe_is_visible(popup):
                candidates.extend(collect_visible_option_candidates(popup))
        return candidates

    def _committed_value_matches(
        self,
        page: Any,
        expected_value: Any,
        aliases: Iterable[Any] | None,
        context: Any | None = None,
    ) -> bool:
        values = self.read_committed_values(page, context)
        if not values:
            return False
        expected_norms = [normalize_for_match(expected_value), *[normalize_for_match(item) for item in aliases or []]]
        expected_norms = [item for item in expected_norms if item]
        for value in values:
            value_norm = normalize_for_match(value)
            if value_norm in expected_norms:
                return True
        return False

    def verify_committed_value(
        self,
        page: Any,
        expected_value: Any,
        aliases: Iterable[Any] | None = None,
        context: Any | None = None,
    ) -> ActionResult:
        ctx = WorkdayWidgetContext.from_any(context)
        before = self.observe(page, ctx)
        if before.normalized_status() == FieldStatus.LOADING.value:
            return self._loading_result("verify_prompt_committed_value", ctx, before, before, expected_value)
        verified = self._committed_value_matches(page, expected_value, aliases or ctx.aliases, ctx)
        reason = "verified" if verified else "committed_value_mismatch"
        if self.is_placeholder(page, ctx):
            reason = "placeholder_not_committed"
        values = self.read_committed_values(page, ctx)
        return self.result(
            acted=False,
            verified=verified,
            action="verify_prompt_committed_value",
            target=ctx.canonical_key,
            value=expected_value,
            before=before,
            after=before,
            reason=reason,
            retryable=not verified,
            metadata={"committed_values": values},
        )

    def verify(self, page: Any, expected_value: Any, context: Any | None = None) -> ActionResult:
        return self.verify_committed_value(page, expected_value, context_list(context, "aliases"), context)

    def act(self, page: Any, value: Any, context: Any | None = None) -> ActionResult:
        return self.select_exact_or_alias(page, value, context_list(context, "aliases"), context)

    def _loading_result(
        self,
        action: str,
        context: WorkdayWidgetContext,
        before: FieldState,
        after: FieldState,
        value: Any = None,
    ) -> ActionResult:
        return self.result(
            acted=False,
            verified=False,
            action=action,
            target=context.canonical_key,
            value=value,
            before=before,
            after=after,
            reason="loading",
            retryable=True,
            metadata={"status": FieldStatus.LOADING.value},
        )

    def _loading_state(self, page: Any, context: WorkdayWidgetContext) -> FieldState:
        for target in self._loading_targets(page, context):
            state = LoadingStateDetector(target).detect()
            if state.normalized_status() == FieldStatus.LOADING.value:
                return state
        return FieldState(status=FieldStatus.UNKNOWN, source=LoadingStateDetector.__name__)

    def _loading_targets(self, page: Any, context: WorkdayWidgetContext) -> list[Any]:
        targets: list[Any] = []
        try:
            locator = self.locate(page, context)
            targets.append(locator)
            scope = self.field_scope(page, context)
            if locator_tag(scope) not in {"body", "html"}:
                targets.append(scope)
            targets.extend(self._active_popups(page, locator, scope))
        except Exception:
            pass
        return targets

    def _active_popups(self, page: Any, locator: Any, scope: Any | None = None) -> list[Any]:
        popups: list[Any] = []
        controlled_ids = [item for item in safe_attr(locator, "aria-controls").split() if item]
        for controlled_id in controlled_ids:
            popup = page.locator(f"xpath=//*[@id={self._xpath_literal(controlled_id)}]").first
            if safe_count(popup) and safe_is_visible(popup):
                popups.append(popup)
        if scope is None or locator_tag(scope) in {"body", "html"}:
            return popups
        popup_selectors = [
            '[role="listbox"]:not([hidden])',
            '[role="menu"]:not([hidden])',
            '[role="dialog"]:not([hidden])',
            '[data-automation-id*="promptOption" i]',
            '[data-testid*="prompt-option" i]',
            ".wd-popup",
            ".wd-option-list",
        ]
        for selector in popup_selectors:
            locators = scope.locator(selector)
            for index in range(min(safe_count(locators), 10)):
                popup = locators.nth(index)
                if safe_is_visible(popup):
                    popups.append(popup)
        return popups

    def _xpath_literal(self, value: str) -> str:
        if '"' not in value:
            return f'"{value}"'
        if "'" not in value:
            return f"'{value}'"
        return "concat(" + ', "\"", '.join(f'"{part}"' for part in value.split('"')) + ")"
