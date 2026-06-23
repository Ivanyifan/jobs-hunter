from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts import ActionResult, FieldState, FieldStatus, OutcomeType
from .utilities import (
    context_get,
    context_list,
    is_placeholder_text,
    nearest_field_scope,
    normalize_space,
    safe_locator_metadata,
    safe_text,
    scoped_locator,
)


@dataclass
class WorkdayWidgetContext:
    canonical_key: str = ""
    selector: str = ""
    scope_index: int = 0
    locator_hints: list[str] = field(default_factory=list)
    expected_value: Any = None
    aliases: list[str] = field(default_factory=list)
    required: bool = False
    group_text: str = ""
    validation_message: str = ""
    current_stage: str = ""

    @classmethod
    def from_any(cls, context: Any | None = None) -> "WorkdayWidgetContext":
        if isinstance(context, cls):
            return context
        return cls(
            canonical_key=str(context_get(context, "canonical_key", "") or ""),
            selector=str(context_get(context, "selector", "") or ""),
            scope_index=int(context_get(context, "scope_index", 0) or 0),
            locator_hints=[str(item) for item in context_list(context, "locator_hints")],
            expected_value=context_get(context, "expected_value"),
            aliases=[str(item) for item in context_list(context, "aliases")],
            required=bool(context_get(context, "required", False)),
            group_text=str(context_get(context, "group_text", "") or ""),
            validation_message=str(context_get(context, "validation_message", "") or ""),
            current_stage=str(context_get(context, "current_stage", "") or ""),
        )


class BaseWorkdayWidget:
    default_timeout_ms = 2000

    def locate(self, page: Any, context: Any | None = None) -> Any:
        ctx = WorkdayWidgetContext.from_any(context)
        selectors = [ctx.selector, *ctx.locator_hints]
        for selector in selectors:
            locator = scoped_locator(page, selector, ctx.scope_index)
            if locator is not None:
                return locator
        raise LookupError(f"widget locator not found for {ctx.canonical_key or '<unknown>'}")

    def observe(self, page: Any, context: Any | None = None) -> FieldState:
        ctx = WorkdayWidgetContext.from_any(context)
        try:
            locator = self.locate(page, ctx)
        except LookupError as exc:
            return FieldState(
                canonical_key=ctx.canonical_key,
                required=ctx.required,
                status=FieldStatus.MISSING if ctx.required else FieldStatus.OPTIONAL,
                expected_value=ctx.expected_value,
                source=self.__class__.__name__,
                locator_hints=[ctx.selector, *ctx.locator_hints],
                validation_messages=[ctx.validation_message] if ctx.validation_message else [],
                metadata={"reason": str(exc)},
            )
        value = safe_text(locator)
        status = FieldStatus.FILLED
        if is_placeholder_text(value):
            status = FieldStatus.MISSING if ctx.required else FieldStatus.OPTIONAL
        return FieldState(
            canonical_key=ctx.canonical_key,
            required=ctx.required,
            status=status,
            visible_value=normalize_space(value),
            expected_value=ctx.expected_value,
            source=self.__class__.__name__,
            locator_hints=[ctx.selector, *ctx.locator_hints],
            validation_messages=[ctx.validation_message] if ctx.validation_message else [],
            metadata=self.snapshot_locator(locator, ctx),
        )

    def act(self, page: Any, value: Any, context: Any | None = None) -> ActionResult:
        raise NotImplementedError

    def verify(self, page: Any, expected_value: Any, context: Any | None = None) -> ActionResult:
        raise NotImplementedError

    def wait_until_stable(self, page: Any, context: Any | None = None, timeout_ms: int | None = None) -> ActionResult:
        from .loading import LoadingStateDetector

        timeout = self.default_timeout_ms if timeout_ms is None else timeout_ms
        try:
            target = self.locate(page, context)
        except LookupError:
            target = page
        return LoadingStateDetector(target).wait_until_resolved(timeout)

    def snapshot_locator(self, locator: Any, context: Any | None = None) -> dict[str, Any]:
        return safe_locator_metadata(locator, context)

    def field_scope(self, page: Any, context: Any | None = None) -> Any:
        return nearest_field_scope(self.locate(page, context))

    def result(
        self,
        *,
        acted: bool = False,
        verified: bool = False,
        action: str = "",
        target: str = "",
        value: Any = None,
        before: Any = None,
        after: Any = None,
        reason: str = "",
        retryable: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ActionResult:
        outcome = OutcomeType.COMPLETE if verified else OutcomeType.RETRYABLE
        return ActionResult(
            acted=acted,
            verified=verified,
            action=action,
            target=target,
            value=value,
            before=before.to_dict() if hasattr(before, "to_dict") else before,
            after=after.to_dict() if hasattr(after, "to_dict") else after,
            reason=reason,
            retryable=retryable,
            metadata=metadata or {},
            changed=acted,
            outcome_type=outcome,
            postcondition_verified=verified,
            field=target,
            details=metadata or {},
            error="" if verified else reason,
        )
