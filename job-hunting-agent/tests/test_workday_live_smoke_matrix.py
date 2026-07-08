from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.workday.live_smoke import (
    ALLOWED_TERMINAL_LABELS,
    SanitizedSmokePage,
    SanitizedSmokeRunner,
    SmokeCase,
    SmokeMatrixHarness,
    is_live_enabled,
    live_access_request_options,
    live_access_runner_factory,
    load_smoke_config,
    sanitized_runner_factory,
    smoke_evidence,
    smoke_summary,
    validate_smoke_requirements,
)


class WorkdayLiveSmokeMatrixHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.artifact_root = Path(self.tempdir.name) / "apply-runs"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def fake_playwright_server(self, result, requests=None):
        module = types.ModuleType("mcp_servers.playwright_server")

        class AccessApplyRequest:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                if requests is not None:
                    requests.append(kwargs)

        module.AccessApplyRequest = AccessApplyRequest
        module.access_apply_form = lambda _request: dict(result)
        return mock.patch.dict(sys.modules, {"mcp_servers.playwright_server": module})

    def wait_until(self, predicate, timeout: float = 1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail("condition was not met before timeout")

    def test_smoke_config_loads_safe_defaults(self):
        config = load_smoke_config(env={})
        smoke_ids = {case.smoke_id for case in config.cases}

        self.assertFalse(config.live_enabled)
        self.assertFalse(config.confirm_submit)
        self.assertIn("my_information_state_street", smoke_ids)
        self.assertIn("experience_education_hp", smoke_ids)
        self.assertIn("application_questions_hp", smoke_ids)
        self.assertIn("auth_boeing_registered", smoke_ids)
        self.assertTrue(all(case.job_url.startswith("https://example.invalid/") for case in config.cases))
        self.assertTrue(all(not case.confirm_submit for case in config.cases))
        self.assertIn(
            "full_date_not_put_in_month",
            config.case("application_questions_hp").required_checks,
        )
        self.assertIn("pre_submit_review", ALLOWED_TERMINAL_LABELS)

    def test_create_run_poll_observable_status_and_no_final_submit_by_default(self):
        config = load_smoke_config(env={})
        submit_calls = []
        harness = SmokeMatrixHarness(
            config,
            artifact_root=self.artifact_root,
            runner_factory=sanitized_runner_factory(hold_seconds=0.12),
            final_submit_callback=lambda _ctx, _payload: submit_calls.append("clicked"),
        )

        created = harness.create_run(
            "application_questions_hp",
            extra_payload={"heartbeat_interval_seconds": 0.01},
        )

        self.assertIn("run_id", created)
        self.assertFalse(created["confirm_submit"])

        observed = self.wait_until(
            lambda: (
                status
                if (
                    (status := harness.poll(created["run_id"])).get("stage") == "APPLICATION_QUESTIONS"
                    and status.get("last_action") == "sanitized_application_questions_observe"
                )
                else None
            )
        )
        self.assertEqual(observed["current_url"], config.case("application_questions_hp").job_url)
        self.assertEqual(observed["last_action"], "sanitized_application_questions_observe")
        self.assertEqual(observed["last_field"], "application_questions.start_date")

        terminal = harness.wait_for_terminal(created["run_id"])
        summary = harness.summarize("application_questions_hp", terminal)
        case = config.case("application_questions_hp")

        self.assertEqual(terminal["status"], "complete")
        self.assertEqual(terminal["outcome"], "pre_submit_review")
        self.assertFalse(terminal["confirm_submit"])
        self.assertEqual(submit_calls, [])
        self.assertEqual(summary["final_submit_clicked"], "no")
        self.assertEqual(validate_smoke_requirements(case, terminal), [])

    def test_blocker_terminal_has_stage_url_action_field_screenshot_and_trace(self):
        config = load_smoke_config(env={})
        harness = SmokeMatrixHarness(
            config,
            artifact_root=self.artifact_root,
            runner_factory=sanitized_runner_factory(
                {"application_questions_hp": "BLOCKED_ON_QUESTIONS"}
            ),
        )

        created = harness.create_run("application_questions_hp")
        terminal = harness.wait_for_terminal(created["run_id"])
        summary = smoke_summary(config.case("application_questions_hp"), terminal)

        self.assertEqual(terminal["status"], "blocked")
        self.assertEqual(terminal["outcome"], "BLOCKED_ON_QUESTIONS")
        self.assertEqual(terminal["stage"], "APPLICATION_QUESTIONS")
        self.assertEqual(terminal["current_url"], config.case("application_questions_hp").job_url)
        self.assertEqual(terminal["last_action"], "sanitized_application_questions_blocked")
        self.assertEqual(terminal["last_field"], "application_questions.start_date")
        self.assertTrue(terminal["blocker"])
        self.assertTrue(terminal["screenshot_path"])
        self.assertTrue(Path(terminal["screenshot_path"]).exists())
        self.assertGreater(len(terminal["trace_events"]), 0)
        self.assertEqual(summary["trace_event_count"], len(terminal["trace_events"]))
        self.assertEqual(summary["confirm_submit"], False)
        self.assertEqual(summary["final_submit_clicked"], "no")
        self.assertEqual(validate_smoke_requirements(config.case("application_questions_hp"), terminal), [])

    def test_timeout_terminal_has_blocker_screenshot_and_trace(self):
        config = load_smoke_config(env={})
        case = config.case("my_information_state_street")

        harness = SmokeMatrixHarness(
            config,
            artifact_root=self.artifact_root,
            runner_factory=sanitized_runner_factory(hold_seconds=1.0),
        )

        created = harness.create_run(
            case,
            extra_payload={
                "per_stage_timeout_seconds": 0.05,
                "total_timeout_seconds": 1,
                "heartbeat_interval_seconds": 0.01,
                "stage_cancel_grace_seconds": 0.2,
            },
        )
        terminal = harness.wait_for_terminal(created["run_id"], timeout=1.0)

        self.assertEqual(terminal["status"], "timed_out")
        self.assertEqual(terminal["outcome"], "STAGE_TIMEOUT")
        self.assertEqual(terminal["blocker"]["stage"], "MY_INFORMATION")
        self.assertEqual(terminal["blocker"]["current_url"], case.job_url)
        self.assertEqual(terminal["blocker"]["last_action"], "sanitized_my_information_observe")
        self.assertEqual(terminal["blocker"]["last_field"], "phone_device_type")
        self.assertTrue(terminal["screenshot_path"])
        self.assertTrue(Path(terminal["screenshot_path"]).exists())
        self.assertGreater(len(terminal["trace_events"]), 0)
        self.assertEqual(validate_smoke_requirements(case, terminal), [])

    def test_live_gate_requires_explicit_env_flag(self):
        self.assertFalse(is_live_enabled({}))
        self.assertFalse(is_live_enabled({"WORKDAY_LIVE_SMOKE": "true"}))
        self.assertTrue(is_live_enabled({"WORKDAY_LIVE_SMOKE": "1"}))

    def test_required_checks_without_evidence_fail_gate(self):
        case = load_smoke_config(env={}).case("application_questions_hp")
        run = {
            "run_id": "missing-evidence",
            "status": "complete",
            "outcome": "pre_submit_review",
            "confirm_submit": False,
            "trace_events": [],
        }

        issues = validate_smoke_requirements(case, run)

        self.assertTrue(any("missing smoke evidence for full_date_not_put_in_month" in issue for issue in issues))
        self.assertTrue(any("missing smoke evidence for unknown_required_blocks_without_trusted_answer" in issue for issue in issues))

    def test_required_check_without_validator_fails_gate(self):
        case = SmokeCase(
            smoke_id="unknown-check",
            matrix="application_questions",
            company="Example",
            job_url="https://example.invalid/apply",
            job_url_env="WORKDAY_SMOKE_EXAMPLE_URL",
            stage="APPLICATION_QUESTIONS",
            required_checks=("unsupported_new_check",),
        )
        run = {
            "run_id": "unknown-check",
            "status": "complete",
            "outcome": "pre_submit_review",
            "confirm_submit": False,
            "trace_events": [{"blocker": {"smoke_evidence": {"unsupported_new_check": {"verified": True}}}}],
        }

        self.assertIn(
            "no smoke requirement validator for unsupported_new_check",
            validate_smoke_requirements(case, run),
        )

    def test_start_date_evidence_rejects_full_date_in_month(self):
        case = SmokeCase(
            smoke_id="bad-start-date",
            matrix="application_questions",
            company="HP",
            job_url="https://example.invalid/apply",
            job_url_env="WORKDAY_SMOKE_HP_QUESTIONS_URL",
            stage="APPLICATION_QUESTIONS",
            required_checks=("full_date_not_put_in_month", "month_day_year_filled_correctly"),
        )
        run = {
            "run_id": "bad-start-date",
            "status": "complete",
            "outcome": "pre_submit_review",
            "confirm_submit": False,
            "trace_events": [{
                "blocker": {
                    "smoke_evidence": {
                        "full_date_not_put_in_month": {
                            "full_date": "12/15/2026",
                            "month": "12/15/2026",
                        },
                        "month_day_year_filled_correctly": {
                            "expected_parts": {"month": "12", "day": "15", "year": "2026"},
                            "actual_parts": {"month": "12/15/2026", "day": "", "year": ""},
                        },
                    }
                }
            }],
        }

        issues = validate_smoke_requirements(case, run)

        self.assertTrue(any("month contains full date" in issue for issue in issues))
        self.assertTrue(any("month expected" in issue for issue in issues))

    def test_live_access_request_options_are_safe_by_default(self):
        case = load_smoke_config(env={}).case("experience_education_hp")

        options = live_access_request_options(case, payload={}, env={})

        self.assertFalse(options["confirm_submit"])
        self.assertFalse(options["probe_fill_unapproved_questions"])
        self.assertFalse(options["allow_visual_fallback"])
        self.assertFalse(options["allow_visual_field_fallback"])
        self.assertFalse(options["allow_resume_upload"])

    def test_live_access_request_options_require_env_for_unsafe_overrides(self):
        case = load_smoke_config(env={}).case("experience_education_hp")

        options = live_access_request_options(
            case,
            payload={"resume_upload_smoke": True},
            env={
                "WORKDAY_LIVE_SMOKE_PROBE_UNAPPROVED_QUESTIONS": "1",
                "WORKDAY_LIVE_SMOKE_ALLOW_VISUAL_FALLBACK": "1",
                "WORKDAY_LIVE_SMOKE_ALLOW_VISUAL_FIELD_FALLBACK": "1",
                "WORKDAY_LIVE_SMOKE_ALLOW_RESUME_UPLOAD": "1",
            },
        )

        self.assertTrue(options["probe_fill_unapproved_questions"])
        self.assertTrue(options["allow_visual_fallback"])
        self.assertTrue(options["allow_visual_field_fallback"])
        self.assertTrue(options["allow_resume_upload"])

    def test_live_runner_smoke_evidence_passes_required_gate(self):
        config = load_smoke_config(env={})
        case = config.case("application_questions_hp")
        evidence = SanitizedSmokeRunner(case)._smoke_evidence()
        requests = []
        result = {
            "success": True,
            "status": "review",
            "current_url": f"{case.job_url}/review",
            "stage": "review",
            "blocked_reason": "final_submit_confirmation_required",
            "smoke_evidence": evidence,
        }
        harness = SmokeMatrixHarness(
            config,
            artifact_root=self.artifact_root,
            runner_factory=live_access_runner_factory(),
        )

        with self.fake_playwright_server(result, requests=requests):
            created = harness.create_run(case)
            terminal = harness.wait_for_terminal(created["run_id"])

        self.assertEqual(terminal["status"], "complete")
        self.assertEqual(terminal["outcome"], "pre_submit_review")
        self.assertFalse(requests[0]["confirm_submit"])
        self.assertEqual(validate_smoke_requirements(case, terminal), [])
        trace_evidence = [
            event["blocker"]["smoke_evidence"]
            for event in terminal["trace_events"]
            if isinstance(event.get("blocker"), dict) and event["blocker"].get("smoke_evidence")
        ]
        self.assertTrue(trace_evidence)
        self.assertIn("full_date_not_put_in_month", smoke_evidence(terminal))
        self.assertEqual(trace_evidence[-1]["full_date_not_put_in_month"]["month"], "12")

    def test_blocking_live_runner_with_screenshot_and_evidence_passes_gate(self):
        config = load_smoke_config(env={})
        case = config.case("application_questions_hp")
        evidence = SanitizedSmokeRunner(case)._smoke_evidence()
        screenshot_path = self.artifact_root / "live-blocked.png"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(b"fake live screenshot")
        result = {
            "success": False,
            "status": "blocked",
            "current_url": f"{case.job_url}/questions",
            "stage": "application_questions",
            "blocked_reason": "trusted_answer_required_for_sensitive_compliance",
            "screenshot_path": str(screenshot_path),
            "smoke_evidence": evidence,
        }
        harness = SmokeMatrixHarness(
            config,
            artifact_root=self.artifact_root,
            runner_factory=live_access_runner_factory(),
        )

        with self.fake_playwright_server(result):
            created = harness.create_run(case)
            terminal = harness.wait_for_terminal(created["run_id"])

        self.assertEqual(terminal["status"], "blocked")
        self.assertEqual(terminal["outcome"], "BLOCKED_ON_QUESTIONS")
        self.assertEqual(terminal["screenshot_path"], str(screenshot_path))
        self.assertEqual(terminal["blocker"]["smoke_evidence"], evidence)
        self.assertEqual(validate_smoke_requirements(case, terminal), [])

    def test_blocking_live_runner_without_screenshot_path_fails_gate(self):
        config = load_smoke_config(env={})
        case = config.case("application_questions_hp")
        evidence = SanitizedSmokeRunner(case)._smoke_evidence()
        result = {
            "success": False,
            "status": "blocked",
            "current_url": case.job_url,
            "stage": "application_questions",
            "blocked_reason": "unknown_required_without_trusted_answer",
            "smoke_evidence": evidence,
        }
        harness = SmokeMatrixHarness(
            config,
            artifact_root=self.artifact_root,
            runner_factory=live_access_runner_factory(),
        )

        with self.fake_playwright_server(result):
            created = harness.create_run(case)
            terminal = harness.wait_for_terminal(created["run_id"])

        self.assertEqual(terminal["status"], "blocked")
        self.assertEqual(terminal["outcome"], "BLOCKED_ON_QUESTIONS")
        self.assertFalse(terminal["screenshot_path"])
        self.assertEqual(
            terminal["blocker"]["screenshot_path_issue"],
            "access_apply_form_returned_no_screenshot_path",
        )
        issues = validate_smoke_requirements(case, terminal)
        self.assertTrue(any("blocking terminal run missing screenshot_path" in issue for issue in issues))
        self.assertTrue(
            any(
                "blocker_screenshot_trace_on_failure" in issue and "screenshot_path" in issue
                for issue in issues
            )
        )


@unittest.skipUnless(is_live_enabled(), "WORKDAY_LIVE_SMOKE=1 required for live Workday smoke matrix")
class WorkdayLiveSmokeMatrixEnabledTests(unittest.TestCase):
    def test_live_matrix_runs_configured_urls(self):
        config = load_smoke_config()
        cases = config.configured_live_cases
        if not cases:
            self.skipTest("No WORKDAY_SMOKE_*_URL values configured")

        harness = SmokeMatrixHarness(
            config,
            artifact_root=Path("artifacts") / "workday-live-smoke",
            runner_factory=live_access_runner_factory(),
        )
        rows = []
        for case in cases:
            created = harness.create_run(case, extra_payload={"confirm_submit": False})
            terminal = harness.wait_for_terminal(created["run_id"], timeout=480)
            issues = validate_smoke_requirements(case, terminal)
            rows.append(harness.summarize(case, terminal))
            self.assertEqual(issues, [], rows[-1])
        self.assertTrue(rows)


if __name__ == "__main__":
    unittest.main()
