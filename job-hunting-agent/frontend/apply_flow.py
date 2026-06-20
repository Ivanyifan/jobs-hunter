BLOCKED_ON_QUESTIONS = "BLOCKED_ON_QUESTIONS"
NEEDS_TECHNICAL_REVIEW = "NEEDS_TECHNICAL_REVIEW"
QUESTION_BLOCKER_WORKFLOW_STATUSES = {BLOCKED_ON_QUESTIONS, NEEDS_TECHNICAL_REVIEW}


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
    next_status = workflow_status if is_blocker else "Queued"
    reason = "playwright_application_question_blocker" if is_blocker else "playwright_apply_failed"

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
    else:
        message = None
    return {
        "status": next_status,
        "reason": reason,
        "is_question_blocker": is_blocker,
        "message": message,
        "status_label": "\u5df2\u6682\u505c\uff0c\u7b49\u5f85\u4eba\u5de5\u5904\u7406" if is_blocker else "\u6295\u9012\u5931\u8d25",
    }
