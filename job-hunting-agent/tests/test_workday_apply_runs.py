from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from adapters.workday.apply_runs import APPLY_RUNNER_NOT_CONFIGURED, ApplyRunService


class FakePage:
    def __init__(self, url: str = "https://example.wd1.myworkdayjobs.com/apply/myInformation") -> None:
        self.url = url
        self.closed = False
        self.screenshot_calls: list[str] = []

    def screenshot(self, path: str, **_kwargs) -> None:
        self.screenshot_calls.append(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"fake screenshot")

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class WorkdayApplyRunTests(unittest.TestCase):
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

    def service(self, stages=None, final_submit_callback=None) -> ApplyRunService:
        return ApplyRunService(
            artifact_root=self.artifact_root,
            stage_runners=stages or [],
            final_submit_callback=final_submit_callback,
        )

    def ready_to_submit_stage(self):
        def ready_stage(ctx, _payload):
            ctx.update(
                stage="REVIEW",
                current_url="https://example.test/apply/review",
                last_action="review_ready",
                last_field="submit_application",
            )
            return {"ready_to_submit": True, "outcome": "READY_TO_SUBMIT"}

        return ready_stage

    def test_no_configured_runner_blocks_without_fake_complete(self):
        service = self.service()
        created = service.create_run({"job_url": "https://example.test/apply"})
        terminal = service.wait_for_terminal(created["run_id"])

        self.assertEqual(terminal["status"], "blocked")
        self.assertEqual(terminal["outcome"], APPLY_RUNNER_NOT_CONFIGURED)
        self.assertEqual(terminal["blocker"]["reason"], APPLY_RUNNER_NOT_CONFIGURED)
        self.assertNotEqual(terminal["status"], "complete")
        self.assertNotEqual(terminal["outcome"], "pre_submit_review")

    def test_gateway_default_service_blocks_when_runner_not_configured(self):
        from mcp_servers import gateway_server

        original_service = gateway_server.apply_run_service
        gateway_server.apply_run_service = ApplyRunService(artifact_root=self.artifact_root)
        try:
            with TestClient(gateway_server.app) as client:
                response = client.post("/apply-runs", json={"job_url": "https://example.test/apply"})
            self.assertEqual(response.status_code, 200)
            created = response.json()
            terminal = gateway_server.apply_run_service.wait_for_terminal(created["run_id"])
        finally:
            gateway_server.apply_run_service = original_service

        self.assertEqual(terminal["status"], "blocked")
        self.assertEqual(terminal["outcome"], APPLY_RUNNER_NOT_CONFIGURED)
        self.assertNotEqual(terminal["status"], "complete")
        self.assertNotEqual(terminal["outcome"], "pre_submit_review")

    def test_create_run_returns_run_id_immediately(self):
        started = threading.Event()

        def slow_stage(ctx, _payload):
            started.set()
            ctx.update(
                stage="MY_INFORMATION",
                current_url="https://example.test/apply/myInformation",
                last_action="observe_fields",
                last_field="phone_device_type",
            )
            while True:
                ctx.check_canceled()
                time.sleep(0.01)

        service = self.service([slow_stage])

        before = time.perf_counter()
        created = service.create_run({
            "job_url": "https://example.test/apply",
            "heartbeat_interval_seconds": 0.02,
        })
        elapsed = time.perf_counter() - before

        self.assertIn("run_id", created)
        self.assertLess(elapsed, 0.15)
        self.assertIn(created["status"], {"queued", "running"})
        self.wait_until(started.is_set)
        service.cancel_run(created["run_id"])
        terminal = service.wait_for_terminal(created["run_id"])
        self.assertEqual(terminal["status"], "canceled")

    def test_get_run_status_returns_observable_fields(self):
        started = threading.Event()

        def observable_stage(ctx, _payload):
            page = FakePage("https://example.test/apply/myExperience")
            ctx.set_page(page)
            ctx.update(
                stage="MY_EXPERIENCE",
                current_url=page.url,
                last_action="fill_education",
                last_field="education.school",
            )
            started.set()
            while True:
                ctx.check_canceled()
                time.sleep(0.01)

        service = self.service([observable_stage])
        created = service.create_run({"job_url": "https://example.test/apply"})
        self.wait_until(started.is_set)

        status = service.get_run(created["run_id"])

        self.assertEqual(status["stage"], "MY_EXPERIENCE")
        self.assertEqual(status["current_url"], "https://example.test/apply/myExperience")
        self.assertEqual(status["last_action"], "fill_education")
        self.assertEqual(status["last_field"], "education.school")
        service.cancel_run(created["run_id"])
        service.wait_for_terminal(created["run_id"])

    def test_heartbeat_updates_while_worker_is_running(self):
        started = threading.Event()

        def heartbeat_stage(ctx, _payload):
            ctx.update(
                stage="APPLICATION_QUESTIONS",
                current_url="https://example.test/apply/applicationQuestions",
                last_action="inspect_questions",
                last_field="application_questions.how_heard",
            )
            started.set()
            time.sleep(0.14)
            return {"ready_to_submit": True, "outcome": "READY_TO_SUBMIT"}

        service = self.service([heartbeat_stage])
        created = service.create_run({
            "job_url": "https://example.test/apply",
            "heartbeat_interval_seconds": 0.02,
        })
        self.wait_until(started.is_set)
        first = service.get_run(created["run_id"])["heartbeat_at"]
        time.sleep(0.07)
        second = service.get_run(created["run_id"])["heartbeat_at"]

        self.assertNotEqual(first, second)
        terminal = service.wait_for_terminal(created["run_id"])
        self.assertEqual(terminal["status"], "complete")

    def test_cancel_sets_flag_exits_worker_and_closes_browser(self):
        started = threading.Event()
        exited = threading.Event()
        browser = FakeBrowser()

        def cancellable_stage(ctx, _payload):
            try:
                ctx.set_browser(browser)
                ctx.update(
                    stage="MY_INFORMATION",
                    current_url="https://example.test/apply/myInformation",
                    last_action="fill_phone",
                    last_field="phone_device_type",
                )
                started.set()
                while True:
                    ctx.check_canceled()
                    time.sleep(0.01)
            finally:
                exited.set()

        service = self.service([cancellable_stage])
        created = service.create_run({"job_url": "https://example.test/apply"})
        self.wait_until(started.is_set)

        canceled = service.cancel_run(created["run_id"])
        terminal = service.wait_for_terminal(created["run_id"])

        self.assertTrue(canceled["cancel_requested"])
        self.assertEqual(terminal["status"], "canceled")
        self.assertTrue(exited.is_set())
        self.assertTrue(browser.closed)
        self.assertTrue(any(event["action"] == "cancel_requested" for event in terminal["trace_events"]))
        self.assertFalse(service.is_worker_alive(created["run_id"]))

    def test_stage_timeout_records_context_screenshot_trace_and_requests_cancel(self):
        page = FakePage("https://example.test/apply/myInformation")
        browser = FakeBrowser()
        exited = threading.Event()

        def hanging_stage(ctx, _payload):
            try:
                ctx.set_page(page)
                ctx.set_browser(browser)
                ctx.update(
                    stage="MY_INFORMATION",
                    current_url=page.url,
                    last_action="fill_phone",
                    last_field="phone_device_type",
                )
                while True:
                    ctx.check_canceled()
                    time.sleep(0.01)
            finally:
                exited.set()

        service = self.service([hanging_stage])
        created = service.create_run({
            "job_url": "https://example.test/apply",
            "per_stage_timeout_seconds": 0.05,
            "total_timeout_seconds": 1,
            "heartbeat_interval_seconds": 0.01,
            "stage_cancel_grace_seconds": 0.2,
        })
        terminal = service.wait_for_terminal(created["run_id"], timeout=1.0)

        self.assertEqual(terminal["status"], "timed_out")
        self.assertEqual(terminal["outcome"], "STAGE_TIMEOUT")
        self.assertTrue(terminal["cancel_requested"])
        self.assertEqual(terminal["blocker"]["stage"], "MY_INFORMATION")
        self.assertEqual(terminal["blocker"]["current_url"], page.url)
        self.assertEqual(terminal["blocker"]["last_action"], "fill_phone")
        self.assertEqual(terminal["blocker"]["last_field"], "phone_device_type")
        self.assertTrue(terminal["screenshot_path"])
        self.assertTrue(Path(terminal["screenshot_path"]).exists())
        self.assertTrue(any(event["outcome"] == "STAGE_TIMEOUT" for event in terminal["trace_events"]))
        self.assertTrue(
            any(
                event["action"] == "cancel_requested"
                and "due to STAGE_TIMEOUT" in event["message"]
                for event in terminal["trace_events"]
            )
        )
        self.assertTrue(exited.is_set())
        self.assertTrue(browser.closed)
        self.assertFalse(service.is_worker_alive(created["run_id"]))

    def test_stage_update_after_terminal_is_not_recorded(self):
        late_update_attempted = threading.Event()

        def non_cooperative_stage(ctx, _payload):
            ctx.update(
                stage="MY_INFORMATION",
                current_url="https://example.test/apply/myInformation",
                last_action="before_timeout",
                last_field="phone_device_type",
            )
            time.sleep(0.12)
            ctx.update(
                last_action="late_action",
                last_field="late_field",
                trace=True,
                message="late action after terminal",
            )
            ctx.append_trace(action="late_trace", outcome="COMPLETE", message="late trace after terminal")
            late_update_attempted.set()
            return {"outcome": "COMPLETE"}

        service = self.service([non_cooperative_stage])
        created = service.create_run({
            "job_url": "https://example.test/apply",
            "per_stage_timeout_seconds": 0.02,
            "total_timeout_seconds": 1,
            "heartbeat_interval_seconds": 0.005,
            "stage_cancel_grace_seconds": 0.02,
        })
        terminal = service.wait_for_terminal(created["run_id"], timeout=1.0)
        self.assertEqual(terminal["status"], "timed_out")
        self.assertEqual(terminal["last_action"], "before_timeout")
        self.assertTrue(any(event["action"] == "stage_thread_still_running" for event in terminal["trace_events"]))

        self.assertTrue(late_update_attempted.wait(timeout=1.0))
        after_late_update = service.get_run(created["run_id"])

        self.assertEqual(after_late_update["status"], "timed_out")
        self.assertEqual(after_late_update["last_action"], "before_timeout")
        self.assertEqual(after_late_update["last_field"], "phone_device_type")
        self.assertFalse(any(event["action"] == "late_action" for event in after_late_update["trace_events"]))
        self.assertFalse(any(event["action"] == "late_trace" for event in after_late_update["trace_events"]))

    def test_blocker_result_records_blocker_screenshot_and_trace(self):
        page = FakePage("https://example.test/apply/applicationQuestions")

        def blocker_stage(ctx, _payload):
            ctx.set_page(page)
            ctx.update(
                stage="APPLICATION_QUESTIONS",
                current_url=page.url,
                last_action="inspect_questions",
                last_field="application_questions.sponsorship",
            )
            return {
                "status": "blocked",
                "outcome": "BLOCKED_ON_QUESTIONS",
                "blocker": {"canonical_key": "application_questions.sponsorship"},
                "message": "question requires review",
            }

        service = self.service([blocker_stage])
        created = service.create_run({"job_url": "https://example.test/apply"})
        terminal = service.wait_for_terminal(created["run_id"])

        self.assertEqual(terminal["status"], "blocked")
        self.assertEqual(terminal["outcome"], "BLOCKED_ON_QUESTIONS")
        self.assertEqual(terminal["blocker"], {"canonical_key": "application_questions.sponsorship"})
        self.assertTrue(Path(terminal["screenshot_path"]).exists())
        self.assertTrue(any(event["outcome"] == "BLOCKED_ON_QUESTIONS" for event in terminal["trace_events"]))
        self.assertFalse(service.is_worker_alive(created["run_id"]))

    def test_confirm_submit_false_skips_final_submit_by_default(self):
        submit_calls = []

        def final_submit(_ctx, _payload):
            submit_calls.append("clicked")
            return {"status": "complete", "outcome": "SUBMITTED"}

        service = self.service([self.ready_to_submit_stage()], final_submit_callback=final_submit)
        created = service.create_run({"job_url": "https://example.test/apply"})
        terminal = service.wait_for_terminal(created["run_id"])

        self.assertEqual(submit_calls, [])
        self.assertFalse(terminal["confirm_submit"])
        self.assertEqual(terminal["status"], "complete")
        self.assertEqual(terminal["outcome"], "pre_submit_review")
        self.assertTrue(
            any("confirm_submit=false" in event["message"] for event in terminal["trace_events"])
        )

    def test_confirm_submit_true_is_required_for_final_submit_callback(self):
        submit_calls = []

        def final_submit(ctx, _payload):
            submit_calls.append("clicked")
            ctx.update(
                stage="FINAL_SUBMIT",
                current_url="https://example.test/apply/review",
                last_action="click_final_submit",
                last_field="submit_application",
            )
            return {"status": "complete", "outcome": "SUBMITTED", "message": "submitted"}

        service = self.service([self.ready_to_submit_stage()], final_submit_callback=final_submit)
        created = service.create_run({
            "job_url": "https://example.test/apply",
            "confirm_submit": True,
        })
        terminal = service.wait_for_terminal(created["run_id"])

        self.assertEqual(submit_calls, ["clicked"])
        self.assertTrue(terminal["confirm_submit"])
        self.assertEqual(terminal["status"], "complete")
        self.assertEqual(terminal["outcome"], "SUBMITTED")
        self.assertEqual(terminal["last_action"], "click_final_submit")
        self.assertFalse(service.is_worker_alive(created["run_id"]))

    def test_loading_stuck_outcome_is_preserved(self):
        page = FakePage("https://example.test/apply/applicationQuestions")

        def loading_stage(ctx, _payload):
            ctx.set_page(page)
            ctx.update(stage="APPLICATION_QUESTIONS", current_url=page.url, last_action="wait_for_loading")
            return {
                "status": "blocked",
                "outcome": "WORKDAY_LOADING_STUCK",
                "blocker": "workday_loading_stuck",
            }

        service = self.service([loading_stage])
        created = service.create_run({"job_url": "https://example.test/apply"})
        terminal = service.wait_for_terminal(created["run_id"])

        self.assertEqual(terminal["status"], "blocked")
        self.assertEqual(terminal["outcome"], "WORKDAY_LOADING_STUCK")
        self.assertTrue(Path(terminal["screenshot_path"]).exists())


if __name__ == "__main__":
    unittest.main()
