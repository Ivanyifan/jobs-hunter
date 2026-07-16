import unittest

from mcp_servers.arize_server import (
    AuditRequest,
    apply_trusted_skill_gate,
    heuristic_audit,
)


BASE_RESUME = """Summary
Python backend engineer focused on reliable API services.

Experience
Built reliable Python APIs and maintained production services for customers.
Improved service quality through testing, monitoring, and code reviews.
"""


class ArizeSkillLibraryTests(unittest.TestCase):
    def test_audit_request_preserves_trusted_skills(self):
        request = AuditRequest(
            resume_v0=BASE_RESUME,
            resume_v1=BASE_RESUME,
            trusted_skills=["FastAPI", "AWS"],
        )

        self.assertEqual(request.model_dump()["trusted_skills"], ["FastAPI", "AWS"])

    def test_library_only_skill_is_allowed_in_dedicated_skills_section(self):
        result = heuristic_audit(AuditRequest(
            resume_v0=BASE_RESUME,
            resume_v1=BASE_RESUME + "\nSkills\nPython, FastAPI\n",
            trusted_skills=["FastAPI"],
        ))

        self.assertTrue(result["passed"])
        self.assertEqual(result["misplaced_trusted_skills"], [])
        self.assertNotIn("fastapi", result["new_entities"])

    def test_library_only_skill_is_blocked_when_attached_to_experience(self):
        result = heuristic_audit(AuditRequest(
            resume_v0=BASE_RESUME,
            resume_v1=BASE_RESUME.replace(
                "Built reliable Python APIs",
                "Built reliable Python and FastAPI APIs",
            ),
            trusted_skills=["FastAPI"],
        ))

        self.assertFalse(result["passed"])
        self.assertEqual(result["misplaced_trusted_skills"], ["FastAPI"])
        self.assertTrue(any("outside the Skills section" in item for item in result["hallucinated_points"]))

    def test_untrusted_new_skill_is_blocked(self):
        result = heuristic_audit(AuditRequest(
            resume_v0=BASE_RESUME,
            resume_v1=BASE_RESUME + "\nSkills\nPython, FastAPI\n",
        ))

        self.assertFalse(result["passed"])
        self.assertIn("fastapi", result["new_entities"])
        self.assertTrue(any("not found in V0" in item for item in result["hallucinated_points"]))

    def test_deterministic_gate_overrides_a_passing_llm_result(self):
        request = AuditRequest(
            resume_v0=BASE_RESUME,
            resume_v1=BASE_RESUME.replace("Python APIs", "Python and FastAPI APIs"),
            trusted_skills=["FastAPI"],
        )
        result = apply_trusted_skill_gate(request, {
            "faithfulness_score": 0.99,
            "risk_score": 0.01,
            "passed": True,
            "hallucinated_points": [],
            "new_entities": [],
        })

        self.assertFalse(result["passed"])
        self.assertLess(result["faithfulness_score"], 0.85)
        self.assertEqual(result["misplaced_trusted_skills"], ["FastAPI"])


if __name__ == "__main__":
    unittest.main()
