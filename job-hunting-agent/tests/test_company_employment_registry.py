import unittest

from application_questions.company_employment import (
    normalize_company_name,
    resolve_company_employment,
)
from frontend.apply_flow import apply_application_profile_library


class CompanyEmploymentRegistryTests(unittest.TestCase):
    def test_company_name_normalization_removes_legal_suffix_only(self):
        self.assertEqual(normalize_company_name("The Razer, Inc."), "razer")
        self.assertNotEqual(normalize_company_name("Razer"), normalize_company_name("Razer Gold"))

    def test_confirmed_company_record_can_answer_no(self):
        result = resolve_company_employment(
            {
                "company_employment_registry": [
                    {
                        "company": "Razer",
                        "aliases": ["Razer Inc."],
                        "previously_employed": False,
                        "confirmed": True,
                    }
                ]
            },
            "Razer, Inc.",
        )

        self.assertEqual(result["answer"], "No")
        self.assertEqual(result["source"], "company_employment_registry")

    def test_missing_record_answers_no_but_matching_unconfirmed_record_blocks(self):
        missing = resolve_company_employment({"experiences": [{"company": "Volcengine"}]}, "Razer")
        unconfirmed = resolve_company_employment(
            {
                "company_employment_registry": [
                    {"company": "Razer", "previously_employed": None}
                ]
            },
            "Razer",
        )

        self.assertEqual(missing["answer"], "No")
        self.assertEqual(missing["source"], "employment_history_absence")
        self.assertEqual(unconfirmed["answer"], "")
        self.assertEqual(unconfirmed["reason"], "company_employment_record_unconfirmed")

    def test_confirmed_company_alias_can_prove_yes(self):
        result = resolve_company_employment(
            {
                "company_employment_registry": [
                    {
                        "company": "THX",
                        "aliases": ["Razer"],
                        "previously_employed": True,
                        "confirmed": True,
                    }
                ]
            },
            "Razer",
        )

        self.assertEqual(result["answer"], "Yes")
        self.assertEqual(result["matched_company"], "THX")

    def test_explicit_no_remains_no_under_closed_world_history(self):
        result = resolve_company_employment(
            {
                "company_employment_registry": [
                    {"company": "Razer", "previously_employed": False, "confirmed": True}
                ]
            },
            "Razer",
        )

        self.assertEqual(result["answer"], "No")
        self.assertEqual(result["source"], "company_employment_registry")

    def test_work_experience_can_prove_yes_but_unrelated_company_cannot(self):
        profile = {"application_profile_library": {"experiences": [{"company": "Razer Inc."}]}}

        self.assertEqual(resolve_company_employment(profile, "Razer")["answer"], "Yes")
        self.assertEqual(resolve_company_employment(profile, "HP")["answer"], "No")

    def test_conflicting_registry_and_work_experience_blocks(self):
        result = resolve_company_employment(
            {
                "company_employment_registry": [
                    {"company": "Razer", "previously_employed": False, "confirmed": True}
                ],
                "experiences": [{"company": "Razer"}],
            },
            "Razer",
        )

        self.assertEqual(result["answer"], "")
        self.assertEqual(result["reason"], "company_employment_records_conflict")

    def test_profile_enrichment_carries_registry_to_playwright_user_data(self):
        registry = [
            {
                "company": "Razer",
                "previously_employed": True,
                "confirmed": True,
            }
        ]
        enriched = apply_application_profile_library(
            {"application_profile_library": {"company_employment_registry": registry}}
        )

        self.assertEqual(enriched["company_employment_registry"], registry)


if __name__ == "__main__":
    unittest.main()
