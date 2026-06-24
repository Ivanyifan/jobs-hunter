from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.workday.contracts import OutcomeType
from tests.workday_fixture_loader import (
    FORBIDDEN_TERMINAL_OUTCOMES,
    field_by_key,
    fixture_to_stage_result,
    load_workday_fixtures,
    validate_fixture,
)


class WorkdayFixtureLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = {fixture["fixture_name"]: fixture for fixture in load_workday_fixtures()}

    def test_loader_validates_required_fields(self):
        fixture = deepcopy(self.fixtures["stord_navigation"])
        fixture.pop("tenant")

        with self.assertRaisesRegex(ValueError, "missing required fixture keys"):
            validate_fixture(fixture)

    def test_required_non_filled_fields_must_be_expected_unresolved(self):
        fixture = deepcopy(self.fixtures["statestreet_phone_group"])
        fixture["expected_unresolved_fields"] = []

        with self.assertRaisesRegex(ValueError, "required non-filled fields"):
            validate_fixture(fixture)

    def test_fixtures_replay_through_stage_contracts(self):
        for fixture in self.fixtures.values():
            with self.subTest(fixture=fixture["fixture_name"]):
                result = fixture_to_stage_result(fixture)
                unresolved = {
                    item["canonical_key"]
                    for item in result.to_dict()["unresolved_required_fields"]
                }

                self.assertEqual(result.normalized_outcome(), fixture["expected_outcome"])
                self.assertEqual(unresolved, set(fixture["expected_unresolved_fields"]))

    def test_every_fixture_has_typed_expected_outcome(self):
        allowed = {item.value for item in OutcomeType}

        for fixture in self.fixtures.values():
            with self.subTest(fixture=fixture["fixture_name"]):
                self.assertIn(fixture["expected_outcome"], allowed)

    def test_no_fixture_uses_generic_terminal_outcome(self):
        for fixture in self.fixtures.values():
            with self.subTest(fixture=fixture["fixture_name"]):
                self.assertNotIn(fixture["expected_outcome"].lower(), FORBIDDEN_TERMINAL_OUTCOMES)

    def test_phone_extension_is_optional_in_state_street_fixture(self):
        fixture = self.fixtures["statestreet_phone_group"]
        extension = field_by_key(fixture, "phone_extension")

        self.assertFalse(extension["required"])
        self.assertEqual(extension["status"], "optional")

    def test_northrop_fixture_identifies_education_fields_independently(self):
        fixture = self.fixtures["northrop_education_group"]

        self.assertEqual(
            set(fixture["expected_unresolved_fields"]),
            {"education.school", "education.degree", "education.end_year"},
        )
        for key in ["education.school", "education.degree", "education.end_year"]:
            with self.subTest(key=key):
                field = field_by_key(fixture, key)
                self.assertTrue(field["required"])
                self.assertEqual(field["status"], "missing")


if __name__ == "__main__":
    unittest.main()
