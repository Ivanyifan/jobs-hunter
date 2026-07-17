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

    def test_new_school_entity_is_a_hard_blocker(self):
        result = heuristic_audit(AuditRequest(
            resume_v0=BASE_RESUME,
            resume_v1=BASE_RESUME + "\nEducation\nStanford University\n",
        ))

        self.assertFalse(result["passed"])
        self.assertLess(result["faithfulness_score"], 0.85)
        self.assertIn("Stanford University", result["unsupported_entities"])
        self.assertIn("unsupported_entities", result["hard_blockers"])

    def test_new_single_word_employer_in_record_header_is_a_hard_blocker(self):
        resume_v0 = (
            "Experience\n"
            "Acme Corporation | Software Engineer\n"
            "Built reliable Python APIs for customers.\n"
        )
        resume_v1 = resume_v0.replace("Acme Corporation", "Google")

        result = heuristic_audit(AuditRequest(
            resume_v0=resume_v0,
            resume_v1=resume_v1,
        ))

        self.assertFalse(result["passed"])
        self.assertIn("Google", result["unsupported_entities"])

    def test_new_metric_is_a_hard_blocker(self):
        result = heuristic_audit(AuditRequest(
            resume_v0=BASE_RESUME,
            resume_v1=BASE_RESUME.replace(
                "maintained production services",
                "maintained production services and improved throughput by 40%",
            ),
        ))

        self.assertFalse(result["passed"])
        self.assertIn("40", result["unsupported_numbers"])
        self.assertIn("unsupported_numbers", result["hard_blockers"])

    def test_new_high_risk_responsibility_is_blocked_without_entity_false_positive(self):
        result = heuristic_audit(AuditRequest(
            resume_v0=BASE_RESUME,
            resume_v1=BASE_RESUME.replace(
                "Built reliable Python APIs",
                "Architected a global Python platform",
            ),
        ))

        self.assertFalse(result["passed"])
        self.assertNotIn("Architected", result["new_entities"])
        self.assertTrue(any("architected" in claim for claim in result["unsupported_claims"]))
        self.assertIn("unsupported_claims", result["hard_blockers"])

    def test_reported_hallucination_cannot_be_overridden_by_passing_score(self):
        request = AuditRequest(resume_v0=BASE_RESUME, resume_v1=BASE_RESUME)
        result = apply_trusted_skill_gate(request, {
            "faithfulness_score": 0.99,
            "risk_score": 0.01,
            "passed": True,
            "hallucinated_points": ["Evaluator found an unsupported employer."],
            "unsupported_numbers": [],
            "new_entities": [],
        })

        self.assertFalse(result["passed"])
        self.assertIn("evaluator_reported_hallucination", result["hard_blockers"])
        self.assertLess(result["faithfulness_score"], 0.85)

    def test_deterministic_entity_gate_overrides_passing_llm_result(self):
        request = AuditRequest(
            resume_v0=BASE_RESUME,
            resume_v1=BASE_RESUME + "\nEducation\nStanford University\n",
        )
        result = apply_trusted_skill_gate(request, {
            "faithfulness_score": 0.99,
            "risk_score": 0.01,
            "passed": True,
            "hallucinated_points": [],
            "unsupported_numbers": [],
            "new_entities": [],
        })

        self.assertFalse(result["passed"])
        self.assertIn("Stanford University", result["new_entities"])
        self.assertIn("unsupported_entities", result["hard_blockers"])

    def test_equivalent_technology_aliases_remain_grounded(self):
        cases = [
            ("Amazon Web Services", "AWS"),
            ("PostgreSQL", "Postgres"),
            ("JavaScript", "JS"),
            ("Kubernetes", "k8s"),
            ("FastAPI", "Fast API"),
        ]
        for original, rewritten in cases:
            with self.subTest(original=original, rewritten=rewritten):
                resume_v0 = f"Experience\nBuilt reliable Python services using {original} for customers."
                resume_v1 = f"Experience\nBuilt reliable Python services using {rewritten} for customers."
                result = heuristic_audit(AuditRequest(
                    resume_v0=resume_v0,
                    resume_v1=resume_v1,
                ))

                self.assertTrue(result["passed"], result)
                self.assertEqual(result["new_entities"], [])
                self.assertEqual(result["untrusted_new_skills"], [])


if __name__ == "__main__":
    unittest.main()
