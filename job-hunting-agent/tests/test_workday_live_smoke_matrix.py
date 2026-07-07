from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.workday.live_smoke import (
    ALLOWED_TERMINAL_LABELS,
    SanitizedSmokePage,
    SmokeMatrixHarness,
    is_live_enabled,
    live_access_runner_factory,
    load_smoke_config,
    sanitized_runner_factory,
    smoke_summary,
    validate_terminal_run,
)


class WorkdayLiveSmokeMatrixHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.artifact_root = Path(self.tempdir.name) / "apply-runs"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

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

        self.assertEqual(terminal["status"], "complete")
        self.assertEqual(terminal["outcome"], "pre_submit_review")
        self.assertFalse(terminal["confirm_submit"])
        self.assertEqual(submit_calls, [])
        self.assertEqual(summary["final_submit_clicked"], "no")
        self.assertEqual(validate_terminal_run(terminal), [])

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
        self.assertEqual(validate_terminal_run(terminal), [])

    def test_timeout_terminal_has_blocker_screenshot_and_trace(self):
        config = load_smoke_config(env={})
        case = config.case("my_information_state_street")

        class HangingStage:
            stage = "MY_INFORMATION"

            def run(self, ctx, payload):
                page = SanitizedSmokePage(str(payload.get("job_url") or case.job_url))
                ctx.set_page(page)
                ctx.update(
                    stage="MY_INFORMATION",
                    current_url=page.url,
                    last_action="sanitized_hang",
                    last_field="phone_device_type",
                    trace=True,
                    message="sanitized hang started",
                )
                while True:
                    ctx.check_canceled()
                    time.sleep(0.01)

        harness = SmokeMatrixHarness(
            config,
            artifact_root=self.artifact_root,
            runner_factory=lambda _case: HangingStage(),
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
        self.assertEqual(terminal["blocker"]["last_action"], "sanitized_hang")
        self.assertEqual(terminal["blocker"]["last_field"], "phone_device_type")
        self.assertTrue(terminal["screenshot_path"])
        self.assertTrue(Path(terminal["screenshot_path"]).exists())
        self.assertGreater(len(terminal["trace_events"]), 0)
        self.assertEqual(validate_terminal_run(terminal), [])

    def test_live_gate_requires_explicit_env_flag(self):
        self.assertFalse(is_live_enabled({}))
        self.assertFalse(is_live_enabled({"WORKDAY_LIVE_SMOKE": "true"}))
        self.assertTrue(is_live_enabled({"WORKDAY_LIVE_SMOKE": "1"}))


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
            issues = validate_terminal_run(terminal)
            rows.append(harness.summarize(case, terminal))
            self.assertEqual(issues, [], rows[-1])
        self.assertTrue(rows)


if __name__ == "__main__":
    unittest.main()
