from copy import deepcopy
import json
import re
from urllib.parse import quote

import requests

try:
    from mcp_servers.soma_algorithm import TECH_ALIASES, extract_resume_evidence
except ImportError:
    try:
        from soma_algorithm import TECH_ALIASES, extract_resume_evidence
    except ImportError:
        TECH_ALIASES = {}
        extract_resume_evidence = None


BLOCKED_ON_QUESTIONS = "BLOCKED_ON_QUESTIONS"
NEEDS_TECHNICAL_REVIEW = "NEEDS_TECHNICAL_REVIEW"
READY_TO_RESUME = "READY_TO_RESUME"
READY_TO_SUBMIT = "READY_TO_SUBMIT"
QUESTION_BLOCKER_WORKFLOW_STATUSES = {BLOCKED_ON_QUESTIONS, NEEDS_TECHNICAL_REVIEW}
NORMAL_APPLY_RUNNABLE_STATUSES = {"Queued", "Applying", READY_TO_RESUME}
KEYWORD_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into",
    "is", "it", "of", "on", "or", "our", "that", "the", "their", "this", "to",
    "with", "you", "your", "we", "will", "work", "team", "role", "job", "using",
    "build", "building", "experience", "required", "requirements", "preferred",
    "responsibilities", "skills", "years", "candidate", "engineer", "engineering",
}
SKILL_DISPLAY_NAMES = {
    "aws": "AWS",
    "azure": "Azure",
    "ci_cd": "CI/CD",
    "cpp": "C++",
    "fastapi": "FastAPI",
    "gcp": "GCP",
    "graphql": "GraphQL",
    "grpc": "gRPC",
    "javascript": "JavaScript",
    "jwt": "JWT",
    "llm": "LLM",
    "mongodb": "MongoDB",
    "nextjs": "Next.js",
    "nodejs": "Node.js",
    "oauth": "OAuth",
    "postgresql": "PostgreSQL",
    "pytorch": "PyTorch",
    "rag": "RAG",
    "rest_api": "REST API",
    "scikit_learn": "scikit-learn",
    "sql": "SQL",
    "typescript": "TypeScript",
}


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
    matcher_config.setdefault("enable_llm_field_canonicalizer", True)
    matcher_config.setdefault("max_llm_match_calls_per_application", 8)
    matcher_config.setdefault("max_llm_field_classification_calls", 8)
    data["application_question_config"] = matcher_config
    return data


def first_present(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_country_calling_code(value, country=""):
    text = str(value or "").strip()
    match = re.search(r"\+\s*(\d{1,4})", text)
    if match:
        return f"+{match.group(1)}"
    if re.fullmatch(r"\d{1,4}", text):
        return f"+{text}"

    country_defaults = {
        "canada": "+1",
        "china": "+86",
        "united kingdom": "+44",
        "united states": "+1",
        "united states of america": "+1",
    }
    return country_defaults.get(str(country or "").strip().lower(), "")


def normalize_skill_entries(value):
    if isinstance(value, dict):
        value = value.get("skills") or value.get("items") or value.get("name") or value.get("skill") or []
    if isinstance(value, str):
        raw_items = re.split(r"[,;\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("name") or item.get("skill") or ""
            raw_items.extend(re.split(r"[,;\n]+", str(item or "")))
    else:
        raw_items = []

    normalized = []
    seen = set()
    for item in raw_items:
        skill = re.sub(r"\s+", " ", str(item or "")).strip()
        key = skill.casefold()
        if skill and key not in seen:
            seen.add(key)
            normalized.append(skill)
    return normalized


def trusted_skill_library(profile_or_config):
    payload = profile_or_config if isinstance(profile_or_config, dict) else {}
    data = payload.get("user_data") if isinstance(payload.get("user_data"), dict) else payload
    library = normalized_profile_library(data)
    return normalize_skill_entries(library.get("skills") or data.get("skills"))


def extract_keyword_terms(text, limit=45):
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+#./-]{1,}", str(text or "").lower())
    counts = {}
    for token in tokens:
        token = token.strip("./-")
        if len(token) < 3 or token in KEYWORD_STOPWORDS:
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _count in ranked[:limit]]


def _normalized_skill_search_text(value):
    return " ".join(re.findall(r"[a-z0-9+#]+", str(value or "").casefold()))


def _alias_in_text(text, alias):
    clean = str(alias or "").strip().casefold()
    if not clean:
        return False
    pattern = r"(?<![a-z0-9+#])" + re.escape(clean) + r"(?![a-z0-9+#])"
    return bool(re.search(pattern, str(text or "").casefold()))


def _canonical_skill(value):
    for canonical, aliases in TECH_ALIASES.items():
        if any(_alias_in_text(value, alias) for alias in aliases):
            return canonical
    return ""


def _text_contains_skill(text, skill):
    canonical = _canonical_skill(skill)
    if canonical:
        return any(_alias_in_text(text, alias) for alias in TECH_ALIASES.get(canonical, []))
    normalized_text = _normalized_skill_search_text(text)
    normalized_skill = _normalized_skill_search_text(skill)
    if not normalized_text or not normalized_skill:
        return False
    return f" {normalized_skill} " in f" {normalized_text} "


def _keyword_match(jd_keywords, text):
    candidate_keywords = set(extract_keyword_terms(text, limit=250))
    return [term for term in jd_keywords if term in candidate_keywords]


def _keyword_coverage(matched, total):
    return round(len(matched) / len(total), 3) if total else 0.0


def _skill_display_name(canonical):
    return SKILL_DISPLAY_NAMES.get(canonical, str(canonical or "").replace("_", " ").title())


def suggest_skills_from_resume(resume_text, existing_skills=None):
    if not str(resume_text or "").strip() or extract_resume_evidence is None:
        return []
    evidence = extract_resume_evidence(str(resume_text))
    existing = {_canonical_skill(skill) or skill.casefold() for skill in normalize_skill_entries(existing_skills)}
    suggestions = []
    for item in evidence.get("supported_skills") or []:
        canonical = str(item.get("canonical") or "").strip()
        if not canonical or canonical in existing:
            continue
        suggestions.append({
            "name": _skill_display_name(canonical),
            "canonical": canonical,
            "evidence": list(item.get("evidence_bullets") or [])[:3],
            "evidence_strength": item.get("evidence_strength"),
        })
    return suggestions


def build_skill_match_report(job_description, resume_v0, skills, resume_v1=None):
    library_skills = normalize_skill_entries(skills)
    jd_matched = [skill for skill in library_skills if _text_contains_skill(job_description, skill)]
    v0_evidenced = [skill for skill in jd_matched if _text_contains_skill(resume_v0, skill)]
    v0_evidenced_keys = {skill.casefold() for skill in v0_evidenced}
    library_only = [skill for skill in jd_matched if skill.casefold() not in v0_evidenced_keys]
    v1_used = [skill for skill in jd_matched if _text_contains_skill(resume_v1, skill)] if resume_v1 else []
    matched_keys = {skill.casefold() for skill in jd_matched}
    not_requested = [skill for skill in library_skills if skill.casefold() not in matched_keys]

    jd_keywords = extract_keyword_terms(job_description)
    matched_keywords_v0 = _keyword_match(jd_keywords, resume_v0)
    matched_keywords_v1 = _keyword_match(jd_keywords, resume_v1) if resume_v1 else []
    effective_matched = matched_keywords_v1 if resume_v1 else matched_keywords_v0
    effective_keys = set(effective_matched)
    missing_keywords = [term for term in jd_keywords if term not in effective_keys]

    return {
        "library_skills": library_skills,
        "jd_matched_skills": jd_matched,
        "v0_evidenced_skills": v0_evidenced,
        "resume_evidenced_skills": v0_evidenced,
        "library_only_skills": library_only,
        "v1_used_skills": v1_used,
        "not_requested_skills": not_requested,
        "matched_keywords_v0": matched_keywords_v0[:20],
        "matched_keywords_v1": matched_keywords_v1[:20],
        "v0_keyword_coverage": _keyword_coverage(matched_keywords_v0, jd_keywords),
        "v1_keyword_coverage": _keyword_coverage(matched_keywords_v1, jd_keywords) if resume_v1 else None,
        "matched_keywords": effective_matched[:20],
        "missing_keywords": missing_keywords[:20],
        "keyword_coverage": _keyword_coverage(effective_matched, jd_keywords),
    }


def skill_match_prompt_context(report):
    report = report if isinstance(report, dict) else {}
    prompt_payload = {
        "jd_matched_trusted_skills": report.get("jd_matched_skills") or [],
        "already_evidenced_in_v0": report.get("v0_evidenced_skills") or report.get("resume_evidenced_skills") or [],
        "trusted_library_only_skills": report.get("library_only_skills") or [],
        "missing_jd_keywords_do_not_invent": report.get("missing_keywords") or [],
    }
    return json.dumps(prompt_payload, ensure_ascii=False, indent=2)


def build_resume_tailoring_prompt(
    resume_v0,
    job_description,
    company,
    role,
    rewrite_guidance=None,
    skill_match=None,
    one_page=True,
):
    page_rules = """
8. Hard one-page budget: the final resume must fit in one PDF page using 9.5pt Helvetica, 92-character wrapping, and at most 58 wrapped lines total.
9. Compress aggressively: keep only the most job-relevant bullets, avoid long paragraphs, and never include content that requires a second page.
""" if one_page else ""
    return f"""
You are the resume optimizer inside a job application agent.

Goal:
Rewrite the original resume into a targeted V1 for this job while staying factually faithful.

Hard constraints:
1. Do not invent new companies, schools, dates, degrees, projects, tools, metrics, awards, citizenship, work authorization, or achievements.
2. You may reorder, compress, emphasize, and rephrase facts that are clearly present in V0.
3. You may use JD language only when V0 or the JD-matched trusted Skill Library supports that skill.
4. A trusted library-only skill may appear only in a dedicated Skills section. Never attach it to an employer, project, duration, metric, responsibility, or achievement unless V0 contains supporting evidence.
5. Do not use Skill Library entries that are not matched to this JD. Missing JD keywords must not be invented.
6. Keep concrete metrics from V0, but do not create new numbers.
7. Return only the resume text in clean Markdown/plain text. No explanation, no JSON, no code fence.
{page_rules}
Target:
Company: {company}
Role: {role}

SOMA Stack-Outcome Guidance:
\"\"\"
{rewrite_guidance or "No historical stack-outcome guidance is available. Use only direct JD and resume evidence."}
\"\"\"

Trusted Skill Library Match:
{skill_match_prompt_context(skill_match)}

Job Description:
\"\"\"
{job_description}
\"\"\"

Original Resume V0:
\"\"\"
{resume_v0}
\"\"\"
"""


def build_resume_compression_prompt(resume_v0, resume_v1, job_description, company, role, skill_match, retry=False):
    retry_rule = "Keep only the strongest 8 to 12 bullets total." if retry else "Remove lower-signal bullets and long paragraphs first."
    return f"""
Compress the tailored resume below to a strict one-page PDF budget.

Rules:
1. Output only the resume text, no explanation.
2. Keep facts faithful to V0; do not invent anything.
3. Fit within 58 wrapped lines at 92 characters per line.
4. Preserve only job-relevant, V0-supported experience.
5. Trusted library-only skills may remain only in a dedicated Skills section and may not be attached to experience, projects, durations, metrics, responsibilities, or achievements.
6. Do not introduce missing JD keywords or unmatched Skill Library entries.
7. {retry_rule}

Target:
Company: {company}
Role: {role}

Trusted Skill Library Match:
{skill_match_prompt_context(skill_match)}

Job Description:
\"\"\"
{job_description}
\"\"\"

Original Resume V0:
\"\"\"
{resume_v0}
\"\"\"

Tailored Resume To Compress:
\"\"\"
{resume_v1}
\"\"\"
"""


def _clamp_unit(value, default=0.0):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = default
    if 1.0 < numeric <= 100.0:
        numeric = numeric / 100.0
    return max(0.0, min(1.0, numeric))


def heuristic_resume_jd_match(resume_text, job_description, label):
    jd_terms = extract_keyword_terms(job_description)
    resume_terms = set(extract_keyword_terms(resume_text, limit=200))
    matched = [term for term in jd_terms if term in resume_terms]
    missing = [term for term in jd_terms if term not in resume_terms]
    coverage = (len(matched) / len(jd_terms)) if jd_terms else 0.0
    resume_depth = min(0.2, len(str(resume_text or "")) / 8000.0)
    score = _clamp_unit((coverage * 0.85) + resume_depth)
    confidence = _clamp_unit(0.42 + min(0.28, len(jd_terms) / 120.0) + min(0.18, len(resume_terms) / 350.0))
    return {
        "label": label,
        "match_score": round(score, 3),
        "confidence": round(confidence, 3),
        "matched_requirements": matched[:12],
        "missing_requirements": missing[:12],
        "summary": f"Heuristic overlap matched {len(matched)} of {len(jd_terms)} extracted JD terms.",
        "scoring_backend": "heuristic_keyword_overlap",
    }


def _normalize_single_match_score(payload, label, fallback):
    if not isinstance(payload, dict):
        return fallback
    return {
        "label": label,
        "match_score": round(_clamp_unit(payload.get("match_score"), fallback.get("match_score", 0.0)), 3),
        "confidence": round(_clamp_unit(payload.get("confidence"), fallback.get("confidence", 0.0)), 3),
        "matched_requirements": normalize_skill_entries(
            payload.get("matched_requirements") or fallback.get("matched_requirements")
        )[:12],
        "missing_requirements": normalize_skill_entries(
            payload.get("missing_requirements") or fallback.get("missing_requirements")
        )[:12],
        "summary": str(payload.get("summary") or fallback.get("summary") or "").strip(),
        "scoring_backend": payload.get("scoring_backend") or "gemini_structured_judge",
    }


def score_resume_versions_for_jd(
    ai_client,
    model_name,
    resume_v0,
    resume_v1,
    job_description,
    company="",
    role="",
    skill_library=None,
):
    skill_match = build_skill_match_report(job_description, resume_v0, skill_library, resume_v1=resume_v1)
    fallback_v0 = heuristic_resume_jd_match(resume_v0, job_description, "V0")
    fallback_v1 = heuristic_resume_jd_match(resume_v1, job_description, "V1") if resume_v1 else {
        "label": "V1",
        "match_score": 0.0,
        "confidence": 0.0,
        "matched_requirements": [],
        "missing_requirements": fallback_v0.get("missing_requirements", []),
        "summary": "V1 resume is not available yet.",
        "scoring_backend": "heuristic_keyword_overlap",
    }
    if not job_description:
        return {
            "v0": fallback_v0,
            "v1": fallback_v1,
            "delta": round(fallback_v1.get("match_score", 0.0) - fallback_v0.get("match_score", 0.0), 3),
            "verdict": "missing_jd",
            "scoring_backend": "heuristic_keyword_overlap",
            "skill_library_match": skill_match,
        }

    fallback_error = None
    if ai_client:
        prompt = f"""
You are scoring resume-to-job-description fit for a job application agent.

Score V0 and V1 independently against the JD. The score measures role fit, keyword
coverage, and evidence alignment. Do not reward claims absent from that specific resume.
Skill Library entries are trusted candidate facts for tailoring, but do not count as
present in V0 or V1 unless that specific resume contains them.

Return ONLY JSON:
{{
  "v0": {{"match_score": 0.0, "confidence": 0.0, "matched_requirements": [], "missing_requirements": [], "summary": ""}},
  "v1": {{"match_score": 0.0, "confidence": 0.0, "matched_requirements": [], "missing_requirements": [], "summary": ""}},
  "verdict": "improved|no_material_change|worse"
}}

Company: {company}
Role: {role}

Skill Library Match:
{skill_match_prompt_context(skill_match)}

JD:
{str(job_description or "")[:7000]}

Resume V0:
{str(resume_v0 or "")[:7000]}

Resume V1:
{str(resume_v1 or "")[:7000]}
"""
        try:
            from google.genai import types as genai_types

            response = ai_client.models.generate_content(
                model=model_name or "gemini-3.5-flash",
                contents=prompt,
                config=genai_types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
            )
            raw = str(getattr(response, "text", "") or "").strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw)
            payload = json.loads(raw)
            v0 = _normalize_single_match_score(payload.get("v0"), "V0", fallback_v0)
            v1 = _normalize_single_match_score(payload.get("v1"), "V1", fallback_v1)
            delta = round(v1["match_score"] - v0["match_score"], 3)
            return {
                "v0": v0,
                "v1": v1,
                "delta": delta,
                "verdict": payload.get("verdict") or ("improved" if delta > 0.03 else "worse" if delta < -0.03 else "no_material_change"),
                "scoring_backend": "gemini_structured_judge",
                "skill_library_match": skill_match,
            }
        except Exception as err:
            fallback_error = str(err)
    else:
        fallback_error = "ai_client_unavailable"

    delta = round(fallback_v1.get("match_score", 0.0) - fallback_v0.get("match_score", 0.0), 3)
    return {
        "v0": fallback_v0,
        "v1": fallback_v1,
        "delta": delta,
        "verdict": "improved" if delta > 0.03 else "worse" if delta < -0.03 else "no_material_change",
        "scoring_backend": "heuristic_keyword_overlap",
        "fallback_reason": fallback_error,
        "skill_library_match": skill_match,
    }


def normalized_profile_library(user_data):
    data = user_data if isinstance(user_data, dict) else {}
    library = data.get("application_profile_library") or data.get("experience_library")
    if isinstance(library, dict):
        return library
    return {}


def normalize_experience_entries(entries):
    if isinstance(entries, dict):
        entries = [entries]
    normalized = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        normalized.append({
            "title": first_present(entry, "title", "job_title", "role") or "",
            "company": first_present(entry, "company", "employer", "organization") or "",
            "location": first_present(entry, "location", "city") or "",
            "start_month": first_present(entry, "start_month", "from_month") or "",
            "start_year": first_present(entry, "start_year", "from_year") or "",
            "end_month": first_present(entry, "end_month", "to_month") or "",
            "end_year": first_present(entry, "end_year", "to_year") or "",
            "current": bool(first_present(entry, "current", "currently_work_here")),
            "description": first_present(entry, "description", "summary", "responsibilities") or "",
        })
    return [
        entry for entry in normalized
        if entry.get("title") or entry.get("company") or entry.get("description")
    ]


def apply_application_profile_library(user_data):
    data = dict(user_data or {})
    library = normalized_profile_library(data)

    calling_code = normalize_country_calling_code(
        first_present(data, "country_phone_code", "phone_country_code", "calling_code"),
        first_present(data, "country", "country_name"),
    )
    if calling_code:
        data["country_phone_code"] = calling_code

    skills = normalize_skill_entries(library.get("skills") or data.get("skills"))
    if skills:
        data["skills"] = skills
        data["skill_entries"] = list(skills)

    education = library.get("education") if isinstance(library.get("education"), dict) else {}
    education_map = {
        "education_school": ("school", "institution", "university"),
        "education_degree": ("degree", "degree_type", "level"),
        "education_field": ("field", "field_of_study", "major"),
        "education_location": ("location", "campus"),
        "education_start_month": ("start_month", "from_month"),
        "education_start_year": ("start_year", "from_year"),
        "education_end_month": ("end_month", "graduation_month", "to_month"),
        "education_end_year": ("end_year", "graduation_year", "to_year"),
        "education_gpa": ("gpa", "overall", "overall_result"),
    }
    for target_key, source_keys in education_map.items():
        data.setdefault(target_key, first_present(education, *source_keys) or data.get(target_key))

    availability = library.get("availability") if isinstance(library.get("availability"), dict) else {}
    start_date = first_present(
        availability,
        "start_date",
        "earliest_start_date",
        "available_date",
        "availability",
    ) or first_present(library, "start_date", "earliest_start_date", "available_date")
    notice_period = first_present(availability, "notice_period") or first_present(library, "notice_period")
    if start_date and not data.get("start_date"):
        data["start_date"] = start_date
    if notice_period and not data.get("notice_period"):
        data["notice_period"] = notice_period
    if notice_period and not data.get("start_date"):
        data["start_date"] = notice_period

    experiences = normalize_experience_entries(
        library.get("experiences") or library.get("experience") or data.get("work_experience_entries")
    )
    if experiences and not data.get("work_experience_entries"):
        data["work_experience_entries"] = experiences

    employment_registry = library.get("company_employment_registry")
    if isinstance(employment_registry, dict):
        employment_registry = [employment_registry]
    if isinstance(employment_registry, list) and not data.get("company_employment_registry"):
        data["company_employment_registry"] = [
            dict(entry) for entry in employment_registry if isinstance(entry, dict)
        ]

    languages = library.get("languages") or library.get("language_entries") or data.get("language_entries")
    if isinstance(languages, dict):
        languages = [languages]
    language_entries = [entry for entry in (languages or []) if isinstance(entry, dict)]
    if language_entries and not data.get("language_entries"):
        data["language_entries"] = language_entries
    if language_entries:
        first_language = language_entries[0]
        data.setdefault("language", first_present(first_language, "language", "name"))
        data.setdefault("language_overall", first_present(first_language, "overall", "proficiency", "level"))

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
