import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from mcp_servers import arize_server
from mcp_servers.arize_server import (
    AuditRequest,
    apply_trusted_skill_gate,
    audit_resume,
    build_trace,
    heuristic_audit,
    log_phoenix_evaluation_annotation,
    normalize_phoenix_base_url,
    phoenix_evals_audit,
)


BASE_RESUME = """Summary
Python backend engineer focused on reliable API services.

Experience
Built reliable Python APIs and maintained production services for customers.
Improved service quality through testing, monitoring, and code reviews.
"""


class ArizeSkillLibraryTests(unittest.TestCase):
    def test_openapi_exposes_phoenix_score_and_complete_gate_contract(self):
        root = Path(__file__).resolve().parents[1]
        payload = yaml.safe_load((root / "openapi_specs" / "arize_openapi.yaml").read_text(encoding="utf-8"))
        audit_schema = payload["paths"]["/audit"]["post"]
        request_properties = audit_schema["requestBody"]["content"]["application/json"]["schema"]["properties"]
        response_properties = audit_schema["responses"]["200"]["content"]["application/json"]["schema"]["properties"]

        self.assertIn("trusted_skills", request_properties)
        self.assertIn("phoenix_evaluation", response_properties)
        self.assertIn("hard_blockers", response_properties)
        self.assertIn("unsupported_claims", response_properties)

    def test_phoenix_base_url_is_derived_from_otlp_trace_endpoint(self):
        self.assertEqual(
            normalize_phoenix_base_url(
                "https://app.phoenix.arize.com/s/example/v1/traces"
            ),
            "https://app.phoenix.arize.com/s/example",
        )

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

    def test_semantic_rephrasing_is_not_rejected_by_deterministic_word_overlap(self):
        result = heuristic_audit(AuditRequest(
            resume_v0=BASE_RESUME,
            resume_v1=BASE_RESUME.replace(
                "Built reliable Python APIs",
                "Architected a global Python platform",
            ),
        ))

        self.assertTrue(result["passed"])
        self.assertNotIn("Architected", result["new_entities"])
        self.assertEqual(result["unsupported_claims"], [])
        self.assertEqual(result["hard_blockers"], [])

    def test_phoenix_faithfulness_evaluator_is_the_semantic_judge(self):
        request = AuditRequest(
            resume_v0="Experience\nLed the migration to Amazon Web Services.",
            resume_v1="Experience\nSpearheaded the cloud migration to AWS.",
            job_description="Cloud migration leadership",
            trusted_skills=["AWS"],
        )
        score = Mock()
        score.to_dict.return_value = {
            "name": "faithfulness",
            "score": 1.0,
            "label": "faithful",
            "explanation": "The rewrite preserves the same supported responsibility.",
            "kind": "llm",
            "direction": "maximize",
            "metadata": {"trace_id": "judge-trace"},
        }
        evaluator = Mock()
        evaluator.evaluate.return_value = [score]

        with (
            patch.object(arize_server, "resolve_api_key", return_value="test-key"),
            patch.object(arize_server, "PhoenixLLM") as llm_class,
            patch.object(
                arize_server,
                "PhoenixFaithfulnessEvaluator",
                return_value=evaluator,
            ) as evaluator_class,
        ):
            result, error = phoenix_evals_audit(request, "gemini-test")

        self.assertIsNone(error)
        self.assertTrue(result["passed"])
        self.assertEqual(result["evaluator_backend"], "phoenix-evals")
        self.assertEqual(result["phoenix_evaluation"]["label"], "faithful")
        llm_class.assert_called_once_with(
            provider="google",
            model="gemini-test",
            api_key="test-key",
        )
        evaluator_class.assert_called_once_with(llm=llm_class.return_value)
        eval_input = evaluator.evaluate.call_args.args[0]
        self.assertIn(request.resume_v0, eval_input["context"])
        self.assertIn("AWS", eval_input["context"])
        self.assertEqual(eval_input["output"], request.resume_v1)

        gated = apply_trusted_skill_gate(request, result)
        self.assertTrue(gated["passed"], gated)

    def test_phoenix_unfaithful_score_blocks_with_explanation(self):
        request = AuditRequest(resume_v0=BASE_RESUME, resume_v1=BASE_RESUME)
        score = Mock()
        score.to_dict.return_value = {
            "name": "faithfulness",
            "score": 0.0,
            "label": "unfaithful",
            "explanation": "V1 adds an unsupported leadership claim.",
            "kind": "llm",
            "direction": "maximize",
            "metadata": {},
        }
        evaluator = Mock()
        evaluator.evaluate.return_value = [score]

        with (
            patch.object(arize_server, "resolve_api_key", return_value="test-key"),
            patch.object(arize_server, "PhoenixLLM"),
            patch.object(arize_server, "PhoenixFaithfulnessEvaluator", return_value=evaluator),
        ):
            result, error = phoenix_evals_audit(request, "gemini-test")

        self.assertIsNone(error)
        self.assertFalse(result["passed"])
        self.assertIn("phoenix_faithfulness_failed", result["hard_blockers"])
        self.assertIn("unsupported leadership claim", result["unsupported_claims"][0])

    def test_audit_endpoint_fails_closed_when_phoenix_judge_is_unavailable(self):
        request = AuditRequest(resume_v0=BASE_RESUME, resume_v1=BASE_RESUME)

        with (
            patch.object(
                arize_server,
                "phoenix_evals_audit",
                return_value=(None, "judge offline"),
            ),
            patch.object(arize_server, "export_trace_to_phoenix", return_value={"exported": False}),
            patch.object(arize_server, "persist_trace"),
        ):
            result = audit_resume(request)

        self.assertFalse(result["passed"])
        self.assertIn("phoenix_judge_unavailable", result["hard_blockers"])
        self.assertEqual(result["recommended_action"], "retry_audit")
        self.assertEqual(result["evaluator_backend"], "phoenix-evals-unavailable")

    def test_audit_endpoint_uses_phoenix_score_before_deterministic_gate(self):
        request = AuditRequest(resume_v0=BASE_RESUME, resume_v1=BASE_RESUME)
        phoenix_result = {
            "faithfulness_score": 1.0,
            "jd_match_score": 0.0,
            "risk_score": 0.0,
            "hallucinated_points": [],
            "unsupported_numbers": [],
            "new_entities": [],
            "unsupported_entities": [],
            "unsupported_claims": [],
            "misplaced_trusted_skills": [],
            "untrusted_new_skills": [],
            "hard_blockers": [],
            "passed": True,
            "feedback": "Faithful.",
            "recommended_action": "approve",
            "evaluator_backend": "phoenix-evals",
            "phoenix_evaluation": {
                "name": "faithfulness",
                "score": 1.0,
                "label": "faithful",
                "kind": "llm",
            },
        }

        with (
            patch.object(
                arize_server,
                "phoenix_evals_audit",
                return_value=(phoenix_result, None),
            ) as judge,
            patch.object(arize_server, "export_trace_to_phoenix", return_value={"exported": False}),
            patch.object(arize_server, "persist_trace"),
        ):
            result = audit_resume(request)

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["phoenix_evaluation"]["label"], "faithful")
        judge.assert_called_once()

    def test_trace_identifies_phoenix_evals_as_the_judge(self):
        request = AuditRequest(resume_v0=BASE_RESUME, resume_v1=BASE_RESUME)
        trace = build_trace(
            request,
            {
                "faithfulness_score": 1.0,
                "jd_match_score": 0.5,
                "risk_score": 0.0,
                "passed": True,
                "recommended_action": "approve",
                "evaluator_backend": "phoenix-evals",
            },
            "gemini-test",
            25,
            "trace-1",
        )

        self.assertEqual(trace["evaluator_name"], "phoenix_evals_resume_faithfulness_judge")
        self.assertEqual(trace["provider"], "phoenix-evals/google-gemini")

    def test_phoenix_score_is_logged_as_an_llm_span_annotation(self):
        client = Mock()
        record = {
            "evaluator_backend": "phoenix-evals",
            "model": "gemini-test",
            "hard_blockers": [],
            "phoenix_evaluation": {
                "name": "faithfulness",
                "score": 1.0,
                "label": "faithful",
                "explanation": "Every V1 claim is grounded in V0.",
            },
        }

        with (
            patch.object(arize_server, "PhoenixClient", return_value=client) as client_class,
            patch.object(
                arize_server,
                "PhoenixSpanAnnotationData",
                side_effect=lambda **values: values,
            ),
        ):
            result = log_phoenix_evaluation_annotation(
                {
                    "base_url": "https://app.phoenix.arize.com/s/example",
                    "api_key": "phoenix-key",
                },
                "0000000000001234",
                record,
            )

        self.assertTrue(result["logged"], result)
        client_class.assert_called_once_with(
            base_url="https://app.phoenix.arize.com/s/example",
            api_key="phoenix-key",
        )
        call = client.spans.log_span_annotations.call_args
        self.assertTrue(call.kwargs["sync"])
        annotation = call.kwargs["span_annotations"][0]
        self.assertEqual(annotation["span_id"], "0000000000001234")
        self.assertEqual(annotation["annotator_kind"], "LLM")
        self.assertEqual(annotation["result"]["label"], "faithful")
        self.assertEqual(annotation["result"]["score"], 1.0)

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
