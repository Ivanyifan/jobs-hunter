from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import time
from typing import Any, Callable, Mapping

from .apply_runs import ApplyRunContext, ApplyRunService


LIVE_SMOKE_ENV = "WORKDAY_LIVE_SMOKE"
COMMON_REQUIRED_CHECKS = ("no_final_submit",)

ALLOWED_RUN_STATUSES = {"complete", "blocked", "timed_out", "canceled"}
ALLOWED_TERMINAL_LABELS = {
    "complete",
    "pre_submit_review",
    "blocked",
    "timed_out",
    "canceled",
    "MY_INFORMATION_BLOCKED",
    "MY_EXPERIENCE_BLOCKED",
    "BLOCKED_ON_QUESTIONS",
    "AUTH_BLOCKED",
    "WORKDAY_LOADING_STUCK",
    "STAGE_TIMEOUT",
}
BLOCKING_TERMINAL_LABELS = {
    "blocked",
    "timed_out",
    "MY_INFORMATION_BLOCKED",
    "MY_EXPERIENCE_BLOCKED",
    "BLOCKED_ON_QUESTIONS",
    "AUTH_BLOCKED",
    "WORKDAY_LOADING_STUCK",
    "STAGE_TIMEOUT",
}


@dataclass(frozen=True)
class SmokeTarget:
    smoke_id: str
    matrix: str
    company: str
    job_url_env: str
    default_job_url: str
    stage: str
    required_checks: tuple[str, ...]


@dataclass(frozen=True)
class SmokeCase:
    smoke_id: str
    matrix: str
    company: str
    job_url: str
    job_url_env: str
    stage: str
    required_checks: tuple[str, ...]
    confirm_submit: bool = False
    configured_from_env: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "smoke_id": self.smoke_id,
            "smoke_matrix": self.matrix,
            "company": self.company,
            "job_url": self.job_url,
            "confirm_submit": self.confirm_submit,
            "expected_stage": self.stage,
            "required_checks": list(self.required_checks),
            "per_stage_timeout_seconds": 90,
            "total_timeout_seconds": 420,
            "heartbeat_interval_seconds": 5,
        }


@dataclass(frozen=True)
class SmokeConfig:
    live_enabled: bool
    confirm_submit: bool
    cases: tuple[SmokeCase, ...]

    def case(self, smoke_id: str) -> SmokeCase:
        for case in self.cases:
            if case.smoke_id == smoke_id:
                return case
        raise KeyError(smoke_id)

    @property
    def configured_live_cases(self) -> tuple[SmokeCase, ...]:
        return tuple(case for case in self.cases if case.configured_from_env)


SMOKE_TARGETS: tuple[SmokeTarget, ...] = (
    SmokeTarget(
        smoke_id="my_information_state_street",
        matrix="my_information",
        company="State Street",
        job_url_env="WORKDAY_SMOKE_STATE_STREET_URL",
        default_job_url="https://example.invalid/workday-smoke/state-street",
        stage="MY_INFORMATION",
        required_checks=(
            "country_us_only",
            "country_phone_code_us",
            "phone_device_type_verified",
            "how_heard_handled",
            "previously_worked_trusted_only",
            "phone_extension_optional_ignored",
            "unsupported_required_controls_structured_blocker",
            "blocker_screenshot_trace_on_failure",
        ),
    ),
    SmokeTarget(
        smoke_id="my_information_cdw",
        matrix="my_information",
        company="CDW",
        job_url_env="WORKDAY_SMOKE_CDW_URL",
        default_job_url="https://example.invalid/workday-smoke/cdw",
        stage="MY_INFORMATION",
        required_checks=(
            "country_us_only",
            "country_phone_code_us",
            "phone_device_type_verified",
            "how_heard_handled",
            "previously_worked_trusted_only",
            "phone_extension_optional_ignored",
            "unsupported_required_controls_structured_blocker",
            "blocker_screenshot_trace_on_failure",
        ),
    ),
    SmokeTarget(
        smoke_id="my_information_stord",
        matrix="my_information",
        company="Stord",
        job_url_env="WORKDAY_SMOKE_STORD_URL",
        default_job_url="https://example.invalid/workday-smoke/stord",
        stage="MY_INFORMATION",
        required_checks=(
            "country_us_only",
            "country_phone_code_us",
            "phone_device_type_verified",
            "how_heard_handled",
            "previously_worked_trusted_only",
            "phone_extension_optional_ignored",
            "unsupported_required_controls_structured_blocker",
            "blocker_screenshot_trace_on_failure",
        ),
    ),
    SmokeTarget(
        smoke_id="experience_education_northrop",
        matrix="experience_education",
        company="Northrop",
        job_url_env="WORKDAY_SMOKE_NORTHROP_URL",
        default_job_url="https://example.invalid/workday-smoke/northrop",
        stage="MY_EXPERIENCE",
        required_checks=(
            "work_experience_complete_or_blocked",
            "school_text_alone_not_success",
            "school_committed_token_required",
            "uiuc_university_of_illinois_urbana_champaign",
            "to_be_reviewed_rejected_when_trusted_exists",
            "degree_select_one_rejected",
            "end_year_yyyy_rejected",
            "resume_upload_exact_marker",
            "blocker_screenshot_trace_on_failure",
        ),
    ),
    SmokeTarget(
        smoke_id="experience_education_hp",
        matrix="experience_education",
        company="HP",
        job_url_env="WORKDAY_SMOKE_HP_EXPERIENCE_URL",
        default_job_url="https://example.invalid/workday-smoke/hp-experience",
        stage="MY_EXPERIENCE",
        required_checks=(
            "work_experience_complete_or_blocked",
            "school_text_alone_not_success",
            "school_committed_token_required",
            "uiuc_university_of_illinois_urbana_champaign",
            "to_be_reviewed_rejected_when_trusted_exists",
            "degree_select_one_rejected",
            "end_year_yyyy_rejected",
            "resume_upload_exact_marker",
            "blocker_screenshot_trace_on_failure",
        ),
    ),
    SmokeTarget(
        smoke_id="application_questions_hp",
        matrix="application_questions",
        company="HP",
        job_url_env="WORKDAY_SMOKE_HP_QUESTIONS_URL",
        default_job_url="https://example.invalid/workday-smoke/hp-questions",
        stage="APPLICATION_QUESTIONS",
        required_checks=(
            "start_date_maps_to_application_questions_start_date",
            "month_day_year_filled_correctly",
            "full_date_not_put_in_month",
            "select_one_required_not_final_question_text",
            "authorized_to_work_trusted_only",
            "sponsorship_trusted_only",
            "conflict_export_control_non_compete_blocks_without_trusted_answer",
            "unknown_required_blocks_without_trusted_answer",
            "sensitive_prefill_does_not_bypass_gate",
            "no_unsafe_first_option_fallback",
            "blocker_screenshot_trace_on_failure",
        ),
    ),
    SmokeTarget(
        smoke_id="auth_boeing_registered",
        matrix="auth_registered_account",
        company="Boeing",
        job_url_env="WORKDAY_SMOKE_BOEING_REGISTERED_URL",
        default_job_url="https://example.invalid/workday-smoke/boeing-registered",
        stage="AUTH",
        required_checks=(
            "recognizes_existing_account_sign_in",
            "does_not_create_duplicate_account",
            "saved_credential_only_if_trusted",
            "auth_blocked_without_trusted_credential",
            "blocker_screenshot_trace_on_failure",
        ),
    ),
)


def is_live_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return str(source.get(LIVE_SMOKE_ENV, "")).strip() == "1"


def _confirm_submit_enabled(env: Mapping[str, str]) -> bool:
    return str(env.get("WORKDAY_LIVE_SMOKE_CONFIRM_SUBMIT", "")).strip().lower() in {"1", "true", "yes", "on"}


def load_smoke_config(env: Mapping[str, str] | None = None) -> SmokeConfig:
    source = os.environ if env is None else env
    confirm_submit = _confirm_submit_enabled(source)
    cases = []
    for target in SMOKE_TARGETS:
        configured_url = str(source.get(target.job_url_env, "")).strip()
        required_checks = tuple(dict.fromkeys((*COMMON_REQUIRED_CHECKS, *target.required_checks)))
        cases.append(
            SmokeCase(
                smoke_id=target.smoke_id,
                matrix=target.matrix,
                company=target.company,
                job_url=configured_url or target.default_job_url,
                job_url_env=target.job_url_env,
                stage=target.stage,
                required_checks=required_checks,
                confirm_submit=confirm_submit,
                configured_from_env=bool(configured_url),
            )
        )
    return SmokeConfig(
        live_enabled=is_live_enabled(source),
        confirm_submit=confirm_submit,
        cases=tuple(cases),
    )


def terminal_label(run: Mapping[str, Any]) -> str:
    status = str(run.get("status") or "")
    outcome = str(run.get("outcome") or "")
    if status == "complete" and outcome == "pre_submit_review":
        return "pre_submit_review"
    if outcome in ALLOWED_TERMINAL_LABELS:
        return outcome
    return status


def final_submit_clicked(run: Mapping[str, Any]) -> bool:
    if str(run.get("outcome") or "") == "SUBMITTED":
        return True
    for event in run.get("trace_events") or []:
        action = str(event.get("action") or "").lower()
        if action == "skip_final_submit":
            continue
        if "submit" in action and ("final" in action or "click" in action):
            return True
    action = str(run.get("last_action") or "").lower()
    return "submit" in action and "click" in action and action != "skip_final_submit"


def validate_terminal_run(run: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    status = str(run.get("status") or "")
    label = terminal_label(run)
    if status not in ALLOWED_RUN_STATUSES:
        issues.append(f"unexpected run status: {status}")
    if label not in ALLOWED_TERMINAL_LABELS:
        issues.append(f"unexpected terminal label: {label}")
    if not run.get("confirm_submit") and final_submit_clicked(run):
        issues.append("final submit clicked while confirm_submit=false")
    if label in BLOCKING_TERMINAL_LABELS:
        if not run.get("blocker"):
            issues.append("blocking terminal run missing blocker")
        if not run.get("screenshot_path"):
            issues.append("blocking terminal run missing screenshot_path")
        if not run.get("trace_events"):
            issues.append("blocking terminal run missing trace_events")
    return issues


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def smoke_evidence(run: Mapping[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {}

    def merge(candidate: Any) -> None:
        if isinstance(candidate, dict):
            evidence.update(candidate)

    merge(run.get("smoke_evidence"))
    merge(run.get("evidence"))
    blocker = _as_dict(run.get("blocker"))
    merge(blocker.get("smoke_evidence"))
    merge(blocker.get("evidence"))
    for event in run.get("trace_events") or []:
        if not isinstance(event, dict):
            continue
        merge(event.get("smoke_evidence"))
        merge(event.get("evidence"))
        event_blocker = _as_dict(event.get("blocker"))
        merge(event_blocker.get("smoke_evidence"))
        merge(event_blocker.get("evidence"))
    return evidence


def _evidence_verified(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, dict):
        return value.get("verified") is True or value.get("passed") is True
    return False


def _missing_evidence(check: str) -> list[str]:
    return [f"missing smoke evidence for {check}"]


def _failed_check(check: str, reason: str) -> list[str]:
    return [f"smoke requirement failed for {check}: {reason}"]


def _generic_evidence_check(_case: SmokeCase, _run: Mapping[str, Any], evidence: Mapping[str, Any], check: str) -> list[str]:
    value = evidence.get(check)
    if value is None:
        return _missing_evidence(check)
    if _evidence_verified(value):
        return []
    return _failed_check(check, sanitize_smoke_text(value) or "evidence was not verified")


def _no_final_submit_check(_case: SmokeCase, run: Mapping[str, Any], _evidence: Mapping[str, Any], check: str) -> list[str]:
    if final_submit_clicked(run):
        return _failed_check(check, "final submit was clicked")
    return []


def _blocker_screenshot_trace_check(
    _case: SmokeCase,
    run: Mapping[str, Any],
    _evidence: Mapping[str, Any],
    check: str,
) -> list[str]:
    if terminal_label(run) not in BLOCKING_TERMINAL_LABELS:
        return []
    issues = []
    if not run.get("blocker"):
        issues.append("blocker")
    if not run.get("screenshot_path"):
        issues.append("screenshot_path")
    if not run.get("trace_events"):
        issues.append("trace_events")
    return _failed_check(check, f"missing {', '.join(issues)}") if issues else []


def _start_date_evidence(evidence: Mapping[str, Any], check: str) -> dict[str, Any]:
    value = evidence.get(check)
    if isinstance(value, dict):
        return value
    value = evidence.get("start_date")
    return value if isinstance(value, dict) else {}


def _full_date_not_put_in_month_check(
    _case: SmokeCase,
    _run: Mapping[str, Any],
    evidence: Mapping[str, Any],
    check: str,
) -> list[str]:
    value = _start_date_evidence(evidence, check)
    if not value:
        return _missing_evidence(check)
    actual_parts = _as_dict(value.get("actual_parts") or value.get("parts"))
    month = str(value.get("month") or value.get("month_value") or actual_parts.get("month") or "")
    full_date = str(value.get("full_date") or value.get("expected_full_date") or "")
    if not month:
        return _failed_check(check, "month value evidence is missing")
    if full_date and month == full_date:
        return _failed_check(check, "month contains full date")
    if "/" in month:
        return _failed_check(check, "month contains a slash-delimited date")
    if value.get("verified") is False:
        return _failed_check(check, "evidence explicitly marked unverified")
    return []


def _month_day_year_filled_check(
    _case: SmokeCase,
    _run: Mapping[str, Any],
    evidence: Mapping[str, Any],
    check: str,
) -> list[str]:
    value = _start_date_evidence(evidence, check)
    if not value:
        return _missing_evidence(check)
    actual = _as_dict(value.get("actual_parts") or value.get("parts"))
    expected = _as_dict(value.get("expected_parts")) or {"month": "12", "day": "15", "year": "2026"}
    for part in ("month", "day", "year"):
        if str(actual.get(part) or "") != str(expected.get(part) or ""):
            return _failed_check(check, f"{part} expected {expected.get(part)!r}, got {actual.get(part)!r}")
    return []


def _resume_upload_exact_marker_check(
    _case: SmokeCase,
    _run: Mapping[str, Any],
    evidence: Mapping[str, Any],
    check: str,
) -> list[str]:
    value = evidence.get(check)
    if value is None:
        return _missing_evidence(check)
    if value is True:
        return []
    if not isinstance(value, dict):
        return _failed_check(check, sanitize_smoke_text(value) or "invalid evidence")
    if value.get("verified") is True or value.get("exact_marker_match") is True:
        return []
    expected = str(value.get("expected_marker") or value.get("expected_filename") or "")
    observed = str(value.get("observed_marker") or value.get("filename_evidence") or value.get("observed_filename") or "")
    if not observed:
        return _failed_check(check, "resume marker or filename evidence is missing")
    if expected and observed != expected:
        return _failed_check(check, f"expected exact marker {expected!r}, got {observed!r}")
    return _failed_check(check, "resume upload evidence was not verified")


GENERIC_EVIDENCE_CHECKS = {
    "authorized_to_work_trusted_only",
    "conflict_export_control_non_compete_blocks_without_trusted_answer",
    "country_phone_code_us",
    "country_us_only",
    "degree_select_one_rejected",
    "does_not_create_duplicate_account",
    "end_year_yyyy_rejected",
    "how_heard_handled",
    "phone_device_type_verified",
    "phone_extension_optional_ignored",
    "previously_worked_trusted_only",
    "recognizes_existing_account_sign_in",
    "saved_credential_only_if_trusted",
    "school_committed_token_required",
    "school_text_alone_not_success",
    "select_one_required_not_final_question_text",
    "sensitive_prefill_does_not_bypass_gate",
    "sponsorship_trusted_only",
    "start_date_maps_to_application_questions_start_date",
    "to_be_reviewed_rejected_when_trusted_exists",
    "uiuc_university_of_illinois_urbana_champaign",
    "unknown_required_blocks_without_trusted_answer",
    "unsupported_required_controls_structured_blocker",
    "auth_blocked_without_trusted_credential",
    "no_unsafe_first_option_fallback",
    "work_experience_complete_or_blocked",
}
SMOKE_CHECK_VALIDATORS: dict[str, Callable[[SmokeCase, Mapping[str, Any], Mapping[str, Any], str], list[str]]] = {
    **{check: _generic_evidence_check for check in GENERIC_EVIDENCE_CHECKS},
    "blocker_screenshot_trace_on_failure": _blocker_screenshot_trace_check,
    "full_date_not_put_in_month": _full_date_not_put_in_month_check,
    "month_day_year_filled_correctly": _month_day_year_filled_check,
    "no_final_submit": _no_final_submit_check,
    "resume_upload_exact_marker": _resume_upload_exact_marker_check,
}


def validate_smoke_requirements(case: SmokeCase, run: Mapping[str, Any]) -> list[str]:
    issues = validate_terminal_run(run)
    evidence = smoke_evidence(run)
    for check in case.required_checks:
        validator = SMOKE_CHECK_VALIDATORS.get(check)
        if validator is None:
            issues.append(f"no smoke requirement validator for {check}")
            continue
        issues.extend(validator(case, run, evidence, check))
    return issues


def smoke_summary(case: SmokeCase, run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "company": case.company,
        "job_url": case.job_url,
        "run_id": run.get("run_id") or "",
        "terminal_status": terminal_label(run),
        "stage": run.get("stage") or "",
        "current_url": run.get("current_url") or "",
        "last_action": run.get("last_action") or "",
        "last_field": run.get("last_field") or "",
        "outcome": run.get("outcome") or "",
        "blocker": run.get("blocker") or "",
        "screenshot_path": run.get("screenshot_path") or "",
        "trace_event_count": len(run.get("trace_events") or []),
        "confirm_submit": bool(run.get("confirm_submit")),
        "final_submit_clicked": "yes" if final_submit_clicked(run) else "no",
    }


class SmokeMatrixHarness:
    def __init__(
        self,
        config: SmokeConfig | None = None,
        *,
        service: ApplyRunService | None = None,
        artifact_root: str | Path | None = None,
        runner_factory: Callable[[SmokeCase], Any] | None = None,
        final_submit_callback: Callable[[ApplyRunContext, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.config = config or load_smoke_config()
        self.service = service or ApplyRunService(
            artifact_root=artifact_root or Path("artifacts") / "workday-live-smoke",
            final_submit_callback=final_submit_callback,
        )
        self.runner_factory = runner_factory

    def case(self, smoke_id: str) -> SmokeCase:
        return self.config.case(smoke_id)

    def create_run(self, case_or_id: SmokeCase | str, extra_payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        case = self.case(case_or_id) if isinstance(case_or_id, str) else case_or_id
        payload = case.payload()
        payload.update(dict(extra_payload or {}))
        payload["confirm_submit"] = bool(payload.get("confirm_submit", False))
        if "stage_runners" not in payload and self.runner_factory is not None:
            payload["stage_runners"] = [self.runner_factory(case)]
        return self.service.create_run(payload)

    def poll(self, run_id: str) -> dict[str, Any]:
        return self.service.get_run(run_id)

    def wait_for_terminal(self, run_id: str, timeout: float = 30.0) -> dict[str, Any]:
        return self.service.wait_for_terminal(run_id, timeout=timeout)

    def summarize(self, case_or_id: SmokeCase | str, run: Mapping[str, Any]) -> dict[str, Any]:
        case = self.case(case_or_id) if isinstance(case_or_id, str) else case_or_id
        return smoke_summary(case, run)


class SanitizedSmokePage:
    def __init__(self, url: str) -> None:
        self.url = url

    def screenshot(self, path: str, **_kwargs: Any) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"sanitized smoke screenshot")

    def close(self) -> None:
        return None


class SanitizedSmokeRunner:
    def __init__(
        self,
        case: SmokeCase,
        *,
        terminal: str = "pre_submit_review",
        hold_seconds: float = 0.0,
    ) -> None:
        self.case = case
        self.stage = case.stage
        self.terminal = terminal
        self.hold_seconds = hold_seconds

    def run(self, ctx: ApplyRunContext, payload: dict[str, Any]) -> dict[str, Any]:
        page = SanitizedSmokePage(str(payload.get("job_url") or self.case.job_url))
        evidence = self._smoke_evidence()
        ctx.set_page(page)
        ctx.update(
            stage=self.case.stage,
            current_url=page.url,
            last_action=f"sanitized_{self.case.matrix}_observe",
            last_field=self._last_field(),
            blocker={"smoke_id": self.case.smoke_id, "smoke_evidence": evidence},
            trace=True,
            message="sanitized live smoke runner started",
        )
        deadline = time.monotonic() + max(0.0, self.hold_seconds)
        while time.monotonic() < deadline:
            ctx.check_canceled()
            time.sleep(0.01)
        if self.terminal == "pre_submit_review":
            return {"ready_to_submit": True, "outcome": "READY_TO_SUBMIT", "message": "sanitized ready for review"}
        if self.terminal in BLOCKING_TERMINAL_LABELS:
            return {
                "status": "blocked",
                "outcome": self.terminal,
                "stage": self.case.stage,
                "current_url": page.url,
                "last_action": f"sanitized_{self.case.matrix}_blocked",
                "last_field": self._last_field(),
                "blocker": {
                    "reason": self.terminal,
                    "company": self.case.company,
                    "smoke_id": self.case.smoke_id,
                    "smoke_evidence": evidence,
                },
            }
        return {
            "status": "complete",
            "outcome": "COMPLETE",
            "stage": self.case.stage,
            "current_url": page.url,
            "last_action": f"sanitized_{self.case.matrix}_complete",
            "last_field": self._last_field(),
        }

    def _last_field(self) -> str:
        by_matrix = {
            "my_information": "phone_device_type",
            "experience_education": "education.school",
            "application_questions": "application_questions.start_date",
            "auth_registered_account": "auth.sign_in",
        }
        return by_matrix.get(self.case.matrix, "workday.smoke")

    def _smoke_evidence(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        for check in self.case.required_checks:
            evidence[check] = {"verified": True, "source": "sanitized_runner"}
        evidence["start_date"] = {
            "verified": True,
            "full_date": "12/15/2026",
            "expected_parts": {"month": "12", "day": "15", "year": "2026"},
            "actual_parts": {"month": "12", "day": "15", "year": "2026"},
        }
        evidence["full_date_not_put_in_month"] = {
            "verified": True,
            "full_date": "12/15/2026",
            "month": "12",
        }
        evidence["month_day_year_filled_correctly"] = {
            "verified": True,
            "expected_parts": {"month": "12", "day": "15", "year": "2026"},
            "actual_parts": {"month": "12", "day": "15", "year": "2026"},
        }
        evidence["unknown_required_blocks_without_trusted_answer"] = {
            "verified": True,
            "outcome": "BLOCKED_ON_QUESTIONS",
        }
        evidence["sensitive_prefill_does_not_bypass_gate"] = {
            "verified": True,
            "outcome": "BLOCKED_ON_QUESTIONS",
        }
        evidence["resume_upload_exact_marker"] = {
            "verified": True,
            "expected_marker": "resume-smoke.pdf",
            "observed_marker": "resume-smoke.pdf",
        }
        return evidence


def sanitized_runner_factory(
    terminal_by_smoke_id: Mapping[str, str] | None = None,
    *,
    default_terminal: str = "pre_submit_review",
    hold_seconds: float = 0.0,
) -> Callable[[SmokeCase], SanitizedSmokeRunner]:
    terminals = dict(terminal_by_smoke_id or {})

    def factory(case: SmokeCase) -> SanitizedSmokeRunner:
        return SanitizedSmokeRunner(
            case,
            terminal=terminals.get(case.smoke_id, default_terminal),
            hold_seconds=hold_seconds,
        )

    return factory


def live_access_runner_factory() -> Callable[[SmokeCase], "LiveAccessApplyStageRunner"]:
    return lambda case: LiveAccessApplyStageRunner(case)


def _env_flag(source: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = source.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def live_access_request_options(
    case: SmokeCase,
    payload: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    source = os.environ if env is None else env
    payload = payload or {}
    allow_resume_upload = (
        _env_flag(source, "WORKDAY_LIVE_SMOKE_ALLOW_RESUME_UPLOAD")
        and bool(payload.get("resume_upload_smoke") or "resume_upload_exact_marker" in case.required_checks)
    )
    return {
        "confirm_submit": False,
        "probe_fill_unapproved_questions": _env_flag(source, "WORKDAY_LIVE_SMOKE_PROBE_UNAPPROVED_QUESTIONS"),
        "allow_visual_fallback": _env_flag(source, "WORKDAY_LIVE_SMOKE_ALLOW_VISUAL_FALLBACK"),
        "allow_visual_field_fallback": _env_flag(source, "WORKDAY_LIVE_SMOKE_ALLOW_VISUAL_FIELD_FALLBACK"),
        "allow_resume_upload": allow_resume_upload,
    }


class LiveAccessApplyStageRunner:
    def __init__(self, case: SmokeCase) -> None:
        self.case = case
        self.stage = case.stage

    def run(self, ctx: ApplyRunContext, payload: dict[str, Any]) -> dict[str, Any]:
        ctx.update(
            stage="NAVIGATION",
            current_url=self.case.job_url,
            last_action="live_smoke_access_apply_start",
            last_field=self.case.smoke_id,
            trace=True,
            message="live Workday smoke started",
        )
        from mcp_servers.playwright_server import AccessApplyRequest, access_apply_form

        safe_options = live_access_request_options(self.case, payload)
        request = AccessApplyRequest(
            url=self.case.job_url,
            user_data=dict(payload.get("user_data") or payload.get("profile") or {}),
            application_id=str(payload.get("application_id") or self.case.smoke_id),
            batch_id=str(payload.get("batch_id") or "workday-live-smoke"),
            resume_path=payload.get("resume_path"),
            max_steps=int(payload.get("max_steps") or 10),
            max_form_pages=int(payload.get("max_form_pages") or 8),
            stop_at_form=False,
            discover_all_steps=False,
            allow_low_risk_autofill=True,
            allow_placeholder_autofill=False,
            allow_visual_fallback=safe_options["allow_visual_fallback"],
            allow_visual_field_fallback=safe_options["allow_visual_field_fallback"],
            allow_resume_upload=safe_options["allow_resume_upload"],
            probe_fill_unapproved_questions=safe_options["probe_fill_unapproved_questions"],
            confirm_submit=False,
        )
        result = access_apply_form(request)
        current_url = str(result.get("current_url") or self.case.job_url)
        stage = _stage_from_result(result, self.case.stage)
        outcome = _outcome_from_access_result(result, self.case)
        ctx.update(
            stage=stage,
            current_url=current_url,
            last_action="live_smoke_access_apply_done",
            last_field=self.case.smoke_id,
            outcome=outcome,
            blocker=result.get("blocked_reason") or result.get("needs_user_action") or "",
            screenshot_path=result.get("screenshot_path") or "",
            trace=True,
            message=str(result.get("status") or outcome),
        )
        if outcome == "READY_TO_SUBMIT":
            return {"ready_to_submit": True, "outcome": "READY_TO_SUBMIT"}
        return {
            "status": "blocked",
            "outcome": outcome,
            "stage": stage,
            "current_url": current_url,
            "last_action": "live_smoke_access_apply_done",
            "last_field": self.case.smoke_id,
            "blocker": {
                "reason": result.get("blocked_reason") or outcome,
                "needs_user_action": result.get("needs_user_action") or "",
                "status": result.get("status") or "",
                "smoke_id": self.case.smoke_id,
            },
            "screenshot_path": result.get("screenshot_path") or "",
        }


def _stage_from_result(result: Mapping[str, Any], default: str) -> str:
    raw = str(result.get("stage") or "").upper()
    if "QUESTION" in raw:
        return "APPLICATION_QUESTIONS"
    if "EXPERIENCE" in raw or "EDUCATION" in raw:
        return "MY_EXPERIENCE"
    if "INFO" in raw:
        return "MY_INFORMATION"
    if "AUTH" in raw or "SIGN" in raw or "LOGIN" in raw:
        return "AUTH"
    if "REVIEW" in raw:
        return "REVIEW"
    return default


def _outcome_from_access_result(result: Mapping[str, Any], case: SmokeCase) -> str:
    outcome = str(result.get("outcome_type") or "").strip()
    if outcome in ALLOWED_TERMINAL_LABELS or outcome == "READY_TO_SUBMIT":
        return outcome
    status = str(result.get("status") or "").lower()
    blocked_reason = str(result.get("blocked_reason") or result.get("needs_user_action") or "").lower()
    if result.get("success") and "final_submit_confirmation_required" in blocked_reason:
        return "READY_TO_SUBMIT"
    if result.get("success") and ("review" in status or "form_detected" in status):
        return "READY_TO_SUBMIT"
    if "auth" in blocked_reason or "credential" in blocked_reason or "sign" in blocked_reason:
        return "AUTH_BLOCKED"
    if "loading" in blocked_reason:
        return "WORKDAY_LOADING_STUCK"
    if case.matrix == "my_information":
        return "MY_INFORMATION_BLOCKED"
    if case.matrix == "experience_education":
        return "MY_EXPERIENCE_BLOCKED"
    if case.matrix == "application_questions":
        return "BLOCKED_ON_QUESTIONS"
    if case.matrix == "auth_registered_account":
        return "AUTH_BLOCKED"
    return "BLOCKED_ON_QUESTIONS"


def redacted_env_report(env: Mapping[str, str] | None = None) -> dict[str, bool]:
    source = os.environ if env is None else env
    return {
        target.job_url_env: bool(str(source.get(target.job_url_env, "")).strip())
        for target in SMOKE_TARGETS
    }


def sanitize_smoke_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"https?://[^\s]+", "[REDACTED_URL]", text)
    text = re.sub(r"[^a-zA-Z0-9_.:/#?=& -]+", "", text)
    return text[:500]
