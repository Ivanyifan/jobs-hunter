from __future__ import annotations

from typing import Any


class BaseStageController:
    stage = "UNKNOWN"

    def observe(self, page: Any, context: dict[str, Any] | None = None):
        raise NotImplementedError

    def plan(self, snapshot: Any, context: dict[str, Any] | None = None) -> list[Any]:
        raise NotImplementedError

    def execute(self, page: Any, action: Any, context: dict[str, Any] | None = None):
        raise NotImplementedError

    def verify(self, page: Any, previous_snapshot: Any, context: dict[str, Any] | None = None):
        raise NotImplementedError

    def run_pass(self, page: Any, context: dict[str, Any] | None = None):
        context = context or {}
        previous_snapshot = self.observe(page, context)
        planned_actions = self.plan(previous_snapshot, context)
        action_results = [self.execute(page, action, context) for action in planned_actions]
        result = self.verify(page, previous_snapshot, context)
        if action_results and not getattr(result, "actions", None):
            result.actions = action_results

        from ..contracts import ALLOWED_OUTCOME_VALUES, validate_stage_result
        from ..state_signature import has_meaningful_progress

        validate_stage_result(result)
        snapshot = getattr(result, "snapshot", None)
        if snapshot is not None and has_meaningful_progress(previous_snapshot, snapshot):
            return result
        if result.normalized_outcome() in ALLOWED_OUTCOME_VALUES:
            return result
        raise ValueError("Workday controller pass returned without progress or typed outcome")

    def run_once(self, context: dict[str, Any]):
        """Compatibility hook for current shadow fixture controllers."""
        snapshot = self.observe(context)  # type: ignore[misc]
        actions = self.plan(snapshot)  # type: ignore[misc]
        executed = self.execute(context, actions)  # type: ignore[misc]
        result = self.verify(snapshot, executed)  # type: ignore[misc]

        from ..contracts import validate_stage_result

        validate_stage_result(result)
        return result


__all__ = ["BaseStageController"]
