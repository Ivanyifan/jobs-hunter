from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.workday.contracts import (
    ActionResult,
    FieldState,
    FieldStatus,
    GroupState,
    GroupStatus,
    OutcomeType,
    StageResult,
    StageSnapshot,
    WorkdayStage,
    validate_stage_result,
)
from adapters.workday.state_signature import (
    build_stage_signature,
    has_meaningful_progress,
    should_stop_for_unchanged_state,
)
from adapters.workday.controllers.base import BaseStageController


def sample_snapshot(
    *,
    start_date: str = "12/15/2026",
    phone_status: FieldStatus = FieldStatus.MISSING,
    phone_value: str = "Select One",
    unresolved: list[str] | None = None,
    loading: list[str] | None = None,
    validation_errors: list[str] | None = None,
    screenshot_path: str = "first.png",
    metadata: dict | None = None,
) -> StageSnapshot:
    unresolved = ["phone_device_type"] if unresolved is None else unresolved
    phone = FieldState(
        canonical_key="phone_device_type",
        label="Phone Device Type",
        required=True,
        status=phone_status,
        visible_value=phone_value,
        validation_messages=validation_errors or [],
    )
    start = FieldState(
        canonical_key="start_date",
        label="When are you available to start?",
        required=True,
        status=FieldStatus.FILLED,
        visible_value=start_date,
    )
    group = GroupState(
        canonical_key="phone_group",
        required=True,
        status=GroupStatus.INCOMPLETE if unresolved else GroupStatus.COMPLETE,
        fields=[phone],
        unresolved_fields=unresolved,
    )
    return StageSnapshot(
        stage=WorkdayStage.MY_INFORMATION,
        url="https://example.wd1.myworkdayjobs.com/en-US/example/apply/myInformation?session=volatile",
        page_heading="My Information",
        groups=[group],
        fields=[start],
        required_fields=["phone_device_type", "start_date"],
        unresolved_required_fields=[{"canonical_key": item} for item in unresolved],
        validation_errors=validation_errors or ["Phone Device Type is required and must have a value."],
        loading_indicators=loading or [],
        screenshot_path=screenshot_path,
        metadata=metadata or {},
    )


class WorkdayContractTests(unittest.TestCase):
    def test_contract_objects_serialize_to_json(self):
        snapshot = sample_snapshot()
        action = ActionResult(
            acted=True,
            verified=True,
            action="return_blocker",
            target="phone_device_type",
            reason="missing_required_field",
        )
        result = StageResult(
            outcome_type=OutcomeType.MY_INFORMATION_BLOCKED,
            stage=WorkdayStage.MY_INFORMATION,
            snapshot=snapshot,
            actions=[action],
            blocked_reason="phone_device_type_unresolved",
        )

        for item in [snapshot.groups[0].fields[0], snapshot.groups[0], snapshot, action, result]:
            json.dumps(item.to_dict(), sort_keys=True)

    def test_controller_base_exposes_required_protocol_methods(self):
        for method_name in ["observe", "plan", "execute", "verify", "run_pass"]:
            with self.subTest(method_name=method_name):
                self.assertTrue(callable(getattr(BaseStageController, method_name)))

    def test_controllers_package_preserves_legacy_public_imports(self):
        import adapters.workday.controllers as controllers

        public_names = [
            "ActionResult",
            "BaseStageController",
            "FieldState",
            "FieldStatus",
            "MyExperienceController",
            "MyInformationController",
            "NavigationController",
            "OutcomeType",
            "StageResult",
            "StageSnapshot",
            "replay_fixture",
            "validate_stage_result",
        ]

        self.assertTrue(controllers.__file__.endswith("__init__.py"))
        for public_name in public_names:
            with self.subTest(public_name=public_name):
                self.assertTrue(hasattr(controllers, public_name))

    def test_serialized_contracts_drop_secret_metadata(self):
        result = StageResult(
            outcome_type=OutcomeType.AUTH_BLOCKED,
            stage=WorkdayStage.AUTH,
            metadata={
                "password": "TenantSecret123!",
                "token": "abc123",
                "safe_reason": "auth_blocked",
            },
        )

        serialized = json.dumps(result.to_dict(), sort_keys=True).lower()

        self.assertIn("safe_reason", serialized)
        self.assertNotIn("tenantsecret123", serialized)
        self.assertNotIn("password", serialized)
        self.assertNotIn("token", serialized)

    def test_stage_signature_is_stable_for_equivalent_snapshots(self):
        first = sample_snapshot()
        second = sample_snapshot()

        self.assertEqual(build_stage_signature(first), build_stage_signature(second))

    def test_timestamp_query_and_screenshot_do_not_change_signature(self):
        first = sample_snapshot(screenshot_path="before.png", metadata={"timestamp": "2026-06-24T00:00:00Z"})
        second = sample_snapshot(screenshot_path="after.png", metadata={"timestamp": "2026-06-25T00:00:00Z"})
        second.url = "https://example.wd1.myworkdayjobs.com/en-US/example/apply/myInformation?session=other"

        self.assertEqual(build_stage_signature(first), build_stage_signature(second))

    def test_required_value_change_changes_signature(self):
        first = sample_snapshot(start_date="12/15/2026")
        second = sample_snapshot(start_date="12/16/2026")

        self.assertNotEqual(build_stage_signature(first), build_stage_signature(second))

    def test_meaningful_progress_ignores_last_field_only_change(self):
        first = sample_snapshot(metadata={"last_field": "phone_device_type"})
        second = sample_snapshot(metadata={"last_field": "start_date"})
        second.groups[0].fields[0].last_action = "focus_only"

        self.assertFalse(has_meaningful_progress(first, second))

    def test_meaningful_progress_detects_required_field_filled(self):
        first = sample_snapshot()
        second = sample_snapshot(
            phone_status=FieldStatus.FILLED,
            phone_value="Business Mobile",
            unresolved=[],
            validation_errors=[],
        )

        self.assertTrue(has_meaningful_progress(first, second))

    def test_ready_to_submit_without_confirm_is_valid_when_satisfied(self):
        snapshot = sample_snapshot(phone_status=FieldStatus.FILLED, phone_value="Business Mobile", unresolved=[], validation_errors=[])
        result = StageResult(
            outcome_type=OutcomeType.READY_TO_SUBMIT,
            stage=WorkdayStage.READY_TO_SUBMIT,
            snapshot=snapshot,
            complete=False,
        )

        validate_stage_result(result, confirm_submit=False)
        self.assertFalse(result.complete)

    def test_submitted_without_confirm_is_invalid(self):
        snapshot = sample_snapshot(phone_status=FieldStatus.FILLED, phone_value="Business Mobile", unresolved=[], validation_errors=[])
        result = StageResult(
            outcome_type=OutcomeType.SUBMITTED,
            stage=WorkdayStage.SUBMITTED,
            snapshot=snapshot,
            complete=True,
        )

        with self.assertRaisesRegex(ValueError, "confirm_submit=true"):
            validate_stage_result(result, confirm_submit=False)
        validate_stage_result(result, confirm_submit=True)

    def test_complete_with_required_blocked_field_is_invalid(self):
        snapshot = sample_snapshot(phone_status=FieldStatus.BLOCKED, phone_value="Select One", unresolved=[], validation_errors=[])
        snapshot.groups[0].status = GroupStatus.COMPLETE
        result = StageResult(outcome_type=OutcomeType.COMPLETE, stage=WorkdayStage.MY_INFORMATION, snapshot=snapshot)

        with self.assertRaisesRegex(ValueError, "required field is not satisfied"):
            validate_stage_result(result)

    def test_complete_with_incomplete_required_group_is_invalid(self):
        snapshot = sample_snapshot(phone_status=FieldStatus.FILLED, phone_value="Business Mobile", unresolved=[], validation_errors=[])
        snapshot.groups[0].status = GroupStatus.INCOMPLETE
        result = StageResult(outcome_type=OutcomeType.COMPLETE, stage=WorkdayStage.MY_INFORMATION, snapshot=snapshot)

        with self.assertRaisesRegex(ValueError, "required group is not complete"):
            validate_stage_result(result)

    def test_group_child_fields_are_validated(self):
        snapshot = sample_snapshot(phone_status=FieldStatus.MISSING, phone_value="Select One", unresolved=[], validation_errors=[])
        snapshot.groups[0].status = GroupStatus.COMPLETE
        result = StageResult(outcome_type=OutcomeType.COMPLETE, stage=WorkdayStage.MY_INFORMATION, snapshot=snapshot)

        with self.assertRaisesRegex(ValueError, "required field is not satisfied"):
            validate_stage_result(result)

    def test_unresolved_string_keys_are_preserved_and_normalized(self):
        snapshot = StageSnapshot(
            stage=WorkdayStage.MY_EXPERIENCE,
            unresolved_required_fields=["education.school"],
        )
        result = StageResult(
            outcome_type=OutcomeType.MY_EXPERIENCE_BLOCKED,
            stage=WorkdayStage.MY_EXPERIENCE,
            unresolved_required_fields=["education.degree"],
            snapshot=snapshot,
        )

        self.assertEqual(snapshot.to_dict()["unresolved_required_fields"], [{"canonical_key": "education.school"}])
        self.assertEqual(result.to_dict()["unresolved_required_fields"], [{"canonical_key": "education.degree"}])

    def test_two_identical_unresolved_signatures_trigger_stop(self):
        signature = build_stage_signature(sample_snapshot())

        self.assertTrue(should_stop_for_unchanged_state([signature, signature]))
        self.assertFalse(should_stop_for_unchanged_state([signature]))

    def test_two_identical_retryable_no_progress_signatures_raise(self):
        class RetryableController(BaseStageController):
            def observe(self, _page, _context):
                return sample_snapshot()

            def plan(self, _snapshot, _context):
                return []

            def execute(self, _page, _action, _context):
                raise AssertionError("no actions planned")

            def verify(self, _page, previous_snapshot, _context):
                return StageResult(
                    outcome_type=OutcomeType.RETRYABLE,
                    stage=WorkdayStage.MY_INFORMATION,
                    snapshot=previous_snapshot,
                    unresolved_required_fields=["phone_device_type"],
                )

        controller = RetryableController()
        context = {}

        controller.run_pass(None, context)
        with self.assertRaisesRegex(ValueError, "RETRYABLE twice"):
            controller.run_pass(None, context)

    def test_value_oscillation_does_not_count_as_indefinite_progress(self):
        select_one = sample_snapshot(phone_status=FieldStatus.MISSING, phone_value="Select One")
        mobile = sample_snapshot(
            phone_status=FieldStatus.FILLED,
            phone_value="Mobile",
            unresolved=[],
            validation_errors=[],
        )
        back_to_select_one = sample_snapshot(phone_status=FieldStatus.MISSING, phone_value="Select One")

        self.assertTrue(has_meaningful_progress(select_one, mobile))
        self.assertFalse(has_meaningful_progress(mobile, back_to_select_one))

    def test_safe_diagnostic_serialization_redacts_pii(self):
        snapshot = sample_snapshot(
            phone_status=FieldStatus.FILLED,
            phone_value="555-123-4567",
            unresolved=[],
            validation_errors=[],
        )
        snapshot.dom_excerpt = "Jane Applicant jane@example.com 555-123-4567 123 Main Street password=Secret123"
        snapshot.accessibility_excerpt = "sessionid=abc123 token=secret"
        action = ActionResult(
            action="fill",
            before="jane@example.com",
            after="555-123-4567 code 123456",
            metadata={"cookie": "raw-cookie"},
        )
        result = StageResult(
            outcome_type=OutcomeType.MY_INFORMATION_BLOCKED,
            stage=WorkdayStage.MY_INFORMATION,
            snapshot=snapshot,
            actions=[action],
        )

        serialized = json.dumps(result.to_safe_diagnostics(), sort_keys=True)

        self.assertIn("[REDACTED_EMAIL]", serialized)
        self.assertIn("[REDACTED_PHONE]", serialized)
        self.assertIn("[REDACTED_ADDRESS]", serialized)
        self.assertNotIn("jane@example.com", serialized)
        self.assertNotIn("555-123-4567", serialized)
        self.assertNotIn("123 Main Street", serialized)
        self.assertNotIn("Secret123", serialized)
        self.assertNotIn("raw-cookie", serialized)


if __name__ == "__main__":
    unittest.main()
