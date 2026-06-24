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
        if context is None:
            context = {}
        previous_snapshot = self.observe(page, context)
        planned_actions = self.plan(previous_snapshot, context)
        action_results = [self.execute(page, action, context) for action in planned_actions]
        result = self.verify(page, previous_snapshot, context)
        if action_results and not getattr(result, "actions", None):
            result.actions = action_results

        from ..contracts import ALLOWED_OUTCOME_VALUES, OutcomeType, validate_stage_result
        from ..state_signature import build_stage_signature, has_meaningful_progress, should_stop_for_unchanged_state

        validate_stage_result(result)
        snapshot = getattr(result, "snapshot", None) or previous_snapshot
        signature_history = context.setdefault("state_signature_history", [])
        if not isinstance(signature_history, list):
            raise ValueError("state_signature_history must be a list")
        signature_history.append(build_stage_signature(snapshot))

        result_snapshot = getattr(result, "snapshot", None)
        if result_snapshot is not None and has_meaningful_progress(previous_snapshot, result_snapshot):
            return result
        outcome = result.normalized_outcome()
        if outcome == OutcomeType.RETRYABLE.value and should_stop_for_unchanged_state(signature_history):
            raise ValueError("Workday controller returned RETRYABLE twice without meaningful progress")
        if outcome in ALLOWED_OUTCOME_VALUES:
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
