import datetime
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
import uvicorn

try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None
    types = None

try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
except Exception:
    otel_trace = None
    Resource = None
    TracerProvider = None
    SimpleSpanProcessor = None
    OTLPSpanExporter = None


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT_ROOT = os.path.dirname(PROJECT_ROOT)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
TRACE_PATH = os.path.join(DATA_DIR, "arize_audit_traces.jsonl")
CONFIG_PATH = os.path.join(DATA_DIR, "scheduler_config.json")

app = FastAPI(title="Arize Phoenix Resume Audit Server", version="1.2.0")


def load_env_manually() -> None:
    for root in [PROJECT_ROOT, PARENT_ROOT]:
        for env_file in [".env", ".env.local"]:
            env_path = os.path.join(root, env_file)
            if not os.path.exists(env_path):
                continue
            try:
                with open(env_path, "r", encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            os.environ[key.strip()] = value.strip().strip('"').strip("'")
            except Exception as err:
                print(f"[Arize Audit] Failed to parse {env_path}: {err}")


load_env_manually()


class AuditRequest(BaseModel):
    resume_v0: str
    resume_v1: str
    job_description: Optional[str] = None
    app_id: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None
    apply_url: Optional[str] = None
    model: Optional[str] = None
    trusted_skills: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RetrieverTraceRequest(BaseModel):
    span_name: str = "retrieve_similar_stack_experience"
    span_kind: str = "RETRIEVER"
    input: Dict[str, Any] = Field(default_factory=dict)
    retrieved_cases: List[Dict[str, Any]] = Field(default_factory=list)
    selected_patterns: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


STOPWORDS = {
    "and", "the", "for", "with", "from", "that", "this", "into", "over", "under", "using",
    "used", "built", "build", "made", "make", "work", "role", "team", "teams", "system",
    "systems", "application", "applications", "service", "services", "project", "projects",
    "experience", "engineered", "developed", "implemented", "optimized", "created", "designed",
    "across", "through", "while", "where", "when", "what", "which", "your", "their", "them",
    "candidate", "resume", "skills", "requirements", "responsibilities", "ability", "strong",
}

TECH_TERMS = {
    "python", "java", "javascript", "typescript", "react", "next.js", "node", "node.js",
    "c++", "c#", "c", "sql", "postgres", "postgresql", "mysql", "mongodb", "redis",
    "kafka", "docker", "kubernetes", "aws", "azure", "gcp", "spring", "spring boot",
    "flask", "django", "fastapi", "graphql", "rest", "grpc", "spark", "hadoop",
    "pandas", "numpy", "scikit-learn", "tensorflow", "pytorch", "bert", "llm",
    "gemini", "langchain", "milvus", "elasticsearch", "apollo", "celery",
    "beautifulsoup", "playwright", "selenium", "html", "css", "tailwind", "vue",
    "angular", "umap", "hdbscan", "bertopic", "whisper", "oauth", "jwt",
}


def now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def load_project_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as err:
        print(f"[Arize Audit] Failed to read scheduler_config.json: {err}")
        return {}


def resolve_api_key() -> Optional[str]:
    return (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or load_project_config().get("active_api_key")
    )


def resolve_model(requested: Optional[str] = None) -> str:
    return requested or load_project_config().get("model_selector") or os.getenv("ARIZE_AUDIT_MODEL") or "gemini-3.5-flash"


def resolve_phoenix_config() -> Dict[str, Any]:
    config = load_project_config()
    endpoint = (
        os.getenv("PHOENIX_COLLECTOR_ENDPOINT")
        or config.get("phoenix_collector_endpoint")
        or ""
    ).strip()
    api_key = (
        os.getenv("PHOENIX_API_KEY")
        or config.get("phoenix_api_key")
        or ""
    ).strip()
    project_name = (
        os.getenv("PHOENIX_PROJECT_NAME")
        or config.get("phoenix_project_name")
        or "job-hunter-agent"
    ).strip()
    return {
        "endpoint": endpoint,
        "trace_endpoint": normalize_phoenix_trace_endpoint(endpoint) if endpoint else "",
        "api_key": api_key,
        "project_name": project_name or "job-hunter-agent",
        "enabled": bool(endpoint),
    }


def normalize_phoenix_trace_endpoint(endpoint: str) -> str:
    clean = (endpoint or "").rstrip("/")
    if clean.endswith("/v1/traces"):
        return clean
    return f"{clean}/v1/traces"


def sha256_short(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def clamp_score(value: Any, default: float = 0.0) -> float:
    try:
        score = float(value)
    except Exception:
        score = default
    return round(max(0.0, min(1.0, score)), 3)


def clean_json_text(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("```json"):
        text = text[7:].strip()
    if text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def words(text: str) -> Set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]{2,}", text or "")
        if token.lower() not in STOPWORDS
    }


def numbers(text: str) -> Set[str]:
    return set(re.findall(r"\b\d+(?:[,.]\d+)*(?:\.\d+)?\s*(?:%|k\+?|m\+?|b\+?|ms|s|x)?\b", text or "", flags=re.IGNORECASE))


def split_claims(text: str) -> List[str]:
    claims = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip(" \t-*•")
        if len(line) >= 30:
            claims.append(line)
    if claims:
        return claims
    for part in re.split(r"(?<=[.!?])\s+", text or ""):
        part = part.strip()
        if len(part) >= 30:
            claims.append(part)
    return claims[:40]


def extract_tech_terms(text: str) -> Set[str]:
    lower = f" {text.lower()} "
    found = set()
    for term in TECH_TERMS:
        pattern = r"(?<![a-z0-9+#.\-])" + re.escape(term) + r"(?![a-z0-9+#.\-])"
        if re.search(pattern, lower):
            found.add(term)
    return found


def normalize_trusted_skills(value: Any) -> List[str]:
    if isinstance(value, str):
        items = re.split(r"[,;\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = []
    normalized = []
    seen = set()
    for item in items:
        skill = re.sub(r"\s+", " ", str(item or "")).strip()
        key = skill.casefold()
        if skill and key not in seen:
            seen.add(key)
            normalized.append(skill)
    return normalized


def text_contains_skill(text: str, skill: str) -> bool:
    text_tokens = " ".join(re.findall(r"[a-z0-9+#]+", str(text or "").casefold()))
    skill_tokens = " ".join(re.findall(r"[a-z0-9+#]+", str(skill or "").casefold()))
    if not text_tokens or not skill_tokens:
        return False
    return f" {skill_tokens} " in f" {text_tokens} "


SKILLS_SECTION_HEADINGS = {
    "skills",
    "technical skills",
    "core skills",
    "technologies",
    "technical expertise",
    "core competencies",
}
RESUME_SECTION_HEADINGS = SKILLS_SECTION_HEADINGS | {
    "summary",
    "profile",
    "professional summary",
    "experience",
    "work experience",
    "professional experience",
    "projects",
    "education",
    "certifications",
    "achievements",
    "awards",
}


def resume_lines_with_sections(text: str) -> List[tuple[str, str]]:
    current_section = ""
    rows = []
    for raw in str(text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        heading = re.sub(r"^[#*_\s]+|[*_\s:]+$", "", line).casefold()
        if heading in RESUME_SECTION_HEADINGS:
            current_section = "skills" if heading in SKILLS_SECTION_HEADINGS else heading
            rows.append((line, current_section))
            continue
        inline_match = re.match(
            r"^(?:#{1,6}\s*)?(skills|technical skills|core skills|technologies|technical expertise|core competencies)\s*:\s*",
            line,
            flags=re.IGNORECASE,
        )
        if inline_match:
            current_section = "skills"
        rows.append((line, current_section))
    return rows


def misplaced_trusted_skills(resume_v0: str, resume_v1: str, trusted_skills: Any) -> List[str]:
    library_only = [
        skill
        for skill in normalize_trusted_skills(trusted_skills)
        if not text_contains_skill(resume_v0, skill)
    ]
    misplaced = []
    for skill in library_only:
        for line, section in resume_lines_with_sections(resume_v1):
            if text_contains_skill(line, skill) and section != "skills":
                misplaced.append(skill)
                break
    return misplaced


def extract_named_phrases(text: str) -> Set[str]:
    phrases = set()
    pattern = r"\b(?:[A-Z][A-Za-z0-9&.+#-]*(?:[ \t]+[A-Z][A-Za-z0-9&.+#-]*){0,3})\b"
    for match in re.findall(pattern, text or ""):
        normalized = " ".join(match.split())
        if len(normalized) < 3:
            continue
        if normalized.lower() in STOPWORDS:
            continue
        phrases.add(normalized)
    return phrases


def keyword_overlap_score(target: str, candidate: str) -> float:
    target_words = words(target) | extract_tech_terms(target)
    if not target_words:
        return 0.0
    candidate_words = words(candidate) | extract_tech_terms(candidate)
    return clamp_score(len(target_words & candidate_words) / max(1, min(len(target_words), 80)))


def heuristic_audit(req: AuditRequest) -> Dict[str, Any]:
    v0_words = words(req.resume_v0)
    v1_words = words(req.resume_v1)
    v0_numbers = numbers(req.resume_v0)
    v1_numbers = numbers(req.resume_v1)
    v0_tech = extract_tech_terms(req.resume_v0)
    v1_tech = extract_tech_terms(req.resume_v1)
    trusted_skills = normalize_trusted_skills(req.trusted_skills)
    trusted_tech = extract_tech_terms("\n".join(trusted_skills))
    trusted_names_lower = {skill.casefold() for skill in trusted_skills}
    misplaced_skills = misplaced_trusted_skills(req.resume_v0, req.resume_v1, trusted_skills)
    v0_names_lower = {name.lower() for name in extract_named_phrases(req.resume_v0)}

    unsupported_numbers = sorted(v1_numbers - v0_numbers)
    new_tech = sorted(v1_tech - v0_tech - trusted_tech)
    new_named_phrases = []
    for phrase in sorted(extract_named_phrases(req.resume_v1)):
        phrase_lower = phrase.lower()
        if (
            phrase_lower not in v0_names_lower
            and phrase_lower not in trusted_names_lower
            and phrase_lower not in TECH_TERMS
            and len(phrase.split()) <= 4
        ):
            if phrase_lower not in {"summary", "education", "experience", "projects", "skills"}:
                new_named_phrases.append(phrase)
    new_named_phrases = new_named_phrases[:12]

    unsupported_claims = []
    for claim in split_claims(req.resume_v1):
        claim_words = words(claim)
        if not claim_words:
            continue
        support = len(claim_words & v0_words) / max(1, len(claim_words))
        claim_nums = numbers(claim) - v0_numbers
        claim_tech = extract_tech_terms(claim) - v0_tech - trusted_tech
        action_heavy = bool(re.search(r"\b(led|owned|architected|launched|managed|scaled|increased|reduced|achieved|delivered)\b", claim, re.I))
        if claim_nums or claim_tech or (support < 0.18 and action_heavy):
            reason_bits = []
            if claim_nums:
                reason_bits.append(f"new metrics: {', '.join(sorted(claim_nums))}")
            if claim_tech:
                reason_bits.append(f"new tools: {', '.join(sorted(claim_tech))}")
            if support < 0.18 and action_heavy:
                reason_bits.append("low textual support in V0")
            unsupported_claims.append(f"{claim} ({'; '.join(reason_bits)})")
        if len(unsupported_claims) >= 8:
            break

    similarity = len(v0_words & v1_words) / max(1, len(v1_words)) if v1_words else 0.0
    penalty = (
        min(0.28, 0.055 * len(unsupported_numbers))
        + min(0.22, 0.04 * len(new_tech))
        + min(0.16, 0.018 * len(new_named_phrases))
        + min(0.34, 0.07 * len(unsupported_claims))
        + min(0.40, 0.18 * len(misplaced_skills))
    )
    if similarity < 0.28:
        penalty += 0.12
    faithfulness_score = clamp_score(1.0 - penalty, 0.5)
    jd_match_score = keyword_overlap_score(req.job_description or "", req.resume_v1)
    risk_score = clamp_score(1.0 - faithfulness_score)

    hallucinated_points = []
    hallucinated_points.extend(unsupported_claims)
    hallucinated_points.extend(
        f"Trusted library skill used outside the Skills section without V0 evidence: {skill}"
        for skill in misplaced_skills
    )
    if unsupported_numbers:
        hallucinated_points.append(f"V1 introduced metrics not found in V0: {', '.join(unsupported_numbers[:10])}")
    if new_tech:
        hallucinated_points.append(f"V1 introduced tools/skills not found in V0: {', '.join(new_tech[:10])}")
    if new_named_phrases:
        hallucinated_points.append(f"V1 introduced names/entities not found in V0: {', '.join(new_named_phrases[:10])}")

    passed = faithfulness_score >= 0.85 and not new_tech and not misplaced_skills
    if passed:
        feedback = "Faithfulness check passed. The tailored resume appears grounded in the original resume."
        action = "approve"
    else:
        feedback = "Audit blocked this version. Remove or rewrite unsupported metrics, tools, entities, or achievement claims before applying."
        action = "revise_before_apply"

    return {
        "faithfulness_score": faithfulness_score,
        "jd_match_score": jd_match_score,
        "risk_score": risk_score,
        "hallucinated_points": hallucinated_points[:12],
        "unsupported_numbers": unsupported_numbers[:12],
        "new_entities": (new_tech + new_named_phrases + misplaced_skills)[:20],
        "misplaced_trusted_skills": misplaced_skills[:12],
        "passed": passed,
        "feedback": feedback,
        "recommended_action": action,
        "evaluator_backend": "heuristic",
    }


def llm_audit(req: AuditRequest, model: str) -> Optional[Dict[str, Any]]:
    api_key = resolve_api_key()
    if not api_key or genai is None or types is None:
        return None

    prompt = f"""
You are an independent Arize Phoenix-style LLM evaluator for a job application agent.
Evaluate whether the tailored resume V1 is factually faithful to the original resume V0.

Original Resume V0:
\"\"\"
{req.resume_v0}
\"\"\"

Tailored Resume V1:
\"\"\"
{req.resume_v1}
\"\"\"

Target Job Description:
\"\"\"
{req.job_description or ""}
\"\"\"

User-confirmed Skill Library:
{json.dumps(normalize_trusted_skills(req.trusted_skills), ensure_ascii=False)}

Rules:
1. Reframing, shortening, reordering, and emphasizing facts already in V0 is acceptable.
2. New companies, employers, schools, dates, awards, tools, metrics, or achievements not supported by V0 are hallucinations.
3. User-confirmed Skill Library entries may be added only to a dedicated Skills section when V0 lacks experience evidence for them.
4. A Skill Library entry used in an employer, project, duration, responsibility, metric, or achievement without V0 evidence is a hallucination.
5. New JD keywords may not be added as user experience unless they are grounded in V0.
6. faithfulness_score below 0.85 means passed=false.
7. jd_match_score should grade how well V1 targets the JD, regardless of faithfulness.

Return JSON only with these keys:
{{
  "faithfulness_score": 0.0,
  "jd_match_score": 0.0,
  "risk_score": 0.0,
  "hallucinated_points": ["specific unsupported claim"],
  "unsupported_numbers": ["specific metric"],
  "new_entities": ["specific tool/company/entity"],
  "passed": false,
  "feedback": "brief actionable feedback",
  "recommended_action": "approve|revise_before_apply"
}}
"""
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        payload = json.loads(clean_json_text(response.text))
        return {
            "faithfulness_score": clamp_score(payload.get("faithfulness_score"), 0.0),
            "jd_match_score": clamp_score(payload.get("jd_match_score"), 0.0),
            "risk_score": clamp_score(payload.get("risk_score"), 0.0),
            "hallucinated_points": payload.get("hallucinated_points") if isinstance(payload.get("hallucinated_points"), list) else [],
            "unsupported_numbers": payload.get("unsupported_numbers") if isinstance(payload.get("unsupported_numbers"), list) else [],
            "new_entities": payload.get("new_entities") if isinstance(payload.get("new_entities"), list) else [],
            "passed": bool(payload.get("passed")),
            "feedback": str(payload.get("feedback") or ""),
            "recommended_action": str(payload.get("recommended_action") or ("approve" if payload.get("passed") else "revise_before_apply")),
            "evaluator_backend": "gemini",
        }
    except Exception as err:
        print(f"[Arize Audit] Gemini judge failed, falling back to heuristic: {err}")
        return None


def apply_trusted_skill_gate(req: AuditRequest, result: Dict[str, Any]) -> Dict[str, Any]:
    misplaced = misplaced_trusted_skills(req.resume_v0, req.resume_v1, req.trusted_skills)
    trusted_tech = extract_tech_terms("\n".join(normalize_trusted_skills(req.trusted_skills)))
    untrusted_new_tech = sorted(
        extract_tech_terms(req.resume_v1) - extract_tech_terms(req.resume_v0) - trusted_tech
    )
    if not misplaced and not untrusted_new_tech:
        return result
    points = list(result.get("hallucinated_points") or [])
    for skill in misplaced:
        issue = f"Trusted library skill used outside the Skills section without V0 evidence: {skill}"
        if issue not in points:
            points.append(issue)
    if untrusted_new_tech:
        issue = f"V1 introduced tools/skills not found in V0 or the trusted Skill Library: {', '.join(untrusted_new_tech[:10])}"
        if issue not in points:
            points.append(issue)
    entities = list(result.get("new_entities") or [])
    entities.extend(skill for skill in misplaced if skill not in entities)
    entities.extend(skill for skill in untrusted_new_tech if skill not in entities)
    result["hallucinated_points"] = points[:12]
    result["new_entities"] = entities[:20]
    result["misplaced_trusted_skills"] = misplaced[:12]
    result["untrusted_new_skills"] = untrusted_new_tech[:12]
    result["faithfulness_score"] = min(clamp_score(result.get("faithfulness_score"), 0.0), 0.82)
    result["risk_score"] = max(clamp_score(result.get("risk_score"), 0.0), 0.18)
    result["passed"] = False
    result["feedback"] = "Audit blocked this version. Keep library-only skills in the Skills section or add V0 experience evidence."
    result["recommended_action"] = "revise_before_apply"
    return result


def build_trace(req: AuditRequest, result: Dict[str, Any], model: str, latency_ms: int, trace_id: str) -> Dict[str, Any]:
    return {
        "trace_id": trace_id,
        "project": "job-hunting-agent",
        "span_kind": "EVALUATOR",
        "span_name": "resume_faithfulness_audit",
        "evaluator_name": "arize_resume_faithfulness_judge",
        "evaluator_version": "2026-07-16.1",
        "created_at": now_iso(),
        "app_id": req.app_id,
        "company": req.company,
        "role": req.role,
        "apply_url": req.apply_url,
        "model": model,
        "provider": "google-gemini" if result.get("evaluator_backend") == "gemini" else "local-heuristic",
        "latency_ms": latency_ms,
        "scores": {
            "faithfulness": result.get("faithfulness_score"),
            "jd_match": result.get("jd_match_score"),
            "risk": result.get("risk_score"),
        },
        "passed": result.get("passed"),
        "recommended_action": result.get("recommended_action"),
        "input_lengths": {
            "resume_v0_chars": len(req.resume_v0 or ""),
            "resume_v1_chars": len(req.resume_v1 or ""),
            "job_description_chars": len(req.job_description or ""),
            "trusted_skill_count": len(normalize_trusted_skills(req.trusted_skills)),
        },
        "input_hashes": {
            "resume_v0_sha256": sha256_short(req.resume_v0 or ""),
            "resume_v1_sha256": sha256_short(req.resume_v1 or ""),
            "job_description_sha256": sha256_short(req.job_description or ""),
            "trusted_skills_sha256": sha256_short("\n".join(normalize_trusted_skills(req.trusted_skills))),
        },
        "metadata": req.metadata or {},
    }


def persist_trace(record: Dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TRACE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def span_set_json(span, key: str, value: Any) -> None:
    span.set_attribute(key, json.dumps(value, ensure_ascii=False))


def export_trace_to_phoenix(record: Dict[str, Any]) -> Dict[str, Any]:
    phoenix = resolve_phoenix_config()
    export_result = {
        "enabled": phoenix["enabled"],
        "exported": False,
        "endpoint": phoenix.get("trace_endpoint", ""),
        "project_name": phoenix.get("project_name", ""),
        "error": None,
    }
    if not phoenix["enabled"]:
        return export_result
    if not all([TracerProvider, Resource, SimpleSpanProcessor, OTLPSpanExporter]):
        export_result["error"] = "OpenTelemetry OTLP HTTP dependencies are not installed."
        return export_result

    headers = {}
    if phoenix.get("api_key"):
        headers["Authorization"] = f"Bearer {phoenix['api_key']}"

    provider = None
    try:
        resource = Resource.create({
            "service.name": "job-hunter-arize-audit",
            "openinference.project.name": phoenix["project_name"],
            "project.name": phoenix["project_name"],
        })
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=phoenix["trace_endpoint"],
            headers=headers,
            timeout=10,
        )
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("job-hunter-arize-audit")
        trace_payload = record.get("trace") or {}

        with tracer.start_as_current_span("resume_faithfulness_audit") as span:
            span.set_attribute("openinference.span.kind", "EVALUATOR")
            span.set_attribute("ai.operation", "resume_audit")
            span.set_attribute("ai.evaluator.name", record.get("evaluator_name", "arize_resume_faithfulness_judge"))
            span.set_attribute("ai.evaluator.version", record.get("evaluator_version", ""))
            span.set_attribute("ai.model.name", record.get("model", ""))
            span.set_attribute("job_hunter.trace_id", record.get("trace_id", ""))
            span.set_attribute("job_hunter.app_id", trace_payload.get("app_id") or "")
            span.set_attribute("job_hunter.company", trace_payload.get("company") or "")
            span.set_attribute("job_hunter.role", trace_payload.get("role") or "")
            span.set_attribute("job_hunter.apply_url", trace_payload.get("apply_url") or "")
            span.set_attribute("eval.passed", bool(record.get("passed")))
            span.set_attribute("eval.faithfulness_score", float(record.get("faithfulness_score") or 0))
            span.set_attribute("eval.jd_match_score", float(record.get("jd_match_score") or 0))
            span.set_attribute("eval.risk_score", float(record.get("risk_score") or 0))
            span.set_attribute("eval.recommended_action", record.get("recommended_action") or "")
            span.set_attribute("eval.feedback", record.get("feedback") or "")
            span.set_attribute("eval.backend", record.get("evaluator_backend") or "")
            span_set_json(span, "eval.hallucinated_points", record.get("hallucinated_points") or [])
            span_set_json(span, "eval.unsupported_numbers", record.get("unsupported_numbers") or [])
            span_set_json(span, "eval.new_entities", record.get("new_entities") or [])
            span_set_json(span, "input.lengths", trace_payload.get("input_lengths") or {})
            span_set_json(span, "input.hashes", trace_payload.get("input_hashes") or {})
            span_set_json(span, "metadata", trace_payload.get("metadata") or {})

        provider.force_flush(timeout_millis=10000)
        export_result["exported"] = True
    except Exception as err:
        export_result["error"] = str(err)
        print(f"[Arize Audit] Phoenix export failed: {err}")
    finally:
        if provider:
            try:
                provider.shutdown()
            except Exception:
                pass
    return export_result


def export_retriever_trace_to_phoenix(record: Dict[str, Any]) -> Dict[str, Any]:
    phoenix = resolve_phoenix_config()
    export_result = {
        "enabled": phoenix["enabled"],
        "exported": False,
        "endpoint": phoenix.get("trace_endpoint", ""),
        "project_name": phoenix.get("project_name", ""),
        "error": None,
    }
    if not phoenix["enabled"]:
        return export_result
    if not all([TracerProvider, Resource, SimpleSpanProcessor, OTLPSpanExporter]):
        export_result["error"] = "OpenTelemetry OTLP HTTP dependencies are not installed."
        return export_result

    headers = {}
    if phoenix.get("api_key"):
        headers["Authorization"] = f"Bearer {phoenix['api_key']}"

    provider = None
    try:
        resource = Resource.create({
            "service.name": "job-hunter-soma-retriever",
            "openinference.project.name": phoenix["project_name"],
            "project.name": phoenix["project_name"],
        })
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=phoenix["trace_endpoint"],
            headers=headers,
            timeout=10,
        )
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("job-hunter-soma-retriever")

        input_payload = record.get("input") or {}
        company = input_payload.get("company") or "unknown company"
        role = input_payload.get("role") or "unknown role"
        display_name = record.get("display_name") or f"SOMA retrieve | {company} | {role}"

        with tracer.start_as_current_span(display_name) as span:
            cases = record.get("retrieved_cases") or []
            patterns = record.get("selected_patterns") or []
            span.set_attribute("openinference.span.kind", record.get("span_kind") or "RETRIEVER")
            span.set_attribute("ai.operation", "soma_retrieve_similar_stack_experience")
            span.set_attribute("job_hunter.display_name", display_name)
            span.set_attribute("job_hunter.trace_id", record.get("trace_id", ""))
            span.set_attribute("job_hunter.app_id", input_payload.get("application_id") or "")
            span.set_attribute("job_hunter.company", input_payload.get("company") or "")
            span.set_attribute("job_hunter.role", input_payload.get("role") or "")
            span.set_attribute("soma.stack_cluster", input_payload.get("current_stack_cluster") or "")
            span.set_attribute("soma.retrieved_count", len(cases))
            span.set_attribute("soma.selected_pattern_count", len(patterns))
            top_case = cases[0] if cases else {}
            span.set_attribute("soma.top_case_id", top_case.get("episode_id") or "")
            span.set_attribute("soma.top_case_utility", float(top_case.get("case_utility") or 0))
            span_set_json(span, "soma.current_tech_stack", input_payload.get("current_tech_stack") or [])
            span_set_json(span, "soma.retrieved_cases", cases[:12])
            span_set_json(span, "soma.selected_patterns", patterns[:12])
            span_set_json(span, "metadata", record.get("metadata") or {})

        provider.force_flush(timeout_millis=10000)
        export_result["exported"] = True
    except Exception as err:
        export_result["error"] = str(err)
        print(f"[Arize Retriever] Phoenix export failed: {err}")
    finally:
        if provider:
            try:
                provider.shutdown()
            except Exception:
                pass
    return export_result


def read_traces(limit: int = 50, app_id: Optional[str] = None) -> List[Dict[str, Any]]:
    if not os.path.exists(TRACE_PATH):
        return []
    rows = []
    with open(TRACE_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            trace_payload = item.get("trace") or {}
            input_payload = item.get("input") or {}
            if input_payload:
                company = input_payload.get("company") or ""
                role = input_payload.get("role") or ""
                item.setdefault("company", company)
                item.setdefault("role", role)
                item.setdefault("app_id", input_payload.get("application_id") or "")
                item.setdefault("display_name", f"SOMA retrieve | {company or 'unknown company'} | {role or 'unknown role'}")
            item_app_id = item.get("app_id") or trace_payload.get("app_id")
            if app_id and item_app_id != app_id:
                continue
            rows.append(item)
    return rows[-limit:][::-1]


@app.get("/health")
def health():
    phoenix = resolve_phoenix_config()
    return {
        "ok": True,
        "service": "arize-resume-audit",
        "trace_store": TRACE_PATH,
        "gemini_configured": bool(resolve_api_key() and genai is not None),
        "phoenix_configured": phoenix["enabled"],
        "phoenix_trace_endpoint": phoenix.get("trace_endpoint", ""),
        "phoenix_project_name": phoenix.get("project_name", ""),
        "phoenix_api_key_configured": bool(phoenix.get("api_key")),
        "otel_dependencies": bool(OTLPSpanExporter and TracerProvider),
    }


@app.post("/audit")
def audit_resume(req: AuditRequest):
    if not req.resume_v0 or not req.resume_v1:
        raise HTTPException(status_code=400, detail="Both resume_v0 and resume_v1 are required for auditing")

    start = time.time()
    trace_id = str(uuid.uuid4())
    model = resolve_model(req.model)
    result = llm_audit(req, model) or heuristic_audit(req)
    result = apply_trusted_skill_gate(req, result)
    result["passed"] = bool(result.get("passed"))
    result["faithfulness_score"] = clamp_score(result.get("faithfulness_score"), 0.0)
    result["jd_match_score"] = clamp_score(result.get("jd_match_score"), 0.0)
    result["risk_score"] = clamp_score(result.get("risk_score"), 1.0 - result["faithfulness_score"])
    result["recommended_action"] = result.get("recommended_action") or ("approve" if result["passed"] else "revise_before_apply")

    latency_ms = int((time.time() - start) * 1000)
    trace = build_trace(req, result, model, latency_ms, trace_id)
    response = {
        "trace_id": trace_id,
        "arize_project": "job-hunting-agent",
        "evaluator_name": trace["evaluator_name"],
        "evaluator_version": trace["evaluator_version"],
        "model": model,
        **result,
        "trace": trace,
    }
    response["phoenix_export"] = export_trace_to_phoenix(response)
    persist_trace(response)
    return response


@app.post("/trace-retriever")
def trace_retriever(req: RetrieverTraceRequest):
    trace_id = str(uuid.uuid4())
    company = (req.input or {}).get("company") or "unknown company"
    role = (req.input or {}).get("role") or "unknown role"
    display_name = f"SOMA retrieve | {company} | {role}"
    record = {
        "trace_id": trace_id,
        "project": "job-hunting-agent",
        "display_name": display_name,
        "span_name": req.span_name,
        "span_kind": req.span_kind,
        "created_at": now_iso(),
        "input": req.input,
        "retrieved_cases": req.retrieved_cases,
        "selected_patterns": req.selected_patterns,
        "metadata": req.metadata,
    }
    record["phoenix_export"] = export_retriever_trace_to_phoenix(record)
    persist_trace(record)
    return record


@app.get("/traces")
def traces(limit: int = Query(50, le=200), app_id: Optional[str] = None):
    return {"traces": read_traces(limit=limit, app_id=app_id), "trace_store": TRACE_PATH}


@app.get("/summary")
def summary():
    traces = read_traces(limit=1000)
    total = len(traces)
    passed = sum(1 for item in traces if item.get("passed"))
    failed = total - passed
    avg_faithfulness = (
        round(sum(float(item.get("faithfulness_score") or 0) for item in traces) / total, 3)
        if total else 0.0
    )
    avg_jd_match = (
        round(sum(float(item.get("jd_match_score") or 0) for item in traces) / total, 3)
        if total else 0.0
    )
    return {
        "total_traces": total,
        "passed": passed,
        "failed": failed,
        "avg_faithfulness_score": avg_faithfulness,
        "avg_jd_match_score": avg_jd_match,
        "trace_store": TRACE_PATH,
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("ARIZE_SERVER_PORT", 8003))
    uvicorn.run(app, host="0.0.0.0", port=port)
