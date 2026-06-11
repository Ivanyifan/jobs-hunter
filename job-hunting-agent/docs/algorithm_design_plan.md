# SOMA: Stack-Outcome Matching Algorithm

## 1. 核心目标

Job Hunter Agent 不是简单的“JD 进来，LLM 改一版简历”。真正有价值的逻辑是：

```text
当前 JD
→ 提取技术栈 fingerprint
→ 从历史投递里找相似技术栈的 episodes
→ 只使用 faithfulness 过关、且当前简历证据可复用的案例
→ 提取 rewrite pattern，而不是复制历史简历文本
→ 生成本次 Resume V1
→ Arize/Phoenix 审计
→ 投递或进入人工仲裁
→ Gmail outcome 回流
→ 更新这个技术栈 cluster 下的经验
```

这个算法可以叫：

```text
SOMA = Stack-Outcome Matching Algorithm
```

一句话 pitch：

```text
SOMA retrieves prior applications with similar technical stacks, filters them by outcome,
faithfulness, and resume evidence compatibility, extracts reusable rewrite strategies,
and continuously updates those strategies using real Gmail outcomes.
```

中文解释：

```text
SOMA 会检索相似技术栈下的历史投递，筛掉不可信或不可复用的案例，
提取成功改写策略，并用 Gmail 里的真实面试/拒信结果持续更新策略权重。
```

## 2. 系统分工

```text
MongoDB        = source of truth，存完整记忆和 episode
Elastic        = retrieval index，做相似岗位/相似 episode 粗召回
Arize/Phoenix  = observability + evaluation，记录检索、改写、审计链路
Playwright     = browser action，打开 apply link、注册/登录、发现表单
Gmail          = outcome signal，识别面试、拒信、OA、recruiter reply
LLM            = extraction, reasoning, rewrite, answer generation
```

重要原则：

- MongoDB 是真实数据源。
- Elastic 只做搜索和召回，不做最终决策。
- Arize 负责证明 agent 不是 blind generation，而是 evidence-based learning。
- Playwright 可以填表和发现字段，但最终 submit 应该默认需要人工确认。
- Gmail outcome 是学习闭环的关键，不只是“存邮件状态”。

## 3. 端到端流程

```text
LinkedIn Search / Job Source
        ↓
JD extraction + external apply link extraction
        ↓
MongoDB stores canonical job/application memory
        ↓
JD fingerprint + resume evidence fingerprint
        ↓
Elastic retrieves similar historical episodes
        ↓
SOMA reranks episodes and selects transferable rewrite patterns
        ↓
LLM generates Resume V1 / form answers
        ↓
Arize audits faithfulness, JD match, hallucination risk
        ↓
Playwright opens apply page and discovers/fills form
        ↓
MongoDB records events, artifacts, resume versions, current status
        ↓
Gmail detects outcome
        ↓
MongoDB updates outcome and pattern stats
        ↓
Elastic re-indexes searchable episode fields
```

## 4. 为什么“相似技术栈”比“相似 JD”更重要

JD 原文很长，里面有大量噪声：

- 公司介绍
- 福利
- 文化描述
- 招聘套话
- 法务和 EEO 文本

真正决定简历怎么改的通常是：

- 语言：Python / Java / TypeScript / Go
- 框架：React / FastAPI / Django / Spring / Next.js
- 数据库：PostgreSQL / MongoDB / Redis
- 云和 DevOps：AWS / Docker / Kubernetes / CI/CD
- 数据栈：Spark / Airflow / dbt / Snowflake
- ML 栈：PyTorch / TensorFlow / LangChain / RAG
- 任务类型：build API / data pipeline / dashboard / ML model / automation
- 岗位类型：frontend / backend / full-stack / data / ML / infra
- seniority：intern / new grad / senior

例子：

```text
A: Python + FastAPI + PostgreSQL + Docker
B: Python + PyTorch + computer vision + research papers
```

这两个 JD 都有 Python，但简历改写策略完全不同。所以 similarity 不能只看 keyword overlap，要看：

- 技术栈是否相似。
- 岗位任务是否相似。
- 当前候选人的简历证据是否可复用。
- 历史 outcome 是否可信。
- 历史改写是否通过 faithfulness 审计。

## 5. 核心数据单位：Application Episode

每一次投递都应该被记录成一个 episode：

```text
application episode =
  JD 技术栈
+ 原始简历证据
+ 改写后的简历
+ 改写策略
+ 审计分数
+ 最终 outcome
```

以后遇到新岗位时，agent 先问：

```text
我以前有没有投过类似技术栈的岗位？
哪些 resume rewrite 拿到了 interview？
那些 rewrite 有没有造假？
这些成功 pattern 当前候选人能不能真实复用？
```

episode 示例：

```json
{
  "application_id": "app_123",
  "job_id": "job_456",
  "resume_version_id": "rv_789",
  "jd_fingerprint": {
    "role_family": "backend",
    "seniority": "intern",
    "tech_stack": [
      {
        "canonical": "python",
        "category": "language",
        "importance": "must_have",
        "weight": 1.0
      },
      {
        "canonical": "fastapi",
        "category": "framework",
        "importance": "preferred",
        "weight": 0.7
      },
      {
        "canonical": "postgresql",
        "category": "database",
        "importance": "must_have",
        "weight": 1.0
      }
    ],
    "responsibilities": [
      "build REST APIs",
      "design backend services",
      "work with relational databases"
    ],
    "stack_cluster": "python_backend_relational_db"
  },
  "resume_evidence_fingerprint": {
    "supported_skills": [
      {
        "canonical": "python",
        "evidence_strength": 0.95,
        "evidence_bullets": ["Built Python scripts to automate reporting"]
      },
      {
        "canonical": "postgresql",
        "evidence_strength": 0.8,
        "evidence_bullets": ["Designed SQL queries for analytics dashboard"]
      }
    ],
    "unsupported_or_missing": ["kubernetes", "aws", "fastapi"]
  },
  "rewrite_actions": [
    {
      "type": "reorder",
      "description": "Moved Python backend project to top"
    },
    {
      "type": "keyword_alignment",
      "description": "Changed 'SQL database' to 'PostgreSQL' because V0 had evidence"
    }
  ],
  "eval_summary": {
    "faithfulness_score": 0.93,
    "jd_match_score": 0.86,
    "keyword_coverage": 0.74,
    "hallucination_risk": 0.08,
    "verdict": "approve"
  },
  "outcome": {
    "label": "INTERVIEW",
    "score": 1.0,
    "confidence": 0.94,
    "source": "gmail",
    "observed_at": "2026-06-09T18:20:00Z"
  }
}
```

## 6. Step 1：提取 JD Fingerprint

每个 JD 进入系统后，先用 LLM 或规则抽取结构化 fingerprint。

输出：

```json
{
  "role_family": "backend",
  "seniority": "intern",
  "tech_stack": [
    {
      "raw": "Python",
      "canonical": "python",
      "category": "language",
      "importance": "must_have",
      "weight": 1.0
    },
    {
      "raw": "REST APIs",
      "canonical": "rest_api",
      "category": "architecture",
      "importance": "must_have",
      "weight": 0.9
    },
    {
      "raw": "PostgreSQL",
      "canonical": "postgresql",
      "category": "database",
      "importance": "preferred",
      "weight": 0.6
    }
  ],
  "responsibilities": [
    "build backend APIs",
    "work with database schemas",
    "collaborate with frontend engineers"
  ],
  "stack_cluster": "python_backend_relational_db"
}
```

这里要做 canonical normalization。Hackathon 版本可以先手写 synonym dictionary：

```text
React.js      → react
Postgres      → postgresql
Node          → nodejs
AWS Lambda    → aws_lambda
RESTful API   → rest_api
CI CD         → ci_cd
K8s           → kubernetes
```

## 7. Step 2：提取 Resume Evidence Fingerprint

这个比 JD fingerprint 更关键，因为它决定哪些历史成功经验能不能复用。

从 `resume_v0` 中提取：

```json
{
  "supported_skills": {
    "python": {
      "evidence_strength": 0.95,
      "evidence": ["Built Python automation scripts for reporting"]
    },
    "react": {
      "evidence_strength": 0.85,
      "evidence": ["Built a React dashboard for visualizing user metrics"]
    },
    "aws": {
      "evidence_strength": 0.0,
      "evidence": []
    },
    "kubernetes": {
      "evidence_strength": 0.0,
      "evidence": []
    }
  }
}
```

这个结构用于防止 agent 错误迁移。

例子：

```text
历史成功案例：
Kubernetes-heavy backend resume 拿到了 interview

当前 resume_v0：
没有 Kubernetes 证据
```

这个成功案例不能直接复用。最多只能借鉴：

```text
强调 deployment / containerization 相关经历
```

但不能写：

```text
Built Kubernetes deployment pipelines
```

## 8. Step 3：计算技术栈相似度

建议用非对称 similarity。

原因：

```text
我们关心的是：历史成功案例能不能覆盖当前 JD 的需求？
而不是两个 JD 是否完全一样。
```

例子：

```text
当前 JD: Python + FastAPI + PostgreSQL
历史 JD: Python + Django + PostgreSQL + Docker
```

FastAPI 和 Django 不完全一样，但都属于 Python backend framework，所以有迁移价值。

### Skill-Level Similarity

```text
exact match / synonym:      1.0
same family:                0.7
complementary tool:         0.4
same broad category only:   0.2
unrelated:                  0.0
```

例子：

```text
Postgres vs PostgreSQL = 1.0
FastAPI vs Django      = 0.7
React vs Vue           = 0.6
Docker vs Kubernetes   = 0.5
Python vs Java         = 0.2
Python vs Figma        = 0.0
```

### Stack Similarity Formula

```text
stack_sim(q, e) =
  0.50 * must_have_transfer
+ 0.25 * weighted_skill_jaccard
+ 0.15 * category_shape_similarity
+ 0.10 * core_stack_combo_bonus
```

含义：

- `must_have_transfer`：当前 JD 的 must-have 技术，历史 episode 覆盖了多少。
- `weighted_skill_jaccard`：带权重的技能重合度。
- `category_shape_similarity`：技术栈形状是否接近。
- `core_stack_combo_bonus`：核心组合相似时加分。

核心组合例子：

```text
Python + backend framework + relational database
TypeScript + React + dashboard
PyTorch + NLP + transformers
Java + Spring + microservices
```

## 9. Step 4：计算岗位任务相似度

只看技术栈还不够。

两个岗位都用 React，但一个是：

```text
Design system frontend engineer
```

另一个是：

```text
Data dashboard engineer
```

简历强调点不同。所以还要提取 responsibilities：

```json
{
  "responsibilities": [
    "build REST APIs",
    "optimize SQL queries",
    "write unit tests",
    "collaborate with frontend team"
  ]
}
```

Hackathon 版本可以先用 rule-based：

```text
API / backend / database overlap        → backend task sim high
dashboard / visualization / React       → frontend-data task sim high
ETL / pipeline / Spark / Airflow        → data engineering task sim high
training / model / PyTorch / inference  → ML task sim high
```

后续复杂版本再上 embedding：

```text
task_sim = cosine_embedding(current_responsibilities, past_responsibilities)
```

## 10. Step 5：计算 Resume Applicability

这是最容易被忽略、但最关键的一步。

历史成功案例不能直接复制。必须问：

```text
这个成功案例里的 rewrite pattern，当前 resume_v0 有没有证据支持？
```

例子 1：

```json
{
  "pattern": "Emphasize Kubernetes deployment experience",
  "required_evidence": ["docker", "kubernetes", "deployment"],
  "observed_outcome": "INTERVIEW"
}
```

当前候选人 evidence：

```json
{
  "docker": 0.6,
  "kubernetes": 0.0,
  "deployment": 0.4
}
```

结论：

```text
applicability_score = 0.33
不能用。
```

例子 2：

```json
{
  "pattern": "Move Python automation project to first bullet",
  "required_evidence": ["python", "automation"],
  "observed_outcome": "INTERVIEW"
}
```

当前候选人 evidence：

```json
{
  "python": 0.95,
  "automation": 0.8
}
```

结论：

```text
applicability_score = 0.88
可以用。
```

## 11. Step 6：综合 Rank 历史案例

分两步：

```text
第一步：Elastic 粗召回相似 episodes
第二步：SOMA 在 app 里按 outcome 和可信度 rerank
```

不要一开始就只找成功案例，因为失败案例也有参考价值。

推荐乘法版本：

```text
case_utility(q, e) =
  stack_sim(q, e)
* task_sim(q, e)
* resume_applicability(q, e)
* faithfulness_weight(e)
* outcome_weight(e)
* recency_weight(e)
```

其中：

```text
faithfulness_weight =
  faithfulness_score * (1 - hallucination_risk)

outcome_weight =
  1.0 for INTERVIEW
  0.7 for ONLINE_ASSESSMENT
  0.5 for RECRUITER_REPLY
  0.2 for NO_RESPONSE_AFTER_21_DAYS
  0.0 for REJECTION

recency_weight =
  1.0 for recent
  0.8 for older than 3 months
  0.6 for older than 6 months
```

Hackathon 版本也可以用加权打分：

```text
case_score =
  0.45 * stack_similarity
+ 0.20 * task_similarity
+ 0.15 * resume_applicability
+ 0.10 * outcome_score
+ 0.10 * faithfulness_score
- 0.30 * hallucination_risk
```

乘法版本更适合做安全 gate：

```text
case_utility =
  stack_sim
* task_sim
* resume_applicability
* faithfulness_weight
* (0.25 + 0.75 * outcome_score)
```

这样如果某个历史案例技术栈很像，但 hallucination risk 高，它不会被用作强参考。

## 12. Step 7：提取 Rewrite Pattern，而不是复制简历

这是算法设计里最重要的一句话：

```text
历史成功案例不能作为文本模板复制，只能作为 rewrite strategy 参考。
```

从成功案例中提取的是：

- 什么经历被提前了。
- 什么关键词被自然加入了。
- 什么项目被强调了。
- 哪些不相关内容被压缩了。
- 哪些 metric 被保留或加强了。
- 哪些 JD skill 因为没有证据而被禁止加入。

Pattern 数据结构：

```json
{
  "pattern_id": "pat_python_backend_api_first",
  "stack_cluster": "python_backend_relational_db",
  "applicability_conditions": {
    "jd_contains": ["python", "backend", "postgresql"],
    "resume_evidence_contains": ["python", "sql", "api"]
  },
  "rewrite_strategy": {
    "type": "emphasize_project",
    "instruction": "Move backend API project into top 1-2 bullets and mention relational database work if supported."
  },
  "risk_constraints": [
    "Do not mention FastAPI unless resume_v0 has FastAPI evidence.",
    "Do not claim production ownership unless resume_v0 says production ownership."
  ],
  "observed_stats": {
    "attempts": 8,
    "interviews": 3,
    "online_assessments": 2,
    "rejections": 2,
    "no_response": 1,
    "avg_outcome_score": 0.64,
    "avg_faithfulness_score": 0.91
  }
}
```

这个比存一堆历史 resume 文本更有价值。

## 13. Step 8：用 Pattern 指导本次 Rewrite

当前 JD：

```text
Backend Intern
Python, FastAPI, PostgreSQL, Docker
```

当前 `resume_v0` 有：

```text
Python automation
SQL dashboard
Docker course project
No FastAPI
No Kubernetes
```

系统检索到历史成功 pattern：

```text
Pattern A:
For Python backend jobs, move Python automation/API project to top.

Pattern B:
For SQL-heavy backend jobs, convert vague "database work" into specific SQL/PostgreSQL wording only when evidence exists.

Pattern C:
For Docker/devops mentions, include containerization only if the resume has evidence.
```

于是 rewrite prompt 里加入：

```text
Use these successful patterns if supported:
1. Emphasize Python automation/API work near the top.
2. Emphasize SQL/database experience using faithful wording.
3. Mention Docker/containerization only if supported by resume_v0.

Do not:
- Add FastAPI unless resume_v0 contains FastAPI evidence.
- Add Kubernetes unless resume_v0 contains Kubernetes evidence.
- Invent production ownership, scale, metrics, or leadership.
```

这样 agent 会生成更贴合 JD 的简历，但仍然安全。

## 14. Step 9：失败案例也要用起来

相似技术栈下的失败案例也有价值，但 rejection 噪声很大。

例子：

```text
Python backend JD
resume_v1 keyword coverage 很高
但是 faithfulness_score 低
最后 rejected
```

这个失败案例可能说明：

```text
强塞关键词没用，而且提高了 hallucination risk。
```

可以形成 caution pattern：

```json
{
  "pattern_id": "neg_001",
  "stack_cluster": "python_backend",
  "warning": "Do not add cloud/devops terms unless directly supported.",
  "evidence": {
    "similar_cases": 4,
    "avg_hallucination_risk": 0.34,
    "avg_outcome_score": 0.12
  }
}
```

但不要把单个 rejection 当成强负样本。

反馈强度建议：

```text
Interview           = 强正反馈
Online assessment   = 中强正反馈
Recruiter reply     = 中等正反馈
Rejection           = 弱负反馈
No response         = 弱负反馈
```

原因：拒信可能不是简历问题，也可能是岗位已招满、公司 freeze、竞争太激烈、地点/visa/timing 问题。

## 15. Pattern 的长期学习算法

当 outcome 回来以后，不只是更新 `resume_version`，还要更新 `rewrite_patterns.stats`。

例子：

```json
{
  "pattern_id": "pat_python_backend_api_first",
  "stack_cluster": "python_backend",
  "attempts": 12,
  "weighted_successes": 5.4,
  "weighted_failures": 6.6,
  "avg_faithfulness_score": 0.92,
  "avg_jd_match_score": 0.84,
  "avg_outcome_score": 0.58
}
```

Bayesian success rate：

```text
pattern_success_score =
  (alpha + weighted_successes) /
  (alpha + beta + weighted_attempts)

alpha = 1
beta = 1
```

这样样本少的时候不会过度自信。

再加 confidence：

```text
confidence = 1 - exp(-attempts / 5)
```

最终：

```text
pattern_utility =
  pattern_success_score
* confidence
* avg_faithfulness_score
* current_applicability_score
* current_stack_similarity
```

## 16. 完整算法流程

Input:

```text
current_jd
resume_v0
historical_episodes
```

Step 1: Extract fingerprints

```text
current_jd_fp = extract_jd_fingerprint(current_jd)
current_resume_fp = extract_resume_evidence(resume_v0)
```

Step 2: Retrieve similar historical episodes

```text
Use Elastic rough retrieval:
- role_family same or similar
- seniority same or close
- tech_stack overlaps with current JD
- responsibilities embedding/rule similarity

Return top 50.
```

Step 3: Rerank

```python
for episode in candidates:
    stack_sim = compute_stack_similarity(current_jd_fp, episode.jd_fingerprint)
    task_sim = compute_task_similarity(current_jd_fp, episode.jd_fingerprint)
    applicability = compute_resume_applicability(
        current_resume_fp,
        episode.rewrite_patterns
    )
    faithfulness_weight = episode.faithfulness_score * (1 - episode.hallucination_risk)
    outcome_weight = 0.25 + 0.75 * episode.outcome_score

    episode.utility = (
        stack_sim
        * task_sim
        * applicability
        * faithfulness_weight
        * outcome_weight
    )
```

Step 4: Select positive and caution references

```python
positive_cases = [
    e for e in candidates
    if e.outcome_score >= 0.7
    and e.faithfulness_score >= 0.85
    and e.hallucination_risk <= 0.20
]

caution_cases = [
    e for e in candidates
    if e.outcome_score <= 0.2
    and e.hallucination_risk >= 0.25
]
```

Step 5: Extract reusable patterns

```text
patterns = extract_patterns(positive_cases, current_resume_fp)
warnings = extract_warnings(caution_cases)
```

Step 6: Rewrite resume

```text
resume_v1 = rewrite_resume(
    resume_v0=resume_v0,
    jd=current_jd,
    successful_patterns=patterns,
    caution_patterns=warnings,
    constraints=[
        "Do not invent skills",
        "Only use skills supported by resume_v0",
        "If a JD skill is missing, do not fabricate it"
    ]
)
```

Step 7: Audit

```text
eval_result = audit_resume(
    resume_v0=resume_v0,
    resume_v1=resume_v1,
    jd=current_jd
)
```

Step 8: Gate

```text
if eval_result.hallucination_risk > 0.25:
    decision = "block"
elif eval_result.faithfulness_score < 0.85:
    decision = "revise"
elif eval_result.jd_match_score < 0.65:
    decision = "revise"
else:
    decision = "approve"
```

Step 9: Outcome feedback

```text
outcome = classify_gmail_outcome(email)

update_resume_version(
    resume_version_id=resume_v1.id,
    outcome_score=outcome.score,
    outcome_label=outcome.label
)

update_pattern_stats(
    patterns_used=patterns,
    stack_cluster=current_jd_fp.stack_cluster,
    outcome_score=outcome.score,
    faithfulness_score=eval_result.faithfulness_score
)
```

## 17. MongoDB 结构建议

新增或重点使用这些 collections：

```text
applications
application_events
application_artifacts
application_resume_versions
job_fingerprints
resume_evidence_maps
application_episodes
rewrite_patterns
```

### job_fingerprints

```json
{
  "_id": "jfp_001",
  "job_id": "job_456",
  "company": "Acme",
  "role_title": "Backend Engineering Intern",
  "role_family": "backend",
  "seniority": "intern",
  "tech_stack": [
    {
      "canonical": "python",
      "raw": "Python",
      "category": "language",
      "importance": "must_have",
      "weight": 1.0
    }
  ],
  "responsibilities": ["build backend APIs"],
  "stack_cluster": "python_backend_relational_db",
  "created_at": "2026-06-10T12:00:00Z"
}
```

### resume_evidence_maps

```json
{
  "_id": "rem_001",
  "candidate_id": "cand_001",
  "resume_version_id": "rv_000",
  "supported_skills": [
    {
      "canonical": "python",
      "category": "language",
      "evidence_strength": 0.95,
      "evidence_bullets": [
        "Built Python automation scripts for reporting workflows"
      ]
    }
  ],
  "unsupported_or_missing": ["kubernetes", "aws", "fastapi"]
}
```

### application_episodes

```json
{
  "_id": "ep_001",
  "application_id": "app_123",
  "job_id": "job_456",
  "candidate_id": "cand_001",
  "job_fingerprint_id": "jfp_001",
  "resume_v0_id": "rv_000",
  "resume_v1_id": "rv_789",
  "resume_evidence_map_id": "rem_001",
  "retrieval_context": {
    "similar_cases_used": [
      {
        "episode_id": "ep_077",
        "stack_sim": 0.88,
        "task_sim": 0.81,
        "resume_applicability": 0.86,
        "outcome_score": 1.0,
        "case_utility": 0.67
      }
    ],
    "patterns_used": [
      "pat_python_backend_api_first",
      "pat_sql_database_emphasis"
    ]
  },
  "eval_summary": {
    "faithfulness_score": 0.91,
    "jd_match_score": 0.84,
    "keyword_coverage": 0.73,
    "hallucination_risk": 0.09,
    "verdict": "approve"
  },
  "outcome": {
    "label": "INTERVIEW",
    "score": 1.0,
    "confidence": 0.94,
    "source": "gmail"
  },
  "arize": {
    "trace_id": "trace_abc",
    "retrieve_span_id": "span_retrieve_001",
    "rewrite_span_id": "span_rewrite_001",
    "audit_span_id": "span_audit_001"
  }
}
```

### rewrite_patterns

```json
{
  "_id": "pat_python_backend_api_first",
  "name": "Emphasize Python backend/API project early",
  "stack_cluster": "python_backend_relational_db",
  "applicability_conditions": {
    "jd_should_contain": ["python", "backend", "api"],
    "resume_must_have_evidence": ["python"],
    "resume_should_have_evidence": ["api", "sql", "database"]
  },
  "rewrite_instruction": "Move the strongest Python backend/API-related project or work experience into the top bullets. Use backend/API wording only if supported by resume_v0.",
  "risk_constraints": [
    "Do not add FastAPI unless explicitly supported.",
    "Do not claim production ownership unless supported.",
    "Do not add cloud infrastructure unless supported."
  ],
  "stats": {
    "attempts": 12,
    "weighted_successes": 5.4,
    "avg_outcome_score": 0.58,
    "avg_faithfulness_score": 0.92,
    "avg_hallucination_risk": 0.08,
    "bayesian_success_score": 0.54
  },
  "last_updated_at": "2026-06-10T12:00:00Z"
}
```

## 18. Elastic 怎么用

Elastic 适合做粗召回，不适合直接做最终决策。

Index 的字段建议：

```json
{
  "episode_id": "ep_001",
  "application_id": "app_123",
  "company": "Acme",
  "role_title": "Backend Engineering Intern",
  "role_family": "backend",
  "seniority": "intern",
  "stack_cluster": "python_backend_relational_db",
  "tech_stack_terms": [
    "python",
    "fastapi",
    "postgresql",
    "docker",
    "rest_api"
  ],
  "responsibilities_text": "build REST APIs work with relational databases backend services",
  "jd_text": "...",
  "outcome_score": 1.0,
  "outcome_label": "INTERVIEW",
  "faithfulness_score": 0.93,
  "hallucination_risk": 0.08,
  "updated_at": "2026-06-10T12:00:00Z"
}
```

查询当前 JD 时：

```text
Filter:
- role_family similar
- seniority similar
- faithfulness_score > 0.85
- hallucination_risk < 0.20

Should match:
- tech_stack_terms overlap
- stack_cluster
- responsibilities_text
- jd_text

Boost:
- outcome_score high
- recent cases
```

后续复杂版本可以加：

```text
jd_embedding
responsibility_embedding
script_score / knn search
```

但 hackathon 版本先做 keyword + filter + rerank 就够。

## 19. Arize / Phoenix 需要新增的 Trace

现在应该重点加一个 span：

```text
retrieve_similar_stack_experience
```

这个 span 很重要，因为它展示 agent 不是 blind generation，而是 evidence-based learning。

Span payload：

```json
{
  "span_name": "retrieve_similar_stack_experience",
  "span_kind": "RETRIEVER",
  "input": {
    "current_job_id": "job_456",
    "current_stack_cluster": "python_backend_relational_db",
    "current_tech_stack": [
      "python",
      "fastapi",
      "postgresql",
      "docker"
    ]
  },
  "retrieved_cases": [
    {
      "episode_id": "ep_077",
      "stack_sim": 0.88,
      "task_sim": 0.81,
      "resume_applicability": 0.86,
      "outcome_label": "INTERVIEW",
      "outcome_score": 1.0,
      "faithfulness_score": 0.91,
      "used": true
    },
    {
      "episode_id": "ep_044",
      "stack_sim": 0.79,
      "task_sim": 0.74,
      "resume_applicability": 0.31,
      "outcome_label": "INTERVIEW",
      "outcome_score": 1.0,
      "faithfulness_score": 0.94,
      "used": false,
      "reason_not_used": "Current resume lacks Kubernetes evidence"
    }
  ],
  "selected_patterns": [
    {
      "pattern_id": "pat_python_backend_api_first",
      "current_applicability_score": 0.88
    }
  ]
}
```

Demo 里可以这样讲：

```text
The agent found previous successful applications with a similar Python backend stack,
but it rejected a Kubernetes-heavy success case because the current resume had no Kubernetes evidence.
```

这就是：

```text
observability + evaluation + safety
```

## 20. 外部 Apply Link 和表单逻辑

SOMA 解决的是“如何更聪明、更安全地改简历”。Job Hunter Agent 还必须做好申请入口：

### JD 和 Apply Link Extraction

输出：

```json
{
  "company": "",
  "role": "",
  "location": "",
  "job_description": "",
  "requirements": [],
  "responsibilities": [],
  "salary_range": "",
  "employment_type": "",
  "external_apply_link": "",
  "source_url": "",
  "confidence": 0.0
}
```

Apply link 优先级：

```text
1. Direct company career application URL
2. ATS URL: Workday / Greenhouse / Lever / BrassRing / iCIMS / Ashby
3. LinkedIn redirected external URL
4. Source job URL fallback
```

### Form Discovery

Playwright 打开 apply page，发现字段，但不自动提交。

```json
{
  "fields": [
    {
      "label": "",
      "name": "",
      "type": "",
      "required": true,
      "options": [],
      "page": 1,
      "suggested_answer_key": ""
    }
  ],
  "requires_login": false,
  "requires_resume_upload": false,
  "requires_cover_letter": false,
  "requires_eeo": false
}
```

### Answer Generation

规则：

- 结构化 profile 里有答案，直接用 profile。
- resume 里有证据，才允许 LLM 推断。
- work authorization、visa、degree、employment history、certification 这类高风险字段不能编。
- 不确定就标记 `needs_review = true`。

输出：

```json
{
  "field_id": "",
  "answer": "",
  "source": "profile | resume | memory | llm_inference | user_required",
  "confidence": 0.0,
  "needs_review": false,
  "reason": ""
}
```

## 21. 一个完整 Demo 例子

当前 JD：

```text
Backend Software Engineer Intern
Requirements:
- Python
- FastAPI or Django
- PostgreSQL
- Docker
- REST APIs
```

当前 `resume_v0`：

```text
- Built Python scripts to automate CSV reporting.
- Created a SQL dashboard for tracking product metrics.
- Containerized a class project using Docker.
- Built a React portfolio site.
```

历史成功案例 A：

```text
JD: Python + Django + PostgreSQL + Docker
Outcome: Interview
Faithfulness: 0.94
Pattern: Move Python backend/database project to top.
```

历史成功案例 B：

```text
JD: Python + Kubernetes + AWS + microservices
Outcome: Interview
Faithfulness: 0.91
Pattern: Emphasize Kubernetes deployment.
```

历史失败案例 C：

```text
JD: Python + AWS + Kubernetes
Outcome: Rejection
Hallucination risk: 0.38
Problem: Resume added Kubernetes without V0 evidence.
```

算法判断：

```text
A:
stack_sim high
task_sim high
applicability high
use it

B:
stack_sim medium
outcome high
but applicability low because current resume has no Kubernetes
do not use Kubernetes pattern

C:
use as warning
avoid adding unsupported Kubernetes/AWS
```

最后生成策略：

```text
Use:
- Emphasize Python automation
- Emphasize SQL/PostgreSQL-like database work if supported
- Mention Docker containerization carefully

Do not use:
- Kubernetes
- AWS
- production microservices
```

这个 demo 很清楚：agent 会学习历史经验，但不会造假。

## 22. Hackathon 最小实现版本

1-2 天内不要做复杂模型。P0 做这个就够：

```text
1. LLM extract JD tech_stack JSON
2. LLM extract resume evidence JSON
3. Store each application episode in MongoDB
4. Sync episode searchable fields to Elastic
5. Use simple Python similarity scoring
6. Retrieve top 3 similar successful cases
7. Extract successful rewrite patterns
8. Generate resume_v1
9. Audit faithfulness with Arize/Phoenix
10. Mock or classify Gmail outcome
11. Update outcome_score and pattern stats
```

Similarity 简化版：

```python
def weighted_jaccard(current_skills, past_skills):
    all_skills = set(current_skills) | set(past_skills)
    numerator = 0
    denominator = 0

    for skill in all_skills:
        w_current = current_skills.get(skill, 0)
        w_past = past_skills.get(skill, 0)
        numerator += min(w_current, w_past)
        denominator += max(w_current, w_past)

    return numerator / denominator if denominator else 0
```

Case ranking 简化版：

```python
case_score = (
    0.45 * stack_similarity
  + 0.20 * task_similarity
  + 0.15 * resume_applicability
  + 0.10 * outcome_score
  + 0.10 * faithfulness_score
  - 0.30 * hallucination_risk
)
```

Gate 简化版：

```python
if hallucination_risk > 0.25:
    decision = "block"
elif faithfulness_score < 0.85:
    decision = "revise"
else:
    decision = "approve"
```

## 23. 实施优先级

### P0: 先把 SOMA 最小闭环跑通

- 新增 `job_fingerprints`。
- 新增 `resume_evidence_maps`。
- 新增 `application_episodes`。
- 新增 `rewrite_patterns`。
- MongoDB episode 同步到 Elastic。
- Elastic 返回 top similar episodes。
- Python rerank。
- Arize 新增 `retrieve_similar_stack_experience` span。
- 8501 展示 retrieved cases、used patterns、blocked patterns。

### P1: 接真实 Gmail Outcome

- 从 Gmail 识别 `INTERVIEW`、`ONLINE_ASSESSMENT`、`RECRUITER_REPLY`、`REJECTION`、`NO_RESPONSE`。
- 写回 MongoDB outcome。
- 更新 pattern stats。
- Elastic re-index。

### P2: 表单和申请自动化

- 按 ATS vendor 做表单发现。
- 账号创建/登录状态记忆。
- 常见问题答案库。
- 高风险字段人工确认。
- 最终 submit 人工确认。

### P3: 更复杂的 Elastic 和 Embedding

- `jd_embedding`
- `responsibility_embedding`
- vector search
- hybrid search
- cluster analytics
- pattern-level dashboard

## 24. 当前最应该实现的下一步

推荐下一步：

```text
实现 MongoDB → Elastic 的 episode sync，并在 8501 展示 SOMA 检索结果。
```

原因：

- MongoDB 已经是 memory/source of truth。
- Elastic track 需要真实搜索能力，不只是本地 JD cache。
- SOMA 需要从历史 episodes 做粗召回。
- 8501 展示 top similar episodes + used/blocked patterns，demo 会非常清楚。

最小 endpoint：

```text
POST /sync-mongo-episodes
POST /retrieve-similar-episodes
POST /rerank-episodes
POST /extract-rewrite-patterns
```

最小 UI：

```text
当前 JD fingerprint
当前 resume evidence map
Elastic retrieved episodes
SOMA reranked cases
Selected positive patterns
Blocked unsafe patterns
Arize audit result
Final decision: approve / revise / block
```

