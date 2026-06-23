from __future__ import annotations

from typing import Any

from .contracts import (
    ActionResult,
    FieldState,
    FieldStatus,
    OutcomeType,
    StageResult,
    StageSnapshot,
    validate_stage_result,
)
from .controllers.base import BaseStageController


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _field_state_from_unresolved(item: dict[str, Any], group: str = "") -> FieldState:
    name = str(item.get("field") or item.get("name") or item.get("canonical_key") or group or "required_field")
    status = item.get("status") or FieldStatus.BLOCKED
    if str(status) not in {status.value for status in FieldStatus}:
        status = FieldStatus.BLOCKED
    return FieldState(
        name=name,
        status=status,
        value=item.get("value"),
        required=True,
        group=str(item.get("group") or group or ""),
        last_attempt=str(item.get("last_attempt") or item.get("reason") or ""),
        evidence=str(item.get("validation_message") or item.get("raw_text") or item.get("label") or ""),
        metadata={key: value for key, value in item.items() if key not in {"field", "name", "value"}},
    )


def _fields_from_fixture(stage: str, payload: dict[str, Any]) -> list[FieldState]:
    fields: list[FieldState] = []
    state_snapshot = payload.get("state_snapshot") or {}
    if isinstance(state_snapshot, dict):
        for key, value in state_snapshot.items():
            if isinstance(value, dict):
                fields.append(
                    FieldState(
                        name=key,
                        status=value.get("status") or FieldStatus.MISSING,
                        value=value.get("value"),
                        source=value.get("source") or "",
                        required=bool(value.get("required")),
                        group=value.get("group") or "",
                        last_attempt=value.get("last_attempt") or "",
                        metadata={k: v for k, v in value.items() if k not in {"status", "value", "source", "required", "group", "last_attempt"}},
                    )
                )
    for item in _as_list(payload.get("unresolved_required_fields")):
        if isinstance(item, dict):
            fields.append(_field_state_from_unresolved(item))
    if stage == "my_experience":
        for group in _as_list(payload.get("unresolved_groups")):
            fields.append(
                FieldState(
                    name=str(group),
                    status=FieldStatus.BLOCKED,
                    required=True,
                    group=str(group),
                    evidence="fixture unresolved group",
                )
            )
    return fields


class MyInformationController(BaseStageController):
    stage = "my_information"

    def observe(self, context: dict[str, Any]) -> StageSnapshot:
        legacy_result = context.get("legacy_result") or context.get("fixture") or {}
        unresolved = _as_list(legacy_result.get("unresolved_required_fields"))
        return StageSnapshot(
            stage=self.stage,
            url=str(context.get("url") or legacy_result.get("current_url") or legacy_result.get("url") or ""),
            fields=_fields_from_fixture(self.stage, legacy_result),
            required_fields=[str(item.get("field") or item.get("name") or item) for item in unresolved],
            unresolved_required_fields=[item for item in unresolved if isinstance(item, dict)],
            validation_errors=[str(item) for item in _as_list(legacy_result.get("validation_errors"))],
            alerts=[str(item) for item in _as_list(legacy_result.get("alerts"))],
            metadata={
                "legacy_outcome_type": legacy_result.get("outcome_type"),
                "field_groups": legacy_result.get("field_groups") or {},
            },
        )

    def plan(self, snapshot: StageSnapshot) -> list[ActionResult]:
        if snapshot.unresolved_required_fields:
            return [
                ActionResult(
                    action="return_structured_my_information_blocker",
                    outcome_type=OutcomeType.MY_INFORMATION_BLOCKED,
                    postcondition_verified=True,
                )
            ]
        return [ActionResult(action="complete_my_information", outcome_type=OutcomeType.COMPLETE, postcondition_verified=True)]

    def execute(self, context: dict[str, Any], actions: list[ActionResult]) -> list[ActionResult]:
        return actions

    def verify(self, snapshot: StageSnapshot, actions: list[ActionResult]) -> StageResult:
        if snapshot.unresolved_required_fields:
            result = StageResult(
                stage=self.stage,
                outcome_type=OutcomeType.MY_INFORMATION_BLOCKED,
                status="BLOCKED_ON_QUESTIONS",
                snapshot=snapshot,
                fields=snapshot.fields,
                unresolved_required_fields=snapshot.unresolved_required_fields,
                validation_errors=snapshot.validation_errors,
                alerts=snapshot.alerts,
                actions=actions,
                message="my_information_required_fields_unresolved",
            )
        else:
            result = StageResult(
                stage=self.stage,
                outcome_type=OutcomeType.COMPLETE,
                status="COMPLETE",
                snapshot=snapshot,
                fields=snapshot.fields,
                alerts=snapshot.alerts,
                actions=actions,
            )
        validate_stage_result(result)
        return result


class MyExperienceController(BaseStageController):
    stage = "my_experience"

    def observe(self, context: dict[str, Any]) -> StageSnapshot:
        fixture = context.get("fixture") or {}
        unresolved = []
        for group in _as_list(fixture.get("unresolved_groups")):
            unresolved.append({"field": str(group), "group": str(group), "reason": "fixture_unresolved_group"})
        unresolved.extend(item for item in _as_list(fixture.get("unresolved_required_fields")) if isinstance(item, dict))
        return StageSnapshot(
            stage=self.stage,
            url=str(fixture.get("url") or context.get("url") or ""),
            fields=_fields_from_fixture(self.stage, fixture),
            required_fields=[str(item.get("field") or item.get("group")) for item in unresolved],
            unresolved_required_fields=unresolved,
            validation_errors=[str(item) for item in _as_list(fixture.get("validation_errors"))],
            metadata={"legacy_outcome_type": fixture.get("outcome_type")},
        )

    def plan(self, snapshot: StageSnapshot) -> list[ActionResult]:
        if snapshot.unresolved_required_fields:
            return [
                ActionResult(
                    action="return_structured_my_experience_blocker",
                    outcome_type=OutcomeType.MY_EXPERIENCE_BLOCKED,
                    postcondition_verified=True,
                )
            ]
        return [ActionResult(action="complete_my_experience", outcome_type=OutcomeType.COMPLETE, postcondition_verified=True)]

    def execute(self, context: dict[str, Any], actions: list[ActionResult]) -> list[ActionResult]:
        return actions

    def verify(self, snapshot: StageSnapshot, actions: list[ActionResult]) -> StageResult:
        if snapshot.unresolved_required_fields:
            result = StageResult(
                stage=self.stage,
                outcome_type=OutcomeType.MY_EXPERIENCE_BLOCKED,
                status="BLOCKED_ON_QUESTIONS",
                snapshot=snapshot,
                fields=snapshot.fields,
                unresolved_required_fields=snapshot.unresolved_required_fields,
                validation_errors=snapshot.validation_errors,
                actions=actions,
                message="my_experience_required_groups_unresolved",
            )
        else:
            result = StageResult(
                stage=self.stage,
                outcome_type=OutcomeType.COMPLETE,
                status="COMPLETE",
                snapshot=snapshot,
                fields=snapshot.fields,
                actions=actions,
            )
        validate_stage_result(result)
        return result


class NavigationController(BaseStageController):
    stage = "navigation"

    def observe(self, context: dict[str, Any]) -> StageSnapshot:
        fixture = context.get("fixture") or {}
        unresolved = []
        if not fixture.get("manual_apply_available") and not fixture.get("recovered"):
            unresolved.append(
                {
                    "field": "application_start_action",
                    "reason": "no_safe_apply_action",
                    "current_url": fixture.get("current_url"),
                    "original_job_url": fixture.get("original_job_url"),
                }
            )
        return StageSnapshot(
            stage=self.stage,
            url=str(fixture.get("current_url") or context.get("url") or ""),
            fields=_fields_from_fixture(self.stage, fixture),
            required_fields=["application_start_action"] if unresolved else [],
            unresolved_required_fields=unresolved,
            metadata={
                "legacy_outcome_type": fixture.get("outcome_type"),
                "original_job_url": fixture.get("original_job_url"),
                "available_actions": fixture.get("available_actions") or [],
            },
        )

    def plan(self, snapshot: StageSnapshot) -> list[ActionResult]:
        if snapshot.unresolved_required_fields:
            return [
                ActionResult(
                    action="return_navigation_blocker",
                    outcome_type=OutcomeType.NAVIGATION_BLOCKED,
                    postcondition_verified=True,
                )
            ]
        return [
            ActionResult(
                action="recover_or_continue_apply_manually",
                changed=True,
                outcome_type=OutcomeType.COMPLETE,
                postcondition_verified=True,
            )
        ]

    def execute(self, context: dict[str, Any], actions: list[ActionResult]) -> list[ActionResult]:
        return actions

    def verify(self, snapshot: StageSnapshot, actions: list[ActionResult]) -> StageResult:
        if snapshot.unresolved_required_fields:
            result = StageResult(
                stage=self.stage,
                outcome_type=OutcomeType.NAVIGATION_BLOCKED,
                status="BLOCKED",
                snapshot=snapshot,
                fields=snapshot.fields,
                unresolved_required_fields=snapshot.unresolved_required_fields,
                actions=actions,
                message="navigation_no_safe_apply_action",
            )
        else:
            result = StageResult(
                stage=self.stage,
                outcome_type=OutcomeType.COMPLETE,
                status="COMPLETE",
                snapshot=snapshot,
                fields=snapshot.fields,
                actions=actions,
            )
        validate_stage_result(result)
        return result


def replay_fixture(fixture: dict[str, Any]) -> StageResult:
    stage = str(fixture.get("stage") or "").lower()
    if stage == "my_information":
        return MyInformationController().run_once({"fixture": fixture})
    if stage == "my_experience":
        return MyExperienceController().run_once({"fixture": fixture})
    if stage == "navigation":
        return NavigationController().run_once({"fixture": fixture})
    if stage == "application_questions":
        unresolved = [item for item in _as_list(fixture.get("unresolved_required_fields")) if isinstance(item, dict)]
        result = StageResult(
            stage=stage,
            outcome_type=OutcomeType.BLOCKED_ON_QUESTIONS,
            status="BLOCKED_ON_QUESTIONS",
            unresolved_required_fields=unresolved,
            blockers=[{"stage": stage, "canonical_key": item.get("canonical_key")} for item in unresolved],
            message="application_questions_require_review",
            metadata={"legacy_outcome_type": fixture.get("outcome_type")},
        )
        validate_stage_result(result)
        return result
    if stage == "auth":
        result = StageResult(
            stage=stage,
            outcome_type=OutcomeType.AUTH_BLOCKED,
            status="NEEDS_TECHNICAL_REVIEW",
            message=str(fixture.get("blocked_reason") or "auth_blocked"),
            metadata={
                "tenant": fixture.get("tenant"),
                "host": fixture.get("host"),
                "auth_strategy": fixture.get("auth_strategy"),
                "password_source": fixture.get("password_source"),
            },
        )
        validate_stage_result(result)
        return result
    raise ValueError(f"unsupported Workday replay fixture stage: {stage}")
