from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import traceback

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from frontend.apply_flow import apply_application_profile_library, enable_application_question_matcher


HP_URL = "https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/job/Browser-Software-Engineer-Intern_3160410-1"
DEFAULT_RESUME = r"C:\Users\li\Downloads\YIFAN LI_Software Engineer 2_20260521.pdf"


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def safe_account_summary(account: dict) -> dict:
    allowed = {
        "tenant",
        "host",
        "email",
        "account_exists",
        "account_created",
        "auth_strategy",
        "password_source",
        "login_attempt_count",
        "last_auth_action",
        "source",
    }
    return {key: account.get(key) for key in allowed if key in (account or {})}


def resume_autofill_diagnostics(data: dict) -> dict:
    return {
        "resume_upload": data.get("resume_upload"),
        "autofill_resume_wait": data.get("autofill_resume_wait"),
        "blank_step_recovery": data.get("blank_step_recovery"),
        "original_job_url": data.get("original_job_url"),
        "dom_excerpt": data.get("dom_excerpt"),
    }


def classify_outcome_type(data: dict) -> str | None:
    if data.get("outcome_type"):
        return data.get("outcome_type")
    if "autofillwithresume" in str(data.get("current_url") or "").lower():
        return "AUTOFILL_RESUME_STUCK"
    return None


def classify_blocked_reason(data: dict) -> str | None:
    if data.get("blocked_reason"):
        return data.get("blocked_reason")
    if classify_outcome_type(data) == "AUTOFILL_RESUME_STUCK":
        return "workday_autofill_resume_stuck"
    return None


def build_access_apply_payload(args, user_data: dict) -> dict:
    payload = {
        "url": args.url,
        "user_data": user_data,
        "resume_path": args.resume_path,
        "application_id": args.application_id,
        "batch_id": args.batch_id,
        "max_steps": 16,
        "wait_for_email_seconds": 30,
        "allow_account_creation": True,
        "allow_email_verification": True,
        "allow_terms_acceptance": True,
        "allow_security_question_autofill": True,
        "adjust_password_to_policy": True,
        "stop_at_form": False,
        "discover_all_steps": True,
        "max_form_pages": 8,
        "allow_low_risk_autofill": True,
        "allow_placeholder_autofill": False,
        "allow_visual_fallback": env_flag("WORKDAY_LIVE_SMOKE_ALLOW_VISUAL_FALLBACK"),
        "allow_visual_field_fallback": env_flag("WORKDAY_LIVE_SMOKE_ALLOW_VISUAL_FIELD_FALLBACK"),
        "allow_resume_upload": env_flag("WORKDAY_LIVE_SMOKE_ALLOW_RESUME_UPLOAD"),
        "prefer_manual_apply": True,
        "disable_resume_autofill_choice": True,
        "probe_fill_unapproved_questions": False,
        "confirm_submit": False,
    }
    return payload


def summarize_response(data: dict, http_status: int, elapsed: float, result_path: Path) -> dict:
    pages = []
    for page in (data.get("discovery") or {}).get("pages") or []:
        preflight = page.get("preflight") or {}
        question_blocker = preflight.get("question_blocker") or {}
        pages.append(
            {
                "stage": page.get("stage"),
                "field_count": len(page.get("fields") or []),
                "missing_required": preflight.get("missing_required") or [],
                "blocking_issues": preflight.get("blocking_issues") or [],
                "question_status": question_blocker.get("status"),
                "questions": [
                    q.get("raw_text") or q.get("normalized_text")
                    for q in (question_blocker.get("questions") or [])
                ],
            }
        )
    return {
        "http": http_status,
        "elapsed_seconds": round(elapsed, 1),
        "success": data.get("success"),
        "status": data.get("status"),
        "stage": data.get("stage"),
        "blocked_reason": classify_blocked_reason(data),
        "outcome_type": classify_outcome_type(data),
        "needs_user_action": data.get("needs_user_action"),
        "account": safe_account_summary(data.get("account") or {}),
        "resume_autofill": resume_autofill_diagnostics(data),
        "current_url": data.get("current_url"),
        "screenshot_path": data.get("screenshot_path"),
        "result_path": str(result_path),
        "pages": pages,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-path", required=True)
    parser.add_argument("--server-url", default="http://127.0.0.1:8004")
    parser.add_argument("--url", default=HP_URL)
    parser.add_argument("--company", default="")
    parser.add_argument("--role", default="")
    parser.add_argument("--resume-path", default=DEFAULT_RESUME)
    parser.add_argument("--timeout-seconds", type=int, default=720)
    parser.add_argument("--application-id", default="live-smoke-hp-background")
    parser.add_argument("--batch-id", default="live-smoke-hp-background")
    args = parser.parse_args()

    result_path = Path(args.result_path)
    result_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        config = json.loads((ROOT / "data" / "scheduler_config.json").read_text(encoding="utf-8"))
        user_data = dict(config.get("user_data") or {})
        user_data.setdefault("email", "sjjsqj@gmail.com")
        user_data["state"] = user_data.get("state") or "Illinois"
        user_data = apply_application_profile_library(user_data)
        user_data = enable_application_question_matcher(user_data)

        payload = build_access_apply_payload(args, user_data)
        if args.company:
            payload["user_data"]["company"] = args.company
        if args.role:
            payload["user_data"]["role"] = args.role

        print(json.dumps({"event": "worker_start", "url": args.url, "company": args.company, "role": args.role}, ensure_ascii=True), flush=True)
        start = time.time()
        response = requests.post(f"{args.server_url}/access-apply-form", json=payload, timeout=args.timeout_seconds)
        elapsed = time.time() - start
        data = response.json()
        result_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(
            "SUMMARY_JSON "
            + json.dumps(summarize_response(data, response.status_code, elapsed, result_path), ensure_ascii=True),
            flush=True,
        )
        return 0
    except Exception as exc:
        failure = {
            "success": False,
            "status": "worker_failed",
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }
        result_path.write_text(json.dumps(failure, indent=2, ensure_ascii=False), encoding="utf-8")
        print("ERROR_JSON " + json.dumps(failure, ensure_ascii=True), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
