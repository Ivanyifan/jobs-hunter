import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from mcp_servers import email_server, playwright_server
from scripts import run_live_hp_smoke


TENANT_HOST = "unit.wd5.myworkdayjobs.com"
EMAIL_ADDRESS = "candidate+workday@example.com"


def verification_request(not_before_epoch=None):
    return email_server.VerificationRequest(
        email_address=EMAIL_ADDRESS,
        sender_filter="workday",
        time_range_minutes=30,
        tenant_host=TENANT_HOST,
        not_before_epoch=not_before_epoch or time.time() - 5,
        correlation_id="correlation-123",
    )


def verification_candidate(**overrides):
    candidate = {
        "id": "message-123",
        "subject": "Verify your Workday account",
        "from": "Workday <no-reply@myworkday.com>",
        "recipient_headers": [f"Candidate <{EMAIL_ADDRESS}>"],
        "body": (
            "Please verify your account using "
            f'<a href="https://{TENANT_HOST}/account/verifyEmail?token=secret">Verify account</a>'
        ),
        "received_at_epoch": time.time(),
    }
    candidate.update(overrides)
    return candidate


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now


class FakeVerificationPage:
    def __init__(self, clock):
        self.clock = clock
        self.url = f"https://{TENANT_HOST}/en-US/test/login"
        self.visited = []

    def wait_for_timeout(self, milliseconds):
        self.clock.now += milliseconds / 1000.0

    def goto(self, url, **_kwargs):
        self.visited.append(url)
        self.url = url


class FakeProcess:
    pid = 4321
    returncode = None

    def poll(self):
        return None


class EmailVerificationCorrelationTests(unittest.TestCase):
    def test_matching_workday_message_requires_all_correlation_checks(self):
        correlation = email_server.verification_candidate_correlation(
            verification_candidate(),
            verification_request(),
        )

        self.assertTrue(correlation["verified"])
        self.assertTrue(all(correlation["checks"].values()))
        self.assertEqual(len(correlation["trusted_links"]), 1)

    def test_wrong_recipient_stale_message_and_cross_tenant_link_are_rejected(self):
        req = verification_request()
        cases = {
            "wrong_recipient": verification_candidate(recipient_headers=["other@example.com"]),
            "stale": verification_candidate(received_at_epoch=req.not_before_epoch - 120),
            "cross_tenant": verification_candidate(
                body=(
                    "Please verify your account using "
                    '<a href="https://other.wd5.myworkdayjobs.com/account/verifyEmail?token=x">Verify</a>'
                )
            ),
        }

        for name, candidate in cases.items():
            with self.subTest(name=name):
                correlation = email_server.verification_candidate_correlation(candidate, req)
                self.assertFalse(correlation["verified"])
                self.assertTrue(correlation["reasons"])

    def test_unrelated_number_is_not_treated_as_verification_code(self):
        self.assertIsNone(
            email_server.extract_verification_otp_code(
                "Your application was received in 2026. No verification is required."
            )
        )
        self.assertEqual(
            email_server.extract_verification_otp_code("Your verification code is 482913."),
            "482913",
        )

    def test_verification_endpoint_returns_only_trusted_artifact_and_digest(self):
        with (
            patch.object(
                email_server,
                "fetch_verification_candidates_gmail_api",
                return_value=[verification_candidate()],
            ),
            patch.object(email_server, "fetch_verification_candidates_imap") as imap_fetch,
        ):
            result = email_server.get_latest_verification_artifacts(verification_request())

        imap_fetch.assert_not_called()
        self.assertTrue(result["trusted"])
        self.assertEqual(result["artifact_type"], "link")
        self.assertEqual(result["correlation"]["tenant_host"], TENANT_HOST)
        self.assertNotIn("subject", result)
        self.assertNotIn("from", result)
        self.assertNotIn("body", result)
        self.assertNotIn("email_address", result)

    def test_verification_endpoint_rejects_uncorrelated_candidates(self):
        candidate = verification_candidate(recipient_headers=["other@example.com"])
        with (
            patch.object(
                email_server,
                "fetch_verification_candidates_gmail_api",
                return_value=[candidate],
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            email_server.get_latest_verification_artifacts(verification_request())

        self.assertEqual(raised.exception.status_code, 409)
        detail = raised.exception.detail
        self.assertEqual(detail["reason"], "verification_email_correlation_failed")
        self.assertNotIn(EMAIL_ADDRESS, json.dumps(detail))

    def test_oauth_file_paths_can_be_configured_without_copying_secrets(self):
        with patch.dict(
            os.environ,
            {
                "GMAIL_OAUTH_TOKEN_PATH": os.path.join("C:\\", "secure", "token.json"),
                "GMAIL_OAUTH_CLIENT_SECRET_PATH": os.path.join("C:\\", "secure", "client.json"),
            },
        ):
            self.assertTrue(email_server.gmail_token_path().endswith(os.path.join("secure", "token.json")))
            self.assertTrue(email_server.gmail_secret_path().endswith(os.path.join("secure", "client.json")))

    def test_health_reports_verification_ready_only_for_usable_mailbox(self):
        with (
            patch.object(email_server, "load_configured_gmail_token", return_value=({"refresh_token": "set"}, "local")),
            patch.object(email_server, "get_gmail_api_token", return_value="access-token"),
            patch.object(email_server, "configured_imap_credentials", return_value=None),
        ):
            result = email_server.health()

        self.assertTrue(result["gmail_oauth_configured"])
        self.assertTrue(result["gmail_oauth_ready"])
        self.assertTrue(result["verification_ready"])


class PlaywrightEmailVerificationTests(unittest.TestCase):
    def trusted_payload(self, link):
        return {
            "trusted": True,
            "source": "gmail_oauth",
            "message_digest": "abc123",
            "received_at_epoch": 999.0,
            "artifact_type": "link",
            "links": [link],
            "otp_code": None,
            "correlation": {
                "verified": True,
                "checks": {
                    "recipient_match": True,
                    "time_match": True,
                    "sender_match": True,
                    "verification_intent_match": True,
                    "tenant_match": True,
                    "artifact_match": True,
                },
            },
        }

    def test_client_navigates_only_to_same_tenant_https_link(self):
        clock = FakeClock()
        page = FakeVerificationPage(clock)
        link = f"https://{TENANT_HOST}/account/verifyEmail?token=secret"
        with (
            patch("requests.post", return_value=FakeResponse(200, self.trusted_payload(link))),
            patch.object(playwright_server.time, "time", side_effect=clock.time),
            patch.object(playwright_server, "workday_email_verification_pending", return_value=False),
        ):
            solved, method, evidence = playwright_server.resolve_email_challenge(
                page,
                EMAIL_ADDRESS,
                5,
                not_before_epoch=990.0,
                correlation_id="correlation-123",
            )

        self.assertTrue(solved)
        self.assertEqual(method, "link")
        self.assertEqual(page.visited, [link])
        self.assertTrue(evidence["verified"])
        self.assertNotIn("secret", json.dumps(evidence))
        self.assertNotIn(EMAIL_ADDRESS, json.dumps(evidence))

    def test_client_rejects_cross_tenant_link_even_if_service_marks_it_trusted(self):
        clock = FakeClock()
        page = FakeVerificationPage(clock)
        link = "https://other.wd5.myworkdayjobs.com/account/verifyEmail?token=secret"
        with (
            patch("requests.post", return_value=FakeResponse(200, self.trusted_payload(link))),
            patch.object(playwright_server.time, "time", side_effect=clock.time),
        ):
            solved, method, evidence = playwright_server.resolve_email_challenge(
                page,
                EMAIL_ADDRESS,
                5,
                not_before_epoch=990.0,
                correlation_id="correlation-123",
            )

        self.assertFalse(solved)
        self.assertEqual(method, "email_verification_correlation_failed")
        self.assertEqual(page.visited, [])
        self.assertFalse(evidence["verified"])

    def test_client_rejects_trusted_response_without_required_checks(self):
        clock = FakeClock()
        page = FakeVerificationPage(clock)
        link = f"https://{TENANT_HOST}/account/verifyEmail?token=secret"
        payload = self.trusted_payload(link)
        payload["correlation"]["checks"] = {}
        with (
            patch("requests.post", return_value=FakeResponse(200, payload)),
            patch.object(playwright_server.time, "time", side_effect=clock.time),
        ):
            solved, method, evidence = playwright_server.resolve_email_challenge(
                page,
                EMAIL_ADDRESS,
                5,
                not_before_epoch=990.0,
                correlation_id="correlation-123",
            )

        self.assertFalse(solved)
        self.assertEqual(method, "email_verification_correlation_failed")
        self.assertEqual(page.visited, [])
        self.assertFalse(evidence["verified"])

    def test_missing_correlation_context_fails_without_calling_email_service(self):
        page = FakeVerificationPage(FakeClock())
        with patch("requests.post") as post:
            solved, method, evidence = playwright_server.resolve_email_challenge(
                page,
                EMAIL_ADDRESS,
                5,
            )

        post.assert_not_called()
        self.assertFalse(solved)
        self.assertEqual(method, "missing_email_verification_correlation")
        self.assertFalse(evidence["verified"])

    def test_auth_blocked_result_keeps_sanitized_email_verification_evidence(self):
        page = SimpleNamespace(url=f"https://{TENANT_HOST}/en-US/test/login")
        evidence = {
            "verified": False,
            "message_digest": "abc123",
            "checks": {"recipient_match": False},
        }
        req = SimpleNamespace(wait_for_email_seconds=120, application_id="unit-auth")
        with (
            patch.object(
                playwright_server,
                "workday_auth_overlay_diagnostic",
                return_value={"visible": False, "auth_flow": "create_account", "auth_error_text": ""},
            ),
            patch.object(playwright_server, "capture_apply_screenshot", return_value="auth.png"),
            patch.object(playwright_server, "_write_workday_auth_diagnostic_artifacts", side_effect=lambda value: value),
        ):
            result = playwright_server.workday_auth_blocked_result(
                page,
                req,
                history=[{"stage": "email_verification", "email_verification": evidence}],
                blocked_reason="email_verification_correlation_failed",
            )

        self.assertEqual(result["stage"], "AUTH")
        self.assertEqual(result["outcome_type"], "AUTH_BLOCKED")
        self.assertEqual(result["email_verification"], evidence)
        self.assertTrue(result["smoke_evidence"]["blocker_screenshot_trace_on_failure"]["verified"])


class EmailServiceRunnerTests(unittest.TestCase):
    def test_runner_reuses_only_a_verification_ready_email_service(self):
        with (
            patch.dict(os.environ, {"EMAIL_URL": "http://127.0.0.1:8005"}),
            patch.object(run_live_hp_smoke, "email_service_health", return_value={"verification_ready": True}),
            patch.object(run_live_hp_smoke.subprocess, "Popen") as popen,
            patch.object(run_live_hp_smoke, "append_log"),
        ):
            result = run_live_hp_smoke.ensure_email_server(SimpleNamespace())

        self.assertIsNone(result)
        popen.assert_not_called()

    def test_runner_fails_closed_for_unready_remote_email_service(self):
        with (
            patch.dict(os.environ, {"EMAIL_URL": "https://email.invalid"}),
            patch.object(run_live_hp_smoke, "email_service_health", return_value={}),
        ):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                run_live_hp_smoke.ensure_email_server(SimpleNamespace())

    def test_runner_respects_headful_environment_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "runner.log"
            progress_path = Path(temp_dir) / "progress.json"
            with (
                patch.dict(os.environ, {"PLAYWRIGHT_HEADLESS": "false"}),
                patch.object(run_live_hp_smoke, "pids_listening_on_port", return_value=[]),
                patch.object(run_live_hp_smoke, "port_open", return_value=True),
                patch.object(run_live_hp_smoke.subprocess, "Popen", return_value=FakeProcess()) as popen,
                patch.object(run_live_hp_smoke, "append_log"),
            ):
                run_live_hp_smoke.start_server(progress_path, "headful-unit", log_path)

            call = popen.call_args
            self.assertEqual(call.kwargs["env"]["PLAYWRIGHT_HEADLESS"], "false")
            call.kwargs["stdout"].close()


if __name__ == "__main__":
    unittest.main()
