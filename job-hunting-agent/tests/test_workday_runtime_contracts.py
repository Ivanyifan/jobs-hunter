from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from adapters.workday.contracts import (
    FieldState,
    FieldStatus,
    OutcomeType,
    StageResult,
    validate_llm_classification,
    validate_stage_result,
)
from adapters.workday.replay import RUNTIME_FIXTURE_DIR, run_runtime_fixture, run_runtime_fixtures


class WorkdayRuntimeContractTests(unittest.TestCase):
    def test_runtime_fixtures_replay_without_generic_terminal_outcomes(self):
        results = run_runtime_fixtures()
        self.assertGreaterEqual(len(results), 5)
        for result in results:
            with self.subTest(result["fixture_id"]):
                self.assertTrue(result["ok"])
                self.assertNotIn(result["outcome_type"].lower(), {"stage_timeout", "unknown", "no_action_found"})

    def test_state_street_phone_device_fixture_returns_structured_blocker(self):
        result = run_runtime_fixture(RUNTIME_FIXTURE_DIR / "state_street_phone_device_blocker.json")
        self.assertEqual(result["outcome_type"], "MY_INFORMATION_BLOCKED")
        self.assertEqual(result["unresolved_required_fields"][0]["canonical_key"], "phone_device_type")
        self.assertEqual(result["fields"][0]["status"], "blocked")

    def test_northrop_experience_fixture_returns_exact_unresolved_groups(self):
        result = run_runtime_fixture(RUNTIME_FIXTURE_DIR / "northrop_education_failure.json")
        self.assertEqual(result["outcome_type"], "MY_EXPERIENCE_BLOCKED")
        groups = {item["group"] for item in result["unresolved_required_fields"]}
        self.assertEqual(groups, {"school", "degree", "expected_end_year"})

    def test_required_fields_must_be_resolved_or_structured(self):
        result = StageResult(
            stage="my_information",
            outcome_type=OutcomeType.MY_INFORMATION_BLOCKED,
            fields=[FieldState(name="phone_device_type", required=True, status=FieldStatus.MISSING)],
        )
        with self.assertRaisesRegex(ValueError, "required field is not resolved"):
            validate_stage_result(result)

    def test_complete_stage_cannot_have_unresolved_required_fields(self):
        result = StageResult(
            stage="my_information",
            outcome_type=OutcomeType.COMPLETE,
            unresolved_required_fields=[{"field": "phone_device_type"}],
        )
        with self.assertRaisesRegex(ValueError, "complete Workday stage"):
            validate_stage_result(result)

    def test_generic_terminal_outcome_is_rejected(self):
        result = StageResult(stage="navigation", outcome_type="no_action_found")
        with self.assertRaisesRegex(ValueError, "generic Workday terminal outcome"):
            validate_stage_result(result)

    def test_ready_to_submit_requires_confirmation_and_zero_unresolved(self):
        result = StageResult(stage="review", outcome_type=OutcomeType.READY_TO_SUBMIT)
        with self.assertRaisesRegex(ValueError, "confirm_submit=true"):
            validate_stage_result(result, confirm_submit=False)
        validate_stage_result(result, confirm_submit=True)

    def test_llm_classifier_contract_rejects_answers_and_actions(self):
        with self.assertRaisesRegex(ValueError, "forbidden keys"):
            validate_llm_classification(
                {
                    "canonical_key": "need_sponsorship",
                    "risk_level": "sensitive_compliance",
                    "confidence": 0.91,
                    "answer": "No",
                }
            )
        with self.assertRaisesRegex(ValueError, "forbidden keys"):
            validate_llm_classification(
                {
                    "canonical_key": "how_heard",
                    "risk_level": "low",
                    "confidence": 0.95,
                    "action": "click first option",
                }
            )

    def test_llm_classifier_contract_accepts_classification_only(self):
        validate_llm_classification(
            {
                "canonical_key": "how_heard",
                "question_text": "How Did You Hear About Us?",
                "risk_level": "low",
                "confidence": 0.95,
                "evidence": "label text",
            }
        )

    def test_core_controllers_do_not_branch_on_company_names(self):
        source = (Path(__file__).resolve().parents[1] / "adapters" / "workday" / "controllers.py").read_text(encoding="utf-8")
        forbidden = re.compile(r"\b(boeing|stord|northrop|state\s*street|hp)\b", re.IGNORECASE)
        self.assertIsNone(forbidden.search(source))

    def test_runtime_fixtures_are_sanitized(self):
        forbidden_keys = {"password", "cookie", "cookies", "storage_state", "token", "resume_text"}
        for path in RUNTIME_FIXTURE_DIR.glob("*.json"):
            with self.subTest(path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertFalse(forbidden_keys.intersection(payload))


if __name__ == "__main__":
    unittest.main()
