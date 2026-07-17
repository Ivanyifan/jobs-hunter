"""Arize/Phoenix resume audit service.

阅读这个文件时先区分两个职责：

1. Audit gate（本服务实现）：比较原始简历 V0 与微调简历 V1，返回是否放行。
2. Phoenix observability（外部系统）：接收 OpenTelemetry trace，展示审计过程和结果。

Phoenix 只负责记录和展示，不决定 passed。最终安全结论由
``apply_trusted_skill_gate`` 重新执行本地确定性检查后给出，因此 Gemini 即使错误地
返回 passed=true，也不能绕过数字、实体、技能和无依据经历的硬阻塞规则。

推荐阅读顺序：
``AuditRequest`` -> ``heuristic_audit`` / ``llm_audit`` ->
``apply_trusted_skill_gate`` -> ``audit_resume`` ->
``build_trace`` / ``export_trace_to_phoenix``。
"""

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
    from mcp_servers.soma_algorithm import TECH_ALIASES, alias_matches
except ImportError:
    from soma_algorithm import TECH_ALIASES, alias_matches

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
    """Load local configuration without requiring python-dotenv."""
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
    """一次简历审计的完整输入。

    ``resume_v0`` 是事实来源；``resume_v1`` 是待审查的微调版本。
    ``trusted_skills`` 只证明用户确认拥有某技能，不证明该技能曾用于某段工作经历。
    """

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
    """SOMA 检索过程的观测数据；它只生成 trace，不参与简历放行判断。"""

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

HIGH_RISK_ACTIONS = {
    "achieved", "architected", "directed", "drove", "increased", "launched", "led",
    "managed", "mentored", "owned", "reduced", "scaled", "spearheaded",
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
    """Resolve the OTLP destination used only for Phoenix trace export.

    未配置 ``PHOENIX_COLLECTOR_ENDPOINT`` 时，审计仍可运行并写入本地 JSONL；
    只是不会向 Phoenix 发送 trace。
    """
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
    """Extract resume lines/sentences long enough to represent factual claims."""
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


def normalized_skill_text(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9+#]+", str(value or "").casefold()))


def canonical_tech_skill(value: Any) -> str:
    """Map an exact technology alias such as AWS to its canonical skill key."""
    normalized = normalized_skill_text(value)
    if not normalized:
        return ""
    for canonical, aliases in TECH_ALIASES.items():
        if any(normalized == normalized_skill_text(alias) for alias in aliases):
            return canonical
    return ""


def extract_tech_terms(text: str) -> Set[str]:
    """Find canonical technology terms while treating known aliases as equivalent.

    审计端故意不使用过宽的 ``API -> REST API`` 别名，否则 ``Fast API`` 会同时被
    误判为新出现的 REST API。
    """
    lower = f" {text.lower()} "
    found = set()
    for canonical, aliases in TECH_ALIASES.items():
        audit_aliases = [
            alias
            for alias in aliases
            if not (canonical == "rest_api" and normalized_skill_text(alias) == "api")
        ]
        if any(alias_matches(text, alias) for alias in audit_aliases):
            found.add(canonical)
    for term in TECH_TERMS:
        if canonical_tech_skill(term):
            continue
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
    canonical = canonical_tech_skill(skill)
    if canonical:
        return any(alias_matches(text, alias) for alias in TECH_ALIASES[canonical])
    text_tokens = normalized_skill_text(text)
    skill_tokens = normalized_skill_text(skill)
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
    """Attach the current resume section to each line.

    这一步是 Skill Library 安全规则的基础：只有 Skills 区可以接收没有 V0 经历证据的
    用户确认技能。
    """
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
    """Return library-only skills that V1 placed outside its Skills section."""
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
    """Extract likely employers, schools, degrees, titles, and other named entities.

    单个大写句首动词通常不是实体，因此会跳过；但 Experience/Education 记录头中的
    单词实体（例如 ``Google | Software Engineer``）会保留。
    """
    phrases = set()
    pattern = r"\b(?:[A-Z][A-Za-z0-9&.+#-]*(?:[ \t]+[A-Z][A-Za-z0-9&.+#-]*){0,3})\b"
    for line, section in resume_lines_with_sections(text):
        content = line.lstrip(" \t-*")
        for match in re.finditer(pattern, content):
            normalized = " ".join(match.group(0).split())
            if len(normalized) < 3:
                continue
            phrase_lower = normalized.casefold()
            if phrase_lower in STOPWORDS or phrase_lower in RESUME_SECTION_HEADINGS:
                continue
            if canonical_tech_skill(normalized):
                continue
            if len(normalized.split()) == 1 and match.start() == 0:
                entity_record_section = section in {
                    "education",
                    "experience",
                    "work experience",
                    "professional experience",
                }
                trailing_text = content[match.end():]
                entity_record_header = bool(
                    re.match(r"\s*(?:\||,|-|\u2013|\u2014)\s*\S", trailing_text)
                )
                standalone_entity = content.strip(" \t:|,-") == normalized
                if not entity_record_section or not (standalone_entity or entity_record_header):
                    continue
            phrases.add(normalized)
    return phrases


def keyword_overlap_score(target: str, candidate: str) -> float:
    """Compute JD coverage; this score never overrides a faithfulness blocker."""
    target_words = words(target) | extract_tech_terms(target)
    if not target_words:
        return 0.0
    candidate_words = words(candidate) | extract_tech_terms(candidate)
    return clamp_score(len(target_words & candidate_words) / max(1, min(len(target_words), 80)))


def heuristic_audit(req: AuditRequest) -> Dict[str, Any]:
    """Run the deterministic local faithfulness evaluator.

    检查顺序：
    1. 从 V0、V1 和 Skill Library 提取规范化证据。
    2. 找出新增数字、技术、实体、职责/成就声明和错误放置的可信技能。
    3. 计算用于排序和展示的分数。
    4. 只要存在任何 ``hard_blockers``，无论分数多高都返回 ``passed=false``。
    """

    # V0 is the factual baseline. The Skill Library is a separate, narrower trust source.
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

    # Compare normalized fact categories instead of relying on one similarity score.
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
    v0_high_risk_actions = {
        action
        for action in HIGH_RISK_ACTIONS
        if re.search(rf"\b{re.escape(action)}\b", req.resume_v0, re.IGNORECASE)
    }
    for claim in split_claims(req.resume_v1):
        claim_words = words(claim)
        if not claim_words:
            continue
        support = len(claim_words & v0_words) / max(1, len(claim_words))
        claim_nums = numbers(claim) - v0_numbers
        claim_tech = extract_tech_terms(claim) - v0_tech - trusted_tech
        claim_actions = {
            action
            for action in HIGH_RISK_ACTIONS
            if re.search(rf"\b{re.escape(action)}\b", claim, re.IGNORECASE)
        }
        new_high_risk_actions = sorted(claim_actions - v0_high_risk_actions)
        if claim_nums or claim_tech or new_high_risk_actions or (support < 0.18 and claim_actions):
            reason_bits = []
            if claim_nums:
                reason_bits.append(f"new metrics: {', '.join(sorted(claim_nums))}")
            if claim_tech:
                reason_bits.append(f"new tools: {', '.join(sorted(claim_tech))}")
            if new_high_risk_actions:
                reason_bits.append(
                    f"new responsibility/achievement verbs: {', '.join(new_high_risk_actions)}"
                )
            if support < 0.18 and claim_actions:
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
    # These categories are non-compensating: a high JD match cannot cancel any of them.
    hard_blockers = []
    if unsupported_numbers:
        hard_blockers.append("unsupported_numbers")
    if new_tech:
        hard_blockers.append("untrusted_new_skills")
    if new_named_phrases:
        hard_blockers.append("unsupported_entities")
    if unsupported_claims:
        hard_blockers.append("unsupported_claims")
    if misplaced_skills:
        hard_blockers.append("misplaced_trusted_skills")

    # Keep the score useful for ranking, but make every hard failure visibly sub-threshold.
    faithfulness_score = clamp_score(1.0 - penalty, 0.5)
    if hard_blockers:
        faithfulness_score = min(faithfulness_score, 0.82)
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

    passed = faithfulness_score >= 0.85 and not hard_blockers
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
        "unsupported_entities": new_named_phrases[:12],
        "unsupported_claims": unsupported_claims[:8],
        "misplaced_trusted_skills": misplaced_skills[:12],
        "untrusted_new_skills": new_tech[:12],
        "hard_blockers": hard_blockers,
        "passed": passed,
        "feedback": feedback,
        "recommended_action": action,
        "evaluator_backend": "heuristic",
    }


def llm_audit(req: AuditRequest, model: str) -> Optional[Dict[str, Any]]:
    """Ask Gemini for a semantic audit candidate, or return None when unavailable.

    这个结果不是最终裁决。调用方随后必须经过 ``apply_trusted_skill_gate``，后者会
    重新运行本地确定性检查并覆盖任何不安全的 passed=true。
    """
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
6. Any hallucinated_points, unsupported_numbers, or new_entities means passed=false even if the score is high.
7. faithfulness_score below 0.85 means passed=false.
8. jd_match_score should grade how well V1 targets the JD, regardless of faithfulness.

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
    """Apply the final non-bypassable deterministic safety gate.

    ``result`` 可能来自 Gemini，也可能来自本地 heuristic。这里总会再次运行
    ``heuristic_audit``，合并双方发现，并遵循两条规则：

    - 任一 evaluator 已经报告 hallucination 时，不能继续保持 passed=true。
    - 本地确定性 hard blocker 永远优先于模型分数和模型结论。

    函数名保留了历史上的 Skill Library gate 名称，但现在它保护所有事实类别。
    """
    deterministic = heuristic_audit(req)

    def merge_unique(*values: Any, limit: int) -> List[Any]:
        merged = []
        for value in values:
            for item in value if isinstance(value, list) else []:
                if item not in merged:
                    merged.append(item)
        return merged[:limit]

    result["hallucinated_points"] = merge_unique(
        result.get("hallucinated_points"),
        deterministic.get("hallucinated_points"),
        limit=12,
    )
    result["unsupported_numbers"] = merge_unique(
        result.get("unsupported_numbers"),
        deterministic.get("unsupported_numbers"),
        limit=12,
    )
    result["new_entities"] = merge_unique(
        result.get("new_entities"),
        deterministic.get("new_entities"),
        limit=20,
    )
    for key, limit in [
        ("unsupported_entities", 12),
        ("unsupported_claims", 8),
        ("misplaced_trusted_skills", 12),
        ("untrusted_new_skills", 12),
    ]:
        result[key] = merge_unique(result.get(key), deterministic.get(key), limit=limit)

    # A contradictory evaluator response (issues present together with passed=true) fails closed.
    reported_hallucination = bool(
        result["hallucinated_points"]
        or result["unsupported_numbers"]
        or result["new_entities"]
        or result["unsupported_claims"]
    )
    hard_blockers = merge_unique(
        result.get("hard_blockers"),
        deterministic.get("hard_blockers"),
        ["evaluator_reported_hallucination"] if reported_hallucination else [],
        limit=12,
    )
    result["hard_blockers"] = hard_blockers
    # Approval requires both the candidate evaluator and deterministic evaluator to pass.
    if not hard_blockers and deterministic.get("passed") and result.get("passed"):
        return result

    result["faithfulness_score"] = min(
        clamp_score(result.get("faithfulness_score"), 0.0),
        clamp_score(deterministic.get("faithfulness_score"), 0.0),
        0.82,
    )
    result["risk_score"] = max(
        clamp_score(result.get("risk_score"), 0.0),
        clamp_score(deterministic.get("risk_score"), 0.0),
        0.18,
    )
    result["passed"] = False
    result["feedback"] = (
        "Audit blocked this version. Remove every unsupported metric, entity, tool, "
        "responsibility, or achievement before applying."
    )
    result["recommended_action"] = "revise_before_apply"
    return result


def build_trace(req: AuditRequest, result: Dict[str, Any], model: str, latency_ms: int, trace_id: str) -> Dict[str, Any]:
    """Build correlation metadata after the gate has finalized the decision.

    Trace metadata stores input lengths and hashes rather than raw resume text. Caller-supplied
    ``metadata`` is passed through unchanged, so callers must not place secrets in it.
    """
    return {
        "trace_id": trace_id,
        "project": "job-hunting-agent",
        "span_kind": "EVALUATOR",
        "span_name": "resume_faithfulness_audit",
        "evaluator_name": "arize_resume_faithfulness_judge",
        "evaluator_version": "2026-07-18.1",
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
    """Append either an audit trace or retriever trace to the local JSONL store."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TRACE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def span_set_json(span, key: str, value: Any) -> None:
    span.set_attribute(key, json.dumps(value, ensure_ascii=False))


def export_trace_to_phoenix(record: Dict[str, Any]) -> Dict[str, Any]:
    """Export the finalized audit result to Phoenix through OTLP/HTTP.

    导出属于 observability，不属于 gate。Phoenix 未配置或导出失败时，此函数只返回
    诊断信息，不会把 passed 从 true 改成 false，也不会把 false 改成 true。
    """
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

        # Each audit becomes one EVALUATOR span searchable by application, company, or role.
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
    """Export SOMA retrieval evidence as a RETRIEVER span; no gate decision is made here."""
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
    """Read newest-first traces from the append-only local JSONL store."""
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
    """Report evaluator, Phoenix, and OpenTelemetry configuration separately."""
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
    """Evaluate V1, finalize the gate, record evidence, and return the decision."""
    if not req.resume_v0 or not req.resume_v1:
        raise HTTPException(status_code=400, detail="Both resume_v0 and resume_v1 are required for auditing")

    start = time.time()
    trace_id = str(uuid.uuid4())
    model = resolve_model(req.model)

    # Gemini is optional. The local heuristic is both the fallback evaluator and final authority.
    result = llm_audit(req, model) or heuristic_audit(req)
    result = apply_trusted_skill_gate(req, result)

    # Normalize the public response only after the final gate has run.
    result["passed"] = bool(result.get("passed"))
    result["faithfulness_score"] = clamp_score(result.get("faithfulness_score"), 0.0)
    result["jd_match_score"] = clamp_score(result.get("jd_match_score"), 0.0)
    result["risk_score"] = clamp_score(result.get("risk_score"), 1.0 - result["faithfulness_score"])
    result["recommended_action"] = result.get("recommended_action") or ("approve" if result["passed"] else "revise_before_apply")

    # Observability happens after the decision and cannot rewrite it.
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
    """Record how SOMA selected historical examples; this endpoint never audits V1."""
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
    """Aggregate the local trace store for the frontend audit console."""
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
