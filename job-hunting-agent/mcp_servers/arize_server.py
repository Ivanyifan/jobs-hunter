"""Arize/Phoenix resume audit service.

阅读这个文件时先区分两个职责：

1. Audit gate（本服务实现）：比较原始简历 V0 与微调简历 V1，返回是否放行。
2. Phoenix observability（外部系统）：接收 OpenTelemetry trace，展示审计过程和结果。

语义判断由 Phoenix Evals ``FaithfulnessEvaluator`` 执行；最终安全结论再经过
``apply_trusted_skill_gate`` 的本地确定性检查，因此 Judge 不能绕过数字、实体、
技能和 Skill Library 位置等硬阻塞规则。

推荐阅读顺序：
``AuditRequest`` -> ``phoenix_evals_audit`` / ``heuristic_audit`` ->
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
    from phoenix.evals import LLM as PhoenixLLM
    from phoenix.evals.metrics import FaithfulnessEvaluator as PhoenixFaithfulnessEvaluator
except Exception:
    PhoenixLLM = None
    PhoenixFaithfulnessEvaluator = None

try:
    from phoenix.client import Client as PhoenixClient
    from phoenix.client.resources.spans import SpanAnnotationData as PhoenixSpanAnnotationData
except Exception:
    PhoenixClient = None
    PhoenixSpanAnnotationData = None

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

app = FastAPI(title="Arize Phoenix Resume Audit Server", version="1.3.0")


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
    base_url = (
        os.getenv("PHOENIX_BASE_URL")
        or config.get("phoenix_base_url")
        or normalize_phoenix_base_url(endpoint)
    ).strip()
    return {
        "endpoint": endpoint,
        "trace_endpoint": normalize_phoenix_trace_endpoint(endpoint) if endpoint else "",
        "base_url": base_url,
        "api_key": api_key,
        "project_name": project_name or "job-hunter-agent",
        "enabled": bool(endpoint),
    }


def normalize_phoenix_trace_endpoint(endpoint: str) -> str:
    clean = (endpoint or "").rstrip("/")
    if clean.endswith("/v1/traces"):
        return clean
    return f"{clean}/v1/traces"


def normalize_phoenix_base_url(endpoint: str) -> str:
    clean = (endpoint or "").rstrip("/")
    if clean.endswith("/v1/traces"):
        return clean[:-len("/v1/traces")]
    return clean


def sha256_short(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def clamp_score(value: Any, default: float = 0.0) -> float:
    try:
        score = float(value)
    except Exception:
        score = default
    return round(max(0.0, min(1.0, score)), 3)


def words(text: str) -> Set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]{2,}", text or "")
    }


def numbers(text: str) -> Set[str]:
    return set(re.findall(r"\b\d+(?:[,.]\d+)*(?:\.\d+)?\s*(?:%|k\+?|m\+?|b\+?|ms|s|x)?\b", text or "", flags=re.IGNORECASE))


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
            if phrase_lower in RESUME_SECTION_HEADINGS:
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
    2. 找出新增数字、技术、实体和错误放置的可信技能。
    3. 计算用于排序和展示的分数。
    4. 只要存在任何 ``hard_blockers``，无论分数多高都返回 ``passed=false``。

    语义等价、改写和无依据声明由 Phoenix FaithfulnessEvaluator 判断。这里不再用
    词面重合度充当语义裁判。
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

    lexical_overlap = len(v0_words & v1_words) / max(1, len(v1_words)) if v1_words else 0.0
    penalty = (
        min(0.28, 0.055 * len(unsupported_numbers))
        + min(0.22, 0.04 * len(new_tech))
        + min(0.16, 0.018 * len(new_named_phrases))
        + min(0.40, 0.18 * len(misplaced_skills))
    )
    # These categories are non-compensating: a high JD match cannot cancel any of them.
    hard_blockers = []
    if unsupported_numbers:
        hard_blockers.append("unsupported_numbers")
    if new_tech:
        hard_blockers.append("untrusted_new_skills")
    if new_named_phrases:
        hard_blockers.append("unsupported_entities")
    if misplaced_skills:
        hard_blockers.append("misplaced_trusted_skills")

    # Keep the score useful for ranking, but make every hard failure visibly sub-threshold.
    faithfulness_score = clamp_score(1.0 - penalty, 0.5)
    if hard_blockers:
        faithfulness_score = min(faithfulness_score, 0.82)
    jd_match_score = keyword_overlap_score(req.job_description or "", req.resume_v1)
    risk_score = clamp_score(1.0 - faithfulness_score)

    hallucinated_points = []
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

    passed = not hard_blockers
    if passed:
        feedback = "Deterministic hard-fact checks passed; semantic approval requires Phoenix Evals."
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
        "unsupported_claims": [],
        "misplaced_trusted_skills": misplaced_skills[:12],
        "untrusted_new_skills": new_tech[:12],
        "hard_blockers": hard_blockers,
        "lexical_overlap_score": clamp_score(lexical_overlap),
        "passed": passed,
        "feedback": feedback,
        "recommended_action": action,
        "evaluator_backend": "deterministic",
    }


def phoenix_evidence_context(req: AuditRequest) -> str:
    """Build the evidence boundary consumed by Phoenix FaithfulnessEvaluator."""
    trusted_skills = normalize_trusted_skills(req.trusted_skills)
    return (
        "ORIGINAL RESUME V0 (primary factual evidence):\n"
        f"{req.resume_v0}\n\n"
        "USER-CONFIRMED SKILL LIBRARY (skill possession only; it does not prove employer, "
        "project, duration, metric, responsibility, or achievement claims):\n"
        f"{json.dumps(trusted_skills, ensure_ascii=False)}"
    )


def phoenix_evals_audit(
    req: AuditRequest,
    model: str,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Run Phoenix Evals' semantic FaithfulnessEvaluator as the primary Judge.

    The SDK executes synchronously so its Score can guard the live apply path. Phoenix
    datasets and server-side evaluators remain useful for offline experiments, but are not
    used as a network-dependent synchronous gate.
    """
    if PhoenixLLM is None or PhoenixFaithfulnessEvaluator is None:
        return None, "arize-phoenix-evals is not installed"
    api_key = resolve_api_key()
    if not api_key:
        return None, "Gemini API key is not configured for Phoenix Evals"

    try:
        llm = PhoenixLLM(
            provider="google",
            model=model,
            api_key=api_key,
        )
        # Phoenix Evals' Google adapter currently forwards evaluator kwargs beside
        # GenerateContentConfig, which google-genai rejects. Use the provider default
        # until the adapter folds invocation parameters into its config object.
        evaluator = PhoenixFaithfulnessEvaluator(llm=llm)
        scores = evaluator.evaluate({
            "input": (
                "Determine whether every factual claim in the tailored resume is supported "
                "by the evidence context. Rewording is allowed. The target JD is not evidence."
            ),
            "output": req.resume_v1,
            "context": phoenix_evidence_context(req),
        })
        if not scores:
            return None, "Phoenix FaithfulnessEvaluator returned no Score"

        score = scores[0]
        payload = score.to_dict() if hasattr(score, "to_dict") else {
            "name": getattr(score, "name", "faithfulness"),
            "score": getattr(score, "score", None),
            "label": getattr(score, "label", None),
            "explanation": getattr(score, "explanation", None),
            "kind": getattr(score, "kind", "llm"),
            "direction": getattr(score, "direction", "maximize"),
            "metadata": getattr(score, "metadata", {}),
        }
        payload = json.loads(json.dumps(payload, default=str))
        label = str(payload.get("label") or "").strip().casefold()
        faithfulness_score = clamp_score(payload.get("score"), 0.0)
        explanation = str(payload.get("explanation") or "").strip()
        passed = label == "faithful" and faithfulness_score >= 0.5
        issue = explanation or "Phoenix FaithfulnessEvaluator classified V1 as unfaithful."

        return {
            "faithfulness_score": faithfulness_score,
            "jd_match_score": keyword_overlap_score(req.job_description or "", req.resume_v1),
            "risk_score": clamp_score(1.0 - faithfulness_score),
            "hallucinated_points": [] if passed else [issue],
            "unsupported_numbers": [],
            "new_entities": [],
            "unsupported_entities": [],
            "unsupported_claims": [] if passed else [issue],
            "misplaced_trusted_skills": [],
            "untrusted_new_skills": [],
            "hard_blockers": [] if passed else ["phoenix_faithfulness_failed"],
            "passed": passed,
            "feedback": explanation or (
                "Phoenix FaithfulnessEvaluator passed V1."
                if passed else
                "Phoenix FaithfulnessEvaluator rejected V1."
            ),
            "recommended_action": "approve" if passed else "revise_before_apply",
            "evaluator_backend": "phoenix-evals",
            "phoenix_evaluation": payload,
        }, None
    except Exception as err:
        reason = f"{type(err).__name__}: {err}"[:500]
        print(f"[Arize Audit] Phoenix FaithfulnessEvaluator failed closed: {reason}")
        return None, reason


def phoenix_judge_unavailable_result(
    req: AuditRequest,
    model: str,
    reason: str,
) -> Dict[str, Any]:
    """Return a typed fail-closed result when Phoenix Evals cannot produce a Score."""
    message = f"Phoenix FaithfulnessEvaluator unavailable: {reason}"
    return {
        "faithfulness_score": 0.0,
        "jd_match_score": keyword_overlap_score(req.job_description or "", req.resume_v1),
        "risk_score": 1.0,
        "hallucinated_points": [],
        "unsupported_numbers": [],
        "new_entities": [],
        "unsupported_entities": [],
        "unsupported_claims": [],
        "misplaced_trusted_skills": [],
        "untrusted_new_skills": [],
        "hard_blockers": ["phoenix_judge_unavailable"],
        "passed": False,
        "feedback": message,
        "recommended_action": "retry_audit",
        "evaluator_backend": "phoenix-evals-unavailable",
        "evaluation_error": reason,
        "phoenix_evaluation": {
            "name": "faithfulness",
            "label": "unavailable",
            "score": 0.0,
            "kind": "llm",
            "metadata": {"model": model},
        },
    }


def llm_audit(req: AuditRequest, model: str) -> Optional[Dict[str, Any]]:
    """Backward-compatible wrapper around the Phoenix Evals Judge."""
    result, _ = phoenix_evals_audit(req, model)
    return result


def apply_trusted_skill_gate(req: AuditRequest, result: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the final non-bypassable deterministic safety gate.

    ``result`` 来自 Phoenix Evals。这里总会再次运行
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
    if "phoenix_judge_unavailable" in hard_blockers:
        result["feedback"] = result.get("feedback") or "Phoenix Judge is unavailable."
        result["recommended_action"] = "retry_audit"
    elif "phoenix_faithfulness_failed" in hard_blockers:
        result["feedback"] = result.get("feedback") or "Phoenix Judge rejected V1."
        result["recommended_action"] = "revise_before_apply"
    else:
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
        "evaluator_name": "phoenix_evals_resume_faithfulness_judge",
        "evaluator_version": "2026-07-21.1",
        "created_at": now_iso(),
        "app_id": req.app_id,
        "company": req.company,
        "role": req.role,
        "apply_url": req.apply_url,
        "model": model,
        "provider": (
            "phoenix-evals/google-gemini"
            if result.get("evaluator_backend") == "phoenix-evals"
            else result.get("evaluator_backend") or "deterministic"
        ),
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


def log_phoenix_evaluation_annotation(
    phoenix: Dict[str, Any],
    span_id: str,
    record: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach the Phoenix Evals Score to its trace as an official LLM annotation."""
    annotation_result = {
        "enabled": bool(phoenix.get("base_url")),
        "logged": False,
        "span_id": span_id,
        "error": None,
    }
    if record.get("evaluator_backend") != "phoenix-evals":
        annotation_result["skipped_reason"] = "Phoenix Judge did not produce a Score."
        return annotation_result
    if not phoenix.get("base_url"):
        annotation_result["skipped_reason"] = "PHOENIX_BASE_URL is not configured."
        return annotation_result
    if PhoenixClient is None or PhoenixSpanAnnotationData is None:
        annotation_result["error"] = "arize-phoenix-client is not installed."
        return annotation_result

    score = record.get("phoenix_evaluation") or {}
    result_payload = {
        key: score.get(key)
        for key in ("label", "score", "explanation")
        if score.get(key) is not None
    }
    if not result_payload:
        annotation_result["error"] = "Phoenix Evals Score has no label, score, or explanation."
        return annotation_result

    annotation = PhoenixSpanAnnotationData(
        name=str(score.get("name") or "resume_faithfulness"),
        annotator_kind="LLM",
        span_id=span_id,
        result=result_payload,
        metadata={
            "evaluator_backend": record.get("evaluator_backend"),
            "model": record.get("model"),
            "hard_blockers": record.get("hard_blockers") or [],
        },
        identifier="job-hunter-resume-faithfulness",
    )
    try:
        client = PhoenixClient(
            base_url=phoenix["base_url"],
            api_key=phoenix.get("api_key") or None,
        )
        client.spans.log_span_annotations(
            span_annotations=[annotation],
            sync=True,
        )
        annotation_result["logged"] = True
    except Exception as err:
        annotation_result["error"] = str(err)
        print(f"[Arize Audit] Phoenix evaluation annotation failed: {err}")
    return annotation_result


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
        "base_url": phoenix.get("base_url", ""),
        "project_name": phoenix.get("project_name", ""),
        "error": None,
        "evaluation_annotation": {
            "enabled": bool(phoenix.get("base_url")),
            "logged": False,
            "error": None,
        },
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
        span_id = ""
        with tracer.start_as_current_span("resume_faithfulness_audit") as span:
            span_id = f"{span.get_span_context().span_id:016x}"
            span.set_attribute("openinference.span.kind", "EVALUATOR")
            span.set_attribute("ai.operation", "resume_audit")
            span.set_attribute("ai.evaluator.name", record.get("evaluator_name", "phoenix_evals_resume_faithfulness_judge"))
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
            span_set_json(span, "eval.unsupported_entities", record.get("unsupported_entities") or [])
            span_set_json(span, "eval.unsupported_claims", record.get("unsupported_claims") or [])
            span_set_json(span, "eval.misplaced_trusted_skills", record.get("misplaced_trusted_skills") or [])
            span_set_json(span, "eval.untrusted_new_skills", record.get("untrusted_new_skills") or [])
            span_set_json(span, "eval.hard_blockers", record.get("hard_blockers") or [])
            span_set_json(span, "eval.phoenix_score", record.get("phoenix_evaluation") or {})
            span_set_json(span, "input.lengths", trace_payload.get("input_lengths") or {})
            span_set_json(span, "input.hashes", trace_payload.get("input_hashes") or {})
            span_set_json(span, "metadata", trace_payload.get("metadata") or {})

        provider.force_flush(timeout_millis=10000)
        export_result["exported"] = True
        export_result["evaluation_annotation"] = log_phoenix_evaluation_annotation(
            phoenix,
            span_id,
            record,
        )
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
        "gemini_configured": bool(resolve_api_key()),
        "phoenix_evals_installed": bool(PhoenixLLM and PhoenixFaithfulnessEvaluator),
        "phoenix_judge_configured": bool(
            resolve_api_key() and PhoenixLLM and PhoenixFaithfulnessEvaluator
        ),
        "phoenix_configured": phoenix["enabled"],
        "phoenix_trace_endpoint": phoenix.get("trace_endpoint", ""),
        "phoenix_base_url": phoenix.get("base_url", ""),
        "phoenix_project_name": phoenix.get("project_name", ""),
        "phoenix_api_key_configured": bool(phoenix.get("api_key")),
        "phoenix_client_installed": bool(PhoenixClient and PhoenixSpanAnnotationData),
        "phoenix_evaluation_annotations_configured": bool(
            phoenix.get("base_url") and PhoenixClient and PhoenixSpanAnnotationData
        ),
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

    # Phoenix Evals is the semantic Judge. Missing Judge evidence always fails closed.
    result, judge_error = phoenix_evals_audit(req, model)
    if result is None:
        result = phoenix_judge_unavailable_result(req, model, judge_error or "unknown error")

    # Local deterministic checks protect exact facts that an LLM must not override.
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
