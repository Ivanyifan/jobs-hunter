import unittest

from frontend.apply_flow import (
    build_resume_tailoring_prompt,
    build_skill_match_report,
    score_resume_versions_for_jd,
    suggest_skills_from_resume,
    trusted_skill_library,
)


class SkillLibraryPipelineTests(unittest.TestCase):
    def test_profile_library_is_canonical_and_legacy_skills_remain_compatible(self):
        config = {
            "user_data": {
                "skills": "Legacy Skill",
                "application_profile_library": {
                    "skills": ["Python", "FastAPI", "python"],
                },
            }
        }
        legacy = {"user_data": {"skills": "Python, AWS; python"}}

        self.assertEqual(trusted_skill_library(config), ["Python", "FastAPI"])
        self.assertEqual(trusted_skill_library(legacy), ["Python", "AWS"])

    def test_match_report_classifies_library_and_resume_evidence(self):
        report = build_skill_match_report(
            "We need Python and FastAPI with Amazon Web Services and Kubernetes.",
            "Skills\nPython\nExperience\nBuilt Python services.",
            ["Python", "FastAPI", "AWS", "React"],
            resume_v1="Skills\nPython, FastAPI, AWS\nExperience\nBuilt Python services.",
        )

        self.assertEqual(report["jd_matched_skills"], ["Python", "FastAPI", "AWS"])
        self.assertEqual(report["v0_evidenced_skills"], ["Python"])
        self.assertEqual(report["library_only_skills"], ["FastAPI", "AWS"])
        self.assertEqual(report["v1_used_skills"], ["Python", "FastAPI", "AWS"])
        self.assertEqual(report["not_requested_skills"], ["React"])
        self.assertIn("kubernetes", report["missing_keywords"])
        self.assertGreater(report["v1_keyword_coverage"], report["v0_keyword_coverage"])

    def test_short_skill_alias_does_not_match_inside_larger_word(self):
        report = build_skill_match_report(
            "Work with Google products and global teams.",
            "Python backend engineer",
            ["Go"],
        )

        self.assertEqual(report["jd_matched_skills"], [])
        self.assertEqual(report["not_requested_skills"], ["Go"])

    def test_resume_skill_suggestions_include_evidence_and_exclude_existing(self):
        suggestions = suggest_skills_from_resume(
            "Technical Skills: Python, FastAPI, AWS\nBuilt Python FastAPI APIs deployed to Amazon Web Services.",
            existing_skills=["Python"],
        )

        by_name = {item["name"]: item for item in suggestions}
        self.assertNotIn("Python", by_name)
        self.assertIn("FastAPI", by_name)
        self.assertIn("AWS", by_name)
        self.assertTrue(by_name["FastAPI"]["evidence"])

    def test_tailoring_prompt_uses_only_jd_matched_trusted_skills(self):
        report = build_skill_match_report(
            "Python and FastAPI are required.",
            "Skills\nPython",
            ["Python", "FastAPI", "React"],
        )
        prompt = build_resume_tailoring_prompt(
            "Skills\nPython",
            "Python and FastAPI are required.",
            "Example",
            "Backend Engineer",
            skill_match=report,
        )

        self.assertIn('"trusted_library_only_skills": [\n    "FastAPI"', prompt)
        self.assertNotIn("React", prompt)
        self.assertIn("only in a dedicated Skills section", prompt)
        self.assertIn("Never attach it to an employer", prompt)

    def test_shared_score_contains_v0_v1_skill_report(self):
        scores = score_resume_versions_for_jd(
            None,
            None,
            "Skills\nPython",
            "Skills\nPython, FastAPI",
            "Python and FastAPI are required.",
            "Example",
            "Backend Engineer",
            skill_library=["Python", "FastAPI"],
        )

        self.assertIn("v0", scores)
        self.assertIn("v1", scores)
        self.assertEqual(scores["skill_library_match"]["library_only_skills"], ["FastAPI"])
        self.assertEqual(scores["skill_library_match"]["v1_used_skills"], ["Python", "FastAPI"])


if __name__ == "__main__":
    unittest.main()
