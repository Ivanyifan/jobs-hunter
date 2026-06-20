from copy import deepcopy
from urllib.parse import quote

import requests


BLOCKED_ON_QUESTIONS = "BLOCKED_ON_QUESTIONS"
NEEDS_TECHNICAL_REVIEW = "NEEDS_TECHNICAL_REVIEW"
READY_TO_RESUME = "READY_TO_RESUME"
READY_TO_SUBMIT = "READY_TO_SUBMIT"
QUESTION_BLOCKER_WORKFLOW_STATUSES = {BLOCKED_ON_QUESTIONS, NEEDS_TECHNICAL_REVIEW}
NORMAL_APPLY_RUNNABLE_STATUSES = {"Queued", "Applying", READY_TO_RESUME}


def mongo_url_from_config(config):
    return (config.get("mongo_url") or "http://localhost:8001").rstrip("/")


def safe_path(value):
    return quote(str(value or ""), safe="")


def can_start_apply(status):
    return status in NORMAL_APPLY_RUNNABLE_STATUSES


def can_confirm_submit(status):
    return status == READY_TO_SUBMIT


def enable_application_question_matcher(user_data):
    data = dict(user_data or {})
    matcher_config = dict(data.get("application_question_config") or {})
    matcher_config.setdefault("enable_llm_library_matcher", True)
    matcher_config.setdefault("enable_llm_library_matcher_for_sensitive_questions", True)
    matcher_config.setdefault("max_llm_match_calls_per_application", 8)
    data["application_question_config"] = matcher_config
    return data


def build_playwright_apply_payload(apply_url, fallback_url, resume_path, user_data, application_id, batch_id, confirm_submit=False):
    payload = {
        "url": apply_url if apply_url else fallback_url,
        "resume_path": resume_path,
        "user_data": user_data,
        "application_id": application_id,
        "batch_id": batch_id,
    }
    if confirm_submit:
        payload["confirm_submit"] = True
    return payload


def is_question_blocker_apply_response(res_data):
    if not isinstance(res_data, dict):
        return False
    if res_data.get("status") in QUESTION_BLOCKER_WORKFLOW_STATUSES:
        return True
    if res_data.get("blocked_reason") == "application_question_blocker":
        return True
    return bool(res_data.get("question_blocker"))


def workflow_status_from_apply_response(res_data):
    if not is_question_blocker_apply_response(res_data):
        return None
    if res_data.get("status") in QUESTION_BLOCKER_WORKFLOW_STATUSES:
        return res_data["status"]
    question_blocker = res_data.get("question_blocker")
    if isinstance(question_blocker, dict) and question_blocker.get("status") in QUESTION_BLOCKER_WORKFLOW_STATUSES:
        return question_blocker["status"]
    return BLOCKED_ON_QUESTIONS


def summarize_playwright_response(res_data):
    if not isinstance(res_data, dict):
        return {}
    return {
        key: res_data.get(key)
        for key in ("success", "status", "blocked_reason", "error")
        if key in res_data
    }


def is_ready_to_submit_apply_response(res_data):
    if not isinstance(res_data, dict):
        return False
    if res_data.get("status") == READY_TO_SUBMIT:
        return True
    return res_data.get("blocked_reason") in {"final_submit_confirmation_required", "final_submit_guard"}


def apply_failure_metadata(company, role, apply_url, res_data):
    metadata = {
        "company": company,
        "role": role,
        "apply_url": apply_url,
        "blocked_reason": res_data.get("blocked_reason") if isinstance(res_data, dict) else None,
        "question_blocker": res_data.get("question_blocker") if isinstance(res_data, dict) else None,
        "missing_required": res_data.get("missing_required") if isinstance(res_data, dict) else None,
        "blocking_issues": res_data.get("blocking_issues") if isinstance(res_data, dict) else None,
        "playwright_response": summarize_playwright_response(res_data),
    }
    if isinstance(res_data, dict) and res_data.get("error"):
        metadata["error"] = res_data.get("error")
    return metadata


def record_playwright_apply_failure(config, db_connection_factory, sync_mongo_status_func, app_id, company, role, apply_url, res_data):
    workflow_status = workflow_status_from_apply_response(res_data)
    is_blocker = workflow_status in QUESTION_BLOCKER_WORKFLOW_STATUSES
    is_ready_to_submit = is_ready_to_submit_apply_response(res_data)
    if is_blocker:
        next_status = workflow_status
        reason = "playwright_application_question_blocker"
    elif is_ready_to_submit:
        next_status = READY_TO_SUBMIT
        reason = "playwright_ready_to_submit"
    else:
        next_status = "Queued"
        reason = "playwright_apply_failed"

    conn = db_connection_factory()
    cursor = conn.cursor()
    cursor.execute("UPDATE mcp_applications SET status = ? WHERE id = ?", (next_status, app_id))
    conn.commit()
    conn.close()

    sync_mongo_status_func(
        config,
        app_id,
        next_status,
        reason=reason,
        metadata=apply_failure_metadata(company, role, apply_url, res_data),
    )

    if next_status == BLOCKED_ON_QUESTIONS:
        message = "\u7533\u8bf7\u5df2\u6682\u505c\uff1a\u9700\u8981\u56de\u7b54\u7533\u8bf7\u95ee\u9898\u3002\u8bf7\u5230\u300c\u5f85\u56de\u7b54\u95ee\u9898\u300dtab \u5904\u7406\u3002"
    elif next_status == NEEDS_TECHNICAL_REVIEW:
        message = "\u7533\u8bf7\u5df2\u6682\u505c\uff1a\u9700\u8981\u6280\u672f\u68c0\u67e5\u3002\u8bf7\u67e5\u770b\u5f85\u56de\u7b54\u95ee\u9898\u4e2d\u7684 TECHNICAL_REVIEW \u9879\u3002"
    elif next_status == READY_TO_SUBMIT:
        message = "申请已到最终提交前：需要你确认后才能提交。"
    else:
        message = None
    return {
        "status": next_status,
        "reason": reason,
        "is_question_blocker": is_blocker,
        "is_ready_to_submit": is_ready_to_submit,
        "message": message,
        "status_label": "\u5df2\u6682\u505c\uff0c\u7b49\u5f85\u4eba\u5de5\u5904\u7406" if (is_blocker or is_ready_to_submit) else "\u6295\u9012\u5931\u8d25",
    }


def _call_memory(config, method, path, payload=None, timeout=6):
    response = requests.request(
        method,
        f"{mongo_url_from_config(config)}{path}",
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        return None
    return response.json() if response.text else {}


def _memory_call(call_memory_func, config, method, path, payload=None, timeout=6):
    if call_memory_func:
        return call_memory_func(config, method, path, payload=payload, timeout=timeout)
    return _call_memory(config, method, path, payload=payload, timeout=timeout)


def stable_question_answer_keys(blocker):
    keys = []
    for key in ["fingerprint", "normalized_text", "raw_text", "canonical_key"]:
        value = blocker.get(key)
        if value and value not in keys:
            keys.append(value)
    return keys


def approved_question_answers_for_application(config, application_id, call_memory_func=None):
    answers = {}
    limit = 100
    offset = 0
    while True:
        path = (
            f"/question-blockers?application_id={safe_path(application_id)}"
            f"&status=APPROVED&limit={limit}&offset={offset}"
        )
        page = _memory_call(call_memory_func, config, "GET", path, timeout=6) or {}
        blockers = page.get("blockers") or []
        for blocker in blockers:
            answer = blocker.get("approved_answer")
            if answer in (None, ""):
                continue
            for key in stable_question_answer_keys(blocker):
                answers[key] = answer
        pagination = page.get("pagination") or {}
        if not pagination.get("has_more"):
            break
        offset += int(pagination.get("limit") or limit)
    return answers


def enrich_user_data_with_approved_question_answers(config, application_id, user_data, call_memory_func=None):
    enriched = deepcopy(user_data or {})
    approved_answers = approved_question_answers_for_application(
        config,
        application_id,
        call_memory_func=call_memory_func,
    )
    if not approved_answers:
        return enriched
    for key in ["approved_question_answers", "question_blocker_answers"]:
        existing = enriched.get(key)
        if not isinstance(existing, dict):
            existing = {}
        merged = dict(existing)
        merged.update(approved_answers)
        enriched[key] = merged
    enriched["approved_question_answer_count"] = len(approved_answers)
    return enriched
