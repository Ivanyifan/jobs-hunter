from __future__ import annotations

import time
from typing import Any

from ..contracts import ActionResult, FieldState, FieldStatus, OutcomeType
from .utilities import (
    context_get,
    is_loading_text,
    safe_attr,
    safe_count,
    safe_is_visible,
    safe_text,
)


class LoadingStateDetector:
    selectors = [
        '[aria-busy="true"]',
        '[role="progressbar"]',
        '[data-automation-id*="loading" i]',
        '[data-automation-id*="spinner" i]',
        '[data-testid*="loading" i]',
        '[data-testid*="spinner" i]',
        ".spinner",
        ".loading",
        ".skeleton",
        '[class*="spinner" i]',
        '[class*="skeleton" i]',
    ]

    def __init__(self, page_or_locator: Any | None = None):
        self.page_or_locator = page_or_locator

    def detect(self, page_or_locator: Any | None = None) -> FieldState:
        target = page_or_locator or self.page_or_locator
        indicators: list[str] = []
        if target is None:
            return FieldState(status=FieldStatus.UNKNOWN, source=self.__class__.__name__)

        metadata: dict[str, Any] = {}
        try:
            text = safe_text(target, include_input_value=False)
            if is_loading_text(text):
                indicators.append(text)
        except Exception:
            pass

        aria_busy = safe_attr(target, "aria-busy")
        if aria_busy.lower() == "true":
            indicators.append("aria-busy")

        try:
            disabled_present = bool(target.evaluate("el => el.disabled === true || el.hasAttribute('disabled')"))
        except Exception:
            disabled_present = bool(safe_attr(target, "disabled"))
        aria_disabled = safe_attr(target, "aria-disabled")
        if disabled_present or aria_disabled.lower() == "true":
            metadata["disabled"] = disabled_present
            metadata["aria_disabled"] = aria_disabled.lower() == "true"

        for selector in self.selectors:
            try:
                locators = target.locator(selector)
            except Exception:
                continue
            for index in range(min(safe_count(locators), 25)):
                locator = locators.nth(index)
                if not safe_is_visible(locator):
                    continue
                text = safe_text(locator, include_input_value=False) or selector
                indicators.append(text)

        unique = [item for item in dict.fromkeys(indicators) if item]
        if unique:
            return FieldState(
                status=FieldStatus.LOADING,
                visible_value=", ".join(unique[:5]),
                source=self.__class__.__name__,
                metadata={"loading_indicators": unique, **metadata},
            )
        return FieldState(status=FieldStatus.UNKNOWN, source=self.__class__.__name__, metadata=metadata)

    def wait_until_resolved(self, timeout_ms: int = 2000) -> ActionResult:
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        last_state = self.detect()
        while last_state.normalized_status() == FieldStatus.LOADING.value and time.monotonic() < deadline:
            time.sleep(0.08)
            last_state = self.detect()
        resolved = last_state.normalized_status() != FieldStatus.LOADING.value
        return ActionResult(
            acted=False,
            verified=resolved,
            action="wait_until_loading_resolved",
            target=str(context_get(last_state.metadata, "target", "")),
            reason="loading_resolved" if resolved else "loading",
            retryable=not resolved,
            metadata={"status": last_state.normalized_status(), **last_state.metadata},
            outcome_type=OutcomeType.COMPLETE if resolved else OutcomeType.RETRYABLE,
            postcondition_verified=resolved,
            error="" if resolved else "loading",
        )
