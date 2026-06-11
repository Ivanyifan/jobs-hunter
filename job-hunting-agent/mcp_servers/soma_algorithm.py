import datetime
import hashlib
import math
import re
from typing import Any, Dict, List, Optional, Tuple


STOPWORDS = {
    "and", "the", "for", "with", "from", "that", "this", "into", "over", "under",
    "using", "used", "build", "built", "work", "role", "team", "teams", "job",
    "candidate", "experience", "requirements", "responsibilities", "skills", "strong",
    "ability", "company", "business", "product", "products", "systems", "system",
}


TECH_ALIASES: Dict[str, List[str]] = {
    "python": ["python", "py"],
    "java": ["java"],
    "javascript": ["javascript", "js"],
    "typescript": ["typescript", "ts"],
    "go": ["golang", "go"],
    "cpp": ["c++", "cpp"],
    "c": [" c "],
    "sql": ["sql"],
    "postgresql": ["postgresql", "postgres", "postgre sql"],
    "mysql": ["mysql", "my sql"],
    "mongodb": ["mongodb", "mongo db", "mongo"],
    "redis": ["redis"],
    "kafka": ["kafka"],
    "react": ["react", "react.js", "reactjs"],
    "vue": ["vue", "vue.js", "vuejs"],
    "angular": ["angular"],
    "nextjs": ["next.js", "nextjs", "next js"],
    "nodejs": ["node.js", "nodejs", "node js"],
    "express": ["express.js", "expressjs", "express"],
    "fastapi": ["fastapi", "fast api"],
    "django": ["django"],
    "flask": ["flask"],
    "spring": ["spring boot", "spring cloud", "spring"],
    "rest_api": ["rest api", "restful api", "rest apis", "restful apis", "api"],
    "graphql": ["graphql", "graph ql"],
    "grpc": ["grpc", "g rpc"],
    "docker": ["docker", "containerized", "containerization", "containers"],
    "kubernetes": ["kubernetes", "k8s"],
    "aws": ["aws", "amazon web services"],
    "azure": ["azure"],
    "gcp": ["gcp", "google cloud"],
    "ci_cd": ["ci/cd", "ci cd", "cicd", "continuous integration"],
    "spark": ["spark", "apache spark"],
    "airflow": ["airflow", "apache airflow"],
    "dbt": ["dbt"],
    "snowflake": ["snowflake"],
    "pandas": ["pandas"],
    "numpy": ["numpy"],
    "scikit_learn": ["scikit-learn", "sklearn", "scikit learn"],
    "pytorch": ["pytorch", "torch"],
    "tensorflow": ["tensorflow"],
    "bert": ["bert"],
    "llm": ["llm", "large language model", "large language models"],
    "rag": ["rag", "retrieval augmented generation"],
    "langchain": ["langchain", "lang chain"],
    "elasticsearch": ["elasticsearch", "elastic search", "elastic"],
    "milvus": ["milvus"],
    "playwright": ["playwright"],
    "selenium": ["selenium"],
    "git": ["git", "github", "gitlab"],
    "oauth": ["oauth", "oauth2"],
    "jwt": ["jwt", "json web token"],
}


SKILL_CATEGORIES: Dict[str, str] = {
    "python": "language",
    "java": "language",
    "javascript": "language",
    "typescript": "language",
    "go": "language",
    "cpp": "language",
    "c": "language",
    "sql": "database",
    "postgresql": "database",
    "mysql": "database",
    "mongodb": "database",
    "redis": "database",
    "kafka": "messaging",
    "react": "frontend_framework",
    "vue": "frontend_framework",
    "angular": "frontend_framework",
    "nextjs": "frontend_framework",
    "nodejs": "backend_framework",
    "express": "backend_framework",
    "fastapi": "backend_framework",
    "django": "backend_framework",
    "flask": "backend_framework",
    "spring": "backend_framework",
    "rest_api": "architecture",
    "graphql": "architecture",
    "grpc": "architecture",
    "docker": "devops",
    "kubernetes": "devops",
    "aws": "cloud",
    "azure": "cloud",
    "gcp": "cloud",
    "ci_cd": "devops",
    "spark": "data",
    "airflow": "data",
    "dbt": "data",
    "snowflake": "data",
    "pandas": "data",
    "numpy": "data",
    "scikit_learn": "ml",
    "pytorch": "ml",
    "tensorflow": "ml",
    "bert": "ml",
    "llm": "ai",
    "rag": "ai",
    "langchain": "ai",
    "elasticsearch": "search",
    "milvus": "search",
    "playwright": "automation",
    "selenium": "automation",
    "git": "tooling",
    "oauth": "security",
    "jwt": "security",
}


SKILL_FAMILIES = [
    {"fastapi", "django", "flask", "spring", "nodejs", "express"},
    {"react", "vue", "angular", "nextjs"},
    {"postgresql", "mysql", "sql"},
    {"mongodb", "redis", "postgresql", "mysql", "sql"},
    {"docker", "kubernetes", "ci_cd"},
    {"aws", "azure", "gcp"},
    {"pytorch", "tensorflow", "scikit_learn"},
    {"llm", "rag", "langchain", "bert"},
    {"playwright", "selenium"},
]


ROLE_PATTERNS = [
    ("fullstack", r"\b(full stack|full-stack|fullstack)\b"),
    ("ml", r"\b(machine learning|ml engineer|ai engineer|deep learning|nlp|computer vision)\b"),
    ("data", r"\b(data engineer|analytics engineer|data scientist|etl|pipeline|warehouse)\b"),
    ("backend", r"\b(backend|back-end|api|microservice|spring|django|fastapi|server)\b"),
    ("infra", r"\b(infrastructure|platform|sre|devops|cloud|kubernetes|distributed systems)\b"),
    ("frontend", r"\b(frontend|front-end|react|vue|angular|design system|ui engineer)\b"),
]


def now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def clamp(value: Any, default: float = 0.0) -> float:
    try:
        score = float(value)
    except Exception:
        score = default
    return round(max(0.0, min(1.0, score)), 3)


def slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    return text or "unknown"


def stable_id(prefix: str, *values: str) -> str:
    raw = "|".join(values)
    return f"{prefix}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def tokenize(text: str) -> List[str]:
    return [
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]{1,}", text or "")
        if token.lower() not in STOPWORDS
    ]


def alias_matches(text: str, alias: str) -> bool:
    lower = f" {text.lower()} "
    clean = alias.lower().strip()
    if clean == " c ":
        return bool(re.search(r"(?<![a-z0-9+#.\-])c(?![a-z0-9+#.\-])", lower))
    pattern = r"(?<![a-z0-9+#])" + re.escape(clean) + r"(?![a-z0-9+#])"
    return bool(re.search(pattern, lower))


def matched_aliases(text: str, canonical: str) -> List[str]:
    return [alias for alias in TECH_ALIASES.get(canonical, [canonical]) if alias_matches(text, alias)]


def line_importance(line: str) -> Tuple[str, float]:
    lower = (line or "").lower()
    if re.search(r"\b(required|must|required qualifications|minimum|proficient|expertise|strong experience)\b", lower):
        return "must_have", 1.0
    if re.search(r"\b(preferred|nice to have|plus|bonus|desired)\b", lower):
        return "preferred", 0.65
    return "mentioned", 0.75


def infer_role_family(role: str, text: str) -> str:
    role_lower = (role or "").lower()
    for family, pattern in ROLE_PATTERNS:
        if re.search(pattern, role_lower):
            return family
    combined = f"{role} {text}".lower()
    scores = []
    for family, pattern in ROLE_PATTERNS:
        matches = len(re.findall(pattern, combined))
        if matches:
            scores.append((matches, family))
    if scores:
        scores.sort(reverse=True)
        return scores[0][1]
    return "general"


def infer_seniority(role: str, text: str) -> str:
    combined = f"{role} {text}".lower()
    if re.search(r"\b(intern|internship|co-op|coop)\b", combined):
        return "intern"
    if re.search(r"\b(new grad|entry level|junior|university graduate|graduate)\b", combined):
        return "new_grad"
    if re.search(r"\b(staff|principal|lead)\b", combined):
        return "staff_plus"
    if re.search(r"\b(senior|sr\.)\b", combined):
        return "senior"
    return "unspecified"


def extract_skill_stack(text: str) -> List[Dict[str, Any]]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        lines = [text or ""]
    skills: Dict[str, Dict[str, Any]] = {}
    for canonical in TECH_ALIASES:
        evidence_lines = []
        raw_aliases = set()
        best_importance = "mentioned"
        best_weight = 0.0
        for line in lines:
            aliases = matched_aliases(line, canonical)
            if not aliases:
                continue
            importance, weight = line_importance(line)
            evidence_lines.append(line[:300])
            raw_aliases.update(aliases)
            if weight > best_weight:
                best_importance = importance
                best_weight = weight
        if evidence_lines:
            skills[canonical] = {
                "canonical": canonical,
                "raw": sorted(raw_aliases)[0],
                "category": SKILL_CATEGORIES.get(canonical, "other"),
                "importance": best_importance,
                "weight": best_weight or 0.75,
                "evidence_lines": evidence_lines[:4],
            }
    return sorted(
        skills.values(),
        key=lambda item: (item["weight"], item["category"], item["canonical"]),
        reverse=True,
    )


def extract_responsibilities(text: str) -> List[str]:
    chunks = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        cleaned = line.strip(" \t-*•")
        if len(cleaned) < 24:
            continue
        if re.search(r"\b(build|design|develop|implement|optimize|maintain|collaborate|own|create|deploy|analyze|support|write|test)\b", cleaned, re.I):
            chunks.append(cleaned[:240])
    if chunks:
        return chunks[:8]

    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        cleaned = sentence.strip()
        if len(cleaned) >= 40:
            chunks.append(cleaned[:240])
    return chunks[:8]


def category_distribution(stack: List[Dict[str, Any]]) -> Dict[str, float]:
    totals: Dict[str, float] = {}
    total = 0.0
    for skill in stack:
        category = skill.get("category") or "other"
        weight = float(skill.get("weight") or 0.0)
        totals[category] = totals.get(category, 0.0) + weight
        total += weight
    if not total:
        return {}
    return {key: value / total for key, value in totals.items()}


def build_stack_cluster(role_family: str, stack: List[Dict[str, Any]]) -> str:
    language = next((s["canonical"] for s in stack if s.get("category") == "language"), "")
    framework = next((s["canonical"] for s in stack if "framework" in (s.get("category") or "")), "")
    database = next((s["canonical"] for s in stack if s.get("category") == "database"), "")
    ai = next((s["canonical"] for s in stack if s.get("category") in {"ai", "ml"}), "")
    parts = [role_family or "general", language, framework or ai, database]
    return "_".join(slug(part) for part in parts if part)


def extract_jd_fingerprint(company: str = "", role: str = "", job_description: str = "") -> Dict[str, Any]:
    text = f"{role}\n{job_description or ''}"
    stack = extract_skill_stack(text)
    role_family = infer_role_family(role, text)
    seniority = infer_seniority(role, text)
    return {
        "company": company or "",
        "role_title": role or "",
        "role_family": role_family,
        "seniority": seniority,
        "tech_stack": [
            {k: v for k, v in item.items() if k != "evidence_lines"}
            for item in stack
        ],
        "responsibilities": extract_responsibilities(job_description or text),
        "category_shape": category_distribution(stack),
        "stack_cluster": build_stack_cluster(role_family, stack),
        "extracted_at": now_iso(),
    }


def bullet_lines(text: str) -> List[str]:
    lines = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        cleaned = line.strip(" \t-*•")
        if len(cleaned) >= 12:
            lines.append(cleaned)
    return lines


def extract_resume_evidence(resume_text: str, jd_fingerprint: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    lines = bullet_lines(resume_text)
    supported = []
    for canonical in TECH_ALIASES:
        evidence = []
        occurrences = 0
        for line in lines:
            aliases = matched_aliases(line, canonical)
            if not aliases:
                continue
            occurrences += len(aliases)
            if len(evidence) < 4:
                evidence.append(line[:260])
        if evidence:
            strength = clamp(0.45 + 0.12 * min(4, occurrences) + 0.08 * min(3, len(evidence)), 0.5)
            supported.append({
                "canonical": canonical,
                "category": SKILL_CATEGORIES.get(canonical, "other"),
                "evidence_strength": strength,
                "evidence_bullets": evidence,
            })

    supported = sorted(supported, key=lambda item: item["evidence_strength"], reverse=True)
    supported_keys = {item["canonical"] for item in supported}
    jd_skills = [
        item.get("canonical")
        for item in (jd_fingerprint or {}).get("tech_stack", [])
        if item.get("canonical")
    ]
    missing = sorted([skill for skill in jd_skills if skill not in supported_keys])
    return {
        "supported_skills": supported,
        "unsupported_or_missing": missing,
        "resume_length_chars": len(resume_text or ""),
        "extracted_at": now_iso(),
    }


def stack_weights(fp: Dict[str, Any]) -> Dict[str, float]:
    return {
        item.get("canonical"): float(item.get("weight") or 0.0)
        for item in fp.get("tech_stack", [])
        if item.get("canonical")
    }


def skill_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    for family in SKILL_FAMILIES:
        if left in family and right in family:
            return 0.7
    if SKILL_CATEGORIES.get(left) == SKILL_CATEGORIES.get(right):
        return 0.35
    return 0.0


def must_have_transfer(current_fp: Dict[str, Any], past_fp: Dict[str, Any]) -> float:
    current = [
        item for item in current_fp.get("tech_stack", [])
        if item.get("importance") == "must_have"
    ] or current_fp.get("tech_stack", [])[:5]
    past = past_fp.get("tech_stack", [])
    if not current:
        return 0.0
    scores = []
    for q_skill in current:
        q = q_skill.get("canonical")
        best = max((skill_similarity(q, p.get("canonical")) for p in past), default=0.0)
        scores.append(best)
    return clamp(sum(scores) / len(scores))


def weighted_skill_jaccard(current_fp: Dict[str, Any], past_fp: Dict[str, Any]) -> float:
    current = stack_weights(current_fp)
    past = stack_weights(past_fp)
    if not current and not past:
        return 0.0
    numerator = 0.0
    denominator = 0.0
    all_skills = set(current) | set(past)
    for skill in all_skills:
        numerator += min(current.get(skill, 0.0), past.get(skill, 0.0))
        denominator += max(current.get(skill, 0.0), past.get(skill, 0.0))
    exact_score = numerator / denominator if denominator else 0.0

    fuzzy_scores = []
    for q_skill, q_weight in current.items():
        best = max((skill_similarity(q_skill, p_skill) for p_skill in past), default=0.0)
        fuzzy_scores.append(best * q_weight)
    fuzzy_score = sum(fuzzy_scores) / max(0.001, sum(current.values())) if current else 0.0
    return clamp(0.55 * exact_score + 0.45 * fuzzy_score)


def category_shape_similarity(current_fp: Dict[str, Any], past_fp: Dict[str, Any]) -> float:
    current = current_fp.get("category_shape") or category_distribution(current_fp.get("tech_stack", []))
    past = past_fp.get("category_shape") or category_distribution(past_fp.get("tech_stack", []))
    categories = set(current) | set(past)
    if not categories:
        return 0.0
    l1 = sum(abs(float(current.get(cat, 0.0)) - float(past.get(cat, 0.0))) for cat in categories)
    return clamp(1.0 - min(1.0, l1 / 2.0))


def core_stack_combo_bonus(current_fp: Dict[str, Any], past_fp: Dict[str, Any]) -> float:
    if current_fp.get("stack_cluster") and current_fp.get("stack_cluster") == past_fp.get("stack_cluster"):
        return 1.0
    if current_fp.get("role_family") and current_fp.get("role_family") == past_fp.get("role_family"):
        current_cats = {item.get("category") for item in current_fp.get("tech_stack", [])}
        past_cats = {item.get("category") for item in past_fp.get("tech_stack", [])}
        if {"language", "database"} <= (current_cats & past_cats):
            return 0.8
        return 0.5
    return 0.0


def compute_stack_similarity(current_fp: Dict[str, Any], past_fp: Dict[str, Any]) -> Dict[str, float]:
    must = must_have_transfer(current_fp, past_fp)
    jaccard = weighted_skill_jaccard(current_fp, past_fp)
    shape = category_shape_similarity(current_fp, past_fp)
    combo = core_stack_combo_bonus(current_fp, past_fp)
    score = 0.50 * must + 0.25 * jaccard + 0.15 * shape + 0.10 * combo
    return {
        "score": clamp(score),
        "must_have_transfer": must,
        "weighted_skill_jaccard": jaccard,
        "category_shape_similarity": shape,
        "core_stack_combo_bonus": combo,
    }


def compute_task_similarity(current_fp: Dict[str, Any], past_fp: Dict[str, Any]) -> float:
    current_text = " ".join(current_fp.get("responsibilities") or [])
    past_text = " ".join(past_fp.get("responsibilities") or [])
    current_terms = set(tokenize(current_text))
    past_terms = set(tokenize(past_text))
    overlap = len(current_terms & past_terms) / max(1, len(current_terms | past_terms))
    role_bonus = 0.25 if current_fp.get("role_family") == past_fp.get("role_family") else 0.0
    return clamp(overlap + role_bonus)


def evidence_strength(resume_fp: Dict[str, Any], skill: str) -> float:
    for item in resume_fp.get("supported_skills", []):
        if item.get("canonical") == skill:
            return clamp(item.get("evidence_strength"), 0.0)
    return 0.0


def episode_required_evidence(episode: Dict[str, Any]) -> List[str]:
    patterns = episode.get("rewrite_patterns") or []
    required = []
    for pattern in patterns:
        conditions = pattern.get("applicability_conditions") or {}
        required.extend(conditions.get("resume_must_have_evidence") or [])
        required.extend(conditions.get("resume_evidence_contains") or [])
    if required:
        return sorted(set(required))

    episode_resume = episode.get("resume_evidence_fingerprint") or {}
    episode_supported = {
        item.get("canonical")
        for item in episode_resume.get("supported_skills", [])
        if item.get("evidence_strength", 0) >= 0.45
    }
    jd_skills = [
        item.get("canonical")
        for item in (episode.get("jd_fingerprint") or {}).get("tech_stack", [])
        if item.get("canonical") in episode_supported
    ]
    return jd_skills[:5]


def compute_resume_applicability(current_resume_fp: Dict[str, Any], episode: Dict[str, Any]) -> Dict[str, Any]:
    required = episode_required_evidence(episode)
    if not required:
        return {"score": 0.5, "required_evidence": [], "missing_evidence": [], "supported_evidence": []}

    strengths = {skill: evidence_strength(current_resume_fp, skill) for skill in required}
    missing = [skill for skill, strength in strengths.items() if strength < 0.35]
    supported = [skill for skill, strength in strengths.items() if strength >= 0.35]
    score = sum(strengths.values()) / max(1, len(required))
    return {
        "score": clamp(score),
        "required_evidence": required,
        "missing_evidence": missing,
        "supported_evidence": supported,
    }


def outcome_score(label: str = "", status: str = "") -> float:
    value = f"{label} {status}".lower()
    if "interview" in value or "offer" in value:
        return 1.0
    if "online_assessment" in value or "oa" in value or "assessment" in value:
        return 0.7
    if "recruiter" in value or "reply" in value:
        return 0.5
    if "applied" in value:
        return 0.35
    if "queued" in value or "pending" in value:
        return 0.25
    if "reject" in value:
        return 0.0
    return 0.2


def extract_eval_summary(episode: Dict[str, Any]) -> Dict[str, Any]:
    eval_summary = episode.get("eval_summary") or {}
    faithfulness = clamp(eval_summary.get("faithfulness_score"), 0.85)
    hallucination = clamp(
        eval_summary.get("hallucination_risk", eval_summary.get("risk_score")),
        max(0.0, 1.0 - faithfulness),
    )
    return {
        "faithfulness_score": faithfulness,
        "jd_match_score": clamp(eval_summary.get("jd_match_score"), 0.0),
        "hallucination_risk": hallucination,
        "verdict": eval_summary.get("verdict") or ("approve" if faithfulness >= 0.85 else "revise"),
    }


def rerank_episode(current_jd_fp: Dict[str, Any], current_resume_fp: Dict[str, Any], episode: Dict[str, Any]) -> Dict[str, Any]:
    past_fp = episode.get("jd_fingerprint") or {}
    stack = compute_stack_similarity(current_jd_fp, past_fp)
    task = compute_task_similarity(current_jd_fp, past_fp)
    applicability = compute_resume_applicability(current_resume_fp, episode)
    eval_summary = extract_eval_summary(episode)
    faithfulness_weight = eval_summary["faithfulness_score"] * (1.0 - eval_summary["hallucination_risk"])
    outcome = episode.get("outcome") or {}
    score = outcome_score(outcome.get("label", ""), episode.get("status", ""))
    utility = (
        stack["score"]
        * task
        * applicability["score"]
        * faithfulness_weight
        * (0.25 + 0.75 * score)
    )

    ranked = dict(episode)
    ranked["stack_sim"] = stack["score"]
    ranked["stack_sim_components"] = stack
    ranked["task_sim"] = task
    ranked["resume_applicability"] = applicability["score"]
    ranked["resume_applicability_detail"] = applicability
    ranked["faithfulness_weight"] = clamp(faithfulness_weight)
    ranked["outcome_score"] = score
    ranked["case_utility"] = clamp(utility)
    ranked["used"] = bool(ranked["case_utility"] >= 0.12 and applicability["score"] >= 0.45 and eval_summary["hallucination_risk"] <= 0.25)
    if not ranked["used"]:
        if applicability["missing_evidence"]:
            ranked["reason_not_used"] = "Current resume lacks evidence: " + ", ".join(applicability["missing_evidence"][:5])
        elif eval_summary["hallucination_risk"] > 0.25:
            ranked["reason_not_used"] = "Historical case has high hallucination risk"
        else:
            ranked["reason_not_used"] = "Low transfer utility"
    return ranked


def make_pattern_id(stack_cluster: str, skill: str, action: str) -> str:
    return stable_id("pat", stack_cluster or "general", skill or "general", action or "emphasize")


def generate_patterns_from_case(case: Dict[str, Any], current_resume_fp: Dict[str, Any]) -> List[Dict[str, Any]]:
    fp = case.get("jd_fingerprint") or {}
    role_family = fp.get("role_family") or "general"
    stack_cluster = fp.get("stack_cluster") or "general"
    patterns = []
    supported = {
        item.get("canonical"): item
        for item in current_resume_fp.get("supported_skills", [])
        if item.get("evidence_strength", 0) >= 0.45
    }
    for skill in [item.get("canonical") for item in fp.get("tech_stack", [])[:6]]:
        if not skill or skill not in supported:
            continue
        category = SKILL_CATEGORIES.get(skill, "skill")
        if category == "language":
            instruction = f"Move the strongest {skill} project or work bullet closer to the top if it is relevant to the JD."
        elif category in {"database", "backend_framework", "architecture"}:
            instruction = f"Emphasize {skill} backend/database/API experience only where resume_v0 already supports it."
        elif category in {"devops", "cloud"}:
            instruction = f"Mention {skill} infrastructure or deployment work carefully, only using evidence already present in resume_v0."
        elif category in {"ml", "ai", "data"}:
            instruction = f"Surface {skill} data/AI experience where resume_v0 contains direct evidence."
        else:
            instruction = f"Use {skill} wording only when supported by resume_v0 evidence."
        patterns.append({
            "pattern_id": make_pattern_id(stack_cluster, skill, "emphasize_supported_evidence"),
            "name": f"Emphasize supported {skill} evidence for {role_family} roles",
            "stack_cluster": stack_cluster,
            "source_episode_ids": [case.get("episode_id")],
            "current_applicability_score": evidence_strength(current_resume_fp, skill),
            "applicability_conditions": {
                "jd_should_contain": [skill, role_family],
                "resume_must_have_evidence": [skill],
            },
            "rewrite_instruction": instruction,
            "risk_constraints": [
                f"Do not claim {skill} experience beyond what resume_v0 explicitly supports.",
                "Do not invent production ownership, scale, metrics, or leadership.",
            ],
            "observed_outcome": case.get("outcome", {}),
            "case_utility": case.get("case_utility", 0.0),
        })
    return patterns


def dedupe_patterns(patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for pattern in patterns:
        key = pattern.get("pattern_id") or pattern.get("rewrite_instruction")
        if key not in by_key:
            by_key[key] = pattern
            continue
        existing = by_key[key]
        existing["source_episode_ids"] = sorted(set((existing.get("source_episode_ids") or []) + (pattern.get("source_episode_ids") or [])))
        existing["case_utility"] = max(float(existing.get("case_utility") or 0), float(pattern.get("case_utility") or 0))
    return sorted(by_key.values(), key=lambda item: item.get("case_utility", 0), reverse=True)


def extract_patterns_and_warnings(ranked_cases: List[Dict[str, Any]], current_resume_fp: Dict[str, Any]) -> Dict[str, Any]:
    positive_cases = [
        case for case in ranked_cases
        if case.get("outcome_score", 0) >= 0.5
        and (case.get("eval_summary") or {}).get("faithfulness_score", case.get("faithfulness_weight", 0.0)) >= 0.75
        and (case.get("eval_summary") or {}).get("hallucination_risk", 0.0) <= 0.25
    ]
    used_cases = [case for case in positive_cases if case.get("used")]
    blocked_cases = [case for case in positive_cases if not case.get("used")]
    caution_cases = [
        case for case in ranked_cases
        if case.get("outcome_score", 0) <= 0.2
        or (case.get("eval_summary") or {}).get("hallucination_risk", 0.0) >= 0.25
    ]

    patterns = []
    for case in used_cases[:5]:
        patterns.extend(generate_patterns_from_case(case, current_resume_fp))

    warnings = []
    for case in blocked_cases[:5]:
        missing = (case.get("resume_applicability_detail") or {}).get("missing_evidence") or []
        if missing:
            warnings.append({
                "type": "blocked_positive_case",
                "episode_id": case.get("episode_id"),
                "warning": "A successful historical case was not reused because current resume lacks required evidence.",
                "missing_evidence": missing,
                "outcome": case.get("outcome"),
            })
    for case in caution_cases[:5]:
        jd_skills = [item.get("canonical") for item in (case.get("jd_fingerprint") or {}).get("tech_stack", [])[:5]]
        missing = (case.get("resume_applicability_detail") or {}).get("missing_evidence") or []
        missing.extend([skill for skill in jd_skills if evidence_strength(current_resume_fp, skill) < 0.35])
        missing = sorted(set(missing))
        warnings.append({
            "type": "caution_case",
            "episode_id": case.get("episode_id"),
            "warning": "Avoid copying low-outcome or high-risk rewrite behavior from this similar case.",
            "skills": jd_skills,
            "missing_evidence": missing,
            "outcome": case.get("outcome"),
            "hallucination_risk": (case.get("eval_summary") or {}).get("hallucination_risk", 0.0),
        })

    return {
        "positive_cases": used_cases[:8],
        "blocked_positive_cases": blocked_cases[:8],
        "caution_cases": caution_cases[:8],
        "selected_patterns": dedupe_patterns(patterns)[:8],
        "warnings": warnings[:10],
    }


def build_rewrite_guidance(patterns: List[Dict[str, Any]], warnings: List[Dict[str, Any]]) -> str:
    lines = ["Use these successful patterns only if supported by resume_v0:"]
    if patterns:
        for index, pattern in enumerate(patterns[:6], 1):
            lines.append(f"{index}. {pattern.get('rewrite_instruction')}")
    else:
        lines.append("1. No strong transferable historical pattern found; use only direct JD/resume evidence.")
    lines.append("")
    lines.append("Do not:")
    blocked_terms = []
    for warning in warnings:
        blocked_terms.extend(warning.get("missing_evidence") or [])
    blocked_terms = sorted(set(term for term in blocked_terms if term))[:8]
    for term in blocked_terms:
        lines.append(f"- Add {term} unless resume_v0 contains direct evidence.")
    lines.extend([
        "- Invent production ownership, scale, metrics, or leadership.",
        "- Copy historical resume bullets as text templates.",
    ])
    return "\n".join(lines)


def extract_artifact(memory: Dict[str, Any], artifact_type: str) -> Optional[Dict[str, Any]]:
    for artifact in memory.get("artifacts") or []:
        if artifact.get("artifact_type") == artifact_type:
            payload = artifact.get("payload") or {}
            return payload if isinstance(payload, dict) else {"value": payload}
    return None


def latest_resume_version(memory: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    versions = memory.get("resume_versions") or []
    return versions[-1] if versions else None


def audit_from_memory(memory: Dict[str, Any]) -> Dict[str, Any]:
    artifact = extract_artifact(memory, "arize_resume_audit") or {}
    version = latest_resume_version(memory) or {}
    audit = artifact or version.get("audit_result") or {}
    faithfulness = clamp(audit.get("faithfulness_score"), 0.85)
    risk = clamp(audit.get("risk_score"), max(0.0, 1.0 - faithfulness))
    return {
        "faithfulness_score": faithfulness,
        "jd_match_score": clamp(audit.get("jd_match_score"), 0.0),
        "hallucination_risk": risk,
        "verdict": audit.get("recommended_action") or ("approve" if faithfulness >= 0.85 else "revise"),
        "trace_id": audit.get("trace_id"),
    }


def outcome_from_application(application: Dict[str, Any]) -> Dict[str, Any]:
    status = application.get("status") or ""
    label = "NO_RESPONSE"
    if status == "Interview":
        label = "INTERVIEW"
    elif status == "Rejected":
        label = "REJECTION"
    elif status == "Applied":
        label = "APPLIED"
    elif status == "Queued":
        label = "QUEUED"
    elif status == "Pending Arbitration":
        label = "PENDING_ARBITRATION"
    return {
        "label": label,
        "score": outcome_score(label, status),
        "confidence": 0.65 if label in {"QUEUED", "PENDING_ARBITRATION", "NO_RESPONSE"} else 0.85,
        "source": "application_status",
    }


def build_episode_from_memory(memory: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    app = memory.get("application") or {}
    if not app:
        return None
    app_id = str(app.get("id") or app.get("_id") or "")
    company = app.get("company") or "Unknown"
    role = app.get("role") or "Unknown"
    jd_text = app.get("job_description") or ""
    if not jd_text:
        extracted = extract_artifact(memory, "extracted_jd") or {}
        jd_text = extracted.get("job_description") or extracted.get("description") or extracted.get("text") or ""
    resume_v0 = app.get("resume_v0") or ""
    version = latest_resume_version(memory) or {}
    resume_v1 = app.get("resume_v1") or version.get("content") or ""

    jd_fp = extract_jd_fingerprint(company, role, jd_text)
    resume_fp = extract_resume_evidence(resume_v0, jd_fp)
    eval_summary = audit_from_memory(memory)
    outcome = outcome_from_application(app)

    episode_id = stable_id("ep", app_id, company, role)
    doc = {
        "episode_id": episode_id,
        "application_id": app_id,
        "company": company,
        "role_title": role,
        "status": app.get("status") or "Pending",
        "apply_link": app.get("apply_url") or "",
        "source_url": app.get("source") or "",
        "jd_text": jd_text,
        "resume_v0_text": resume_v0,
        "resume_v1_text": resume_v1,
        "jd_fingerprint": jd_fp,
        "resume_evidence_fingerprint": resume_fp,
        "rewrite_actions": [],
        "eval_summary": eval_summary,
        "outcome": outcome,
        "role_family": jd_fp.get("role_family"),
        "seniority": jd_fp.get("seniority"),
        "stack_cluster": jd_fp.get("stack_cluster"),
        "tech_stack_terms": [item.get("canonical") for item in jd_fp.get("tech_stack", []) if item.get("canonical")],
        "responsibilities_text": " ".join(jd_fp.get("responsibilities") or []),
        "outcome_score": outcome["score"],
        "outcome_label": outcome["label"],
        "faithfulness_score": eval_summary["faithfulness_score"],
        "hallucination_risk": eval_summary["hallucination_risk"],
        "updated_at": app.get("updated_at") or now_iso(),
    }
    doc["rewrite_patterns"] = generate_patterns_from_case(doc, resume_fp)
    return doc


def summarize_case(case: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "episode_id": case.get("episode_id"),
        "application_id": case.get("application_id"),
        "company": case.get("company"),
        "role_title": case.get("role_title"),
        "stack_cluster": case.get("stack_cluster"),
        "tech_stack_terms": case.get("tech_stack_terms", [])[:10],
        "outcome_label": (case.get("outcome") or {}).get("label") or case.get("outcome_label"),
        "outcome_score": case.get("outcome_score"),
        "faithfulness_score": (case.get("eval_summary") or {}).get("faithfulness_score") or case.get("faithfulness_score"),
        "hallucination_risk": (case.get("eval_summary") or {}).get("hallucination_risk") or case.get("hallucination_risk"),
        "stack_sim": case.get("stack_sim"),
        "task_sim": case.get("task_sim"),
        "resume_applicability": case.get("resume_applicability"),
        "case_utility": case.get("case_utility"),
        "used": case.get("used"),
        "reason_not_used": case.get("reason_not_used"),
    }
