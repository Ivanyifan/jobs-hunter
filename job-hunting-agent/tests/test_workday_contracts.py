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

    def test_action_result_legacy_aliases_are_bidirectionally_normalized(self):
        modern = ActionResult(acted=True, verified=True)
        legacy = ActionResult(changed=True, postcondition_verified=True)

        self.assertTrue(modern.changed)
        self.assertTrue(modern.postcondition_verified)
        self.assertTrue(legacy.acted)
        self.assertTrue(legacy.verified)

    def test_controller_base_exposes_required_protocol_methods(self):
        for method_name in ["observe", "plan", "execute", "verify", "run_pass"]:
            with self.subTest(method_name=method_name):
                self.assertTrue(callable(getattr(BaseStageController, method_name)))

    def test_contracts_reexports_controller_base_without_defining_duplicate(self):
        import adapters.workday.contracts as contracts

        contract_source = Path(contracts.__file__).read_text(encoding="utf-8")

        self.assertIs(contracts.BaseStageController, BaseStageController)
        self.assertNotIn("class BaseStageController", contract_source)

    def test_controllers_package_preserves_legacy_public_imports(self):
        import adapters.workday.controllers as controllers

        public_names = [
            "ActionResult",
            "ApplicationQuestionsController",
            "BaseStageController",
            "FieldState",
            "FieldStatus",
            "LegacyApplicationQuestionsController",
            "MyExperienceController",
            "MyInformationController",
            "NavigationController",
            "OutcomeType",
            "StageResult",
            "StageSnapshot",
            "ShadowApplicationQuestionsController",
            "ShadowMyExperienceController",
            "ShadowMyInformationController",
            "ShadowNavigationController",
            "replay_fixture",
            "validate_stage_result",
        ]

        self.assertTrue(controllers.__file__.endswith("__init__.py"))
        for public_name in public_names:
            with self.subTest(public_name=public_name):
                self.assertTrue(hasattr(controllers, public_name))

    def test_public_shadow_controllers_run_pass_uses_full_protocol(self):
        import adapters.workday.controllers as controllers

        cases = [
            (
                controllers.MyInformationController,
                {
                    "unresolved_required_fields": [
                        {"field": "phone_device_type", "reason": "fixture_required"}
                    ]
                },
                OutcomeType.MY_INFORMATION_BLOCKED.value,
            ),
            (
                controllers.MyExperienceController,
                {
                    "unresolved_groups": ["education.school"],
                    "unresolved_required_fields": [
                        {"field": "education.degree", "reason": "fixture_required"}
                    ],
                },
                OutcomeType.MY_EXPERIENCE_BLOCKED.value,
            ),
            (
                controllers.ApplicationQuestionsController,
                {"unresolved_required_fields": ["how_heard"]},
                OutcomeType.BLOCKED_ON_QUESTIONS.value,
            ),
            (
                controllers.NavigationController,
                {"manual_apply_available": False, "recovered": False},
                OutcomeType.NAVIGATION_BLOCKED.value,
            ),
        ]

        for controller_type, fixture, expected_outcome in cases:
            with self.subTest(controller=controller_type.__name__):
                result = controller_type().run_pass(None, {"fixture": fixture})

                self.assertEqual(result.normalized_outcome(), expected_outcome)

    def test_my_information_shadow_accepts_string_unresolved_key(self):
        import adapters.workday.controllers as controllers

        result = controllers.MyInformationController().run_pass(
            None,
            {"fixture": {"unresolved_required_fields": ["phone_device_type"]}},
        )
        serialized = result.to_dict()

        self.assertEqual(result.normalized_outcome(), OutcomeType.MY_INFORMATION_BLOCKED.value)
        self.assertEqual(serialized["unresolved_required_fields"], [{"canonical_key": "phone_device_type"}])
        self.assertEqual(result.snapshot.required_fields, ["phone_device_type"])

    def test_my_information_shadow_accepts_canonical_only_unresolved_key(self):
        import adapters.workday.controllers as controllers

        result = controllers.MyInformationController().run_pass(
            None,
            {"fixture": {"unresolved_required_fields": [{"canonical_key": "phone_device_type"}]}},
        )
        serialized = result.to_dict()

        self.assertEqual(result.normalized_outcome(), OutcomeType.MY_INFORMATION_BLOCKED.value)
        self.assertEqual(serialized["unresolved_required_fields"], [{"canonical_key": "phone_device_type"}])
        self.assertEqual(result.snapshot.required_fields, ["phone_device_type"])

    def test_my_experience_shadow_preserves_string_and_canonical_unresolved_keys(self):
        import adapters.workday.controllers as controllers

        result = controllers.MyExperienceController().run_pass(
            None,
            {
                "fixture": {
                    "unresolved_groups": ["education.school"],
                    "unresolved_required_fields": [
                        {"canonical_key": "education.degree"},
                        "education.end_year",
                    ],
                }
            },
        )
        serialized = result.to_dict()
        unresolved = {item["canonical_key"] for item in serialized["unresolved_required_fields"]}

        self.assertEqual(result.normalized_outcome(), OutcomeType.MY_EXPERIENCE_BLOCKED.value)
        self.assertEqual(unresolved, {"education.school", "education.degree", "education.end_year"})

    def test_shadow_controller_required_fields_do_not_stringify_records(self):
        import adapters.workday.controllers as controllers

        snapshot = controllers.MyExperienceController().observe(
            {
                "fixture": {
                    "unresolved_groups": [{"canonical_key": "education.school"}],
                    "unresolved_required_fields": [
                        {"canonical_key": "education.degree"},
                        {"group": "education.end_year"},
                    ],
                }
            }
        )

        self.assertEqual(snapshot.required_fields, ["education.school", "education.degree", "education.end_year"])
        for key in snapshot.required_fields:
            self.assertNotEqual(key, "None")
            self.assertNotIn("{'canonical_key'", key)

    def test_application_question_replay_preserves_string_unresolved_keys(self):
        import adapters.workday.controllers as controllers

        result = controllers.replay_fixture(
            {
                "stage": "application_questions",
                "unresolved_required_fields": [
                    "how_heard",
                    {"canonical_key": "authorized_to_work_us"},
                ],
            }
        )
        unresolved = {item["canonical_key"] for item in result.to_dict()["unresolved_required_fields"]}

        self.assertEqual(result.normalized_outcome(), OutcomeType.BLOCKED_ON_QUESTIONS.value)
        self.assertEqual(unresolved, {"how_heard", "authorized_to_work_us"})

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

    def test_outcome_complete_terminal_contract_is_enforced(self):
        snapshot = sample_snapshot(phone_status=FieldStatus.FILLED, phone_value="Business Mobile", unresolved=[], validation_errors=[])

        with self.assertRaisesRegex(ValueError, "complete=true"):
            validate_stage_result(StageResult(outcome_type=OutcomeType.COMPLETE, stage=WorkdayStage.REVIEW, snapshot=snapshot))

        with self.assertRaisesRegex(ValueError, "terminal=false"):
            validate_stage_result(
                StageResult(
                    outcome_type=OutcomeType.RETRYABLE,
                    stage=WorkdayStage.REVIEW,
                    terminal=True,
                )
            )

        with self.assertRaisesRegex(ValueError, "complete=false"):
            validate_stage_result(
                StageResult(
                    outcome_type=OutcomeType.MY_INFORMATION_BLOCKED,
                    stage=WorkdayStage.MY_INFORMATION,
                    complete=True,
                )
            )

    def test_complete_with_required_blocked_field_is_invalid(self):
        snapshot = sample_snapshot(phone_status=FieldStatus.BLOCKED, phone_value="Select One", unresolved=[], validation_errors=[])
        snapshot.groups[0].status = GroupStatus.COMPLETE
        result = StageResult(outcome_type=OutcomeType.COMPLETE, stage=WorkdayStage.MY_INFORMATION, complete=True, snapshot=snapshot)

        with self.assertRaisesRegex(ValueError, "required field is not satisfied"):
            validate_stage_result(result)

    def test_complete_with_incomplete_required_group_is_invalid(self):
        snapshot = sample_snapshot(phone_status=FieldStatus.FILLED, phone_value="Business Mobile", unresolved=[], validation_errors=[])
        snapshot.groups[0].status = GroupStatus.INCOMPLETE
        result = StageResult(outcome_type=OutcomeType.COMPLETE, stage=WorkdayStage.MY_INFORMATION, complete=True, snapshot=snapshot)

        with self.assertRaisesRegex(ValueError, "required group is not complete"):
            validate_stage_result(result)

    def test_group_child_fields_are_validated(self):
        snapshot = sample_snapshot(phone_status=FieldStatus.MISSING, phone_value="Select One", unresolved=[], validation_errors=[])
        snapshot.groups[0].status = GroupStatus.COMPLETE
        result = StageResult(outcome_type=OutcomeType.COMPLETE, stage=WorkdayStage.MY_INFORMATION, complete=True, snapshot=snapshot)

        with self.assertRaisesRegex(ValueError, "required field is not satisfied"):
            validate_stage_result(result)

    def test_complete_with_declared_required_field_without_state_is_invalid(self):
        snapshot = StageSnapshot(
            stage=WorkdayStage.MY_EXPERIENCE,
            required_fields=["education.school"],
            fields=[],
            groups=[],
        )
        result = StageResult(
            outcome_type=OutcomeType.COMPLETE,
            stage=WorkdayStage.MY_EXPERIENCE,
            complete=True,
            snapshot=snapshot,
        )

        with self.assertRaisesRegex(ValueError, "required field state missing"):
            validate_stage_result(result)

    def test_blocked_declared_required_field_without_state_is_valid_when_unresolved(self):
        snapshot = StageSnapshot(
            stage=WorkdayStage.MY_EXPERIENCE,
            required_fields=["education.school"],
            unresolved_required_fields=["education.school"],
        )
        result = StageResult(
            outcome_type=OutcomeType.MY_EXPERIENCE_BLOCKED,
            stage=WorkdayStage.MY_EXPERIENCE,
            snapshot=snapshot,
        )

        validate_stage_result(result)

    def test_blocked_declared_required_field_state_must_be_explicitly_unresolved(self):
        snapshot = StageSnapshot(
            stage=WorkdayStage.MY_EXPERIENCE,
            required_fields=["education.school"],
            fields=[
                FieldState(
                    canonical_key="education.school",
                    required=True,
                    status=FieldStatus.BLOCKED,
                )
            ],
        )
        result = StageResult(
            outcome_type=OutcomeType.MY_EXPERIENCE_BLOCKED,
            stage=WorkdayStage.MY_EXPERIENCE,
            snapshot=snapshot,
        )

        with self.assertRaisesRegex(ValueError, "explicitly unresolved"):
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
        self.assertEqual(
            result.to_dict()["unresolved_required_fields"],
            [{"canonical_key": "education.degree"}, {"canonical_key": "education.school"}],
        )

    def test_stage_result_top_level_diagnostics_merge_snapshot_and_groups(self):
        snapshot = StageSnapshot(
            stage=WorkdayStage.MY_EXPERIENCE,
            groups=[
                GroupState(
                    canonical_key="education",
                    required=True,
                    status=GroupStatus.INCOMPLETE,
                    unresolved_fields=["education.degree"],
                    validation_messages=["Education section has errors"],
                    fields=[
                        FieldState(
                            canonical_key="education.degree",
                            label="Degree",
                            required=True,
                            status=FieldStatus.MISSING,
                            validation_messages=["Degree is required"],
                        )
                    ],
                )
            ],
            unresolved_required_fields=["education.school"],
            validation_errors=["School is required"],
            alerts=["Fix education"],
        )
        result = StageResult(
            outcome_type=OutcomeType.MY_EXPERIENCE_BLOCKED,
            stage=WorkdayStage.MY_EXPERIENCE,
            snapshot=snapshot,
            unresolved_required_fields=["education.end_year"],
            validation_errors=["End Year is required"],
            alerts=["Fix end year"],
        )

        serialized = result.to_dict()

        self.assertEqual(
            {item["canonical_key"] for item in serialized["unresolved_required_fields"]},
            {"education.school", "education.degree", "education.end_year"},
        )
        self.assertEqual(
            set(serialized["validation_errors"]),
            {
                "School is required",
                "End Year is required",
                "Education section has errors",
                "Degree is required",
            },
        )
        self.assertEqual(set(serialized["alerts"]), {"Fix education", "Fix end year"})
        self.assertIn("education", serialized["unresolved_required_groups"])

    def test_rich_unresolved_metadata_survives_deduplication(self):
        rich_unresolved = {
            "canonical_key": "education.school",
            "validation_message": "School is required",
            "options": ["Other"],
            "locator_hints": ["#school"],
        }
        snapshot = StageSnapshot(
            stage=WorkdayStage.MY_EXPERIENCE,
            groups=[
                GroupState(
                    canonical_key="education",
                    required=True,
                    status=GroupStatus.INCOMPLETE,
                    unresolved_fields=["education.school"],
                )
            ],
            unresolved_required_fields=[rich_unresolved],
        )
        result = StageResult(
            outcome_type=OutcomeType.MY_EXPERIENCE_BLOCKED,
            stage=WorkdayStage.MY_EXPERIENCE,
            snapshot=snapshot,
            unresolved_required_fields=["education.school"],
        )

        [serialized] = [
            item
            for item in result.to_dict()["unresolved_required_fields"]
            if item["canonical_key"] == "education.school"
        ]

        self.assertEqual(serialized["validation_message"], "School is required")
        self.assertEqual(serialized["options"], ["Other"])
        self.assertEqual(serialized["locator_hints"], ["#school"])
        self.assertEqual(serialized["group"], "education")

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
                    terminal=False,
                )

        controller = RetryableController()
        context = {}

        controller.run_pass(None, context)
        with self.assertRaisesRegex(ValueError, "RETRYABLE twice"):
            controller.run_pass(None, context)

    def test_controller_passes_confirm_submit_to_submitted_validation(self):
        class SubmittedController(BaseStageController):
            def observe(self, _page, _context):
                return StageSnapshot(stage=WorkdayStage.REVIEW)

            def plan(self, _snapshot, _context):
                return []

            def execute(self, _page, _action, _context):
                raise AssertionError("no actions planned")

            def verify(self, _page, _previous_snapshot, _context):
                return StageResult(
                    outcome_type=OutcomeType.SUBMITTED,
                    stage=WorkdayStage.SUBMITTED,
                    complete=True,
                    terminal=True,
                    snapshot=StageSnapshot(stage=WorkdayStage.SUBMITTED),
                )

        controller = SubmittedController()

        with self.assertRaisesRegex(ValueError, "confirm_submit=true"):
            controller.run_pass(None, {})
        result = controller.run_pass(None, {"confirm_submit": True})
        self.assertEqual(result.normalized_outcome(), OutcomeType.SUBMITTED.value)

    def test_short_cycle_signatures_trigger_stop(self):
        a = build_stage_signature(sample_snapshot(phone_value="Select One"))
        b = build_stage_signature(sample_snapshot(phone_value="Mobile"))

        self.assertFalse(should_stop_for_unchanged_state([a, b]))
        self.assertTrue(should_stop_for_unchanged_state([a, b, a]))

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

    def test_filled_field_swap_does_not_count_as_progress(self):
        phone_filled = sample_snapshot(
            phone_status=FieldStatus.FILLED,
            phone_value="Mobile",
            unresolved=["education.school"],
            validation_errors=["School is required"],
        )
        school_filled = sample_snapshot(
            phone_status=FieldStatus.MISSING,
            phone_value="Select One",
            unresolved=["phone_device_type"],
            validation_errors=["Phone Device Type is required"],
        )
        phone_filled.fields.append(
            FieldState(
                canonical_key="education.school",
                required=True,
                status=FieldStatus.MISSING,
                visible_value="",
            )
        )
        school_filled.fields.append(
            FieldState(
                canonical_key="education.school",
                required=True,
                status=FieldStatus.FILLED,
                visible_value="University",
            )
        )

        self.assertFalse(has_meaningful_progress(phone_filled, school_filled))

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
