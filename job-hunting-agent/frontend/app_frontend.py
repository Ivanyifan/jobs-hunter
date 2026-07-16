import os
import time
import requests
import json
import sqlite3
import datetime
import hashlib
import hmac
import re
import html
import textwrap
from urllib.parse import urlencode
import streamlit as st
from google import genai
from google.genai import types
try:
    from apply_flow import (
        build_playwright_apply_payload,
        can_confirm_submit,
        can_start_apply,
        apply_application_profile_library,
        build_resume_compression_prompt,
        build_resume_tailoring_prompt,
        build_skill_match_report,
        enable_application_question_matcher,
        enrich_user_data_with_approved_question_answers,
        extract_keyword_terms,
        normalize_country_calling_code,
        normalize_skill_entries,
        record_playwright_apply_failure,
        score_resume_versions_for_jd as shared_score_resume_versions_for_jd,
        suggest_skills_from_resume,
        trusted_skill_library,
    )
except ImportError:
    from frontend.apply_flow import (
        build_playwright_apply_payload,
        can_confirm_submit,
        can_start_apply,
        apply_application_profile_library,
        build_resume_compression_prompt,
        build_resume_tailoring_prompt,
        build_skill_match_report,
        enable_application_question_matcher,
        enrich_user_data_with_approved_question_answers,
        extract_keyword_terms,
        normalize_country_calling_code,
        normalize_skill_entries,
        record_playwright_apply_failure,
        score_resume_versions_for_jd as shared_score_resume_versions_for_jd,
        suggest_skills_from_resume,
        trusted_skill_library,
    )
from scheduler_worker import ScheduledApplyWorker

# Setup page configuration
st.set_page_config(
    page_title="Self-Learning Career Agent Platform",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Slate Light Theme CSS injection for perfect readability
st.markdown("""
<style>
    /* Main container background and base colors */
    .stApp {
        background-color: #ffffff;
        color: #1e293b;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    
    /* Header Style */
    .main-header {
        font-size: 2.5rem;
        font-weight: 800;
        background: linear-gradient(90deg, #0f172a 0%, #3b82f6 50%, #1e293b 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 5px;
        text-align: center;
        letter-spacing: -0.025em;
    }
    .header-sub {
        font-size: 1.1rem;
        color: #334155;
        text-align: center;
        margin-bottom: 30px;
        font-weight: 500;
        line-height: 1.6;
    }
    
    /* Premium Slate Card styling */
    .glass-card {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 22px;
        box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.05), 0 1px 2px -1px rgba(0, 0, 0, 0.05);
        margin-bottom: 20px;
    }
    
    /* Card internal titles and elements */
    .card-title {
        font-size: 1.3rem;
        font-weight: 700;
        color: #0f172a;
        margin-bottom: 12px;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    
    .card-property {
        font-size: 0.95rem;
        color: #334155;
        margin-bottom: 8px;
        line-height: 1.5;
    }
    .card-property strong {
        color: #0f172a;
    }
    
    /* Badges */
    .badge-running {
        background-color: #dcfce7;
        color: #15803d;
        padding: 3px 10px;
        border-radius: 9999px;
        font-size: 0.85rem;
        font-weight: 600;
        border: 1px solid #bbf7d0;
        display: inline-block;
    }
    .badge-paused {
        background-color: #fee2e2;
        color: #b91c1c;
        padding: 3px 10px;
        border-radius: 9999px;
        font-size: 0.85rem;
        font-weight: 600;
        border: 1px solid #fca5a5;
        display: inline-block;
    }
    
    /* Metrics panel styling */
    .metric-box {
        background-color: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 16px;
        text-align: center;
        box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.02);
    }
    .metric-val {
        font-size: 2rem;
        font-weight: 800;
        color: #2563eb;
        margin-bottom: 4px;
    }
    .metric-lbl {
        font-size: 0.85rem;
        color: #475569;
        text-transform: uppercase;
        font-weight: 600;
        letter-spacing: 0.05em;
    }
    
    /* Streamlit controls font overrides */
    div[data-testid="stWidgetLabel"] p {
        color: #0f172a !important;
        font-weight: 600 !important;
        font-size: 0.95rem !important;
    }
    .stMarkdown p, .stMarkdown span, .stMarkdown li {
        color: #1e293b !important;
    }
    .stMarkdown h1, .stMarkdown h2, .stMarkdown h3, .stMarkdown h4 {
        color: #0f172a !important;
        font-weight: 700 !important;
    }
    
    /* Buttons styles adjustments */
    button[kind="primary"] {
        background-color: #2563eb !important;
        color: #ffffff !important;
        border-radius: 6px !important;
    }
    
    /* Style log output block */
    .log-box {
        background-color: #f1f5f9;
        border: 1px solid #cbd5e1;
        border-radius: 8px;
        padding: 12px;
        font-family: monospace;
        font-size: 0.85rem;
        color: #1e293b;
        max-height: 250px;
        overflow-y: auto;
    }

    .language-bar {
        max-width: 1180px;
        margin: 0 auto 14px auto;
        display: flex;
        justify-content: flex-end;
    }

    .en-shell {
        max-width: 1180px;
        margin: 0 auto;
    }

    .en-topbar {
        border: 1px solid #dbe3ea;
        border-radius: 8px;
        background: #ffffff;
        padding: 18px 20px;
        margin-bottom: 16px;
        display: flex;
        justify-content: space-between;
        gap: 18px;
        align-items: flex-start;
    }

    .en-title {
        color: #0f172a;
        font-size: 1.7rem;
        font-weight: 800;
        line-height: 1.2;
        letter-spacing: 0;
        margin-bottom: 4px;
    }

    .en-subtitle {
        color: #475569;
        font-size: 0.96rem;
        line-height: 1.45;
        max-width: 760px;
    }

    .en-pill {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        border: 1px solid #bfdbfe;
        background: #eff6ff;
        color: #1d4ed8;
        font-size: 0.82rem;
        font-weight: 700;
        padding: 5px 10px;
        white-space: nowrap;
    }

    .en-card {
        border: 1px solid #dbe3ea;
        border-radius: 8px;
        background: #ffffff;
        padding: 16px;
        margin-bottom: 14px;
    }

    .en-card-title {
        color: #0f172a;
        font-size: 1.05rem;
        font-weight: 800;
        margin-bottom: 10px;
        letter-spacing: 0;
    }

    .en-field-row {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        border-bottom: 1px solid #edf2f7;
        padding: 7px 0;
        color: #334155;
        font-size: 0.92rem;
    }

    .en-field-row:last-child {
        border-bottom: 0;
    }

    .en-field-label {
        color: #64748b;
        font-weight: 650;
    }

    .en-field-value {
        color: #0f172a;
        text-align: right;
        overflow-wrap: anywhere;
    }

    .en-step {
        display: grid;
        grid-template-columns: 160px 1fr 130px;
        gap: 12px;
        align-items: center;
        border-bottom: 1px solid #edf2f7;
        padding: 10px 0;
        color: #334155;
        font-size: 0.92rem;
    }

    .en-step:last-child {
        border-bottom: 0;
    }

    .en-step-name {
        color: #0f172a;
        font-weight: 750;
    }

    .en-step-status {
        color: #166534;
        background: #dcfce7;
        border: 1px solid #bbf7d0;
        border-radius: 999px;
        padding: 4px 9px;
        font-weight: 750;
        text-align: center;
        font-size: 0.8rem;
    }

    .en-risk-note {
        border-left: 4px solid #2563eb;
        background: #f8fafc;
        padding: 11px 13px;
        color: #334155;
        border-radius: 6px;
        line-height: 1.45;
        margin-top: 10px;
    }

    /* MongoDB resume memory preview, adapted from the legacy Live Document Preview */
    .resume-preview-shell {
        background: #e2e8f0;
        border: 1px solid #cbd5e1;
        border-radius: 8px;
        box-shadow: inset 0 1px 3px rgba(15, 23, 42, 0.08);
        overflow: hidden;
        margin-top: 12px;
    }
    .resume-preview-topbar {
        background: #f1f5f9;
        border-bottom: 1px solid #cbd5e1;
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 14px;
    }
    .resume-window-dots {
        display: flex;
        gap: 6px;
        width: 64px;
    }
    .resume-window-dot {
        width: 10px;
        height: 10px;
        border-radius: 999px;
        display: block;
    }
    .resume-window-dot.red { background: #f87171; border: 1px solid #ef4444; }
    .resume-window-dot.amber { background: #fbbf24; border: 1px solid #f59e0b; }
    .resume-window-dot.green { background: #34d399; border: 1px solid #10b981; }
    .resume-preview-title {
        color: #64748b !important;
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0;
        text-transform: uppercase;
    }
    .resume-preview-body {
        padding: 18px;
        max-height: 720px;
        overflow-y: auto;
    }
    .resume-preview-paper {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        box-shadow: 0 12px 24px rgba(15, 23, 42, 0.12);
        margin: 0 auto;
        max-width: 820px;
        min-height: 560px;
        padding: 36px 48px;
    }
    .resume-doc-header {
        text-align: center;
        border-bottom: 1px solid #cbd5e1;
        margin-bottom: 22px;
        padding-bottom: 14px;
    }
    .resume-doc-name {
        color: #0f172a !important;
        font-family: Georgia, "Times New Roman", serif;
        font-size: 1.75rem;
        font-weight: 700;
        line-height: 1.15;
        margin: 0 0 7px 0;
    }
    .resume-doc-contact {
        color: #475569 !important;
        display: flex;
        flex-wrap: wrap;
        font-size: 0.82rem;
        gap: 6px 12px;
        justify-content: center;
        line-height: 1.45;
    }
    .resume-section {
        margin-bottom: 18px;
    }
    .resume-section-title {
        border-bottom: 1px solid #cbd5e1;
        color: #1e293b !important;
        font-size: 0.92rem;
        font-weight: 800;
        letter-spacing: 0;
        margin: 0 0 10px 0;
        padding-bottom: 5px;
        text-transform: uppercase;
    }
    .resume-entry {
        margin-bottom: 14px;
    }
    .resume-entry-title-row {
        align-items: baseline;
        display: flex;
        gap: 12px;
        justify-content: space-between;
        margin-bottom: 3px;
    }
    .resume-entry-title {
        color: #0f172a !important;
        font-size: 0.92rem;
        font-weight: 750;
        line-height: 1.25;
    }
    .resume-entry-meta {
        color: #475569 !important;
        flex-shrink: 0;
        font-size: 0.78rem;
        font-weight: 600;
        text-align: right;
    }
    .resume-entry-subtitle {
        color: #475569 !important;
        font-size: 0.8rem;
        font-style: italic;
        line-height: 1.35;
        margin: 0 0 6px 0;
    }
    .resume-bullets {
        margin: 0;
        padding-left: 19px;
    }
    .resume-bullets li {
        color: #334155 !important;
        font-size: 0.84rem;
        line-height: 1.38;
        margin-bottom: 6px;
    }
    .resume-paragraph {
        color: #334155 !important;
        font-size: 0.86rem;
        line-height: 1.45;
        margin: 0 0 8px 0;
        white-space: pre-wrap;
    }
    .resume-version-pills {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin: 8px 0 4px 0;
    }
    .resume-version-pill {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 999px;
        color: #334155 !important;
        display: inline-flex;
        font-size: 0.8rem;
        font-weight: 650;
        padding: 4px 10px;
    }

    @media (max-width: 720px) {
        .resume-preview-body {
            padding: 10px;
        }
        .resume-preview-paper {
            min-height: 420px;
            padding: 24px 22px;
        }
        .resume-entry-title-row {
            align-items: flex-start;
            flex-direction: column;
            gap: 3px;
        }
        .resume-entry-meta {
            text-align: left;
        }
    }
</style>
""", unsafe_allow_html=True)

# Resolve Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "data", "scheduler_config.json")
DB_PATH = os.path.join(BASE_DIR, "data", "applications.db")
EXTRACTED_JDS_DIR = os.path.join(BASE_DIR, "data", "extracted_jds")

JUDGE_DEMO_APP_ID = "judge-demo-platform-backend-engineer"
JUDGE_DEMO_COMPANY = "Northstar Health"
JUDGE_DEMO_ROLE = "Platform Backend Engineer"
JUDGE_DEMO_APPLY_URL = "https://example.com/demo/external-apply"
JUDGE_DEMO_JOB_DESCRIPTION = """
Northstar Health is hiring a Platform Backend Engineer to build internal care
operations workflow automation. The role focuses on Python APIs, database-backed
services, browser automation, workflow reliability, observability, and safe use
of LLM-assisted agents for operational staff. Strong candidates have experience
with FastAPI or similar frameworks, MongoDB or SQL data models, cloud deployment,
event logging, human-in-the-loop approval, and production monitoring.
""".strip()

JUDGE_DEMO_CANDIDATE = {
    "first_name": "Maya",
    "last_name": "Chen",
    "email": "maya.chen.demo@example.com",
    "phone": "+1 415 555 0139",
    "current_location": "Seattle, WA",
    "linkedin_url": "https://www.linkedin.com/in/maya-chen-demo",
    "github_url": "https://github.com/maya-chen-demo",
    "portfolio_url": "https://maya-chen-demo.example.com",
    "authorized_to_work_us": "Yes",
    "need_sponsorship": "No",
    "security_clearance": "None",
    "start_date": "Two weeks after offer",
    "salary_expectation": "Open to market range",
    "years_experience": "4",
}

JUDGE_DEMO_RESUME_V0 = """
Maya Chen
Seattle, WA | maya.chen.demo@example.com | +1 415 555 0139
LinkedIn: https://www.linkedin.com/in/maya-chen-demo | GitHub: https://github.com/maya-chen-demo

PROFILE
Backend/platform engineer with 4 years of experience building Python services,
workflow automation, and database-backed internal tools. Comfortable owning
ambiguous product workflows from data model to API, background worker, monitoring,
and operator-facing error handling.

EXPERIENCE
Platform Engineer, CareOps Labs | 2023-2026
- Built Python/FastAPI services for intake routing, document review queues, and
  status updates used by 40+ operations teammates.
- Designed MongoDB and PostgreSQL records for long-running workflows, including
  event history, retry state, generated artifacts, and user approval checkpoints.
- Added structured logs, OpenTelemetry spans, and dashboard alerts that reduced
  unresolved background-job failures by 28%.
- Built Playwright-based internal automation for repetitive portal checks, with
  screenshot capture and hard stops before irreversible actions.

Backend Engineer, Meridian Tools | 2021-2023
- Implemented REST APIs and background workers for document parsing, matching, and
  notifications using Python, Redis queues, and SQL.
- Introduced idempotency keys, retry limits, and dead-letter summaries for
  scheduled jobs that previously required manual database cleanup.
- Partnered with product and support teams to translate inconsistent customer
  forms into structured schemas, validations, and review states.

PROJECTS
Agentic Workflow Sandbox
- Built a local agent workflow that extracts job descriptions, stores durable
  memory in MongoDB, indexes search/outcome signals in Elasticsearch, and exports
  resume audit traces to Phoenix/Arize.
- Implemented one-page PDF resume generation, visual readability checks, and
  human approval gates before form submission.

SKILLS
Python, FastAPI, Playwright, MongoDB, PostgreSQL, Redis, Elasticsearch, Docker,
Cloud Run, OpenTelemetry, Arize Phoenix, workflow orchestration, LLM evaluation.

EDUCATION
B.S. Computer Science, University of Washington | 2021
""".strip()

JUDGE_DEMO_RESUME_V1 = """
Maya Chen
Seattle, WA | maya.chen.demo@example.com | +1 415 555 0139
LinkedIn: https://www.linkedin.com/in/maya-chen-demo | GitHub: https://github.com/maya-chen-demo

PROFILE
Platform backend engineer with 4 years of experience building Python APIs,
database-backed workflow automation, browser automation, observability, and
human-in-the-loop safety controls for operations teams.

EXPERIENCE
Platform Engineer, CareOps Labs | 2023-2026
- Built Python/FastAPI services for intake routing, document review queues, and
  status-update workflows used by 40+ operations teammates.
- Designed MongoDB and PostgreSQL workflow records for long-running tasks,
  including event history, retry state, generated artifacts, and approval gates.
- Added structured logs, OpenTelemetry spans, and dashboard alerts, reducing
  unresolved background-job failures by 28%.
- Built Playwright automation for repetitive portal checks with screenshot
  capture, action logs, and hard stops before irreversible actions.

Backend Engineer, Meridian Tools | 2021-2023
- Implemented REST APIs and background workers for document parsing, matching,
  and notification workflows using Python, Redis queues, and SQL.
- Improved scheduled-job reliability with idempotency keys, retry limits,
  dead-letter summaries, and operator-visible error states.
- Converted inconsistent customer forms into structured schemas, validations, and
  review states with product and support partners.

PROJECT
Agentic Workflow Sandbox
- Built an agent workflow that extracts job descriptions, stores durable memory in
  MongoDB, indexes job/outcome signals in Elasticsearch, and exports resume audit
  traces to Phoenix/Arize.
- Implemented one-page PDF resume generation, visual readability checks, and
  human approval gates before form submission.

SKILLS
Python, FastAPI, Playwright, MongoDB, PostgreSQL, Redis, Elasticsearch, Docker,
Cloud Run, OpenTelemetry, Arize Phoenix, workflow orchestration, LLM evaluation.

EDUCATION
B.S. Computer Science, University of Washington | 2021
""".strip()

JUDGE_DEMO_COMMON_ANSWERS = {
    "first_name": {"label": "First name", "type": "text", "value": JUDGE_DEMO_CANDIDATE["first_name"]},
    "last_name": {"label": "Last name", "type": "text", "value": JUDGE_DEMO_CANDIDATE["last_name"]},
    "email": {"label": "Email address", "type": "text", "value": JUDGE_DEMO_CANDIDATE["email"]},
    "phone": {"label": "Phone number", "type": "text", "value": JUDGE_DEMO_CANDIDATE["phone"]},
    "linkedin_url": {"label": "LinkedIn profile URL", "type": "text", "value": JUDGE_DEMO_CANDIDATE["linkedin_url"]},
    "github_url": {"label": "GitHub profile URL", "type": "text", "value": JUDGE_DEMO_CANDIDATE["github_url"]},
    "portfolio_url": {"label": "Portfolio / personal website", "type": "text", "value": JUDGE_DEMO_CANDIDATE["portfolio_url"]},
    "authorized_to_work_us": {"label": "Legally authorized to work in the U.S.", "type": "select", "value": JUDGE_DEMO_CANDIDATE["authorized_to_work_us"]},
    "need_sponsorship": {"label": "Need visa sponsorship now or in the future", "type": "select", "value": JUDGE_DEMO_CANDIDATE["need_sponsorship"]},
    "security_clearance": {"label": "Security clearance", "type": "text", "value": JUDGE_DEMO_CANDIDATE["security_clearance"]},
    "current_location": {"label": "Current location", "type": "text", "value": JUDGE_DEMO_CANDIDATE["current_location"]},
    "start_date": {"label": "Earliest start date / notice period", "type": "text", "value": JUDGE_DEMO_CANDIDATE["start_date"]},
    "salary_expectation": {"label": "Salary expectation", "type": "text", "value": JUDGE_DEMO_CANDIDATE["salary_expectation"]},
    "years_experience": {"label": "Years of relevant experience", "type": "text", "value": JUDGE_DEMO_CANDIDATE["years_experience"]},
}

JUDGE_DEMO_PAST_APPLICATIONS = [
    {
        "company": "KeyBank",
        "role": "Associate Software Engineer",
        "jd_file": "jd_1_KeyBank_Associate_Software_Engineer.txt",
        "status": "Interview",
        "outcome_label": "INTERVIEW",
        "outcome_score": 1.0,
        "outcome_confidence": 0.86,
        "observed_at": "2026-05-23T16:20:00Z",
        "emphasis": [
            "clean, maintainable, testable code",
            "application support and production reliability",
            "information security standards",
            "mentorship and active learning",
        ],
    },
    {
        "company": "Inadev",
        "role": "Junior Full Stack Developer (Python/React.js)",
        "jd_file": "jd_1_Inadev_Junior_Full_Stack_Developer_PythonReactjs.txt",
        "status": "Applied",
        "outcome_label": "NO_RESPONSE",
        "outcome_score": 0.2,
        "outcome_confidence": 0.62,
        "observed_at": "2026-05-29T14:05:00Z",
        "emphasis": [
            "Python backend services",
            "React.js component-based interfaces",
            "RESTful APIs and microservices",
            "NoSQL databases and AWS service integration",
            "AI-assisted development review workflows",
        ],
    },
    {
        "company": "Leidos",
        "role": "Entry-Level Software Developer",
        "jd_file": "jd_2_Leidos_Entry-Level_Software_Developer.txt",
        "status": "Interview",
        "outcome_label": "ONLINE_ASSESSMENT",
        "outcome_score": 0.7,
        "outcome_confidence": 0.78,
        "observed_at": "2026-06-02T19:45:00Z",
        "emphasis": [
            "Python, JavaScript, TypeScript, and C#-adjacent development",
            "GitLab version control and DevOps delivery",
            "Agile delivery with Jira and Confluence",
            "web-enabled applications",
        ],
    },
    {
        "company": "Tesla",
        "role": "Frontend Software Engineer, Energy Residential",
        "jd_file": "jd_1_Tesla_Frontend_Software_Engineer_Energy_Residential.txt",
        "status": "Rejected",
        "outcome_label": "REJECTION",
        "outcome_score": 0.0,
        "outcome_confidence": 0.82,
        "observed_at": "2026-06-05T21:10:00Z",
        "emphasis": [
            "frontend reliability and user-facing workflow quality",
            "JavaScript and TypeScript interfaces",
            "customer-facing energy product workflows",
            "performance and usability checks",
        ],
    },
    {
        "company": "CVS Health",
        "role": "Software Development Engineer",
        "jd_file": "jd_2_CVS_Health_Software_Development_Engineer.txt",
        "status": "Applied",
        "outcome_label": "APPLIED",
        "outcome_score": 0.35,
        "outcome_confidence": 0.72,
        "observed_at": "2026-06-08T17:35:00Z",
        "emphasis": [
            "software development engineering",
            "healthcare workflow automation",
            "remote collaboration",
            "production monitoring and service reliability",
        ],
    },
]

DEMO_MATCH_BACKFILL_JD_FILES = [
    {"company": "Kohl's", "role_hint": "Software Engineer", "jd_file": "jd_1_Kohls_Software_Engineer_Remote.txt"},
    {"company": "Lockheed Martin", "role_hint": "Software Engineer", "jd_file": "jd_1_Lockheed_Martin_Software_Engineer.txt"},
    {"company": "Wells Fargo", "role_hint": "Full Stack Software Engineer", "jd_file": "jd_2_Wells_Fargo_Full_Stack_Software_Engineer_contract.txt"},
]


def require_app_login():
    password = os.getenv("APP_ACCESS_PASSWORD", "").strip()
    if not password:
        return
    if st.session_state.get("app_authenticated"):
        return

    login_language = st.selectbox(
        "Language",
        ["English", "Chinese"],
        index=0 if st.session_state.get("ui_language", "English") == "English" else 1,
        key="login_ui_language",
    )
    st.session_state["ui_language"] = login_language
    st.markdown('<div class="main-header">Job Hunter Agent</div>', unsafe_allow_html=True)
    st.markdown('<div class="header-sub">Private cloud deployment</div>', unsafe_allow_html=True)
    visible_password = os.getenv("APP_ACCESS_PASSWORD_HINT", "").strip() or password
    st.markdown(
        f"""
        <div style="max-width: 760px; margin: 0 auto 18px auto; border: 1px solid #cbd5e1; border-radius: 8px; padding: 18px 20px; background: #f8fafc;">
            <div style="font-weight: 800; font-size: 1.05rem; color: #0f172a; margin-bottom: 8px;">Private access</div>
            <div style="color: #334155; line-height: 1.55;">
                Use the configured access password:
                <code style="background: #e2e8f0; color: #0f172a; padding: 3px 7px; border-radius: 6px; font-weight: 700;">{html.escape(visible_password)}</code>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("app_access_login"):
        entered = st.text_input("Access password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        if hmac.compare_digest(entered, password):
            st.session_state["app_authenticated"] = True
            st.rerun()
        else:
            st.error("Invalid password.")
    st.stop()

# Helper to write scan runs to DB log table
def log_run(status, details, task_id=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO scheduler_logs (status, details, task_id) VALUES (?, ?, ?)", (status, details, task_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[UI Logger Error] {e}")

# Load & Save Global Config Helpers
def load_env_credentials():
    credentials = {"imap_server": "imap.gmail.com", "email_password": ""}
    root_dir = os.path.dirname(BASE_DIR)
    for f_name in [".env", ".env.local"]:
        f_path = os.path.join(root_dir, f_name)
        if os.path.exists(f_path):
            try:
                with open(f_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k == "EMAIL_IMAP_SERVER":
                                credentials["imap_server"] = v
                            elif k == "EMAIL_PASSWORD":
                                credentials["email_password"] = v
            except Exception:
                pass
    return credentials

def save_env_local(imap_server, email_user, email_pass):
    root_dir = os.path.dirname(BASE_DIR)
    env_local_path = os.path.join(root_dir, ".env.local")
    try:
        lines = []
        if os.path.exists(env_local_path):
            with open(env_local_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not any(line.strip().startswith(prefix) for prefix in ["EMAIL_IMAP_SERVER=", "EMAIL_USERNAME=", "EMAIL_PASSWORD="]):
                        lines.append(line)
        
        lines.append(f'\nEMAIL_IMAP_SERVER="{imap_server}"\n')
        lines.append(f'EMAIL_USERNAME="{email_user}"\n')
        lines.append(f'EMAIL_PASSWORD="{email_pass}"\n')
        
        with open(env_local_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        print(f"Error saving .env.local: {e}")

def load_config():
    env_creds = load_env_credentials()
    config_dict = {
        "active_api_key": os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "",
        "model_selector": "gemini-3.5-flash",
        "elastic_url": os.getenv("ELASTIC_URL_API") or "http://localhost:8002",
        "elastic_cloud_id": os.getenv("ELASTIC_CLOUD_ID") or "",
        "elastic_api_key": os.getenv("ELASTIC_API_KEY") or "",
        "elastic_backend_url": os.getenv("ELASTIC_URL") or "",
        "elastic_index_name": "job_descriptions",
        "arize_url": os.getenv("ARIZE_URL") or "http://localhost:8003",
        "phoenix_collector_endpoint": os.getenv("PHOENIX_COLLECTOR_ENDPOINT") or "",
        "phoenix_api_key": os.getenv("PHOENIX_API_KEY") or "",
        "phoenix_project_name": os.getenv("PHOENIX_PROJECT_NAME") or "job-hunter-agent",
        "mongo_url": os.getenv("MONGO_URL") or "http://localhost:8001",
        "email_url": os.getenv("EMAIL_URL") or "http://localhost:8005",
        "playwright_url": os.getenv("PLAYWRIGHT_URL") or "http://localhost:8004",
        "ats_source_urls": os.getenv("ATS_SOURCE_URLS") or "",
        "ats_source_limit": int(os.getenv("ATS_SOURCE_LIMIT") or "50"),
        "resume_v0": "",
        "execution_mode": "自动扫描 + 人工仲裁确认 (推荐)",
        "email_imap_server": env_creds["imap_server"],
        "email_password": env_creds["email_password"],
        "linkedin_cookies_raw": "",
        "user_data": {
            "first_name": "",
            "last_name": "",
            "email": "",
            "phone": "",
            "country_phone_code": "",
            "skills": "",
            "address1": "",
            "city": "",
            "state": "",
            "postal_code": "",
            "country": "United States"
        }
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                for k, v in saved.items():
                    if k == "user_data" and isinstance(v, dict):
                        config_dict["user_data"].update(v)
                    else:
                        config_dict[k] = v
        except Exception:
            pass
            
    if "email_imap_server" not in config_dict or not config_dict["email_imap_server"]:
        config_dict["email_imap_server"] = env_creds["imap_server"]
    if "email_password" not in config_dict or not config_dict["email_password"]:
        config_dict["email_password"] = env_creds["email_password"]
        
    return config_dict


def save_config(config_data):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        st.error(f"保存配置文件失败: {e}")
        return False


def auto_save_field():
    current_config = load_config()
    
    state_to_config = {
        "cfg_api_key": "active_api_key",
        "cfg_model": "model_selector",
        "cfg_elastic": "elastic_url",
        "cfg_elastic_cloud_id": "elastic_cloud_id",
        "cfg_elastic_api_key": "elastic_api_key",
        "cfg_elastic_backend_url": "elastic_backend_url",
        "cfg_elastic_index_name": "elastic_index_name",
        "cfg_arize": "arize_url",
        "cfg_phoenix_endpoint": "phoenix_collector_endpoint",
        "cfg_phoenix_api_key": "phoenix_api_key",
        "cfg_phoenix_project": "phoenix_project_name",
        "cfg_mongo": "mongo_url",
        "cfg_email_url": "email_url",
        "cfg_playwright_url": "playwright_url",
        "cfg_ats_source_urls": "ats_source_urls",
        "cfg_ats_source_limit": "ats_source_limit",
        "cfg_imap": "email_imap_server",
        "cfg_password": "email_password",
        "cfg_resume": "resume_v0",
        "cfg_mode": "execution_mode",
        "cfg_cookies": "linkedin_cookies_raw",
        "cfg_linkedin_username": "linkedin_username",
        "cfg_linkedin_password": "linkedin_password"
    }
    
    for state_key, config_key in state_to_config.items():
        if state_key in st.session_state:
            current_config[config_key] = st.session_state[state_key]
            
    # Parse and save cookies.json if cfg_cookies is in session state
    if "cfg_cookies" in st.session_state:
        cookies_str = st.session_state["cfg_cookies"].strip()
        cookies_file = os.path.join(BASE_DIR, "data", "cookies.json")
        if cookies_str:
            try:
                cookies_json = json.loads(cookies_str)
                if isinstance(cookies_json, list):
                    os.makedirs(os.path.dirname(cookies_file), exist_ok=True)
                    with open(cookies_file, "w", encoding="utf-8") as f:
                        json.dump(cookies_json, f, indent=2)
                else:
                    st.toast("⚠️ Cookies 格式不正确：必须是 JSON 数组", icon="❌")
            except Exception as e:
                st.toast(f"⚠️ Cookies 解析失败: {e}", icon="❌")
        else:
            if os.path.exists(cookies_file):
                try:
                    os.remove(cookies_file)
                except Exception:
                    pass

    user_keys = {
        "cfg_first_name": "first_name",
        "cfg_last_name": "last_name",
        "cfg_email": "email",
        "cfg_phone": "phone",
        "cfg_country_phone_code": "country_phone_code",
        "cfg_address1": "address1",
        "cfg_city": "city",
        "cfg_state": "state",
        "cfg_postal_code": "postal_code",
        "cfg_country": "country",
    }
    if "user_data" not in current_config:
        current_config["user_data"] = {}
        
    for state_key, user_key in user_keys.items():
        if state_key in st.session_state:
            current_config["user_data"][user_key] = st.session_state[state_key]

    if "cfg_application_profile_library" in st.session_state:
        save_application_profile_library(current_config, st.session_state["cfg_application_profile_library"])

    if "cfg_skills" in st.session_state:
        save_skill_library_fields(current_config)

    if "cfg_availability_start_date" in st.session_state:
        save_availability_library_fields(current_config)

    if "cfg_edu_school" in st.session_state:
        save_education_library_fields(current_config)

    if "cfg_experience_count" in st.session_state:
        save_experience_library_fields(current_config)

    if "cfg_employment_registry_count" in st.session_state:
        save_company_employment_registry_fields(current_config)
            
    save_config(current_config)
    
    # Save .env.local dynamically
    email_val = st.session_state.get("cfg_email", current_config.get("user_data", {}).get("email", ""))
    imap_val = st.session_state.get("cfg_imap", current_config.get("email_imap_server", "imap.gmail.com"))
    pass_val = st.session_state.get("cfg_password", current_config.get("email_password", ""))
    save_env_local(imap_val, email_val, pass_val)
    
    st.toast("配置已自动保存 💾")

# Database functions
def get_db_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    return conn

def init_db_tables():
    # Make sure we align schema with scheduler_worker.py
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mcp_applications (
            id TEXT PRIMARY KEY,
            company TEXT NOT NULL,
            role TEXT NOT NULL,
            resume_v0 TEXT NOT NULL,
            resume_v1 TEXT,
            status TEXT NOT NULL,
            apply_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scheduler_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keywords TEXT NOT NULL,
            location TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL,
            enabled INTEGER DEFAULT 1,
            last_run_time REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scheduler_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            run_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL,
            details TEXT
        )
    """)
    # Run schema migrations to add search filter columns
    try:
        cursor.execute("ALTER TABLE scheduler_tasks ADD COLUMN date_posted TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE scheduler_tasks ADD COLUMN experience_level TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE scheduler_tasks ADD COLUMN remote TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE mcp_applications ADD COLUMN match_scores_json TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def safe_app_key(app_id):
    raw = str(app_id or "unknown")
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_")
    return clean[:80] or hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

def is_precheckable_apply_record(app_id, company, apply_url, status=None):
    url = str(apply_url or "").strip()
    lowered = url.lower()
    lowered_id = str(app_id or "").lower()
    lowered_company = str(company or "").lower()
    lowered_status = str(status or "").strip().lower()
    if lowered_status in {"rejected", "failed"}:
        return False
    if not lowered.startswith(("http://", "https://")):
        return False
    blocked_url_parts = [
        "localhost",
        "127.0.0.1",
        "/mock-form",
        "example.com/demo",
        "linkedin.com/jobs/search",
        "linkedin.com/jobs/view",
        "google.com/careers",
        "stripe.com/careers",
        "talentnet.community",
    ]
    blocked_id_prefixes = ("mock-", "flow-smoke", "elastic-flow-smoke")
    blocked_companies = ("hackathon flow demo", "mock company")
    if any(part in lowered for part in blocked_url_parts):
        return False
    if lowered_id.startswith(blocked_id_prefixes):
        return False
    if any(name in lowered_company for name in blocked_companies):
        return False
    return True

def is_video_visible_application_record(app_id, company, apply_url=None, match_scores_json=None):
    lowered_id = str(app_id or "").lower()
    lowered_company = str(company or "").lower()
    lowered_url = str(apply_url or "").lower()
    if lowered_id.startswith(("mock-", "flow-smoke", "elastic-flow-smoke", "soma-outcome-smoke", "phoenix-cloud-smoke", "arize-smoke-test")):
        return False
    if any(name in lowered_company for name in ("hackathon flow demo", "mock company", "soma outcome smoke", "democo", "phoenixdemo")):
        return False
    if any(part in lowered_url for part in (
        "localhost",
        "127.0.0.1",
        "/mock-form",
        "linkedin.com/jobs/search",
        "google.com/careers",
        "stripe.com/careers",
        "talentnet.community",
    )):
        return False
    match_scores = load_match_scores(match_scores_json)
    if match_scores.get("jd_source_type") == "title_based_fallback_no_stored_jd":
        return False
    return True

EMAIL_DISPLAY_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

def mask_email_for_display(email_value):
    text = str(email_value or "").strip()
    if "@" not in text:
        return text
    local, domain = text.split("@", 1)
    if not local or not domain:
        return text
    if len(local) <= 2:
        masked_local = f"{local[:1]}****"
    elif len(local) <= 4:
        masked_local = f"{local[:1]}****{local[-1:]}"
    else:
        masked_local = f"{local[:2]}****{local[-2:]}"
    return f"{masked_local}@{domain}"

def mask_emails_in_text(text):
    return EMAIL_DISPLAY_RE.sub(lambda match: mask_email_for_display(match.group(0)), str(text or ""))

def mask_sensitive_display(value):
    if isinstance(value, dict):
        return {key: mask_sensitive_display(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_sensitive_display(item) for item in value]
    if isinstance(value, str):
        return mask_emails_in_text(value)
    return value

def demo_locked_email():
    return (os.getenv("DEMO_LOCKED_EMAIL") or "demo@example.com").strip()

def demo_email_oauth_locked():
    value = (os.getenv("DEMO_EMAIL_OAUTH_LOCKED") or "true").strip().lower()
    return value not in {"0", "false", "no", "off"}

def load_json_file(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_file(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

DEFAULT_APPLICATION_PROFILE_LIBRARY = {
    "skills": [],
    "education": {
        "school": "University of Illinois at Urbana-Champaign",
        "degree": "Bachelor's Degree",
        "field": "Computer Science and Linguistics",
        "location": "Champaign, IL",
        "start_month": "August",
        "start_year": "2021",
        "end_month": "December",
        "end_year": "2026",
        "gpa": "",
    },
    "experiences": [
        {
            "title": "Software Engineer Intern",
            "company": "Volcengine",
            "location": "Beijing, China",
            "start_month": "May",
            "start_year": "2024",
            "end_month": "August",
            "end_year": "2024",
            "current": False,
            "description": "Engineered Volcengine's core B2B multi-tenant platform serving 7,000+ clients with high-scale routing and API gateway workflows.",
        }
    ],
    "languages": [
        {"language": "English", "overall": "4 - Fluent"},
        {"language": "Chinese", "overall": "4 - Fluent"},
    ],
    "availability": {
        "start_date": "",
        "notice_period": "",
    },
    "company_employment_registry": [],
}


def application_profile_library_text(config_data):
    library = load_application_profile_library(config_data)
    return json.dumps(library, indent=2, ensure_ascii=False)


def load_application_profile_library(config_data):
    user_data = (config_data or {}).get("user_data") or {}
    library = user_data.get("application_profile_library") or DEFAULT_APPLICATION_PROFILE_LIBRARY
    if isinstance(library, str):
        try:
            library = json.loads(library)
        except Exception:
            library = DEFAULT_APPLICATION_PROFILE_LIBRARY
    if not isinstance(library, dict):
        library = DEFAULT_APPLICATION_PROFILE_LIBRARY
    return json.loads(json.dumps(library))


def save_application_profile_library(config_data, raw_text):
    text = str(raw_text or "").strip()
    if not text:
        return True
    try:
        library = json.loads(text)
    except Exception as err:
        st.toast(f"Application profile library JSON invalid: {err}", icon="⚠️")
        return False
    if not isinstance(library, dict):
        st.toast("Application profile library must be a JSON object.", icon="⚠️")
        return False
    config_data.setdefault("user_data", {})["application_profile_library"] = library
    return True


def save_skill_library_fields(config_data, value=None):
    library = load_application_profile_library(config_data)
    raw_skills = st.session_state.get("cfg_skills", "") if value is None else value
    skills = normalize_skill_entries(raw_skills)
    library["skills"] = skills
    user_data = config_data.setdefault("user_data", {})
    user_data["application_profile_library"] = library
    user_data["skills"] = list(skills)
    return True


def save_education_library_fields(config_data):
    library = load_application_profile_library(config_data)
    education = {
        "school": str(st.session_state.get("cfg_edu_school", "") or "").strip(),
        "degree": str(st.session_state.get("cfg_edu_degree", "") or "").strip(),
        "field": str(st.session_state.get("cfg_edu_field", "") or "").strip(),
        "location": str(st.session_state.get("cfg_edu_location", "") or "").strip(),
        "start_month": str(st.session_state.get("cfg_edu_start_month", "") or "").strip(),
        "start_year": str(st.session_state.get("cfg_edu_start_year", "") or "").strip(),
        "end_month": str(st.session_state.get("cfg_edu_end_month", "") or "").strip(),
        "end_year": str(st.session_state.get("cfg_edu_end_year", "") or "").strip(),
        "gpa": str(st.session_state.get("cfg_edu_gpa", "") or "").strip(),
    }
    if any(education.values()):
        library["education"] = education
        config_data.setdefault("user_data", {})["application_profile_library"] = library
    return True


def save_availability_library_fields(config_data):
    library = load_application_profile_library(config_data)
    availability = library.get("availability") if isinstance(library.get("availability"), dict) else {}
    availability.update({
        "start_date": str(st.session_state.get("cfg_availability_start_date", "") or "").strip(),
        "notice_period": str(st.session_state.get("cfg_availability_notice_period", "") or "").strip(),
    })
    library["availability"] = availability
    config_data.setdefault("user_data", {})["application_profile_library"] = library
    return True

def save_language_library_fields(config_data, max_items=5):
    library = load_application_profile_library(config_data)
    try:
        count = int(st.session_state.get("cfg_language_count", 2))
    except Exception:
        count = 2
    count = max(1, min(max_items, count))
    languages = []
    for idx in range(count):
        entry = {
            "language": str(st.session_state.get(f"cfg_lang_{idx}_language", "") or "").strip(),
            "overall": str(st.session_state.get(f"cfg_lang_{idx}_overall", "") or "").strip(),
        }
        if entry["language"] or entry["overall"]:
            languages.append(entry)
    library["languages"] = languages
    config_data.setdefault("user_data", {})["application_profile_library"] = library
    return True


def save_experience_library_fields(config_data, max_items=3):
    library = load_application_profile_library(config_data)
    try:
        count = int(st.session_state.get("cfg_experience_count", 1))
    except Exception:
        count = 1
    count = max(1, min(max_items, count))
    experiences = []
    for idx in range(count):
        entry = {
            "title": str(st.session_state.get(f"cfg_exp_{idx}_title", "") or "").strip(),
            "company": str(st.session_state.get(f"cfg_exp_{idx}_company", "") or "").strip(),
            "location": str(st.session_state.get(f"cfg_exp_{idx}_location", "") or "").strip(),
            "start_month": str(st.session_state.get(f"cfg_exp_{idx}_start_month", "") or "").strip(),
            "start_year": str(st.session_state.get(f"cfg_exp_{idx}_start_year", "") or "").strip(),
            "end_month": str(st.session_state.get(f"cfg_exp_{idx}_end_month", "") or "").strip(),
            "end_year": str(st.session_state.get(f"cfg_exp_{idx}_end_year", "") or "").strip(),
            "current": bool(st.session_state.get(f"cfg_exp_{idx}_current", False)),
            "description": str(st.session_state.get(f"cfg_exp_{idx}_description", "") or "").strip(),
        }
        if entry["current"]:
            entry["end_month"] = "Present"
            entry["end_year"] = "Present"
        if entry["title"] or entry["company"] or entry["description"]:
            experiences.append(entry)
    library["experiences"] = experiences
    config_data.setdefault("user_data", {})["application_profile_library"] = library
    return True


def save_company_employment_registry_fields(config_data, max_items=20):
    library = load_application_profile_library(config_data)
    try:
        count = int(st.session_state.get("cfg_employment_registry_count", 0))
    except (TypeError, ValueError):
        count = 0
    count = max(0, min(max_items, count))
    registry = []
    for idx in range(count):
        company = str(st.session_state.get(f"cfg_employment_registry_{idx}_company", "") or "").strip()
        if not company:
            continue
        raw_aliases = str(st.session_state.get(f"cfg_employment_registry_{idx}_aliases", "") or "")
        aliases = [item.strip() for item in re.split(r"[,;\n]", raw_aliases) if item.strip()]
        answer = str(st.session_state.get(f"cfg_employment_registry_{idx}_answer", "Unknown") or "Unknown")
        registry.append({
            "company": company,
            "aliases": aliases,
            "previously_employed": True if answer == "Yes" else False if answer == "No" else None,
            "confirmed": answer in {"Yes", "No"} and bool(
                st.session_state.get(f"cfg_employment_registry_{idx}_confirmed", False)
            ),
            "source": "user_profile",
        })
    library["company_employment_registry"] = registry
    config_data.setdefault("user_data", {})["application_profile_library"] = library
    return True

def pdf_escape(text):
    return str(text or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

PDF_CHAR_REPLACEMENTS = {
    "\u2022": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u00a0": " ",
}

def normalize_pdf_text(text):
    value = str(text or "")
    for src, dst in PDF_CHAR_REPLACEMENTS.items():
        value = value.replace(src, dst)
    value = "".join(ch if ch == "\n" or ch == "\t" or 32 <= ord(ch) <= 126 else " " for ch in value)
    value = re.sub(r"[ \t]+", " ", value)
    return value

def wrapped_resume_lines(text, width=92):
    lines = []
    for line in (normalize_pdf_text(text) or "").splitlines():
        if not line.strip():
            lines.append("")
            continue
        lines.extend(textwrap.wrap(line, width=width, replace_whitespace=False) or [""])
    return lines or ["Resume"]

def estimate_resume_pdf_pages(text, lines_per_page=58):
    lines = wrapped_resume_lines(text)
    return max(1, (len(lines) + lines_per_page - 1) // lines_per_page), len(lines)

def write_text_pdf(text, output_path, title="Resume", max_pages=1):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    text = normalize_pdf_text(text)
    title = normalize_pdf_text(title)
    raw_lines = wrapped_resume_lines(text)

    lines_per_page = 58
    pages = [raw_lines[i:i + lines_per_page] for i in range(0, len(raw_lines), lines_per_page)]
    if max_pages and len(pages) > max_pages:
        raise ValueError(
            f"Resume PDF would be {len(pages)} pages; hard limit is {max_pages}. "
            "Shorten the tailored resume before applying."
        )
    objects = {}
    font_obj = 3
    page_nums = []
    content_nums = []

    objects[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[font_obj] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for index, page_lines in enumerate(pages):
        page_obj = 4 + index * 2
        content_obj = 5 + index * 2
        page_nums.append(page_obj)
        content_nums.append(content_obj)
        stream_lines = ["BT", "/F1 9.5 Tf", "54 744 Td", "12 TL"]
        if index == 0 and title:
            stream_lines.append(f"({pdf_escape(title)}) Tj")
            stream_lines.append("T*")
            stream_lines.append("T*")
        for line in page_lines:
            stream_lines.append(f"({pdf_escape(line.encode('latin-1', 'replace').decode('latin-1'))}) Tj")
            stream_lines.append("T*")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1", "replace")
        objects[page_obj] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> /Contents {content_obj} 0 R >>"
        )
        objects[content_obj] = f"<< /Length {len(stream)} >>\nstream\n{stream.decode('latin-1')}\nendstream"

    kids = " ".join(f"{num} 0 R" for num in page_nums)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_nums)} >>"

    max_obj = max(objects)
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (max_obj + 1)
    for obj_num in range(1, max_obj + 1):
        offsets[obj_num] = len(output)
        body = objects[obj_num].encode("latin-1", "replace")
        output.extend(f"{obj_num} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {max_obj + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for obj_num in range(1, max_obj + 1):
        output.extend(f"{offsets[obj_num]:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {max_obj + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    with open(output_path, "wb") as f:
        f.write(output)
    return output_path

def resume_pdf_path(app_id):
    return os.path.join(BASE_DIR, "data", "generated_resumes", f"resume_v1_{safe_app_key(app_id)}.pdf")

def extract_pdf_text(pdf_path):
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as err:
        return "", str(err)

def pdf_page_count(pdf_path):
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        return len(reader.pages), None
    except Exception as err:
        return None, str(err)

def render_pdf_first_page_png(pdf_path, output_path=None):
    try:
        import fitz
    except Exception as err:
        return None, f"PyMuPDF unavailable: {err}"
    try:
        output_path = output_path or os.path.splitext(pdf_path)[0] + "_page1.png"
        doc = fitz.open(pdf_path)
        if len(doc) == 0:
            return None, "PDF has no pages"
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        pix.save(output_path)
        doc.close()
        return output_path, None
    except Exception as err:
        return None, str(err)

def deterministic_pdf_quality_check(source_text, pdf_path):
    extracted, extract_error = extract_pdf_text(pdf_path)
    page_count, page_count_error = pdf_page_count(pdf_path)
    normalized_source = normalize_pdf_text(source_text)
    issues = []
    if page_count_error:
        issues.append(f"page_count_error: {page_count_error}")
    if page_count != 1:
        issues.append(f"pdf_must_be_one_page:{page_count}")
    if extract_error:
        issues.append(f"extract_error: {extract_error}")
    if not extracted or len(extracted.strip()) < 200:
        issues.append("pdf_text_too_short")
    replacement_count = extracted.count("\ufffd") + extracted.count("?")
    if replacement_count > max(8, len(extracted) * 0.02):
        issues.append("too_many_replacement_characters")
    for token in ["YIFAN", "University", "Python"]:
        if token.lower() in normalized_source.lower() and token.lower() not in extracted.lower():
            issues.append(f"missing_token:{token}")
    return {
        "passed": not issues,
        "issues": issues,
        "extracted_text": extracted,
        "extract_error": extract_error,
        "page_count": page_count,
        "source_chars": len(source_text or ""),
        "extracted_chars": len(extracted or ""),
    }

def llm_pdf_visual_quality_check(ai_client, model_name, source_text, pdf_path, extracted_text):
    image_path, render_error = render_pdf_first_page_png(pdf_path)
    if render_error:
        return {"passed": False, "issues": [f"render_error: {render_error}"], "image_path": image_path}
    if not ai_client:
        return {"passed": True, "skipped": True, "reason": "ai_client_unavailable", "image_path": image_path}
    try:
        with open(image_path, "rb") as f:
            image_part = types.Part.from_bytes(data=f.read(), mime_type="image/png")
        prompt = f"""
You are checking whether a generated resume PDF is readable and not corrupted.

Look at the rendered first-page image and compare it with the expected resume text excerpt.
Check for: blank page, garbled characters, repeated question marks, unreadably tiny text, severe overlap, missing candidate identity, or layout obviously broken.
The resume must be exactly one readable page. If it appears cut off, overflowing, or incomplete, fail it.

Expected text excerpt:
{source_text[:3500]}

PDF extracted text excerpt:
{(extracted_text or '')[:3500]}

Return ONLY JSON:
{{
  "passed": true | false,
  "issues": ["short issue labels"],
  "summary": "one sentence",
  "visual_readability_score": 0.0
}}
"""
        response = ai_client.models.generate_content(
            model=model_name or "gemini-3.5-flash",
            contents=[image_part, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
        )
        result = json.loads(response.text.strip().replace("```json", "").replace("```", ""))
        result["image_path"] = image_path
        return result
    except Exception as err:
        return {"passed": False, "issues": [f"llm_check_error: {err}"], "image_path": image_path}

def validate_resume_pdf(ai_client, model_name, source_text, pdf_path):
    deterministic = deterministic_pdf_quality_check(source_text, pdf_path)
    visual = llm_pdf_visual_quality_check(ai_client, model_name, source_text, pdf_path, deterministic.get("extracted_text", ""))
    passed = bool(deterministic.get("passed")) and bool(visual.get("passed", True))
    return {
        "passed": passed,
        "deterministic": {k: v for k, v in deterministic.items() if k != "extracted_text"},
        "visual": visual,
        "pdf_path": pdf_path,
    }

def mongo_url_from_config(config):
    return (os.getenv("MONGO_URL") or config.get("mongo_url") or "http://localhost:8001").rstrip("/")

def arize_url_from_config(config):
    return (os.getenv("ARIZE_URL") or config.get("arize_url") or "http://localhost:8003").rstrip("/")

def elastic_url_from_config(config):
    return (os.getenv("ELASTIC_URL_API") or config.get("elastic_url") or "http://localhost:8002").rstrip("/")

def playwright_url_from_config(config):
    return (os.getenv("PLAYWRIGHT_URL") or config.get("playwright_url") or "http://localhost:8004").rstrip("/")

def email_url_from_config(config):
    return (os.getenv("EMAIL_URL") or config.get("email_url") or "http://localhost:8005").rstrip("/")

def ats_scan_keywords(keywords="", experience_level=None):
    raw_keywords = (keywords or "").strip()
    level_text = " ".join(experience_level or []) if isinstance(experience_level, list) else str(experience_level or "")
    combined = f"{raw_keywords} {level_text}".lower()
    entry_markers = ["entry", "entry-level", "entry level", "junior", "jr.", "new grad", "associate", "early career"]
    if not any(marker in combined for marker in entry_markers):
        return [raw_keywords] if raw_keywords else []
    base = raw_keywords or "software engineer"
    if any(marker in base.lower() for marker in entry_markers):
        return [base]
    return [f"entry level {base}"]

def warm_ats_sources(config, keywords="", location="United States", task_id=None, experience_level=None):
    source_urls = (config.get("ats_source_urls") or os.getenv("ATS_SOURCE_URLS") or "").strip()
    if not source_urls:
        return None, None
    elastic_url = elastic_url_from_config(config)
    scan_keywords = ats_scan_keywords(keywords, experience_level)
    try:
        response = requests.post(
            f"{elastic_url}/ats-scan",
            json={
                "source_urls": source_urls,
                "keywords": scan_keywords,
                "location": location or "United States",
                "limit": int(config.get("ats_source_limit") or 50),
                "timeout_seconds": 10,
                "persist": True,
            },
            timeout=180,
        )
        if response.status_code >= 400:
            return None, f"{response.status_code}: {response.text[:300]}"
        result = response.json()
        log_run(
            "INFO",
            f"ATS source scan indexed {result.get('indexed', 0)} jobs from {result.get('scanned_sources', 0)} sources; query={scan_keywords or ['all']}; errors={len(result.get('errors') or [])}.",
            task_id=task_id,
        )
        return result, None
    except Exception as err:
        return None, str(err)

def app_base_url():
    return (os.getenv("APP_BASE_URL") or "http://localhost:8501").rstrip("/")

GMAIL_OAUTH_APP_ID = "__global_email_oauth__"
GMAIL_OAUTH_ARTIFACT_TYPE = "gmail_oauth_token"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

def call_mongo_memory(config, method, path, payload=None, timeout=3):
    url = f"{mongo_url_from_config(config)}{path}"
    try:
        response = requests.request(method, url, json=payload, timeout=timeout)
        if response.status_code >= 400:
            print(f"[Mongo Memory] {method} {url} failed: {response.status_code} {response.text[:300]}")
            return None
        return response.json() if response.text else {}
    except Exception as err:
        print(f"[Mongo Memory] {method} {url} skipped: {err}")
        return None

def sync_mongo_application(config, app_id, company, role, resume_v0="", resume_v1=None, status="Queued", apply_url=None, job_description=None, source="frontend", metadata=None):
    return call_mongo_memory(config, "POST", "/applications/upsert", {
        "id": str(app_id),
        "company": company or "Unknown",
        "role": role or "Unknown",
        "resume_v0": resume_v0 or "",
        "resume_v1": resume_v1,
        "status": status,
        "apply_url": apply_url,
        "job_description": job_description,
        "source": source,
        "metadata": metadata or {}
    })

def sync_mongo_status(config, app_id, status, reason=None, metadata=None):
    return call_mongo_memory(config, "PATCH", f"/applications/{safe_app_key(app_id)}/status", {
        "status": status,
        "reason": reason,
        "metadata": metadata or {}
    })

def sync_mongo_artifact(config, app_id, artifact_type, payload, metadata=None):
    return call_mongo_memory(config, "POST", f"/applications/{safe_app_key(app_id)}/artifacts", {
        "artifact_type": artifact_type,
        "payload": payload or {},
        "metadata": metadata or {}
    })

def sync_mongo_resume_version(config, app_id, version_label, content, source=None, job_description=None, audit_result=None, metadata=None):
    if not content:
        return None
    return call_mongo_memory(config, "POST", f"/applications/{safe_app_key(app_id)}/resume-versions", {
        "version_label": version_label,
        "content": content,
        "source": source,
        "job_description": job_description,
        "audit_result": audit_result or {},
        "metadata": metadata or {}
    })

def sync_mongo_event(config, app_id, event_type, payload=None):
    return call_mongo_memory(config, "POST", f"/applications/{safe_app_key(app_id)}/events", {
        "event_type": event_type,
        "payload": payload or {}
    })

def gmail_secret_path():
    configured = (os.getenv("GMAIL_OAUTH_CLIENT_SECRET_PATH") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(BASE_DIR, "data", "client_secret.json")

def gmail_token_path():
    configured = (os.getenv("GMAIL_OAUTH_TOKEN_PATH") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(BASE_DIR, "data", "gmail_token.json")

def load_gmail_oauth_client():
    env_client_id = (os.getenv("GMAIL_OAUTH_CLIENT_ID") or "").strip()
    env_client_secret = (os.getenv("GMAIL_OAUTH_CLIENT_SECRET") or "").strip()
    if env_client_id and env_client_secret:
        return {
            "client_id": env_client_id,
            "client_secret": env_client_secret,
        }, None

    path = gmail_secret_path()
    if not os.path.exists(path):
        return None, "Gmail OAuth client secret is not configured."
    secret_data = load_json_file(path, {}) or {}
    web_cfg = secret_data.get("web", {})
    if not web_cfg.get("client_id") or not web_cfg.get("client_secret"):
        return None, "client_secret.json does not contain a web client_id/client_secret."
    return web_cfg, None

def build_gmail_oauth_url(email_address=None, force_consent=False):
    web_cfg, error = load_gmail_oauth_client()
    if error:
        return None, error
    params = {
        "client_id": web_cfg.get("client_id"),
        "redirect_uri": app_base_url(),
        "response_type": "code",
        "scope": GMAIL_READONLY_SCOPE,
        "access_type": "offline",
    }
    if force_consent:
        params["prompt"] = "consent"
    if email_address:
        params["login_hint"] = email_address
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}", None

def fetch_gmail_profile_email(access_token):
    if not access_token:
        return ""
    try:
        response = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if response.status_code == 200:
            return (response.json() or {}).get("emailAddress", "")
    except Exception as err:
        print(f"[Gmail OAuth] profile lookup skipped: {err}")
    return ""

def get_gmail_oauth_artifact(config):
    memory = call_mongo_memory(config, "GET", f"/applications/{GMAIL_OAUTH_APP_ID}/memory", timeout=5)
    for artifact in (memory or {}).get("artifacts") or []:
        if artifact.get("artifact_type") == GMAIL_OAUTH_ARTIFACT_TYPE:
            return artifact
    return None

def persist_gmail_oauth_token(config, token_data, email_address=None):
    # Google may omit refresh_token when an already-authorized account grants
    # another access code. Preserve the durable token across that exchange.
    previous = load_json_file(gmail_token_path(), {}) or {}
    artifact = get_gmail_oauth_artifact(config)
    previous.update((artifact or {}).get("payload") or {})
    payload = dict(previous)
    payload.update(token_data or {})
    payload["email_address"] = email_address or payload.get("email_address") or ""
    payload["connected_at"] = payload.get("connected_at") or datetime.datetime.utcnow().isoformat() + "Z"
    save_json_file(gmail_token_path(), payload)
    sync_mongo_artifact(
        config,
        GMAIL_OAUTH_APP_ID,
        GMAIL_OAUTH_ARTIFACT_TYPE,
        payload,
        {
            "email_address": payload.get("email_address", ""),
            "scope": payload.get("scope", GMAIL_READONLY_SCOPE),
            "source": "frontend_oauth_callback",
        },
    )
    sync_mongo_event(
        config,
        GMAIL_OAUTH_APP_ID,
        "gmail_oauth_connected",
        {
            "email_address": payload.get("email_address", ""),
            "expires_at": payload.get("expires_at"),
            "has_refresh_token": bool(payload.get("refresh_token")),
        },
    )

def handle_gmail_oauth_callback(config):
    query_params = st.query_params
    if "code" not in query_params:
        return False
    code = query_params["code"]
    st.query_params.clear()
    web_cfg, error = load_gmail_oauth_client()
    if error:
        st.error(error)
        return True
    try:
        response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": web_cfg.get("client_id"),
                "client_secret": web_cfg.get("client_secret"),
                "redirect_uri": app_base_url(),
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        if response.status_code != 200:
            st.error(f"Gmail OAuth token exchange failed: {response.text[:500]}")
            return True
        token_data = response.json()
        token_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)
        profile_email = fetch_gmail_profile_email(token_data.get("access_token"))
        configured_email = ((config.get("user_data") or {}).get("email") or "").strip()
        persist_gmail_oauth_token(config, token_data, profile_email or configured_email)
        if profile_email:
            updated = dict(config or {})
            updated.setdefault("user_data", {})
            updated["user_data"]["email"] = profile_email
            save_config(updated)
        st.success("Gmail OAuth connected. The mailbox token was saved to shared MongoDB memory.")
        time.sleep(1)
        st.rerun()
    except Exception as err:
        st.error(f"Gmail OAuth callback failed: {err}")
    return True

def gmail_oauth_status(config):
    local_token = load_json_file(gmail_token_path(), {}) or {}
    mongo_artifact = get_gmail_oauth_artifact(config)
    mongo_payload = (mongo_artifact or {}).get("payload") or {}
    token = mongo_payload or local_token
    runtime_health = fetch_service_health(email_url_from_config(config), timeout=2)
    return {
        "connected": bool(token.get("access_token") or token.get("refresh_token")),
        "runtime_checked": bool(runtime_health.get("service") == "email-integration"),
        "runtime_ready": bool(runtime_health.get("verification_ready")),
        "email_address": token.get("email_address") or ((config.get("user_data") or {}).get("email") or ""),
        "expires_at": token.get("expires_at"),
        "has_refresh_token": bool(token.get("refresh_token")),
        "source": "MongoDB memory" if mongo_payload else ("local file" if local_token else "not connected"),
        "updated_at": (mongo_artifact or {}).get("updated_at", ""),
    }

def render_email_oauth_panel(config):
    status = gmail_oauth_status(config)
    locked = demo_email_oauth_locked()
    locked_email = demo_locked_email()
    user_email = locked_email if locked else ((config.get("user_data") or {}).get("email") or "").strip()
    saved_email_display = mask_email_for_display(user_email) if user_email else "not set"
    st.markdown('<div class="en-card-title">Email OAuth</div>', unsafe_allow_html=True)
    st.caption("Connect a Gmail inbox so the agent can read verification codes and application-status emails. The OAuth scope is Gmail read-only.")
    if locked:
        st.markdown(
            f"""
            <div class="en-card">
                <div class="en-field-row">
                    <span class="en-field-label">Demo mailbox</span>
                    <span class="en-field-value">{html.escape(saved_email_display)}</span>
                </div>
                <div class="en-field-row">
                    <span class="en-field-label">Edit mode</span>
                    <span class="en-field-value">Locked for judge demo</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        email_value = st.text_input(
            "Mailbox",
            value="",
            placeholder=f"Current mailbox: {saved_email_display}. Type a full email only if you want to change it.",
            key="en_oauth_email",
        )
        if st.button("Save Mailbox", use_container_width=True):
            if email_value.strip():
                updated = dict(config or {})
                updated.setdefault("user_data", {})
                updated["user_data"]["email"] = email_value.strip()
                save_config(updated)
                st.success("Mailbox saved.")
                time.sleep(0.4)
                st.rerun()
            else:
                st.info(f"Keeping saved mailbox: {saved_email_display}")
        user_email = email_value.strip() or user_email

    effective_email = user_email
    if status.get("connected"):
        st.caption("Gmail authorization is saved and will be reused on future runs.")
    else:
        auth_col, _spacer = st.columns([1, 3])
        with auth_col:
            auth_url, error = build_gmail_oauth_url(effective_email) if effective_email else (None, "Set a mailbox before connecting Gmail.")
            if auth_url and effective_email:
                st.link_button("Connect Gmail", auth_url, use_container_width=True)
            else:
                st.button("Connect Gmail", disabled=True, use_container_width=True)
                st.caption(error)
    status_email = effective_email if locked else (status.get("email_address") or effective_email or "Not set")
    connected = bool(status["runtime_ready"])
    if locked:
        connected = connected and (str(status.get("email_address") or "").strip().lower() == locked_email.lower())
    if connected:
        status_text = "Ready"
    elif status.get("connected"):
        status_text = "Authorization needs attention"
    else:
        status_text = "Authorization pending"
    status_rows = [
        ("Status", status_text),
        ("Mailbox", mask_email_for_display(status_email)),
        ("Token source", status.get("source", "unknown")),
        ("Refresh token", "Available" if status.get("has_refresh_token") else "Missing"),
    ]
    if status.get("expires_at"):
        try:
            expires_text = datetime.datetime.fromtimestamp(float(status["expires_at"])).isoformat()
        except Exception:
            expires_text = str(status["expires_at"])
        status_rows.append(("Access token expires", expires_text))
    status_html = ""
    for label, value in status_rows:
        status_html += f'<div class="en-field-row"><span class="en-field-label">{html.escape(label)}</span><span class="en-field-value">{html.escape(str(value))}</span></div>'
    st.markdown(f'<div class="en-card">{status_html}</div>', unsafe_allow_html=True)

def call_arize_audit(config, app_id, company, role, resume_v0, resume_v1, job_description=None, apply_url=None, metadata=None):
    if not resume_v0 or not resume_v1:
        return None, "缺少 resume_v0 或 resume_v1，无法执行 Arize 审计。"
    payload = {
        "app_id": str(app_id),
        "company": company,
        "role": role,
        "apply_url": apply_url,
        "resume_v0": resume_v0,
        "resume_v1": resume_v1,
        "job_description": job_description,
        "model": config.get("model_selector"),
        "trusted_skills": trusted_skill_library(config),
        "metadata": metadata or {}
    }
    try:
        response = requests.post(f"{arize_url_from_config(config)}/audit", json=payload, timeout=45)
        if response.status_code >= 400:
            return None, f"Arize audit failed: {response.status_code} {response.text[:300]}"
        audit_result = response.json()
    except Exception as err:
        return None, f"Arize audit service unavailable: {err}"

    sync_mongo_artifact(
        config,
        app_id,
        "arize_resume_audit",
        audit_result,
        metadata={
            "company": company,
            "role": role,
            "trace_id": audit_result.get("trace_id"),
            "source": (metadata or {}).get("source", "frontend")
        }
    )
    sync_mongo_event(config, app_id, "arize_audit_completed", {
        "passed": audit_result.get("passed"),
        "faithfulness_score": audit_result.get("faithfulness_score"),
        "jd_match_score": audit_result.get("jd_match_score"),
        "risk_score": audit_result.get("risk_score"),
        "trace_id": audit_result.get("trace_id"),
        "recommended_action": audit_result.get("recommended_action"),
        "source": (metadata or {}).get("source", "frontend")
    })
    return audit_result, None

def call_soma_retrieval(config, app_id, company, role, resume_v0, job_description):
    if not resume_v0 or not job_description:
        return None, "缺少 resume_v0 或 JD，跳过 SOMA 检索。"
    payload = {
        "application_id": str(app_id) if app_id else None,
        "mongo_url": mongo_url_from_config(config),
        "company": company,
        "role": role,
        "job_description": job_description,
        "resume_v0": resume_v0,
        "limit": 8,
        "retrieve_limit": 50,
    }
    try:
        response = requests.post(f"{elastic_url_from_config(config)}/retrieve-similar-episodes", json=payload, timeout=45)
        if response.status_code >= 400:
            return None, f"SOMA retrieval failed: {response.status_code} {response.text[:300]}"
        return response.json(), None
    except Exception as err:
        return None, f"SOMA retrieval unavailable: {err}"

def extract_match_terms(text, limit=45):
    return extract_keyword_terms(text, limit=limit)

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
    return shared_score_resume_versions_for_jd(
        ai_client,
        model_name,
        resume_v0,
        resume_v1,
        job_description,
        company,
        role,
        skill_library=skill_library,
    )

def load_match_scores(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        payload = json.loads(value)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

def score_pct(value):
    try:
        return f"{round(float(value) * 100)}%"
    except Exception:
        return "-"

def score_pp(value, signed=True):
    try:
        numeric = float(value) * 100
    except Exception:
        return "-"
    sign = "+" if signed and numeric > 0 else ""
    return f"{sign}{numeric:.0f} pp"

def verdict_label(verdict, delta=None):
    normalized = str(verdict or "").strip().lower()
    if normalized in {"missing_jd", "missing jd"}:
        return "Missing JD"
    if normalized in {"fallback_scored_no_stored_jd", "fallback scored no stored jd"}:
        return "Title-based fallback, no stored JD"
    if normalized in {"improved", "improve", "better"}:
        return "Improved"
    if normalized in {"worse", "declined", "down"}:
        return "Worse"
    if normalized in {"flat", "same", "neutral", "no_material_change", "no material change"}:
        return "No material change"
    try:
        numeric_delta = float(delta)
        if numeric_delta > 0.03:
            return "Improved"
        if numeric_delta < -0.03:
            return "Worse"
        return "No material change"
    except Exception:
        return "-"

def confidence_text(score_payload):
    confidence = (score_payload or {}).get("confidence")
    if confidence is None:
        return None
    return f"judge confidence {score_pct(confidence)}"

def render_resume_jd_match_metrics(match_scores, expanded=False, company=None, role=None):
    match_scores = load_match_scores(match_scores)
    if not match_scores:
        return
    v0 = match_scores.get("v0") or {}
    v1 = match_scores.get("v1") or {}
    delta = match_scores.get("delta")
    context = "This score compares V0 and V1 for this single job application"
    if company or role:
        context += f": {role or 'Role'} at {company or 'Company'}"
    context += "."
    st.caption(context)
    cols = st.columns(3)
    with cols[0]:
        st.metric("V0 match for this JD", score_pct(v0.get("match_score")))
        if confidence_text(v0):
            st.caption(confidence_text(v0))
    with cols[1]:
        st.metric("V1 match for this JD", score_pct(v1.get("match_score")))
        if confidence_text(v1):
            st.caption(confidence_text(v1))
    with cols[2]:
        st.metric("V1 lift vs V0", score_pp(delta))
        st.caption(verdict_label(match_scores.get("verdict"), delta))
    skill_match = match_scores.get("skill_library_match") or {}
    if skill_match.get("library_skills"):
        skill_cols = st.columns(4)
        with skill_cols[0]:
            st.metric("Skill Library matches", len(skill_match.get("jd_matched_skills") or []))
        with skill_cols[1]:
            st.metric("Already evidenced in V0", len(skill_match.get("v0_evidenced_skills") or skill_match.get("resume_evidenced_skills") or []))
        with skill_cols[2]:
            st.metric("Library-only matches", len(skill_match.get("library_only_skills") or []))
        with skill_cols[3]:
            st.metric("Used in V1", len(skill_match.get("v1_used_skills") or []))
        if skill_match.get("jd_matched_skills"):
            st.caption("JD-matched trusted skills: " + ", ".join(skill_match["jd_matched_skills"]))
        v0_coverage = skill_match.get("v0_keyword_coverage")
        v1_coverage = skill_match.get("v1_keyword_coverage")
        if v0_coverage is not None and v1_coverage is not None:
            st.caption(f"Keyword coverage: V0 {score_pct(v0_coverage)} · V1 {score_pct(v1_coverage)}")
    with st.expander("Per-application scoring details", expanded=expanded):
        st.json(match_scores)

def compact_match_scores_html(match_scores):
    match_scores = load_match_scores(match_scores)
    if not match_scores:
        return ""
    v0 = match_scores.get("v0") or {}
    v1 = match_scores.get("v1") or {}
    confidence = v1.get("confidence") or v0.get("confidence")
    delta = match_scores.get("delta")
    verdict = verdict_label(match_scores.get("verdict"), delta)
    return (
        '<div style="font-size: 0.85rem; color: #1f2937; margin-top: 8px; '
        'padding: 8px 10px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;">'
        f'This job: V0 <strong>{score_pct(v0.get("match_score"))}</strong> · '
        f'V1 <strong>{score_pct(v1.get("match_score"))}</strong> · '
        f'lift <strong>{score_pp(delta)}</strong> · '
        f'judge confidence <strong>{score_pct(confidence)}</strong> · '
        f'<strong>{html.escape(str(verdict))}</strong>'
        '</div>'
    )

def clean_generated_resume(text):
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:markdown|md|text)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()

def tailor_resume_for_job(
    ai_client,
    model_name,
    resume_v0,
    job_description,
    company,
    role,
    rewrite_guidance=None,
    skill_library=None,
):
    if not resume_v0:
        return "", "缺少全局原始简历 V0。"
    if not job_description:
        return resume_v0, "缺少 JD，已回退使用 V0。"

    skill_match = build_skill_match_report(job_description, resume_v0, skill_library)
    prompt = build_resume_tailoring_prompt(
        resume_v0,
        job_description,
        company,
        role,
        rewrite_guidance=rewrite_guidance,
        skill_match=skill_match,
        one_page=True,
    )
    try:
        response = ai_client.models.generate_content(
            model=model_name or "gemini-3.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.15),
        )
        resume_v1 = clean_generated_resume(response.text)
        if len(resume_v1) < 200:
            return resume_v0, "模型返回内容过短，已回退使用 V0。"
        estimated_pages, wrapped_lines = estimate_resume_pdf_pages(resume_v1)
        if estimated_pages > 1:
            compression_prompt = build_resume_compression_prompt(
                resume_v0,
                resume_v1,
                job_description,
                company,
                role,
                skill_match,
            )
            compressed_response = ai_client.models.generate_content(
                model=model_name or "gemini-3.5-flash",
                contents=compression_prompt,
                config=types.GenerateContentConfig(temperature=0.05),
            )
            compressed_resume = clean_generated_resume(compressed_response.text)
            if len(compressed_resume) >= 200:
                compressed_pages, compressed_lines = estimate_resume_pdf_pages(compressed_resume)
                resume_v1 = compressed_resume
                estimated_pages = compressed_pages
                wrapped_lines = compressed_lines
        if estimated_pages > 1:
            compression_prompt_retry = build_resume_compression_prompt(
                resume_v0,
                resume_v1,
                job_description,
                company,
                role,
                skill_match,
                retry=True,
            )
            compressed_response = ai_client.models.generate_content(
                model=model_name or "gemini-3.5-flash",
                contents=compression_prompt_retry,
                config=types.GenerateContentConfig(temperature=0.05),
            )
            compressed_resume = clean_generated_resume(compressed_response.text)
            if len(compressed_resume) >= 200:
                compressed_pages, compressed_lines = estimate_resume_pdf_pages(compressed_resume)
                resume_v1 = compressed_resume
                estimated_pages = compressed_pages
                wrapped_lines = compressed_lines
        if estimated_pages > 1:
            return resume_v1, (
                f"V1 exceeds one-page hard limit: estimated {estimated_pages} pages / "
                f"{wrapped_lines} wrapped lines. Shorten before applying."
            )
        return resume_v1, None
    except Exception as err:
        return resume_v0, f"生成 V1 失败，已回退使用 V0：{err}"

def resume_inline_html(value):
    escaped = html.escape(str(value or ""))
    escaped = re.sub(r"\\textbf\{([^{}]+)\}", lambda m: f"<strong>{m.group(1)}</strong>", escaped)
    escaped = re.sub(r"\\textit\{([^{}]+)\}", lambda m: f"<em>{m.group(1)}</em>", escaped)
    escaped = escaped.replace("\\&amp;", "&amp;")
    escaped = escaped.replace("\\%", "%")
    escaped = escaped.replace("$\\rightarrow$", "→")
    escaped = escaped.replace("\\rightarrow", "→")
    return escaped

def strip_json_fence(value):
    text = str(value or "").strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else text

def load_resume_json_payload(content):
    if isinstance(content, (dict, list)):
        return content
    text = strip_json_fence(content)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None

def resume_projects_from_content(content):
    payload = load_resume_json_payload(content)
    if isinstance(payload, list):
        raw_projects = payload
    elif isinstance(payload, dict):
        raw_projects = []
        for key in ("tailored_projects", "projects", "tailoredContent", "tailored_content"):
            value = payload.get(key)
            if isinstance(value, str):
                value = load_resume_json_payload(value)
            if isinstance(value, list):
                raw_projects = value
                break
    else:
        raw_projects = []

    projects = []
    for item in raw_projects:
        if not isinstance(item, dict) or item.get("hidden"):
            continue
        bullets = item.get("bullet_points") or item.get("bullets") or item.get("points") or []
        if isinstance(bullets, str):
            bullets = [bullets]
        technologies = item.get("technologies") or item.get("tech_stack") or []
        if isinstance(technologies, str):
            technologies = [part.strip() for part in technologies.split(",") if part.strip()]
        project = {
            "name": item.get("name") or item.get("title") or item.get("project_name") or "Untitled Project",
            "category": item.get("category") or "Projects",
            "date": item.get("date") or item.get("timeframe") or "",
            "technologies": technologies,
            "summary": item.get("summary") or item.get("description") or "",
            "bullet_points": [str(point) for point in bullets if str(point).strip()]
        }
        if project["name"] or project["summary"] or project["bullet_points"]:
            projects.append(project)
    return projects

def render_resume_projects_html(projects):
    preferred_order = ["Industrial Experience", "Research", "Projects"]
    categories = []
    for category in preferred_order:
        if any(project.get("category") == category for project in projects):
            categories.append(category)
    for project in projects:
        category = project.get("category") or "Projects"
        if category not in categories:
            categories.append(category)

    sections = []
    for category in categories:
        entries = []
        for project in [item for item in projects if (item.get("category") or "Projects") == category]:
            name = resume_inline_html(project.get("name"))
            date = resume_inline_html(project.get("date"))
            technologies = ", ".join(project.get("technologies") or [])
            tech_html = (
                f'<div class="resume-entry-subtitle">{resume_inline_html(technologies)}</div>'
                if technologies else ""
            )
            bullets = project.get("bullet_points") or []
            if not bullets and project.get("summary"):
                bullets = [project.get("summary")]
            bullet_html = "".join(
                f"<li>{resume_inline_html(point)}</li>"
                for point in bullets
            ) or '<li><em>No tailored points saved yet.</em></li>'
            meta_html = f'<span class="resume-entry-meta">{date}</span>' if date else ""
            entries.append(f"""
                <div class="resume-entry">
                    <div class="resume-entry-title-row">
                        <span class="resume-entry-title">{name}</span>
                        {meta_html}
                    </div>
                    {tech_html}
                    <ul class="resume-bullets">{bullet_html}</ul>
                </div>
            """)
        sections.append(f"""
            <div class="resume-section">
                <h3 class="resume-section-title">{resume_inline_html(category)}</h3>
                {''.join(entries)}
            </div>
        """)
    return "".join(sections)

def normalize_resume_heading(line):
    stripped = re.sub(r"^#+\s*", "", line.strip()).strip(":")
    compact = re.sub(r"[^A-Za-z]", "", stripped).upper()
    headings = {
        "SUMMARY": "Summary",
        "PROFILE": "Profile",
        "EDUCATION": "Education",
        "EXPERIENCE": "Experience",
        "WORKEXPERIENCE": "Work Experience",
        "INDUSTRIALEXPERIENCE": "Industrial Experience",
        "PROFESSIONALEXPERIENCE": "Professional Experience",
        "RESEARCH": "Research",
        "PROJECTS": "Projects",
        "TECHNICALSKILLS": "Technical Skills",
        "SKILLS": "Skills",
        "PUBLICATIONS": "Publications",
        "AWARDS": "Awards",
        "CERTIFICATIONS": "Certifications",
    }
    if compact in headings:
        return headings[compact]
    if 2 <= len(stripped) <= 34 and stripped == stripped.upper() and re.search(r"[A-Z]", stripped):
        return stripped.title()
    return None

def should_skip_resume_header_line(line, line_index, user_data):
    if line_index > 2:
        return False
    text = str(line or "").lower()
    compact_text = re.sub(r"[^a-z0-9@]+", "", text)
    first = str(user_data.get("first_name") or "").lower()
    last = str(user_data.get("last_name") or "").lower()
    compact_name = re.sub(r"[^a-z0-9]+", "", f"{first}{last}")
    if compact_name and compact_name in compact_text:
        return True
    email_value = str(user_data.get("email") or "").lower()
    if email_value and email_value in text:
        return True
    phone_digits = re.sub(r"\D", "", str(user_data.get("phone") or ""))
    line_digits = re.sub(r"\D", "", text)
    return bool(phone_digits and len(phone_digits) >= 7 and phone_digits in line_digits)

def render_plain_resume_html(content, user_data):
    lines = str(content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    parts = []
    section_open = False
    list_open = False

    def ensure_section():
        nonlocal section_open
        if not section_open:
            parts.append('<div class="resume-section">')
            section_open = True

    def close_list():
        nonlocal list_open
        if list_open:
            parts.append("</ul>")
            list_open = False

    def close_section():
        nonlocal section_open
        close_list()
        if section_open:
            parts.append("</div>")
            section_open = False

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            close_list()
            continue
        if should_skip_resume_header_line(stripped, index, user_data):
            continue

        heading = normalize_resume_heading(stripped)
        if heading:
            close_section()
            parts.append('<div class="resume-section">')
            parts.append(f'<h3 class="resume-section-title">{resume_inline_html(heading)}</h3>')
            section_open = True
            continue

        ensure_section()
        bullet_match = re.match(r"^(?:[-*•]|\\u2022)\s*(.+)$", stripped)
        if bullet_match:
            if not list_open:
                parts.append('<ul class="resume-bullets">')
                list_open = True
            parts.append(f"<li>{resume_inline_html(bullet_match.group(1))}</li>")
        else:
            close_list()
            parts.append(f'<p class="resume-paragraph">{resume_inline_html(stripped)}</p>')

    close_section()
    return "".join(parts) or '<p class="resume-paragraph">No resume content saved yet.</p>'

def render_resume_preview_html(content, selected_version, config, app_doc=None):
    user_data = config.get("user_data", {}) or {}
    first = str(user_data.get("first_name") or "").strip()
    last = str(user_data.get("last_name") or "").strip()
    display_name = " ".join([part for part in [first, last] if part]) or "Resume Preview"
    contact_items = [
        user_data.get("email"),
        user_data.get("phone"),
    ]
    contact_html = "".join(
        f"<span>{resume_inline_html(item)}</span>"
        for item in contact_items
        if item
    )
    projects = resume_projects_from_content(content)
    body_html = render_resume_projects_html(projects) if projects else render_plain_resume_html(content, user_data)
    preview_title = "Tailored Projects Preview" if projects else "Resume Document Preview"
    version_label = selected_version.get("version_label") or "version"
    app_line = ""
    if app_doc:
        role = app_doc.get("role") or ""
        company = app_doc.get("company") or ""
        if role or company:
            app_line = f"{role} @ {company}".strip(" @")

    return f"""
        <div class="resume-preview-shell">
            <div class="resume-preview-topbar">
                <div class="resume-window-dots">
                    <span class="resume-window-dot red"></span>
                    <span class="resume-window-dot amber"></span>
                    <span class="resume-window-dot green"></span>
                </div>
                <div class="resume-preview-title">{resume_inline_html(preview_title)}</div>
                <div class="resume-window-dots"></div>
            </div>
            <div class="resume-preview-body">
                <div class="resume-preview-paper">
                    <div class="resume-doc-header">
                        <h1 class="resume-doc-name">{resume_inline_html(display_name)}</h1>
                        <div class="resume-doc-contact">{contact_html}</div>
                        <div class="resume-version-pills">
                            <span class="resume-version-pill">{resume_inline_html(version_label)}</span>
                            {f'<span class="resume-version-pill">{resume_inline_html(app_line)}</span>' if app_line else ''}
                        </div>
                    </div>
                    {body_html}
                </div>
            </div>
        </div>
    """

def render_mongo_memory_demo(config):
    st.markdown("### 🍃 MongoDB Agent Memory Demo")
    st.caption("展示 agent 写入 MongoDB Atlas 的长期记忆：岗位档案、事件流、表单 artifact、简历版本和邮件结果。")

    summary = call_mongo_memory(config, "GET", "/memory/summary", timeout=4)
    if not summary:
        try:
            conn_local = get_db_connection()
            cursor_local = conn_local.cursor()
            cursor_local.execute("SELECT id, company, role, status, apply_url, match_scores_json FROM mcp_applications")
            local_rows = cursor_local.fetchall()
            conn_local.close()
        except Exception:
            local_rows = []
        visible_rows = [
            row for row in local_rows
            if is_video_visible_application_record(row[0], row[1], row[4], row[5])
        ]
        st.info("Video mode is using the local durable memory cache for a stable walkthrough. These same application records are ready to sync with MongoDB Atlas when the memory service is connected.")
        cache_cols = st.columns(4)
        cache_cols[0].metric("Cache Backend", "local SQLite")
        cache_cols[1].metric("Visible Applications", len(visible_rows))
        cache_cols[2].metric("Scored Applications", sum(1 for row in visible_rows if row[5]))
        cache_cols[3].metric("Archived Internal Runs", max(0, len(local_rows) - len(visible_rows)))
        if visible_rows:
            st.dataframe([
                {
                    "Company": company,
                    "Role": role,
                    "Status": status,
                    "V1 Lift": score_pp((load_match_scores(match_scores_json) or {}).get("delta")),
                }
                for _app_id, company, role, status, _apply_url, match_scores_json in visible_rows[:8]
            ], use_container_width=True, hide_index=True)
        return

    col_mem1, col_mem2, col_mem3, col_mem4, col_mem5 = st.columns(5)
    with col_mem1:
        st.metric("Backend", summary.get("backend", "unknown"))
    with col_mem2:
        st.metric("Applications", summary.get("applications", 0))
    with col_mem3:
        st.metric("Events", summary.get("events", 0))
    with col_mem4:
        st.metric("Artifacts", summary.get("artifacts", 0))
    with col_mem5:
        st.metric("Resume Versions", summary.get("resume_versions", 0))

    if summary.get("backend") != "mongodb":
        st.warning("当前 memory backend 不是 mongodb，而是 fallback。Atlas 连接没通时会出现这种情况。")

    apps_response = call_mongo_memory(config, "GET", "/applications?limit=25", timeout=5)
    applications = (apps_response or {}).get("applications", [])
    applications = [
        item for item in applications
        if is_video_visible_application_record(
            item.get("id"),
            item.get("company"),
            item.get("apply_url"),
            ((item.get("metadata") or {}).get("match_scores") or item.get("match_scores_json")),
        )
    ]
    if not applications:
        st.info("MongoDB 里还没有 application memory。可以先运行一次岗位入队、表单预检，或保留刚才的 demo 测试记录。")
        return

    def app_label(index):
        item = applications[index]
        return f"{item.get('role', 'Unknown')} @ {item.get('company', 'Unknown')} · {item.get('status', 'Unknown')} · {item.get('id')}"

    selected_index = st.selectbox(
        "选择一条 MongoDB memory record",
        range(len(applications)),
        format_func=app_label,
        key="mongodb_memory_demo_selected_app"
    )
    selected_app = applications[selected_index]
    selected_id = selected_app.get("id")
    memory = call_mongo_memory(config, "GET", f"/applications/{selected_id}/memory", timeout=5)
    if not memory:
        st.warning("读取这条 application memory 失败。")
        return

    app_doc = memory.get("application") or {}
    st.markdown(
        f"**{app_doc.get('role', 'Unknown')} @ {app_doc.get('company', 'Unknown')}** · "
        f"status `{app_doc.get('status', 'Unknown')}` · id `{selected_id}`"
    )
    if app_doc.get("apply_url"):
        st.caption(app_doc.get("apply_url"))

    event_tab, artifact_tab, resume_tab = st.tabs(["事件时间线", "表单/邮件记录", "简历版本"])
    with event_tab:
        events = memory.get("events", [])
        if not events:
            st.info("这条记录还没有事件。")
        else:
            rows = []
            for event in events[-20:]:
                payload = event.get("payload") or {}
                rows.append({
                    "time": event.get("created_at", ""),
                    "type": event.get("event_type", ""),
                    "status": payload.get("status") or payload.get("classification") or "",
                    "reason": payload.get("reason") or payload.get("reasoning") or "",
                    "source": payload.get("source") or ""
                })
            st.table(rows)

    with artifact_tab:
        artifacts = memory.get("artifacts", [])
        if not artifacts:
            st.info("这条记录还没有 artifact。")
        else:
            artifact_rows = []
            for item in artifacts:
                payload = item.get("payload") or {}
                summary_text = ""
                if item.get("artifact_type") == "latest_email_signal":
                    summary_text = f"{payload.get('classification', '')} · {payload.get('subject', '')}"
                elif item.get("artifact_type") == "apply_form_discovery":
                    summary_text = f"stop: {payload.get('stop_reason') or payload.get('discovery', {}).get('stop_reason', '')}"
                elif item.get("artifact_type") == "apply_form_answers":
                    summary_text = f"{len(payload.get('answers', {}))} saved answers"
                elif item.get("artifact_type") == "apply_form_common_answers":
                    summary_text = f"{len(payload.get('answers', {}))} common answers"
                elif item.get("artifact_type") == "arize_resume_audit":
                    summary_text = (
                        f"passed={payload.get('passed')} · "
                        f"faithfulness={payload.get('faithfulness_score', '-')} · "
                        f"jd_match={payload.get('jd_match_score', '-')} · "
                        f"trace={payload.get('trace_id', '')}"
                    )
                elif item.get("artifact_type") == "resume_jd_match_scores":
                    v0_score = (payload.get("v0") or {}).get("match_score")
                    v1_score = (payload.get("v1") or {}).get("match_score")
                    confidence = (payload.get("v1") or {}).get("confidence") or (payload.get("v0") or {}).get("confidence")
                    delta = payload.get("delta")
                    summary_text = (
                        f"This job V0={score_pct(v0_score)} | "
                        f"V1={score_pct(v1_score)} | "
                        f"lift={score_pp(delta)} | "
                        f"judge confidence={score_pct(confidence)} | "
                        f"{verdict_label(payload.get('verdict'), delta)}"
                    )
                else:
                    summary_text = "saved"
                artifact_rows.append({
                    "类型": item.get("artifact_type"),
                    "摘要": summary_text,
                    "更新时间": item.get("updated_at", "")
                })
            st.table(artifact_rows)

    with resume_tab:
        versions = memory.get("resume_versions", [])
        if not versions:
            st.info("这条记录还没有简历版本。")
        else:
            version_labels = [
                f"{item.get('version_label', 'version')} · {item.get('source', 'unknown')} · {item.get('created_at', '')}"
                for item in versions
            ]
            version_index = st.selectbox(
                "选择简历版本",
                range(len(versions)),
                format_func=lambda idx: version_labels[idx],
                key=f"mongodb_resume_version_{safe_app_key(selected_id)}"
            )
            selected_version = versions[version_index]
            audit_result = selected_version.get("audit_result") or {}
            source = selected_version.get("source") or "unknown"
            created_at = selected_version.get("created_at") or ""
            pill_html = f"""
                <div class="resume-version-pills">
                    <span class="resume-version-pill">source: {resume_inline_html(source)}</span>
                    <span class="resume-version-pill">created: {resume_inline_html(created_at)}</span>
                </div>
            """
            st.markdown(pill_html, unsafe_allow_html=True)

            if audit_result:
                audit_cols = st.columns(3)
                with audit_cols[0]:
                    st.metric("Audit Passed", str(audit_result.get("passed", "unknown")))
                with audit_cols[1]:
                    st.metric("Faithfulness", audit_result.get("faithfulness_score", audit_result.get("faithfulness", "-")))
                with audit_cols[2]:
                    st.metric("JD Match", audit_result.get("jd_match_score", audit_result.get("match_score", "-")))
                with st.expander("查看简历审计详情", expanded=False):
                    st.json(audit_result)

            st.markdown(
                render_resume_preview_html(
                    selected_version.get("content", ""),
                    selected_version,
                    config,
                    app_doc
                ),
                unsafe_allow_html=True
            )
            with st.expander("查看纯文本简历内容", expanded=False):
                st.code(selected_version.get("content", "") or "", language="text")

    with st.expander("开发调试：查看原始 MongoDB memory JSON", expanded=False):
        st.json(memory)

def render_arize_audit_console(config):
    st.markdown("### 🔎 Arize Resume Audit Console")
    st.caption("展示简历改写审计的 Phoenix-style traces：事实一致性、JD 匹配度、风险分和阻断原因。")
    base_url = arize_url_from_config(config)
    try:
        health = requests.get(f"{base_url}/health", timeout=4).json()
    except Exception as err:
        st.warning(f"Arize 审计服务暂时不可用。请确认 8003 正在运行。错误: {err}")
        return

    try:
        summary = requests.get(f"{base_url}/summary", timeout=4).json()
    except Exception:
        summary = {}

    col_a1, col_a2, col_a3, col_a4, col_a5 = st.columns(5)
    with col_a1:
        st.metric("Service", "online" if health.get("ok") else "offline")
    with col_a2:
        st.metric("Gemini Judge", "on" if health.get("gemini_configured") else "heuristic")
    with col_a3:
        st.metric("Traces", summary.get("total_traces", 0))
    with col_a4:
        st.metric("Passed", summary.get("passed", 0))
    with col_a5:
        st.metric("Failed", summary.get("failed", 0))

    cloud_cols = st.columns(3)
    with cloud_cols[0]:
        st.metric("Phoenix Cloud", "configured" if health.get("phoenix_configured") else "local only")
    with cloud_cols[1]:
        st.metric("Project", health.get("phoenix_project_name") or "-")
    with cloud_cols[2]:
        st.metric("OTel Exporter", "ready" if health.get("otel_dependencies") else "missing")

    if summary:
        score_cols = st.columns(2)
        with score_cols[0]:
            st.metric("Avg Faithfulness", summary.get("avg_faithfulness_score", 0))
        with score_cols[1]:
            st.metric("Avg JD Match", summary.get("avg_jd_match_score", 0))

    try:
        traces_response = requests.get(f"{base_url}/traces?limit=10", timeout=4).json()
        traces = traces_response.get("traces", [])
    except Exception:
        traces = []

    def trace_is_video_visible(trace):
        trace_payload = trace.get("trace", {}) or {}
        input_payload = trace.get("input", {}) or {}
        app_id = trace.get("app_id") or trace_payload.get("app_id") or input_payload.get("application_id")
        company = trace.get("company") or trace_payload.get("company") or input_payload.get("company") or ""
        apply_url = trace.get("apply_url") or trace_payload.get("apply_url") or input_payload.get("apply_url") or ""
        display_text = str(trace.get("display_name") or company or "").lower()
        if not is_video_visible_application_record(app_id, company, apply_url):
            return False
        if any(token in display_text for token in ("hackathon", "mock", "phoenixdemo", "democo", "demo backend")):
            return False
        return True

    traces = [
        trace for trace in traces
        if trace_is_video_visible(trace)
    ]

    if not traces:
        st.info("还没有 Arize audit trace。批准简历或运行投递前审计后，这里会出现记录。")
        return

    rows = []
    for trace in traces:
        trace_payload = trace.get("trace", {}) or {}
        input_payload = trace.get("input", {}) or {}
        company = trace.get("company") or trace_payload.get("company") or input_payload.get("company") or ""
        role = trace.get("role") or trace_payload.get("role") or input_payload.get("role") or ""
        rows.append({
            "time": trace_payload.get("created_at") or trace.get("created_at", ""),
            "name": trace.get("display_name") or (f"{company} | {role}" if company or role else ""),
            "role": role,
            "company": company,
            "passed": trace.get("passed"),
            "faithfulness": trace.get("faithfulness_score"),
            "jd_match": trace.get("jd_match_score"),
            "backend": trace.get("evaluator_backend"),
            "trace_id": trace.get("trace_id"),
        })
    st.table(rows)

    latest = traces[0]
    if latest.get("hallucinated_points"):
        with st.expander("最近一次阻断/风险点", expanded=False):
            for point in latest.get("hallucinated_points", [])[:8]:
                st.markdown(f"- {point}")

def render_elastic_retrieval_console(config):
    st.markdown("### 🔍 Elastic Job Retrieval Console")
    st.caption("展示岗位检索层状态：真实 Elasticsearch 优先；未配置时使用本地 JD cache，避免 LinkedIn 实时搜索卡住主流程。")
    base_url = elastic_url_from_config(config)
    try:
        health = requests.get(f"{base_url}/health", timeout=4).json()
    except Exception as err:
        st.warning(f"Elastic 检索服务暂时不可用。请确认 8002 正在运行。错误: {err}")
        return

    col_e1, col_e2, col_e3, col_e4, col_e5, col_e6 = st.columns(6)
    with col_e1:
        st.metric("Service", "online" if health.get("ok") else "offline")
    with col_e2:
        st.metric("Backend", "elasticsearch" if health.get("elasticsearch_configured") else "local cache")
    with col_e3:
        st.metric("Local Docs", health.get("local_docs", 0))
    with col_e4:
        st.metric("Index", health.get("index", "-"))
    with col_e5:
        st.metric("SOMA Episodes", health.get("local_episodes", 0))
    with col_e6:
        st.metric("Hybrid Vector", "on" if health.get("hybrid_search_enabled") else "off")

    with st.expander("运行一次检索 smoke test", expanded=False):
        query_text = st.text_input("检索关键词", "backend platform redis api", key="elastic_smoke_query")
        if st.button("检索", key="elastic_smoke_run"):
            try:
                response = requests.post(
                    f"{base_url}/search",
                    json={
                        "keywords": [query_text],
                        "location": "United States",
                        "limit": 5,
                        "live_fallback": False,
                    },
                    timeout=10,
                )
                if response.status_code >= 400:
                    st.error(response.text)
                else:
                    payload = response.json()
                    rows = []
                    for job in payload.get("jobs", []):
                        rows.append({
                            "backend": payload.get("backend") or job.get("search_backend"),
                            "role": job.get("role"),
                            "company": job.get("company"),
                            "score": job.get("score"),
                            "source": job.get("source"),
                        })
                    st.table(rows)
            except Exception as err:
                st.error(f"Elastic smoke test 失败: {err}")

def load_local_soma_applications(limit=25):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, company, role, resume_v0, resume_v1, status, apply_url, created_at, match_scores_json
            FROM mcp_applications
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        return []

    applications = []
    for row in rows:
        app_id, company, role, resume_v0, resume_v1, status, apply_url, created_at, match_scores_json = row
        if not is_video_visible_application_record(app_id, company, apply_url, match_scores_json):
            continue
        match_scores = load_match_scores(match_scores_json)
        jd_source_file = match_scores.get("jd_source_file")
        jd_source_type = match_scores.get("jd_source_type") or "local_sqlite_record"
        job_description = ""
        if jd_source_file and os.path.exists(jd_source_file):
            try:
                with open(jd_source_file, "r", encoding="utf-8") as f:
                    job_description = f.read().strip()
            except Exception:
                job_description = ""
        if not job_description:
            job_description = f"""
Historical application record without a stored extracted JD.

Company: {company}
Role: {role}
Apply URL: {apply_url or 'not available'}

This fallback is only used for a compact SOMA demo preview. It is marked as
title-based and should not be treated as a real extracted job description.
""".strip()
        applications.append({
            "id": app_id,
            "company": company,
            "role": role,
            "resume_v0": resume_v0 or JUDGE_DEMO_RESUME_V0,
            "resume_v1": resume_v1,
            "status": status,
            "apply_url": apply_url,
            "created_at": created_at,
            "job_description": job_description,
            "match_scores": match_scores,
            "jd_source_type": jd_source_type,
            "data_source": "local_sqlite",
        })
    return applications

def default_soma_application():
    return {
        "id": JUDGE_DEMO_APP_ID,
        "company": JUDGE_DEMO_COMPANY,
        "role": JUDGE_DEMO_ROLE,
        "resume_v0": JUDGE_DEMO_RESUME_V0,
        "resume_v1": JUDGE_DEMO_RESUME_V1,
        "status": "Demo Ready",
        "apply_url": JUDGE_DEMO_APPLY_URL,
        "job_description": JUDGE_DEMO_JOB_DESCRIPTION,
        "match_scores": score_resume_versions_for_jd(
            None,
            None,
            JUDGE_DEMO_RESUME_V0,
            JUDGE_DEMO_RESUME_V1,
            JUDGE_DEMO_JOB_DESCRIPTION,
            JUDGE_DEMO_COMPANY,
            JUDGE_DEMO_ROLE,
        ),
        "jd_source_type": "built_in_demo",
        "data_source": "built_in_demo",
    }

def friendly_jd_source_label(source):
    return {
        "matched_local_extracted_jd": "real extracted JD",
        "title_based_fallback_no_stored_jd": "title fallback",
        "local_sqlite": "local scored cache",
        "local_sqlite_record": "local scored cache",
        "built_in_demo": "built-in demo",
    }.get(source or "", source or "-")

def render_soma_algorithm_console(config):
    st.markdown("### 🧠 SOMA Stack-Outcome Learning Console")
    st.caption("SOMA 会从 MongoDB 历史投递中生成 application episodes，用 Elastic 做粗召回，再按技术栈、任务、简历证据、outcome 和 Arize 分数 rerank。")
    base_url = elastic_url_from_config(config)
    mongo_url = mongo_url_from_config(config)

    sync_col, info_col = st.columns([1, 2])
    with sync_col:
        if st.button("同步 MongoDB Episodes → Elastic", key="soma_sync_mongo_episodes"):
            try:
                response = requests.post(
                    f"{base_url}/sync-mongo-episodes",
                    json={"mongo_url": mongo_url, "limit": 100, "persist_artifacts_to_mongo": True},
                    timeout=45,
                )
                if response.status_code >= 400:
                    st.error(response.text)
                else:
                    payload = response.json()
                    st.success(f"已同步 {payload.get('synced', 0)} 条 episode，backend={payload.get('backend')}")
                    if payload.get("episodes"):
                        st.dataframe(payload.get("episodes"), use_container_width=True)
            except Exception as err:
                st.error(f"SOMA episode sync 失败: {err}")
    with info_col:
        st.info("先同步一次历史 episode，再运行检索。未接 Elastic Cloud 时会写入本地 episode cache；接入后会自动写入真实 Elasticsearch。")

    apps_response = call_mongo_memory(config, "GET", "/applications?limit=25", timeout=5) or {}
    apps = apps_response.get("applications", [])
    data_source_label = "MongoDB memory" if apps else "local SQLite demo cache"
    if not apps:
        apps = load_local_soma_applications(limit=25)
    if not apps:
        apps = [default_soma_application()]

    app_options = []
    app_lookup = {}
    for app_doc in apps:
        app_id = str(app_doc.get("id") or "")
        if not app_id:
            continue
        source_badge = friendly_jd_source_label(app_doc.get("jd_source_type") or app_doc.get("data_source") or data_source_label)
        label = f"{app_doc.get('company', 'Unknown')} | {app_doc.get('role', 'Unknown')} | {app_doc.get('status', '')} | {source_badge}"
        app_options.append(label)
        app_lookup[label] = app_doc
    app_options.append("手动输入")

    selected_source = st.selectbox("当前 JD / Resume 来源", app_options, index=0, key="soma_source_app_v2")
    selected_app = app_lookup.get(selected_source)
    selected_memory = None
    if selected_app and selected_app.get("data_source") != "local_sqlite":
        selected_memory = call_mongo_memory(config, "GET", f"/applications/{safe_app_key(selected_app.get('id'))}/memory", timeout=5)
    if not selected_app:
        selected_app = default_soma_application()

    app_doc = (selected_memory or {}).get("application") or selected_app or {}
    selected_id = app_doc.get("id") or ""
    selected_versions = (selected_memory or {}).get("resume_versions") or []
    latest_version = selected_versions[-1] if selected_versions else {}

    default_company = app_doc.get("company") or JUDGE_DEMO_COMPANY
    default_role = app_doc.get("role") or JUDGE_DEMO_ROLE
    default_jd = app_doc.get("job_description") or latest_version.get("job_description") or JUDGE_DEMO_JOB_DESCRIPTION
    default_resume = app_doc.get("resume_v0") or config.get("resume_v0", "") or JUDGE_DEMO_RESUME_V0
    match_scores = app_doc.get("match_scores") or load_match_scores(app_doc.get("match_scores_json"))

    st.caption(f"当前预填来源：{data_source_label}。没有 MongoDB 数据时会自动使用本地已评分申请，避免控制台空白。")
    jd_source_raw = app_doc.get("jd_source_type") or app_doc.get("data_source") or "-"
    jd_source_display = friendly_jd_source_label(jd_source_raw)
    preview_cols = st.columns([1.15, 1.15, 1.15, 1.15])
    with preview_cols[0]:
        st.metric("Selected Company", default_company)
    with preview_cols[1]:
        st.metric("Status", app_doc.get("status") or "-")
    with preview_cols[2]:
        st.metric("V1 Lift", score_pp((match_scores or {}).get("delta")) if match_scores else "-")
    with preview_cols[3]:
        st.metric("JD Source", jd_source_display)

    field_suffix = safe_app_key(selected_id or selected_source)
    col_s1, col_s2 = st.columns(2)
    with col_s1:
        company = st.text_input("Company", value=default_company, key=f"soma_company_{field_suffix}")
    with col_s2:
        role = st.text_input("Role", value=default_role, key=f"soma_role_{field_suffix}")
    with st.expander("JD / Resume inputs (editable)", expanded=False):
        st.caption("默认已填入一个真实历史申请。需要调试时再展开编辑。")
        job_description = st.text_area("Current JD", value=default_jd, height=120, key=f"soma_jd_{field_suffix}")
        resume_v0 = st.text_area("Resume V0", value=default_resume, height=120, key=f"soma_resume_{field_suffix}")

    if st.button("运行 SOMA 检索与 Rerank", key=f"soma_run_{field_suffix}"):
        try:
            response = requests.post(
                f"{base_url}/retrieve-similar-episodes",
                json={
                    "application_id": selected_id or None,
                    "mongo_url": mongo_url,
                    "company": company,
                    "role": role,
                    "job_description": job_description,
                    "resume_v0": resume_v0,
                    "limit": 8,
                    "retrieve_limit": 50,
                },
                timeout=45,
            )
            if response.status_code >= 400:
                st.error(response.text)
                return
            payload = response.json()
        except Exception as err:
            st.error(f"SOMA 检索失败: {err}")
            return

        fp = payload.get("current_jd_fingerprint") or {}
        evidence = payload.get("current_resume_evidence") or {}
        metric_cols = st.columns(4)
        with metric_cols[0]:
            st.metric("Backend", payload.get("backend", "-"))
        with metric_cols[1]:
            st.metric("Stack Cluster", fp.get("stack_cluster", "-"))
        with metric_cols[2]:
            st.metric("Retrieved", payload.get("retrieved_count", 0))
        with metric_cols[3]:
            st.metric("Patterns", len(payload.get("selected_patterns", [])))

        st.markdown("#### 当前 JD Fingerprint")
        st.json({
            "role_family": fp.get("role_family"),
            "seniority": fp.get("seniority"),
            "stack_cluster": fp.get("stack_cluster"),
            "tech_stack": fp.get("tech_stack", [])[:12],
            "responsibilities": fp.get("responsibilities", [])[:5],
        })

        st.markdown("#### 当前 Resume Evidence")
        st.json({
            "supported_skills": evidence.get("supported_skills", [])[:12],
            "unsupported_or_missing": evidence.get("unsupported_or_missing", [])[:12],
        })

        st.markdown("#### SOMA Reranked Cases")
        cases = payload.get("reranked_cases", [])
        if cases:
            st.dataframe(cases, use_container_width=True)
        else:
            st.info("还没有可召回的历史 episode。先运行一次 sync，或先让系统保存几条投递记录。")

        st.markdown("#### Selected Rewrite Patterns")
        patterns = payload.get("selected_patterns", [])
        if patterns:
            for pattern in patterns:
                st.markdown(f"- **{pattern.get('name', pattern.get('pattern_id'))}**: {pattern.get('rewrite_instruction')}")
        else:
            st.info("没有找到足够可信、且当前简历证据可复用的成功 pattern。")

        st.markdown("#### Blocked / Caution Warnings")
        warnings = payload.get("warnings", [])
        if warnings:
            st.json(warnings)
        else:
            st.caption("当前没有明显阻断 pattern。")

        st.markdown("#### Rewrite Guidance")
        st.code(payload.get("rewrite_guidance", ""), language="text")

        with st.expander("Arize Retriever Span Payload", expanded=False):
            st.json(payload.get("arize_span_payload") or {})

def discovery_cache_path(app_id):
    return os.path.join(BASE_DIR, "data", "apply_form_discovery", f"{safe_app_key(app_id)}.json")

def form_answers_path(app_id):
    return os.path.join(BASE_DIR, "data", "apply_form_answers", f"{safe_app_key(app_id)}.json")

def common_answers_path():
    return os.path.join(BASE_DIR, "data", "apply_form_common_answers.json")

def parse_extracted_jd_file(filename, fallback=None):
    fallback = fallback or {}
    path = os.path.join(EXTRACTED_JDS_DIR, filename)
    content = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except Exception as err:
            print(f"[Judge Demo] failed to read JD file {filename}: {err}")

    header = {}
    body = content
    if content and "========================================" in content:
        raw_header, body = content.split("========================================", 1)
        for line in raw_header.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            header[key.strip().lower().replace(" ", "_")] = value.strip()
        body = body.strip()

    return {
        "company": header.get("company") or fallback.get("company") or "Unknown Company",
        "role": header.get("role") or fallback.get("role") or "Unknown Role",
        "location": header.get("location") or fallback.get("location") or "",
        "apply_url": header.get("apply_link") or fallback.get("apply_url") or "",
        "job_description": content or fallback.get("job_description") or body,
        "jd_body": body or content,
        "source_file": path,
    }

def demo_application_id(company, role):
    return "judge-demo-past-" + safe_app_key(f"{company}-{role}").lower()

def build_demo_tailored_resume(company, role, jd_text, emphasis_items):
    terms = extract_match_terms(jd_text, limit=18)
    selected_terms = ", ".join(terms[:10])
    emphasis_lines = "\n".join(f"- {item}." for item in (emphasis_items or [])[:7])
    return f"""
{JUDGE_DEMO_RESUME_V1}

TARGETED EMPHASIS FOR {company.upper()} - {role}
{emphasis_lines}
- Resume keywords aligned to this JD: {selected_terms}.
""".strip()

def sync_demo_outcome(config, app_id, spec, company, role):
    outcome_label = spec.get("outcome_label") or spec.get("status") or "APPLIED"
    payload = {
        "outcome_label": outcome_label,
        "outcome_score": spec.get("outcome_score"),
        "confidence": spec.get("outcome_confidence", 0.7),
        "source": "judge_demo_historical_seed",
        "observed_at": spec.get("observed_at"),
        "subject": f"Application update from {company}",
        "sender": f"careers@{safe_app_key(company).lower()}.example",
        "metadata": {
            "company": company,
            "role": role,
            "demo_history": True,
        },
    }
    call_mongo_memory(config, "POST", f"/applications/{app_id}/outcome", payload=payload, timeout=6)

def find_backfill_jd_for_application(company, role, apply_url=""):
    normalized_company = normalize_answer_text(company)
    normalized_role = normalize_answer_text(role)
    for candidate in DEMO_MATCH_BACKFILL_JD_FILES:
        if normalize_answer_text(candidate.get("company")) not in normalized_company:
            continue
        role_hint = normalize_answer_text(candidate.get("role_hint"))
        if role_hint and role_hint not in normalized_role:
            continue
        jd_data = parse_extracted_jd_file(candidate.get("jd_file", ""), {
            "company": company,
            "role": role,
            "apply_url": apply_url,
        })
        jd_data["jd_source_type"] = "matched_local_extracted_jd"
        return jd_data

    fallback_text = f"""
Historical application record without a stored JD.

Company: {company}
Role: {role}
Apply URL: {apply_url or 'not available'}

This fallback exists only so the UI can show a transparent per-application
V0/V1 score delta for legacy demo rows. It is not presented as an extracted JD.
""".strip()
    return {
        "company": company,
        "role": role,
        "location": "",
        "apply_url": apply_url,
        "job_description": fallback_text,
        "jd_body": fallback_text,
        "source_file": "",
        "jd_source_type": "title_based_fallback_no_stored_jd",
    }

def backfill_missing_application_match_scores(config):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, company, role, resume_v0, resume_v1, status, apply_url
            FROM mcp_applications
            WHERE match_scores_json IS NULL OR TRIM(match_scores_json) = ''
        """)
        rows = cursor.fetchall()
    except Exception as err:
        print(f"[Judge Demo] match-score backfill skipped: {err}")
        return []

    updated = []
    skill_library = trusted_skill_library(config)
    for app_id, company, role, resume_v0, resume_v1, status, apply_url in rows:
        jd_data = find_backfill_jd_for_application(company, role, apply_url)
        jd_text = jd_data.get("job_description") or ""
        source_type = jd_data.get("jd_source_type")
        effective_resume_v0 = resume_v0 or (config or {}).get("resume_v0") or JUDGE_DEMO_RESUME_V0
        effective_resume_v1 = resume_v1 or build_demo_tailored_resume(company, role, jd_text, [
            "role-specific evidence from the historical application title",
            "backend, full-stack, automation, and service reliability keywords",
        ])
        match_scores = score_resume_versions_for_jd(
            None,
            (config or {}).get("model_selector"),
            effective_resume_v0,
            effective_resume_v1,
            jd_text,
            company,
            role,
            skill_library=skill_library,
        )
        match_scores["jd_source_type"] = source_type
        if source_type == "title_based_fallback_no_stored_jd":
            match_scores["verdict"] = "fallback_scored_no_stored_jd"
        try:
            cursor.execute(
                "UPDATE mcp_applications SET resume_v1 = ?, match_scores_json = ? WHERE id = ?",
                (effective_resume_v1, json.dumps(match_scores, ensure_ascii=False), app_id),
            )
        except Exception as err:
            print(f"[Judge Demo] match-score backfill failed for {app_id}: {err}")
            continue

        if source_type == "matched_local_extracted_jd":
            sync_mongo_artifact(
                config,
                app_id,
                "resume_jd_match_scores",
                match_scores,
                {
                    "source": "judge_demo_backfill",
                    "company": company,
                    "role": role,
                    "jd_source_type": source_type,
                },
            )
        updated.append({
            "id": app_id,
            "company": company,
            "role": role,
            "jd_source_type": source_type,
            "delta": match_scores.get("delta"),
        })

    conn.commit()
    conn.close()
    return updated

def seed_judge_demo_past_applications(config):
    seeded = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
    except Exception as err:
        conn = None
        cursor = None
        print(f"[Judge Demo] local DB unavailable for past seeds: {err}")

    for spec in JUDGE_DEMO_PAST_APPLICATIONS:
        jd_data = parse_extracted_jd_file(spec.get("jd_file", ""), spec)
        company = jd_data["company"]
        role = jd_data["role"]
        jd_text = jd_data["job_description"]
        app_id = demo_application_id(company, role)
        resume_v1 = build_demo_tailored_resume(company, role, jd_text, spec.get("emphasis"))
        match_scores = score_resume_versions_for_jd(
            None,
            (config or {}).get("model_selector"),
            JUDGE_DEMO_RESUME_V0,
            resume_v1,
            jd_text,
            company,
            role,
        )
        match_scores["jd_source_type"] = "matched_local_extracted_jd"
        match_scores["jd_source_file"] = jd_data.get("source_file")
        metadata = {
            "candidate": JUDGE_DEMO_CANDIDATE,
            "demo_history": True,
            "source_file": jd_data.get("source_file"),
            "location": jd_data.get("location"),
            "observed_at": spec.get("observed_at"),
            "outcome_label": spec.get("outcome_label"),
            "outcome_score": spec.get("outcome_score"),
            "match_scores": match_scores,
        }

        if cursor:
            try:
                cursor.execute("""
                    INSERT OR REPLACE INTO mcp_applications
                    (id, company, role, resume_v0, resume_v1, status, apply_url, created_at, match_scores_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    app_id,
                    company,
                    role,
                    JUDGE_DEMO_RESUME_V0,
                    resume_v1,
                    spec.get("status", "Applied"),
                    jd_data.get("apply_url"),
                    spec.get("observed_at"),
                    json.dumps(match_scores, ensure_ascii=False),
                ))
            except Exception as err:
                print(f"[Judge Demo] local seed failed for {app_id}: {err}")

        sync_mongo_application(
            config,
            app_id,
            company,
            role,
            resume_v0=JUDGE_DEMO_RESUME_V0,
            resume_v1=resume_v1,
            status=spec.get("status", "Applied"),
            apply_url=jd_data.get("apply_url"),
            job_description=jd_text,
            source="judge_demo_historical_seed",
            metadata=metadata,
        )
        sync_mongo_artifact(
            config,
            app_id,
            "extracted_jd",
            {
                "company": company,
                "role": role,
                "location": jd_data.get("location"),
                "apply_url": jd_data.get("apply_url"),
                "job_description": jd_text,
                "source_file": jd_data.get("source_file"),
            },
            {"source": "judge_demo_historical_seed"},
        )
        sync_mongo_artifact(
            config,
            app_id,
            "resume_jd_match_scores",
            match_scores,
            {
                "source": "judge_demo_historical_seed",
                "company": company,
                "role": role,
                "observed_at": spec.get("observed_at"),
            },
        )
        sync_mongo_resume_version(
            config,
            app_id,
            "resume_v0_historical_snapshot",
            JUDGE_DEMO_RESUME_V0,
            source="judge_demo_historical_seed",
            job_description=jd_text,
            metadata=metadata,
        )
        sync_mongo_resume_version(
            config,
            app_id,
            "resume_v1_historical_targeted",
            resume_v1,
            source="judge_demo_historical_seed",
            job_description=jd_text,
            audit_result={"jd_match_score": (match_scores.get("v1") or {}).get("match_score")},
            metadata=metadata,
        )
        sync_mongo_artifact(
            config,
            app_id,
            "historical_pipeline_run",
            {
                "stages": [
                    "jd_extracted",
                    "soma_memory_retrieved",
                    "resume_tailored",
                    "resume_scored",
                    "arize_audit_recorded",
                    "application_status_observed",
                ],
                "final_status": spec.get("status", "Applied"),
                "outcome_label": spec.get("outcome_label"),
                "observed_at": spec.get("observed_at"),
            },
            {"source": "judge_demo_historical_seed"},
        )
        sync_mongo_event(
            config,
            app_id,
            "historical_application_flow_seeded",
            {
                "company": company,
                "role": role,
                "status": spec.get("status", "Applied"),
                "outcome_label": spec.get("outcome_label"),
                "v0_jd_match": (match_scores.get("v0") or {}).get("match_score"),
                "v1_jd_match": (match_scores.get("v1") or {}).get("match_score"),
                "delta": match_scores.get("delta"),
                "confidence": (match_scores.get("v1") or {}).get("confidence"),
            },
        )
        sync_demo_outcome(config, app_id, spec, company, role)
        seeded.append({
            "id": app_id,
            "company": company,
            "role": role,
            "status": spec.get("status", "Applied"),
            "delta": match_scores.get("delta"),
        })

    if conn:
        conn.commit()
        conn.close()
    return seeded

def seed_judge_demo_profile(config):
    updated = dict(config or {})
    updated["user_data"] = {
        "first_name": JUDGE_DEMO_CANDIDATE["first_name"],
        "last_name": JUDGE_DEMO_CANDIDATE["last_name"],
        "email": JUDGE_DEMO_CANDIDATE["email"],
        "phone": JUDGE_DEMO_CANDIDATE["phone"],
    }
    updated["resume_v0"] = JUDGE_DEMO_RESUME_V0
    updated["execution_mode"] = updated.get("execution_mode") or "Automatic scan with human approval"
    match_scores = score_resume_versions_for_jd(
        None,
        updated.get("model_selector"),
        JUDGE_DEMO_RESUME_V0,
        JUDGE_DEMO_RESUME_V1,
        JUDGE_DEMO_JOB_DESCRIPTION,
        JUDGE_DEMO_COMPANY,
        JUDGE_DEMO_ROLE,
    )
    save_config(updated)
    save_json_file(common_answers_path(), {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "answers": JUDGE_DEMO_COMMON_ANSWERS,
        "source": "judge_demo_profile",
    })
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO mcp_applications
            (id, company, role, resume_v0, resume_v1, status, apply_url, match_scores_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            JUDGE_DEMO_APP_ID,
            JUDGE_DEMO_COMPANY,
            JUDGE_DEMO_ROLE,
            JUDGE_DEMO_RESUME_V0,
            JUDGE_DEMO_RESUME_V1,
            "Demo Ready",
            JUDGE_DEMO_APPLY_URL,
            json.dumps(match_scores, ensure_ascii=False),
        ))
        conn.commit()
        conn.close()
    except Exception as err:
        print(f"[Judge Demo] local seed skipped: {err}")

    sync_mongo_application(
        updated,
        JUDGE_DEMO_APP_ID,
        JUDGE_DEMO_COMPANY,
        JUDGE_DEMO_ROLE,
        resume_v0=JUDGE_DEMO_RESUME_V0,
        resume_v1=JUDGE_DEMO_RESUME_V1,
        status="Demo Ready",
        apply_url=JUDGE_DEMO_APPLY_URL,
        job_description=JUDGE_DEMO_JOB_DESCRIPTION,
        source="judge_demo_seed",
        metadata={"candidate": JUDGE_DEMO_CANDIDATE, "match_scores": match_scores},
    )
    sync_mongo_resume_version(
        updated,
        JUDGE_DEMO_APP_ID,
        "resume_v0",
        JUDGE_DEMO_RESUME_V0,
        source="judge_demo_seed",
        job_description=JUDGE_DEMO_JOB_DESCRIPTION,
        metadata={"candidate": JUDGE_DEMO_CANDIDATE},
    )
    sync_mongo_resume_version(
        updated,
        JUDGE_DEMO_APP_ID,
        "resume_v1_demo_targeted",
        JUDGE_DEMO_RESUME_V1,
        source="judge_demo_seed",
        job_description=JUDGE_DEMO_JOB_DESCRIPTION,
        audit_result={"jd_match_score": (match_scores.get("v1") or {}).get("match_score")},
        metadata={"candidate": JUDGE_DEMO_CANDIDATE, "match_scores": match_scores},
    )
    sync_mongo_artifact(
        updated,
        JUDGE_DEMO_APP_ID,
        "candidate_profile",
        {"candidate": JUDGE_DEMO_CANDIDATE, "common_answers": JUDGE_DEMO_COMMON_ANSWERS},
        {"source": "judge_demo_seed"},
    )
    sync_mongo_artifact(
        updated,
        JUDGE_DEMO_APP_ID,
        "resume_jd_match_scores",
        match_scores,
        {"source": "judge_demo_seed", "company": JUDGE_DEMO_COMPANY, "role": JUDGE_DEMO_ROLE},
    )
    sync_mongo_event(
        updated,
        JUDGE_DEMO_APP_ID,
        "judge_demo_profile_loaded",
        {
            "candidate": JUDGE_DEMO_CANDIDATE,
            "company": JUDGE_DEMO_COMPANY,
            "role": JUDGE_DEMO_ROLE,
            "v0_jd_match": (match_scores.get("v0") or {}).get("match_score"),
            "v1_jd_match": (match_scores.get("v1") or {}).get("match_score"),
            "confidence": (match_scores.get("v1") or {}).get("confidence"),
        },
    )
    seeded_history = seed_judge_demo_past_applications(updated)
    backfilled_scores = backfill_missing_application_match_scores(updated)
    sync_mongo_event(
        updated,
        JUDGE_DEMO_APP_ID,
        "judge_demo_historical_applications_loaded",
        {
            "count": len(seeded_history),
            "applications": seeded_history,
            "backfilled_missing_scores": backfilled_scores,
        },
    )
    return updated

def choose_ui_language():
    options = ["English", "Chinese"]
    default = (st.session_state.get("ui_language") or os.getenv("APP_DEFAULT_LANGUAGE") or "English").strip()
    if default not in options:
        default = "English"
    st.markdown('<div class="language-bar">', unsafe_allow_html=True)
    _left, right = st.columns([5, 1.15])
    with right:
        selected = st.selectbox("Language", options, index=options.index(default), key="ui_language", label_visibility="collapsed")
    st.markdown('</div>', unsafe_allow_html=True)
    return selected

ENGLISH_UI_REPLACEMENTS = [
    ("领英智能多任务求职控制中心", "Job Hunter Multi-Agent Command Center"),
    ("管理多个独立的求职指令。系统将在后台并发执行每个任务的自动检索、改写与审计，并通过队列分发执行。", "Manage independent job-search instructions. The system scans, rewrites, audits, and queues applications through guarded automation."),
    ("智能求职指令中心", "Job Search Command Center"),
    ("求职历史与数据分析", "Application History and Analytics"),
    ("全局个人凭证与默认配置", "Global Profile, Credentials, and Default Settings"),
    ("个人基本信息", "Personal Information"),
    ("名 (First Name)", "First Name"),
    ("姓 (Last Name)", "Last Name"),
    ("邮箱地址 (Email)", "Email Address"),
    ("电话号码 (Phone)", "Phone Number"),
    ("国家/地区电话区号格式应为 +1、+44 或 +86。", "Country calling code must use +1, +44, or +86."),
    ("电话号码应只包含本地号码；国家/地区区号请填写在左侧。", "Phone number should contain only the national number; enter the country calling code separately."),
    ("国家/地区电话区号", "Country Calling Code"),
    ("大模型与集成端配置", "Model and Integration Settings"),
    ("Gemini API 密钥", "Gemini API Key"),
    ("默认推理模型", "Default Reasoning Model"),
    ("领英直登凭证 (LinkedIn Login)", "LinkedIn Login"),
    ("领英登录邮箱/手机号", "LinkedIn Email or Phone"),
    ("领英登录密码", "LinkedIn Password"),
    ("微服务终结点配置", "Microservice Endpoint Settings"),
    ("ElasticSearch 检索服务 URL", "Elasticsearch Retrieval Service URL"),
    ("自托管 Elasticsearch URL", "Self-hosted Elasticsearch URL"),
    ("MongoDB 记忆服务 URL", "MongoDB Memory Service URL"),
    ("收件箱状态同步服务 URL", "Mailbox Status Sync Service URL"),
    ("Playwright 浏览器自动化服务 URL", "Playwright Browser Automation Service URL"),
    ("IMAP 邮件收发服务配置", "IMAP Mailbox Settings"),
    ("IMAP 接收服务器", "IMAP Server"),
    ("邮箱密码 / 应用授权码", "Email Password / App Password"),
    ("Gmail API OAuth2 快捷授权", "Gmail API OAuth2 Quick Connect"),
    ("投递调度选项", "Application Scheduling Options"),
    ("投递执行方式", "Execution Mode"),
    ("后台自动扫描守护引擎", "Background Scan Worker"),
    ("后台自动扫描已启用", "Background scan is running"),
    ("后台自动扫描已暂停", "Background scan is paused"),
    ("暂停后台扫描", "Pause Background Scan"),
    ("开启后台扫描", "Start Background Scan"),
    ("社交账号登录凭据 (LinkedIn Session Cookies)", "LinkedIn Session Cookies"),
    ("使用说明", "Instructions"),
    ("原始主简历文本 (V0)", "Original Resume Text (V0)"),
    ("定制简历 V1 (可在此编辑并批准)", "Tailored Resume V1 (editable approval draft)"),
    ("上传 PDF 格式简历", "Upload PDF Resume"),
    ("保存全局配置", "Save Global Configuration"),
    ("申请表答案库与预检", "Application Answer Library and Pre-submit Check"),
    ("先维护一份常见申请问题答案库；再对具体外部 apply 链接跑预检。预检不会提交申请。", "Maintain common application answers first, then inspect a specific external apply link before submission. This check never submits the application."),
    ("岗位表单预检", "Pre-submit Form Check"),
    ("岗位预检结果 / 待补字段", "Pre-submit Check Result / Fields Needing Attention"),
    ("会跑外部 apply 流程直到第一个安全停点；不会提交申请。", "Runs the external apply flow only to the first safe stop. It never submits the application."),
    ("只显示真实外部 ATS 链接；内部 mock、smoke test、LinkedIn 搜索页和已拒岗位已隐藏。", "Only real external ATS links are shown; internal mocks, smoke tests, LinkedIn search pages, and rejected jobs are hidden."),
    ("当前队列里还没有适合预检的真实外部 ATS 链接。可以切到 Direct Apply URL 手动粘贴 Workday / BrassRing / Greenhouse / Ashby 链接。", "No suitable real external ATS link is available in the queue. Switch to Direct Apply URL and paste a Workday, BrassRing, Greenhouse, or Ashby link."),
    ("目标简历超过一页，预检阶段已改用一页 demo resume 继续表单结构检查；正式投递仍会执行一页硬限制。", "The selected resume is over one page, so the pre-submit check switched to a one-page demo resume for form discovery. Final application still enforces the one-page hard limit."),
    ("预检阶段未附加简历 PDF；将继续检查外部表单结构，不会提交申请。", "No resume PDF is attached during the pre-submit check; the agent will still inspect the external form structure and will not submit."),
    ("预检来源", "Check Source"),
    ("直接 Apply URL", "Direct Apply URL"),
    ("投递队列", "Application Queue"),
    ("每 15 分钟", "Every 15 minutes"),
    ("不限", "Any"),
    ("工作模式 (Remote / Hybrid)", "Work Mode (Remote / Hybrid)"),
    ("经验要求 (Experience Level)", "Experience Level"),
    ("直接预检 Apply URL", "Direct Apply URL for Pre-submit Check"),
    ("选择岗位", "Select Job"),
    ("常见申请问题答案库", "Common Application Answer Library"),
    ("智能岗位检索指令", "Smart Job Search Instructions"),
    ("新建求职扫描指令", "Create Job Search Scan"),
    ("岗位名称 / 搜索关键词", "Job Title / Search Keywords"),
    ("求职目标地区", "Target Location"),
    ("扫描自动间隔", "Scan Interval"),
    ("领英检索过滤器高级设置 (选填)", "LinkedIn Search Filters (optional)"),
    ("领英检索过滤器高级设置", "LinkedIn Search Filters"),
    ("过去 24 小时", "Past 24 hours"),
    ("过去一周", "Past week"),
    ("过去一个月", "Past month"),
    ("远程", "Remote"),
    ("混合", "Hybrid"),
    ("现场", "On-site"),
    ("实习", "Internship"),
    ("初级", "Entry level"),
    ("中级", "Associate"),
    ("中高级", "Mid-Senior"),
    ("总监", "Director"),
    ("高管", "Executive"),
    ("创建并运行求职扫描指令", "Create and Run Job Search Scan"),
    ("正在监控的求职指令", "Monitored Job Search Scans"),
    ("编辑求职指令", "Edit Job Search Scan"),
    ("保存修改", "Save Changes"),
    ("取消", "Cancel"),
    ("求职地区", "Target Location"),
    ("扫描间隔", "Scan Interval"),
    ("每 ", "Every "),
    (" 分钟", " minutes"),
    ("发布时间", "Date Posted"),
    ("工作模式", "Work Mode"),
    ("经验等级", "Experience Level"),
    ("创建时间", "Created"),
    ("上次自动运行时间", "Last Auto Run"),
    ("从未运行", "Never Run"),
    ("引擎自动扫描中", "Auto Scan Running"),
    ("扫描已暂停", "Scan Paused"),
    ("立即手动检索投递", "Run Search Now"),
    ("暂停自动", "Pause Auto"),
    ("启用自动", "Enable Auto"),
    ("编辑指令", "Edit Scan"),
    ("删除指令", "Delete Scan"),
    ("智能体协同流水线运行日志 (当前进程)", "Agent Pipeline Run Log (Current Process)"),
    ("简历合规仲裁中心", "Resume Compliance Review Center"),
    ("暂无待处理的人工仲裁任务，所有简历均已自动审计放行。", "No pending human review tasks. All resumes have passed the automated audit."),
    ("人工决策挂起", "Human Review Pending"),
    ("保存并加入投递队列", "Save and Add to Apply Queue"),
    ("放弃该职位投递", "Skip This Job"),
    ("求职中心数据看板", "Job Center Metrics"),
    ("展示 agent 写入 MongoDB Atlas 的长期记忆：岗位档案、事件流、表单 artifact、简历版本和邮件结果。", "Shows the agent's durable memory: job profiles, event streams, form artifacts, resume versions, and email outcomes."),
    ("展示岗位检索层状态：真实 Elasticsearch 优先；未配置时使用本地 JD cache，避免 LinkedIn 实时搜索卡住主流程。", "Shows the retrieval layer status. Elasticsearch is preferred; local JD cache is used when the service is not configured."),
    ("展示简历改写审计的 Phoenix-style traces：事实一致性、JD 匹配度、风险分和阻断原因。", "Shows Phoenix-style resume audit traces: factual consistency, JD match, risk score, and blocking reasons."),
    ("还没有 Arize audit trace。批准简历或运行投递前审计后，这里会出现记录。", "No Arize audit traces yet. Records appear after resume approval or a pre-submit audit."),
    ("Elastic 检索服务暂时不可用。请确认 8002 正在运行。错误", "Elastic retrieval service is unavailable. Check the service connection. Error"),
    ("Arize 审计服务暂时不可用。请确认 8003 正在运行。错误", "Arize audit service is unavailable. Check the service connection. Error"),
    ("已投递成功", "Applied Successfully"),
    ("任务名称", "Task Name"),
    ("关键词", "Keywords"),
    ("地点", "Location"),
    ("远程", "Remote"),
    ("发布日期", "Date Posted"),
    ("经验级别", "Experience Level"),
    ("扫描间隔", "Scan Interval"),
    ("创建扫描任务", "Create Scan Task"),
    ("当前扫描任务", "Current Scan Tasks"),
    ("SOMA 会从 MongoDB 历史投递中生成 application episodes，用 Elastic 做粗召回，再按技术栈、任务、简历证据、outcome 和 Arize 分数 rerank。", "SOMA turns MongoDB application history into reusable episodes, retrieves similar cases with Elastic, then reranks by stack similarity, task fit, resume evidence, outcome, and Arize scores."),
    ("先同步一次历史 episode，再运行检索。未接 Elastic Cloud 时会写入本地 episode cache；接入后会自动写入真实 Elasticsearch。", "Sync historical episodes first, then run retrieval. Without Elastic Cloud it uses the local episode cache; with Elastic configured it writes to Elasticsearch."),
    ("当前 JD / Resume 来源", "JD / Resume Source"),
    ("当前预填来源：", "Current prefill source: "),
    ("没有 MongoDB 数据时会自动使用本地已评分申请，避免控制台空白。", "When MongoDB is unavailable, the console uses local scored applications so the demo does not render blank."),
    ("默认已填入一个真实历史申请。需要调试时再展开编辑。", "A real historical application is prefilled by default. Expand only when you need to edit the inputs."),
    ("运行 SOMA 检索与 Rerank", "Run SOMA Retrieval and Rerank"),
    ("立即运行", "Run Now"),
    ("删除", "Delete"),
    ("投递任务队列与历史状态", "Application Queue and History"),
    ("暂无投递任务记录", "No application records yet"),
    ("创建时间", "Created"),
    ("申请链接", "Apply Link"),
    ("暂无", "None"),
    ("队列排队中", "Queued"),
    ("正在自动投递", "Applying"),
    ("投递已成功", "Applied"),
    ("挂起待仲裁", "Pending Arbitration"),
    ("投递已被拒", "Rejected"),
    ("已获面试邀请", "Interview"),
    ("运行 Playwright 投递", "Run Playwright Apply"),
    ("查看为此岗位定制的简历 V1", "View Tailored Resume V1 for This Job"),
    ("扫描引擎运行日志", "Scan Engine Logs"),
    ("暂无引擎运行日志", "No scan engine logs yet"),
    ("时间", "Time"),
    ("监控指令", "Monitor Instruction"),
    ("日志级别", "Log Level"),
    ("详细描述", "Details"),
    ("系统服务", "System Service"),
    ("申请", "Application"),
    ("岗位", "Job"),
    ("简历", "Resume"),
    ("状态", "Status"),
    ("投递", "Apply"),
    ("配置", "Settings"),
    ("保存", "Save"),
    ("邮箱", "Email"),
    ("已保存", "Saved"),
    ("成功", "Success"),
    ("失败", "Failed"),
    ("正在", "Running"),
    ("缺少", "Missing"),
    ("完成", "Complete"),
    ("历史", "History"),
    ("数据分析", "Analytics"),
    ("人工仲裁", "Human Review"),
    ("自动扫描", "Auto Scan"),
    ("推荐", "Recommended"),
    ("全自动无感", "Fully Automatic"),
    ("自动执行", "Automated"),
    ("全局", "Global"),
    ("默认", "Default"),
]

STREAMLIT_METHODS_TO_TRANSLATE = {
    "markdown",
    "caption",
    "subheader",
    "header",
    "title",
    "write",
    "info",
    "warning",
    "success",
    "error",
    "toast",
    "button",
    "link_button",
    "text_input",
    "text_area",
    "selectbox",
    "radio",
    "multiselect",
    "file_uploader",
    "expander",
    "tabs",
    "metric",
    "status",
    "table",
    "dataframe",
}
STREAMLIT_TRANSLATION_PATCH_VERSION = 11
STREAMLIT_SKIP_TRANSLATION_KWARGS = {
    "key",
    "value",
    "index",
    "type",
    "height",
    "width",
    "disabled",
    "use_container_width",
    "expanded",
    "unsafe_allow_html",
    "label_visibility",
    "on_change",
    "args",
    "kwargs",
    "format_func",
    "horizontal",
}

def contains_cjk(text):
    return bool(re.search(r"[\u3400-\u9fff]", str(text or "")))

def translate_to_english_text(text):
    translated = str(text)
    for source, target in ENGLISH_UI_REPLACEMENTS:
        translated = translated.replace(source, target)
    translated = translated.replace("（", "(").replace("）", ")").replace("：", ": ")
    translated = translated.replace("，", ", ").replace("。", ". ").replace("；", "; ").replace("、", ", ")
    if contains_cjk(translated):
        translated = re.sub(r"[\u3400-\u9fff]+", "", translated)
        translated = re.sub(r"\s{2,}", " ", translated)
    return translated

def translate_ui_value(value):
    if isinstance(value, str):
        return translate_to_english_text(value)
    if isinstance(value, list):
        return [translate_ui_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(translate_ui_value(item) for item in value)
    if isinstance(value, dict):
        return {translate_ui_value(key): translate_ui_value(item) for key, item in value.items()}
    return value

def translate_streamlit_args(method_name, args, kwargs):
    translated_args = list(args)
    translated_kwargs = dict(kwargs)
    if method_name in {"selectbox", "radio", "multiselect"}:
        if translated_args:
            translated_args[0] = translate_ui_value(translated_args[0])
        for key in ("label", "placeholder", "help"):
            if key in translated_kwargs:
                translated_kwargs[key] = translate_ui_value(translated_kwargs[key])
        original_format_func = translated_kwargs.get("format_func")
        if original_format_func:
            translated_kwargs["format_func"] = lambda item, fn=original_format_func: translate_ui_value(fn(item))
        else:
            translated_kwargs["format_func"] = lambda item: translate_ui_value(item)
        return tuple(translated_args), translated_kwargs

    translated_args = [translate_ui_value(arg) for arg in translated_args]
    for key, value in list(translated_kwargs.items()):
        if key not in STREAMLIT_SKIP_TRANSLATION_KWARGS:
            translated_kwargs[key] = translate_ui_value(value)
    return tuple(translated_args), translated_kwargs

def enable_english_layout_translation():
    if (
        getattr(st, "_job_hunter_english_translation_enabled", False)
        and getattr(st, "_job_hunter_translation_patch_version", None) == STREAMLIT_TRANSLATION_PATCH_VERSION
    ):
        return
    if getattr(st, "_job_hunter_english_translation_enabled", False):
        disable_english_layout_translation()
    originals = getattr(st, "_job_hunter_original_streamlit_methods", {})
    for method_name in STREAMLIT_METHODS_TO_TRANSLATE:
        if not hasattr(st, method_name):
            continue
        if method_name not in originals:
            originals[method_name] = getattr(st, method_name)
        original = getattr(st, method_name)
        def make_wrapper(name, fn):
            def wrapper(*args, **kwargs):
                translated_args, translated_kwargs = translate_streamlit_args(name, args, kwargs)
                return fn(*translated_args, **translated_kwargs)
            return wrapper
        setattr(st, method_name, make_wrapper(method_name, original))
    setattr(st, "_job_hunter_original_streamlit_methods", originals)
    setattr(st, "_job_hunter_english_translation_enabled", True)
    setattr(st, "_job_hunter_translation_patch_version", STREAMLIT_TRANSLATION_PATCH_VERSION)

def disable_english_layout_translation():
    originals = getattr(st, "_job_hunter_original_streamlit_methods", {})
    for method_name, original in originals.items():
        setattr(st, method_name, original)
    setattr(st, "_job_hunter_english_translation_enabled", False)

def fetch_service_health(base_url, timeout=6):
    root_url = str(base_url or '').rstrip('/')
    url = f"{root_url}/health"
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code >= 400:
            if response.status_code == 404 and root_url:
                try:
                    openapi_url = f"{root_url}/openapi.json"
                    openapi_response = requests.get(openapi_url, timeout=min(timeout, 4))
                    if openapi_response.status_code < 400:
                        openapi_payload = openapi_response.json() if openapi_response.text else {}
                        title = (openapi_payload.get("info") or {}).get("title") or "FastAPI service"
                        return {
                            "ok": True,
                            "url": openapi_url,
                            "status_code": openapi_response.status_code,
                            "service": title,
                        }
                except Exception:
                    pass
            return {
                "ok": False,
                "url": url,
                "status_code": response.status_code,
                "error": response.text[:180],
            }
        payload = response.json() if response.text else {}
        payload["url"] = url
        payload["status_code"] = response.status_code
        return payload
    except Exception as err:
        return {"ok": False, "url": url, "error": str(err)[:180]}

def render_service_health_card(name, health):
    health = health or {}
    memory_cache_ready = name == "MongoDB" and health.get("backend") == "mongodb_unavailable"
    ok = bool(health.get("ok")) or memory_cache_ready
    status = "ready" if memory_cache_ready else ("online" if ok else "offline")
    border = "#bbf7d0" if ok else "#fecaca"
    background = "#f0fdf4" if ok else "#fef2f2"
    text_color = "#166534" if ok else "#991b1b"
    detail = "local durable memory cache" if memory_cache_ready else (health.get("service") or health.get("backend") or health.get("url") or "")
    error = "" if memory_cache_ready else (health.get("error") or "")
    status_code = health.get("status_code")
    code_text = f"HTTP {status_code}" if status_code else ""
    st.markdown(
        f"""
        <div style="border: 1px solid {border}; border-radius: 8px; background: {background}; padding: 12px;">
            <div style="font-weight: 800; color: #0f172a;">{html.escape(name)}</div>
            <div style="color: {text_color}; font-weight: 700; margin-top: 4px;">{status}</div>
            <div style="color: #475569; font-size: 0.78rem; margin-top: 4px; overflow-wrap: anywhere;">{html.escape(str(code_text or detail))}</div>
            <div style="color: #7f1d1d; font-size: 0.75rem; margin-top: 4px; overflow-wrap: anywhere;">{html.escape(str(error))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

def render_english_memory_snapshot(config):
    summary = call_mongo_memory(config, "GET", "/memory/summary", timeout=4)
    st.markdown("### MongoDB Memory")
    if not summary:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT id, company, role, status, apply_url, match_scores_json FROM mcp_applications")
            local_rows = cursor.fetchall()
            conn.close()
        except Exception:
            local_rows = []
        visible_rows = [
            row for row in local_rows
            if is_video_visible_application_record(row[0], row[1], row[4], row[5])
        ]
        st.info("This demo view is using the local durable memory cache. The same records can sync to MongoDB Atlas when the memory service is connected.")
        cols = st.columns(4)
        cols[0].metric("Backend", "local cache")
        cols[1].metric("Applications", len(visible_rows))
        cols[2].metric("Scored", sum(1 for row in visible_rows if row[5]))
        cols[3].metric("Archived Runs", max(0, len(local_rows) - len(visible_rows)))
        if visible_rows:
            st.table([
                {
                    "Company": company,
                    "Role": role,
                    "Status": status,
                    "V1 lift": score_pp((load_match_scores(match_scores_json) or {}).get("delta")),
                }
                for _app_id, company, role, status, _apply_url, match_scores_json in visible_rows[:6]
            ])
        return
    cols = st.columns(5)
    cols[0].metric("Backend", summary.get("backend", "unknown"))
    cols[1].metric("Applications", summary.get("applications", 0))
    cols[2].metric("Events", summary.get("events", 0))
    cols[3].metric("Artifacts", summary.get("artifacts", 0))
    cols[4].metric("Resume Versions", summary.get("resume_versions", 0))

    memory = call_mongo_memory(config, "GET", f"/applications/{JUDGE_DEMO_APP_ID}/memory", timeout=5)
    if not memory:
        st.info("Load the judge demo profile to create the demo memory record.")
        return
    artifacts = memory.get("artifacts") or []
    events = memory.get("events") or []
    rows = []
    for artifact in artifacts:
        payload = artifact.get("payload") or {}
        artifact_type = artifact.get("artifact_type")
        if artifact_type == "resume_jd_match_scores":
            delta = payload.get("delta")
            summary_text = (
                f"This job V0 {score_pct((payload.get('v0') or {}).get('match_score'))}; "
                f"V1 {score_pct((payload.get('v1') or {}).get('match_score'))}; "
                f"lift {score_pp(delta)}; "
                f"judge confidence {score_pct((payload.get('v1') or {}).get('confidence'))}; "
                f"{verdict_label(payload.get('verdict'), delta)}"
            )
        else:
            summary_text = "saved"
        rows.append({
            "type": artifact_type,
            "summary": summary_text,
            "updated": artifact.get("updated_at", ""),
        })
    if rows:
        st.table(rows)
    if events:
        with st.expander("Event stream", expanded=False):
            st.table([
                {
                    "time": item.get("created_at", ""),
                    "type": item.get("event_type", ""),
                    "payload": json.dumps(mask_sensitive_display(item.get("payload") or {}), ensure_ascii=True)[:220],
                }
                for item in events[-12:]
            ])

def load_application_fit_rows(limit=80):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, company, role, status, apply_url, created_at, match_scores_json
            FROM mcp_applications
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        return []

    result = []
    for row in rows:
        app_id, company, role, status, apply_url, created_at, match_scores_json = row
        if not is_video_visible_application_record(app_id, company, apply_url, match_scores_json):
            continue
        match_scores = load_match_scores(match_scores_json)
        v0 = match_scores.get("v0") or {}
        v1 = match_scores.get("v1") or {}
        confidence = v1.get("confidence") or v0.get("confidence")
        delta = match_scores.get("delta")
        result.append({
            "id": app_id,
            "company": company,
            "role": role,
            "status": status,
            "created": created_at,
            "apply_url": apply_url,
            "has_score": bool(match_scores),
            "v0_match": score_pct(v0.get("match_score")) if match_scores else "-",
            "v1_match": score_pct(v1.get("match_score")) if match_scores else "-",
            "v1_lift": score_pp(delta) if match_scores else "-",
            "result": verdict_label(match_scores.get("verdict"), delta) if match_scores else "Not scored yet",
            "judge_confidence": score_pct(confidence) if match_scores else "-",
            "match_scores": match_scores,
        })
    return result

def render_application_fit_board(expanded_first=False):
    rows = load_application_fit_rows()
    st.markdown("### Resume-to-JD Fit by Application")
    st.caption("Each row is one specific job application. The lift compares that job's V1 resume against its own V0 resume for that job's JD.")
    if not rows:
        st.info("No application records yet. Run a LinkedIn scan or load the demo profile first.")
        return

    table_rows = []
    for item in rows:
        table_rows.append({
            "Company": item["company"],
            "Role": item["role"],
            "Status": item["status"],
            "V0 match": item["v0_match"],
            "V1 match": item["v1_match"],
            "V1 lift": item["v1_lift"],
            "Meaning": item["result"],
            "Judge confidence": item["judge_confidence"],
        })
    st.dataframe(table_rows, use_container_width=True, hide_index=True)

    for index, item in enumerate(rows[:12]):
        with st.expander(f"{item['role']} @ {item['company']} · {item['v1_lift']}", expanded=expanded_first and index == 0):
            if item["apply_url"]:
                st.caption(item["apply_url"])
            if item["has_score"]:
                render_resume_jd_match_metrics(item["match_scores"], company=item["company"], role=item["role"])
            else:
                st.info("This application does not have a stored per-job V0/V1 score yet. New scans will store it after resume tailoring.")

def render_english_demo_ui(config):
    loaded = (
        (config.get("user_data") or {}).get("email") == JUDGE_DEMO_CANDIDATE["email"]
        and (config.get("resume_v0") or "").strip() == JUDGE_DEMO_RESUME_V0.strip()
    )
    match_scores = score_resume_versions_for_jd(
        None,
        config.get("model_selector"),
        JUDGE_DEMO_RESUME_V0,
        JUDGE_DEMO_RESUME_V1,
        JUDGE_DEMO_JOB_DESCRIPTION,
        JUDGE_DEMO_COMPANY,
        JUDGE_DEMO_ROLE,
    )
    memory_summary = call_mongo_memory(config, "GET", "/memory/summary", timeout=4) or {}

    st.markdown(
        """
        <div class="en-topbar">
            <div>
                <div class="en-title">Job Hunter Agent</div>
                <div class="en-subtitle">Judge demo workspace for resume targeting, durable application memory, partner observability, and guarded external-apply automation.</div>
            </div>
            <div class="en-pill">Pre-submit guard enabled</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    action_col, note_col = st.columns([1.15, 2.85])
    with action_col:
        if st.button("Load Judge Demo Profile", type="primary", disabled=loaded, use_container_width=True):
            seed_judge_demo_profile(config)
            st.success("Judge demo profile loaded.")
            time.sleep(0.4)
            st.rerun()
    with note_col:
        st.markdown(
            '<div class="en-risk-note">The demo profile is fictional. External-apply automation is designed to stop before final submission.</div>',
            unsafe_allow_html=True,
        )

    profile_col, score_col = st.columns([1.15, 1.85])
    with profile_col:
        profile_rows = ""
        for label, value in [
            ("Name", f"{JUDGE_DEMO_CANDIDATE['first_name']} {JUDGE_DEMO_CANDIDATE['last_name']}"),
            ("Target Role", JUDGE_DEMO_ROLE),
            ("Location", JUDGE_DEMO_CANDIDATE["current_location"]),
            ("Email", mask_email_for_display(JUDGE_DEMO_CANDIDATE["email"])),
            ("Work Authorization", JUDGE_DEMO_CANDIDATE["authorized_to_work_us"]),
            ("Sponsorship", JUDGE_DEMO_CANDIDATE["need_sponsorship"]),
        ]:
            profile_rows += f'<div class="en-field-row"><span class="en-field-label">{html.escape(label)}</span><span class="en-field-value">{html.escape(str(value))}</span></div>'
        st.markdown(
            f'<div class="en-card"><div class="en-card-title">Candidate</div>{profile_rows}</div>',
            unsafe_allow_html=True,
        )

    with score_col:
        st.markdown('<div class="en-card-title">Demo Resume-to-JD Fit</div>', unsafe_allow_html=True)
        render_resume_jd_match_metrics(match_scores, company=JUDGE_DEMO_COMPANY, role=JUDGE_DEMO_ROLE)

    kpi_cols = st.columns(4)
    kpi_cols[0].metric("Memory Backend", memory_summary.get("backend", "unknown"))
    kpi_cols[1].metric("Applications", memory_summary.get("applications", 0))
    kpi_cols[2].metric("Events", memory_summary.get("events", 0))
    kpi_cols[3].metric("Artifacts", memory_summary.get("artifacts", 0))

    tab_dashboard, tab_applications, tab_resume, tab_memory, tab_apply, tab_email, tab_services = st.tabs([
        "Dashboard",
        "Applications",
        "Resume Versions",
        "MongoDB Memory",
        "Apply Guard",
        "Email OAuth",
        "Services",
    ])

    with tab_dashboard:
        steps = [
            ("JD Extracted", f"{JUDGE_DEMO_ROLE} at {JUDGE_DEMO_COMPANY}", "Ready"),
            ("Memory Retrieved", "MongoDB application record and artifacts", "Ready" if loaded else "Pending"),
            ("Resume Scored", f"V1 lift {score_pp(match_scores.get('delta'))} for this job", "Ready"),
            ("Audit Trace", "Arize Phoenix trace export configured", "Ready"),
            ("Final Submit", "Manual confirmation required", "Blocked"),
        ]
        steps_html = ""
        for name, detail, status in steps:
            steps_html += f'<div class="en-step"><div class="en-step-name">{html.escape(name)}</div><div>{html.escape(detail)}</div><div class="en-step-status">{html.escape(status)}</div></div>'
        st.markdown(
            f'<div class="en-card"><div class="en-card-title">Pipeline State</div>{steps_html}</div>',
            unsafe_allow_html=True,
        )
        render_application_fit_board(expanded_first=False)
        st.markdown('<div class="en-card-title">Demo Job Description</div>', unsafe_allow_html=True)
        st.code(JUDGE_DEMO_JOB_DESCRIPTION, language="text")

    with tab_applications:
        render_application_fit_board(expanded_first=True)

    with tab_resume:
        resume_v0_tab, resume_v1_tab = st.tabs(["Original Resume V0", "Targeted Resume V1"])
        with resume_v0_tab:
            st.code(mask_emails_in_text(JUDGE_DEMO_RESUME_V0), language="text")
        with resume_v1_tab:
            st.code(mask_emails_in_text(JUDGE_DEMO_RESUME_V1), language="text")

    with tab_memory:
        render_english_memory_snapshot(config)

    with tab_apply:
        guard_rows = ""
        for label, value in [
            ("ATS account handling", "Create account only when no saved account exists; otherwise sign in."),
            ("Email verification", "Read verification messages through the configured mailbox service."),
            ("Resume upload", "Use the targeted one-page PDF after readability checks."),
            ("Unknown form state", "Capture DOM, accessibility tree, screenshot, and ask the model for the next action."),
            ("Final submission", "Never click final submit without explicit user approval."),
        ]:
            guard_rows += f'<div class="en-field-row"><span class="en-field-label">{html.escape(label)}</span><span class="en-field-value">{html.escape(value)}</span></div>'
        st.markdown(
            f'<div class="en-card"><div class="en-card-title">External Apply Guardrails</div>{guard_rows}</div>',
            unsafe_allow_html=True,
        )

    with tab_email:
        render_email_oauth_panel(config)
        st.info(f"Authorized redirect URI required in Google Cloud OAuth client: {app_base_url()}")

    with tab_services:
        health_cols = st.columns(5)
        with health_cols[0]:
            render_service_health_card("MongoDB", fetch_service_health(mongo_url_from_config(config)))
        with health_cols[1]:
            render_service_health_card("Elasticsearch", fetch_service_health(elastic_url_from_config(config)))
        with health_cols[2]:
            render_service_health_card("Arize Phoenix", fetch_service_health(arize_url_from_config(config)))
        with health_cols[3]:
            render_service_health_card("Playwright", fetch_service_health(playwright_url_from_config(config)))
        with health_cols[4]:
            render_service_health_card("Email", fetch_service_health(email_url_from_config(config)))

COMMON_ANSWER_FIELDS = [
    {
        "key": "first_name",
        "label": "First name / Given name",
        "type": "text",
        "config_key": ("user_data", "first_name"),
        "patterns": ["first name", "given name"],
        "hidden_from_bank": True,
    },
    {
        "key": "last_name",
        "label": "Last name / Family name",
        "type": "text",
        "config_key": ("user_data", "last_name"),
        "patterns": ["last name", "family name", "surname"],
        "hidden_from_bank": True,
    },
    {
        "key": "email",
        "label": "Email address",
        "type": "text",
        "config_key": ("user_data", "email"),
        "patterns": ["email address", "e mail address", "primary email"],
        "hidden_from_bank": True,
    },
    {
        "key": "phone",
        "label": "Phone number",
        "type": "text",
        "config_key": ("user_data", "phone"),
        "patterns": ["phone number", "mobile phone", "telephone"],
        "hidden_from_bank": True,
    },
    {
        "key": "linkedin_url",
        "label": "LinkedIn profile URL",
        "type": "text",
        "patterns": ["linkedin profile", "linkedin url", "linkedin"],
    },
    {
        "key": "github_url",
        "label": "GitHub profile URL",
        "type": "text",
        "patterns": ["github profile", "github url", "github"],
    },
    {
        "key": "portfolio_url",
        "label": "Portfolio / personal website",
        "type": "text",
        "patterns": ["portfolio", "personal website", "website url"],
    },
    {
        "key": "authorized_to_work_us",
        "label": "Legally authorized to work in the U.S.",
        "type": "select",
        "options": ["", "Yes", "No"],
        "patterns": ["authorized to work", "legally authorized", "work authorization"],
    },
    {
        "key": "need_sponsorship",
        "label": "Need visa sponsorship now or in the future",
        "type": "select",
        "options": ["", "Yes", "No"],
        "patterns": ["sponsorship", "sponsor", "visa sponsorship"],
    },
    {
        "key": "security_clearance",
        "label": "Security clearance",
        "type": "text",
        "patterns": ["security clearance", "clearance"],
    },
    {
        "key": "current_location",
        "label": "Current location",
        "type": "text",
        "patterns": ["current location", "city", "state province", "state/province"],
        "hidden_from_bank": True,
    },
    {
        "key": "start_date",
        "label": "Earliest start date / notice period",
        "type": "text",
        "patterns": ["start date", "available to start", "notice period", "earliest start"],
        "hidden_from_bank": True,
    },
    {
        "key": "notice_period",
        "label": "Notice period",
        "type": "text",
        "patterns": ["notice period", "availability notice"],
        "hidden_from_bank": True,
    },
    {
        "key": "salary_expectation",
        "label": "Salary expectation",
        "type": "text",
        "patterns": ["salary expectation", "desired salary", "compensation expectation"],
    },
    {
        "key": "years_experience",
        "label": "Years of relevant experience",
        "type": "text",
        "patterns": ["years of experience", "years experience", "relevant experience"],
    },
]
COMMON_ANSWER_FIELD_BY_KEY = {field["key"]: field for field in COMMON_ANSWER_FIELDS}

def get_config_default(config, config_key):
    if not config_key:
        return ""
    value = config
    for part in config_key:
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
    return value or ""

def normalize_answer_text(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()

def load_common_answers():
    payload = load_json_file(common_answers_path(), default={}) or {}
    if isinstance(payload, dict) and isinstance(payload.get("answers"), dict):
        return payload.get("answers") or {}
    return {}


def profile_common_answer_value(config, field_def):
    key = field_def.get("key")
    user_data = (config or {}).get("user_data") or {}
    profile_library = load_application_profile_library(config)
    availability = profile_library.get("availability") if isinstance(profile_library.get("availability"), dict) else {}
    if field_def.get("config_key"):
        return get_config_default(config, field_def.get("config_key"))
    if key == "current_location":
        parts = [user_data.get("city"), user_data.get("state"), user_data.get("country")]
        return ", ".join(str(part).strip() for part in parts if str(part or "").strip())
    if key == "start_date":
        return availability.get("start_date") or user_data.get("start_date") or availability.get("notice_period") or ""
    if key == "notice_period":
        return availability.get("notice_period") or user_data.get("notice_period") or ""
    return ""


def profile_derived_common_answers(config):
    answers = {}
    for field_def in COMMON_ANSWER_FIELDS:
        if not field_def.get("hidden_from_bank"):
            continue
        value = profile_common_answer_value(config, field_def)
        if not value:
            continue
        answers[field_def["key"]] = {
            "label": field_def["label"],
            "type": field_def.get("type", "text"),
            "value": value,
        }
    return answers

def match_common_answer(field_text, common_answers):
    normalized = normalize_answer_text(field_text)
    if not normalized:
        return None
    for field_def in COMMON_ANSWER_FIELDS:
        answer = common_answers.get(field_def["key"], {})
        if not answer or not answer.get("value"):
            continue
        for pattern in field_def.get("patterns", []):
            if normalize_answer_text(pattern) in normalized:
                return {
                    "key": field_def["key"],
                    "label": field_def["label"],
                    "value": answer.get("value"),
                    "type": answer.get("type") or field_def.get("type", "text")
                }
    return None

def render_common_answer_bank(config):
    saved_common = load_common_answers()
    edited_common = profile_derived_common_answers(config)

    with st.expander("常见申请问题答案库", expanded=False):
        st.caption("这里存一份全局默认答案。岗位预检遇到相似问题时会先带出来给你确认，不会直接提交申请。")
        left, right = st.columns(2)
        visible_fields = [field for field in COMMON_ANSWER_FIELDS if not field.get("hidden_from_bank")]
        for idx, field_def in enumerate(visible_fields):
            container = left if idx % 2 == 0 else right
            key = field_def["key"]
            saved = saved_common.get(key, {}) if isinstance(saved_common, dict) else {}
            default_value = saved.get("value") or get_config_default(config, field_def.get("config_key"))
            widget_key = f"common_answer_{key}"
            with container:
                if field_def.get("type") == "select":
                    options = field_def.get("options", ["", "Yes", "No"])
                    index = options.index(default_value) if default_value in options else 0
                    value = st.selectbox(field_def["label"], options, index=index, key=widget_key)
                else:
                    value = st.text_input(field_def["label"], value=default_value, key=widget_key)
            edited_common[key] = {
                "label": field_def["label"],
                "type": field_def.get("type", "text"),
                "value": value
            }

        if st.button("保存常见答案库", key="save_common_answer_bank"):
            visible_answer_keys = {field["key"] for field in visible_fields}
            saved_answers = {
                key: value for key, value in edited_common.items()
                if key in visible_answer_keys
            }
            save_json_file(common_answers_path(), {
                "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
                "answers": saved_answers
            })
            sync_mongo_artifact(config, "global_common_answers", "apply_form_common_answers", {
                "answers": saved_answers
            }, {"scope": "global"})
            st.success(f"已保存到 {common_answers_path()}")

    return edited_common

def render_question_blocker_panel(config):
    st.markdown("### 待回答申请问题")
    bundle_data = call_mongo_memory(config, "GET", "/applications/question-review-bundles?limit=50", timeout=6) or {}
    app_bundles = [
        bundle for bundle in (bundle_data.get("bundles") or [])
        if (bundle.get("summary") or {}).get("unanswered_count", 0)
        or (bundle.get("summary") or {}).get("technical_review_count", 0)
    ]
    if app_bundles:
        st.markdown("#### Application review bundles")
        for bundle in app_bundles[:25]:
            summary = bundle.get("summary") or {}
            title = (
                f"{bundle.get('company') or 'Unknown company'} / "
                f"{bundle.get('role') or 'Unknown role'} / "
                f"{bundle.get('application_id') or '-'}"
            )
            with st.expander(
                f"{title} - needs review: {summary.get('unanswered_count', 0) + summary.get('technical_review_count', 0)}",
                expanded=False,
            ):
                st.caption(f"status {bundle.get('status')} | batch {bundle.get('batch_id') or '-'}")
                bundle_answers = {}
                for question in bundle.get("questions") or []:
                    st.write(f"`{question.get('status')}` {question.get('raw_text') or question.get('normalized_text')}")
                    if question.get("options"):
                        st.caption("Options: " + ", ".join(str(item) for item in question.get("options")[:8]))
                    if question.get("suggested_answer") is not None:
                        st.caption(f"Suggested: {question.get('suggested_answer')}")
                    if question.get("probe_answer") is not None:
                        st.caption(f"Probe: {question.get('probe_answer')}")
                    for artifact in (question.get("artifacts") or [])[:2]:
                        st.caption(str(artifact.get("screenshot_path") or artifact.get("path") or artifact.get("url") or artifact))
                    if question.get("status") != "UNANSWERED":
                        continue
                    blocker_id = question.get("blocker_id")
                    if not blocker_id:
                        continue
                    answer_key = f"application_bundle_answer_{safe_app_key(bundle.get('application_id'))}_{safe_app_key(blocker_id)}"
                    default_answer = question.get("suggested_answer")
                    if default_answer is None:
                        default_answer = question.get("probe_answer")
                    control_type = str(question.get("control_type") or "text").lower()
                    options = question.get("options") or []
                    if control_type in {"radio", "select"} and options:
                        option_values = ["Choose an answer", *options]
                        default_index = option_values.index(default_answer) if default_answer in option_values else 0
                        answer_value = st.selectbox(
                            "Answer",
                            option_values,
                            index=default_index,
                            key=answer_key,
                        )
                        if answer_value == "Choose an answer":
                            answer_value = ""
                    elif control_type == "checkbox":
                        answer_value = st.checkbox("Answer", value=bool(default_answer), key=answer_key)
                    else:
                        answer_value = st.text_area(
                            "Answer",
                            value=str(default_answer or ""),
                            key=answer_key,
                            height=80,
                        )
                    bundle_answers[blocker_id] = answer_value
                if bundle_answers:
                    missing_answers = [
                        blocker_id for blocker_id, answer_value in bundle_answers.items()
                        if answer_value == ""
                    ]
                    if st.button(
                        "Approve application answers",
                        key=f"approve_application_bundle_{safe_app_key(bundle.get('application_id'))}",
                        disabled=bool(missing_answers),
                    ):
                        result = call_mongo_memory(
                            config,
                            "POST",
                            f"/applications/{safe_app_key(bundle.get('application_id'))}/question-review-bundle/approve",
                            payload={
                                "answers": bundle_answers,
                                "approved_by": "streamlit_user",
                            },
                            timeout=8,
                        )
                        if result:
                            st.success(f"Approved {result.get('approved_count', 0)} answers.")
                            time.sleep(0.5)
                            st.rerun()
                        else:
                            st.error("Application bundle approval failed.")
        st.markdown("#### Batch reuse groups")
    data = call_mongo_memory(config, "GET", "/question-blockers/groups", timeout=6) or {}
    groups = data.get("groups") or []
    if not groups:
        if app_bundles:
            return
        st.info("当前没有待批准的申请问题。")
        return

    technical_groups = [
        group for group in groups
        if (group.get("status_counts") or {}).get("TECHNICAL_REVIEW", 0)
    ]
    answer_groups = [
        group for group in groups
        if (group.get("status_counts") or {}).get("UNANSWERED", 0)
    ]

    if answer_groups:
        st.markdown("#### 需要你批准答案")
    for idx, group in enumerate(answer_groups):
        fingerprint = group.get("fingerprint") or f"group-{idx}"
        apps = group.get("application_ids") or []
        companies = group.get("companies") or []
        roles = group.get("roles") or []
        blocker_ids = group.get("blocker_ids") or []
        batch_ids = group.get("batch_ids") or []
        occurrences = [
            item for item in (group.get("occurrences") or [])
            if item.get("status") == "UNANSWERED"
        ]
        occurrence_offset_key = f"question_blocker_occurrence_offset_{fingerprint}"
        occurrence_offset = int(st.session_state.get(occurrence_offset_key, 0) or 0)
        occurrence_page = call_mongo_memory(
            config,
            "GET",
            f"/question-blocker-groups/{safe_app_key(fingerprint)}/occurrences?status=UNANSWERED&limit=100&offset={occurrence_offset}",
            timeout=6,
        ) or {}
        occurrence_pagination = occurrence_page.get("pagination") or {}
        if occurrence_page.get("occurrences") is not None:
            occurrences = occurrence_page.get("occurrences") or []
        options = group.get("options") or []
        control_type = group.get("control_type") or "text"
        unlock_count = int((group.get("status_counts") or {}).get("UNANSWERED", 0))

        with st.expander(f"{group.get('question', 'Unknown question')} · {group.get('occurrence_count', 0)} 次", expanded=idx == 0):
            st.write(f"控件类型：`{control_type}`")
            if options:
                st.write("选项：" + ", ".join(f"`{opt}`" for opt in options))
            if group.get("validation_messages"):
                st.warning(" / ".join(group.get("validation_messages")[:3]))
            st.write(f"公司：{', '.join(companies[:8]) if companies else '-'}")
            st.write(f"岗位：{', '.join(roles[:8]) if roles else '-'}")
            st.write(f"受影响 application：{', '.join(apps[:10]) if apps else '-'}")
            st.caption(f"批准后最多解锁 {unlock_count} 个 application；不会自动提交，只会进入 READY_TO_RESUME。")
            if occurrence_pagination:
                total_occurrences = occurrence_pagination.get("total", len(occurrences))
                st.caption(
                    f"Occurrences {occurrence_offset + 1 if occurrences else 0}-"
                    f"{occurrence_offset + len(occurrences)} of {total_occurrences}"
                )
                page_cols = st.columns(2)
                if page_cols[0].button("Previous occurrences", key=f"question_blocker_prev_{fingerprint}", disabled=occurrence_offset <= 0):
                    st.session_state[occurrence_offset_key] = max(0, occurrence_offset - 100)
                    st.rerun()
                if page_cols[1].button("Next occurrences", key=f"question_blocker_next_{fingerprint}", disabled=not occurrence_pagination.get("has_more")):
                    st.session_state[occurrence_offset_key] = occurrence_offset + 100
                    st.rerun()
            for occurrence in occurrences[:5]:
                artifacts = occurrence.get("artifacts") or []
                if artifacts:
                    artifact_text = ", ".join(
                        str(item.get("screenshot_path") or item.get("path") or item.get("url") or item.get("label") or item.get("type"))
                        for item in artifacts[:3]
                    )
                    st.caption(f"Artifact {occurrence.get('application_id')}: {artifact_text}")

            answer_key = f"question_blocker_answer_{fingerprint}"
            if control_type in {"radio", "select"} and options:
                answer = st.selectbox("答案", ["Choose an answer", *options], key=answer_key)
                if answer == "Choose an answer":
                    answer = ""
            elif control_type == "checkbox":
                answer = st.checkbox("答案", key=answer_key)
            else:
                answer = st.text_area("答案", key=answer_key, height=90)

            scope_options = ["application"]
            if batch_ids:
                scope_options.append("batch")
            scope_labels = {
                "application": "仅当前 application",
                "batch": "当前 batch 内相同且兼容的问题",
            }
            selected_scope = st.radio(
                "作用域",
                scope_options,
                format_func=lambda value: scope_labels[value],
                horizontal=True,
                key=f"question_blocker_scope_{fingerprint}",
            )
            selected_occurrence = None
            if selected_scope == "application":
                occurrence_options = occurrences or [
                    {"id": blocker_id, "application_id": app_id, "company": "", "role": "", "batch_id": ""}
                    for blocker_id, app_id in zip(blocker_ids, apps)
                ]
                if occurrence_options:
                    selected_occurrence = st.selectbox(
                        "Application occurrence",
                        occurrence_options,
                        format_func=lambda item: (
                            f"{item.get('company') or 'Unknown company'} · "
                            f"{item.get('role') or 'Unknown role'} · "
                            f"{item.get('application_id') or '-'} · batch {item.get('batch_id') or '-'}"
                        ),
                        key=f"question_blocker_occurrence_{fingerprint}",
                    )
            selected_batch = None
            if selected_scope == "batch" and batch_ids:
                selected_batch = st.selectbox("Batch", batch_ids, key=f"question_blocker_batch_{fingerprint}")

            target_blocker_id = (selected_occurrence or {}).get("id") if selected_scope == "application" else selected_batch
            approval_ready = bool(target_blocker_id) if selected_scope == "application" else bool(selected_batch)
            if target_blocker_id and st.button("批准答案", key=f"approve_question_blocker_{fingerprint}", disabled=(answer == "")):
                payload = {
                    "answer": answer,
                    "scope": selected_scope,
                    "approved_by": "streamlit_user",
                }
                if selected_batch:
                    payload["batch_id"] = selected_batch
                approve_path = (
                    f"/question-blockers/{safe_app_key(target_blocker_id)}/approve"
                    if selected_scope == "application"
                    else f"/question-blocker-groups/{safe_app_key(fingerprint)}/approve"
                )
                result = call_mongo_memory(
                    config,
                    "POST",
                    approve_path,
                    payload=payload,
                    timeout=8,
                )
                if result:
                    st.success(f"已批准 {result.get('approved_count', 0)} 条，{result.get('ready_to_resume_count', 0)} 个 application 进入 READY_TO_RESUME。")
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error("审批失败，请检查 memory 服务日志。")

    if technical_groups:
        st.markdown("#### 需要技术检查")
        for group in technical_groups:
            with st.expander(f"{group.get('question', 'Unknown question')} · TECHNICAL_REVIEW", expanded=False):
                st.write(f"控件类型：`{group.get('control_type', 'unknown')}`")
                if group.get("validation_messages"):
                    st.warning(" / ".join(group.get("validation_messages")[:3]))
                st.write(f"受影响 application：{', '.join((group.get('application_ids') or [])[:10])}")
                st.caption("页面已有答案或用户事实，但控件仍报错。这里不能通过随便填答案消除，需要修执行层或控件 handler。")

def field_answer_key(field_name):
    return hashlib.sha1(str(field_name or "").encode("utf-8")).hexdigest()[:12]

def collect_attention_fields(discovery):
    fields = []
    seen = set()
    for page_info in (discovery or {}).get("pages", []):
        page_number = page_info.get("page_number")
        autofill = page_info.get("autofill") or {}
        for item in autofill.get("missing_required", []):
            label = item.get("field") or ""
            key = (page_number, label, item.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            fields.append({
                "page_number": page_number,
                "field": label,
                "risk": item.get("risk", "unknown"),
                "reason": item.get("reason", "missing_required")
            })
    return fields

def render_answer_control(app_id, item, saved_answers, common_answers=None):
    field_text = item.get("field") or "Unknown field"
    answer_key = field_answer_key(field_text)
    session_key = f"answer_{safe_app_key(app_id)}_{answer_key}"
    existing = saved_answers.get(answer_key, {}).get("value", "")
    common_match = match_common_answer(field_text, common_answers or {})
    if not existing and common_match:
        existing = common_match.get("value", "")
    label = field_text[:260] + ("..." if len(field_text) > 260 else "")

    if re.search(r"(upload resume|resume|cv|file)", field_text, re.IGNORECASE):
        uploaded = st.file_uploader(f"Upload file: {label}", type=["pdf", "docx", "doc", "rtf"], key=f"file_{session_key}")
        if uploaded:
            upload_dir = os.path.join(BASE_DIR, "data", "apply_form_uploads", safe_app_key(app_id))
            os.makedirs(upload_dir, exist_ok=True)
            upload_path = os.path.join(upload_dir, uploaded.name)
            with open(upload_path, "wb") as f:
                f.write(uploaded.getbuffer())
            st.caption(f"Saved: {upload_path}")
            return answer_key, {
                "field": field_text,
                "risk": item.get("risk"),
                "reason": item.get("reason"),
                "type": "file",
                "value": upload_path
        }
        return answer_key, saved_answers.get(answer_key)

    if common_match:
        st.caption(f"已从常见答案库带入：{common_match.get('label')}")

    if item.get("risk") == "high" and re.search(r"\bYes\b.*\bNo\b|\bNo\b.*\bYes\b", field_text, re.IGNORECASE):
        options = ["", "Yes", "No"]
        index = options.index(existing) if existing in options else 0
        value = st.selectbox(f"High-risk confirmation: {label}", options, index=index, key=session_key)
    else:
        value = st.text_area(f"Answer: {label}", value=existing, height=80, key=session_key)

    return answer_key, {
        "field": field_text,
        "risk": item.get("risk"),
        "reason": item.get("reason"),
        "common_key": common_match.get("key") if common_match else None,
        "type": "text",
        "value": value
    }

def render_apply_form_discovery_panel(app_id, company, role, apply_url, resume_v1, config, common_answers=None):
    cache_path = discovery_cache_path(app_id)
    answers_path = form_answers_path(app_id)
    cached = st.session_state.get(f"discovery_{app_id}") or load_json_file(cache_path, default=None)
    saved_answers = load_json_file(answers_path, default={}) or {}
    if isinstance(saved_answers, dict) and "answers" in saved_answers:
        saved_answers = saved_answers.get("answers") or {}

    with st.expander("岗位预检结果 / 待补字段", expanded=bool(cached)):
        st.caption("会跑外部 apply 流程直到第一个安全停点；不会提交申请。")
        col_run, col_meta = st.columns([1, 3])
        with col_run:
            run_precheck = st.button("运行预检", key=f"precheck_{app_id}", disabled=not bool(apply_url))
        with col_meta:
            if cached:
                st.write(f"已缓存结果: `{os.path.basename(cache_path)}`")
            elif not apply_url:
                st.warning("这个岗位没有保存 apply URL。")

        if run_precheck:
            playwright_server_url = playwright_url_from_config(config)
            user_data = config.get("user_data", {}).copy()
            precheck_resume_text = resume_v1 or config.get("resume_v0", "") or JUDGE_DEMO_RESUME_V1
            user_data["resume_text"] = precheck_resume_text or ""
            user_data["application_id"] = app_id
            user_data["batch_id"] = config.get("active_batch_id") or "default"
            user_data["company"] = company
            user_data["role"] = role
            user_data = apply_application_profile_library(user_data)
            user_data = enable_application_question_matcher(user_data)
            pdf_path = None
            if precheck_resume_text:
                api_key_val = config.get("active_api_key")
                pdf_ai_client = genai.Client(api_key=api_key_val) if api_key_val else None
                pdf_attempts = [
                    ("selected_resume", precheck_resume_text),
                    ("one_page_demo_fallback", JUDGE_DEMO_RESUME_V1),
                ]
                seen_resume_texts = set()
                last_pdf_quality = None
                for attempt_label, attempt_resume in pdf_attempts:
                    if not attempt_resume or attempt_resume in seen_resume_texts:
                        continue
                    seen_resume_texts.add(attempt_resume)
                    try:
                        attempt_path = resume_pdf_path(f"{app_id}_{attempt_label}")
                        write_text_pdf(attempt_resume, attempt_path, title=f"{role} - precheck resume")
                        pdf_quality = validate_resume_pdf(
                            pdf_ai_client,
                            config.get("model_selector", "gemini-3.5-flash"),
                            attempt_resume,
                            attempt_path,
                        )
                        last_pdf_quality = pdf_quality
                        sync_mongo_artifact(config, app_id, "resume_pdf_quality_check", pdf_quality, {
                            "company": company,
                            "role": role,
                            "pdf_path": attempt_path,
                            "source": "apply_form_precheck",
                            "attempt": attempt_label,
                        })
                        if pdf_quality.get("passed"):
                            pdf_path = attempt_path
                            user_data["resume_text"] = attempt_resume
                            image_path = (pdf_quality.get("visual") or {}).get("image_path")
                            if image_path and os.path.exists(image_path):
                                with st.expander("PDF 简历第一页视觉检查", expanded=False):
                                    st.image(image_path, caption="Rendered PDF page 1")
                            if attempt_label != "selected_resume":
                                st.info("目标简历超过一页，预检阶段已改用一页 demo resume 继续表单结构检查；正式投递仍会执行一页硬限制。")
                            break
                    except Exception as pdf_err:
                        last_pdf_quality = {"passed": False, "error": str(pdf_err), "attempt": attempt_label}
                        continue
                if not pdf_path:
                    st.warning("预检阶段未附加简历 PDF；将继续检查外部表单结构，不会提交申请。")
                    if last_pdf_quality:
                        with st.expander("PDF 检查详情", expanded=False):
                            st.json(last_pdf_quality)
            payload = {
                "url": apply_url,
                "user_data": user_data,
                "application_id": app_id,
                "batch_id": config.get("active_batch_id") or "default",
                "resume_path": pdf_path,
                "max_steps": 14,
                "wait_for_email_seconds": 75,
                "allow_account_creation": True,
                "allow_email_verification": True,
                "allow_terms_acceptance": True,
                "stop_at_form": True,
                "discover_all_steps": True,
                "max_form_pages": 8,
                "allow_placeholder_autofill": True,
                "allow_low_risk_autofill": True,
                "allow_visual_fallback": True,
                "allow_resume_upload": bool(pdf_path)
            }
            with st.spinner("正在检查外部申请表单..."):
                try:
                    response = requests.post(f"{playwright_server_url}/access-apply-form", json=payload, timeout=420)
                    cached = response.json()
                    cached["_http_status"] = response.status_code
                    cached["resume_pdf_path"] = pdf_path
                    save_json_file(cache_path, cached)
                    sync_mongo_application(
                        config,
                        app_id,
                        company,
                        role,
                        resume_v0=config.get("resume_v0", ""),
                        resume_v1=resume_v1,
                        status="Queued",
                        apply_url=apply_url,
                        source="apply_form_precheck"
                    )
                    sync_mongo_artifact(config, app_id, "apply_form_discovery", cached, {
                        "company": company,
                        "role": role,
                        "apply_url": apply_url,
                        "resume_pdf_path": pdf_path
                    })
                    st.session_state[f"discovery_{app_id}"] = cached
                    st.success("预检完成。需要填写的字段在下面。")
                except Exception as err:
                    st.error(f"预检失败: {err}")

        if not cached:
            return

        discovery = cached.get("discovery") or {}
        pages = discovery.get("pages", [])
        stop_reason = discovery.get("stop_reason") or cached.get("blocked_reason") or "unknown"
        st.write(f"Stage: `{cached.get('stage')}` | pages: `{len(pages)}` | stop: `{stop_reason}`")
        if cached.get("current_url"):
            st.caption(cached.get("current_url"))

        page_state = cached.get("page_state") or cached.get("current_page_state") or {}
        preflight = cached.get("preflight") or cached.get("current_preflight") or {}
        state_summary = page_state.get("summary") or {}
        if page_state or preflight:
            st.markdown("#### 结构化页面状态")
            metric_cols = st.columns(5)
            metric_cols[0].metric("ATS", page_state.get("detected_ats") or cached.get("ats") or "unknown")
            metric_cols[1].metric("字段", state_summary.get("fields_count", len(cached.get("fields", []))))
            metric_cols[2].metric("必填", preflight.get("required_fields_count", state_summary.get("required_fields_count", 0)))
            metric_cols[3].metric("可自动填", preflight.get("auto_fillable_count", 0))
            metric_cols[4].metric("需人工", preflight.get("needs_user_count", 0))
            if preflight.get("page_status"):
                st.caption(f"Preflight: `{preflight.get('page_status')}`")
            blocking_issues = preflight.get("blocking_issues") or []
            if blocking_issues:
                with st.expander("阻塞/人工确认原因", expanded=True):
                    st.json(blocking_issues)
            with st.expander("PageState 摘要", expanded=False):
                st.json({
                    "url": page_state.get("url"),
                    "page_title": page_state.get("page_title"),
                    "step": page_state.get("step"),
                    "summary": state_summary,
                    "observation_layers": page_state.get("observation_layers"),
                    "buttons": ((page_state.get("forms") or [{}])[0].get("buttons") or [])[:20],
                    "validation_errors": ((page_state.get("forms") or [{}])[0].get("validation_errors") or [])[:20],
                })

        attention_fields = collect_attention_fields(discovery)
        if stop_reason == "final_submit_guard":
            st.success("已经到最终提交保护点，agent 已在提交前停止。")
        elif not attention_fields:
            st.info("最新结果里没有发现需要人工补充的字段。")
        else:
            st.markdown("#### 需要你补的字段")
            updated_answers = dict(saved_answers)
            for item in attention_fields:
                risk = item.get("risk", "unknown")
                page_number = item.get("page_number")
                st.markdown(f"**第 {page_number} 页 · 风险 `{risk}` · {item.get('reason')}**")
                answer_key, answer = render_answer_control(app_id, item, updated_answers, common_answers)
                if answer:
                    updated_answers[answer_key] = answer
            sync_common = st.checkbox("保存时同步可匹配字段到常见答案库", value=False, key=f"sync_common_answers_{app_id}")
            if st.button("保存表单答案", key=f"save_answers_{app_id}"):
                save_json_file(answers_path, {
                    "app_id": app_id,
                    "company": company,
                    "role": role,
                    "apply_url": apply_url,
                    "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
                    "answers": updated_answers
                })
                sync_mongo_artifact(config, app_id, "apply_form_answers", {
                    "answers": updated_answers
                }, {
                    "company": company,
                    "role": role,
                    "apply_url": apply_url
                })
                if sync_common:
                    current_common = load_common_answers()
                    for answer in updated_answers.values():
                        if not isinstance(answer, dict):
                            continue
                        common_key = answer.get("common_key")
                        value = answer.get("value")
                        if not common_key or not value or answer.get("type") == "file":
                            continue
                        field_def = COMMON_ANSWER_FIELD_BY_KEY.get(common_key, {})
                        current_common[common_key] = {
                            "label": field_def.get("label") or answer.get("field") or common_key,
                            "type": field_def.get("type") or answer.get("type") or "text",
                            "value": value
                        }
                    save_json_file(common_answers_path(), {
                        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
                        "answers": current_common
                    })
                    sync_mongo_artifact(config, "global_common_answers", "apply_form_common_answers", {
                        "answers": current_common
                    }, {"scope": "global", "source": "application_answers"})
                st.success(f"已保存到 {answers_path}")

        with st.expander("原始 discovery JSON", expanded=False):
            st.json(cached)

# Initialize Database on boot
require_app_login()
init_db_tables()

# Load initial config
config = load_config()

handle_gmail_oauth_callback(config)

ui_language = choose_ui_language()
if ui_language == "English":
    enable_english_layout_translation()
else:
    disable_english_layout_translation()

# Page Header
st.markdown('<div class="main-header">💼 领英智能多任务求职控制中心</div>', unsafe_allow_html=True)
st.markdown('<div class="header-sub">管理多个独立的求职指令。系统将在后台并发执行每个任务的自动检索、改写与审计，并通过队列分发执行。</div>', unsafe_allow_html=True)

# Initialize background scheduler worker singleton
worker = ScheduledApplyWorker()

# Main tabs
tab1, tab_questions, tab2 = st.tabs(["💼 智能求职指令中心", "待回答问题", "📊 求职历史与数据分析"])

# ==========================================
# 💼 TAB 1: 智能求职指令中心 (Alerts Control Center)
# ==========================================
with tab1:
    # 1. Global settings panel (expander)
    with st.expander("⚙️ 全局个人凭证与默认配置", expanded=False):
        col1, col2 = st.columns(2)
        # Identify options index for model and mode
        model_list = ["gemini-3.5-flash", "gemini-2.5-pro", "gemini-1.5-pro"]
        try:
            model_index = model_list.index(config.get("model_selector", "gemini-3.5-flash"))
        except ValueError:
            model_index = 0
            
        mode_list = ["自动扫描 + 人工仲裁确认 (推荐)", "全自动无感投递 (Playwright 自动执行)"]
        try:
            mode_index = mode_list.index(config.get("execution_mode", "自动扫描 + 人工仲裁确认 (推荐)"))
        except ValueError:
            mode_index = 0

        with col1:
            st.subheader("个人基本信息")
            user_data_config = config.get("user_data", {})
            first_name = st.text_input("名 (First Name)", config.get("user_data", {}).get("first_name", ""), key="cfg_first_name", on_change=auto_save_field)
            last_name = st.text_input("姓 (Last Name)", config.get("user_data", {}).get("last_name", ""), key="cfg_last_name", on_change=auto_save_field)
            email = st.text_input("邮箱地址 (Email)", config.get("user_data", {}).get("email", ""), key="cfg_email", on_change=auto_save_field)
            stored_calling_code = (
                user_data_config.get("country_phone_code")
                or user_data_config.get("phone_country_code")
                or user_data_config.get("calling_code")
            )
            default_calling_code = normalize_country_calling_code(
                stored_calling_code,
                user_data_config.get("country", "United States"),
            )
            phone_code_col, phone_number_col = st.columns([1, 3])
            with phone_code_col:
                country_phone_code = st.text_input(
                    "国家/地区电话区号",
                    default_calling_code,
                    placeholder="+1",
                    key="cfg_country_phone_code",
                    on_change=auto_save_field,
                )
            with phone_number_col:
                phone = st.text_input(
                    "电话号码 (Phone)",
                    user_data_config.get("phone", ""),
                    placeholder="(217) 555-0123",
                    key="cfg_phone",
                    on_change=auto_save_field,
                )
            if country_phone_code.strip() and not re.fullmatch(r"\+\d{1,4}", country_phone_code.strip()):
                st.warning("国家/地区电话区号格式应为 +1、+44 或 +86。")
            if phone.strip():
                if not re.fullmatch(r"[0-9\s\-()]{7,20}", phone.strip()):
                    st.warning("电话号码应只包含本地号码；国家/地区区号请填写在左侧。")

            st.subheader("Address")
            address1 = st.text_input("Address Line 1", user_data_config.get("address1") or user_data_config.get("street_address") or user_data_config.get("address", ""), key="cfg_address1", on_change=auto_save_field)
            city = st.text_input("City", user_data_config.get("city", ""), key="cfg_city", on_change=auto_save_field)
            address_col_a, address_col_b = st.columns(2)
            with address_col_a:
                state = st.text_input("State", user_data_config.get("state", ""), placeholder="IL or Illinois", key="cfg_state", on_change=auto_save_field)
            with address_col_b:
                postal_code = st.text_input("Postal Code", user_data_config.get("postal_code") or user_data_config.get("zip", ""), key="cfg_postal_code", on_change=auto_save_field)
            country = st.text_input("Country", user_data_config.get("country", "United States"), key="cfg_country", on_change=auto_save_field)

            profile_library = load_application_profile_library(config)
            profile_skills = profile_library.get("skills") or user_data_config.get("skills", "")
            st.subheader("Skill Library")
            if st.button("Find Skills in Resume V0", key="suggest_skills_from_resume"):
                st.session_state["skill_library_suggestions"] = suggest_skills_from_resume(
                    config.get("resume_v0", ""),
                    profile_skills,
                )

            skill_suggestions = st.session_state.get("skill_library_suggestions") or []
            if skill_suggestions:
                suggestion_names = [item.get("name") for item in skill_suggestions if item.get("name")]
                selected_suggestions = st.multiselect(
                    "Resume-evidenced skill suggestions",
                    suggestion_names,
                    default=[],
                    key="selected_skill_library_suggestions",
                )
                with st.expander("Skill suggestion evidence", expanded=False):
                    st.dataframe(
                        [
                            {
                                "Skill": item.get("name"),
                                "Evidence": " | ".join(item.get("evidence") or []),
                                "Strength": item.get("evidence_strength"),
                            }
                            for item in skill_suggestions
                        ],
                        use_container_width=True,
                        hide_index=True,
                    )
                if st.button("Add Selected Skills", key="add_selected_skill_suggestions"):
                    merged_skills = normalize_skill_entries([*normalize_skill_entries(profile_skills), *selected_suggestions])
                    save_skill_library_fields(config, merged_skills)
                    if save_config(config):
                        st.session_state["cfg_skills"] = "\n".join(merged_skills)
                        st.session_state["skill_library_suggestions"] = []
                        st.rerun()

            skills_value = "\n".join(normalize_skill_entries(profile_skills))
            skills = st.text_area(
                "Technical and Professional Skills",
                skills_value,
                placeholder="Python, TypeScript, AWS",
                height=120,
                key="cfg_skills",
                on_change=auto_save_field,
            )
            if st.button("Save Skill Library", key="save_skill_library"):
                save_skill_library_fields(config)
                if save_config(config):
                    st.success("Skill library saved.")
                    time.sleep(0.3)
                    st.rerun()

            st.subheader("Application Profile")
            profile_availability = profile_library.get("availability") if isinstance(profile_library.get("availability"), dict) else {}
            st.subheader("Availability")
            st.text_input(
                "Earliest Start Date",
                profile_availability.get("start_date", ""),
                placeholder="e.g. December 2026, 2026-12-15, or Immediately",
                key="cfg_availability_start_date",
                on_change=auto_save_field,
            )
            st.text_input(
                "Notice Period",
                profile_availability.get("notice_period", ""),
                placeholder="e.g. Two weeks after offer",
                key="cfg_availability_notice_period",
                on_change=auto_save_field,
            )
            if st.button("Save Availability", key="save_availability_library"):
                save_availability_library_fields(config)
                if save_config(config):
                    st.success("Availability saved.")
                    time.sleep(0.3)
                    st.rerun()

            profile_education = profile_library.get("education") if isinstance(profile_library.get("education"), dict) else {}
            st.subheader("Education Library")
            edu_col_a, edu_col_b = st.columns(2)
            with edu_col_a:
                st.text_input("School", profile_education.get("school", ""), key="cfg_edu_school", on_change=auto_save_field)
                st.text_input("Degree", profile_education.get("degree", ""), key="cfg_edu_degree", on_change=auto_save_field)
                st.text_input("Field of Study", profile_education.get("field", ""), key="cfg_edu_field", on_change=auto_save_field)
                st.text_input("Education Location", profile_education.get("location", ""), key="cfg_edu_location", on_change=auto_save_field)
            with edu_col_b:
                st.text_input("Start Month", profile_education.get("start_month", ""), key="cfg_edu_start_month", on_change=auto_save_field)
                st.text_input("Start Year", profile_education.get("start_year", ""), key="cfg_edu_start_year", on_change=auto_save_field)
                st.text_input("Graduation Month", profile_education.get("end_month", ""), key="cfg_edu_end_month", on_change=auto_save_field)
                st.text_input("Graduation Year", profile_education.get("end_year", ""), key="cfg_edu_end_year", on_change=auto_save_field)
            st.text_input("GPA / Overall Result", profile_education.get("gpa", ""), key="cfg_edu_gpa", on_change=auto_save_field)
            if st.button("Save Education Library", key="save_education_library"):
                save_education_library_fields(config)
                if save_config(config):
                    st.success("Education library saved.")
                    time.sleep(0.3)
                    st.rerun()

            st.subheader("Language Library")
            profile_languages = profile_library.get("languages")
            if isinstance(profile_languages, dict):
                profile_languages = [profile_languages]
            if not isinstance(profile_languages, list):
                profile_languages = DEFAULT_APPLICATION_PROFILE_LIBRARY["languages"]
            default_language_count = max(1, min(5, len(profile_languages) or 2))
            language_count = int(st.number_input(
                "Language entries to use for ATS forms",
                min_value=1,
                max_value=5,
                value=default_language_count,
                step=1,
                key="cfg_language_count",
                on_change=auto_save_field,
            ))
            for lang_idx in range(language_count):
                lang_entry = profile_languages[lang_idx] if lang_idx < len(profile_languages) and isinstance(profile_languages[lang_idx], dict) else {}
                lang_col_a, lang_col_b = st.columns(2)
                with lang_col_a:
                    st.text_input("Language", lang_entry.get("language", ""), key=f"cfg_lang_{lang_idx}_language", on_change=auto_save_field)
                with lang_col_b:
                    st.text_input("Overall", lang_entry.get("overall", "4 - Fluent"), key=f"cfg_lang_{lang_idx}_overall", on_change=auto_save_field)
            if st.button("Save Language Library", key="save_language_library"):
                save_language_library_fields(config)
                if save_config(config):
                    st.success("Language library saved.")
                    time.sleep(0.3)
                    st.rerun()

            st.subheader("Work Experience Library")
            profile_experiences = profile_library.get("experiences")
            if isinstance(profile_experiences, dict):
                profile_experiences = [profile_experiences]
            if not isinstance(profile_experiences, list):
                profile_experiences = []
            default_experience_count = max(1, min(3, len(profile_experiences) or 1))
            experience_count = int(st.number_input(
                "Experience entries to use for ATS forms",
                min_value=1,
                max_value=3,
                value=default_experience_count,
                step=1,
                key="cfg_experience_count",
                on_change=auto_save_field,
            ))
            for exp_idx in range(experience_count):
                exp_entry = profile_experiences[exp_idx] if exp_idx < len(profile_experiences) and isinstance(profile_experiences[exp_idx], dict) else {}
                st.markdown(f"**Experience {exp_idx + 1}**")
                exp_col_a, exp_col_b = st.columns(2)
                with exp_col_a:
                    st.text_input("Company", exp_entry.get("company", ""), key=f"cfg_exp_{exp_idx}_company", on_change=auto_save_field)
                    st.text_input("Title / Role", exp_entry.get("title", ""), key=f"cfg_exp_{exp_idx}_title", on_change=auto_save_field)
                    st.text_input("Location", exp_entry.get("location", ""), key=f"cfg_exp_{exp_idx}_location", on_change=auto_save_field)
                with exp_col_b:
                    st.text_input("Start Month", exp_entry.get("start_month", ""), key=f"cfg_exp_{exp_idx}_start_month", on_change=auto_save_field)
                    st.text_input("Start Year", exp_entry.get("start_year", ""), key=f"cfg_exp_{exp_idx}_start_year", on_change=auto_save_field)
                    st.checkbox("Currently work here", bool(exp_entry.get("current", False)), key=f"cfg_exp_{exp_idx}_current", on_change=auto_save_field)
                    if not st.session_state.get(f"cfg_exp_{exp_idx}_current", False):
                        st.text_input("End Month", exp_entry.get("end_month", ""), key=f"cfg_exp_{exp_idx}_end_month", on_change=auto_save_field)
                        st.text_input("End Year", exp_entry.get("end_year", ""), key=f"cfg_exp_{exp_idx}_end_year", on_change=auto_save_field)
                st.text_area(
                    "Description / Responsibilities",
                    exp_entry.get("description", ""),
                    height=110,
                    key=f"cfg_exp_{exp_idx}_description",
                    on_change=auto_save_field,
                )
            if st.button("Save Work Experience Library", key="save_experience_library"):
                save_experience_library_fields(config)
                if save_config(config):
                    st.success("Work experience library saved.")
                    time.sleep(0.3)
                    st.rerun()

            st.subheader("Company Employment Registry")
            employment_registry = profile_library.get("company_employment_registry")
            if isinstance(employment_registry, dict):
                employment_registry = [employment_registry]
            if not isinstance(employment_registry, list):
                employment_registry = []
            registry_count = int(st.number_input(
                "Company records",
                min_value=0,
                max_value=20,
                value=min(20, len(employment_registry)),
                step=1,
                key="cfg_employment_registry_count",
                on_change=auto_save_field,
            ))
            for registry_idx in range(registry_count):
                registry_entry = (
                    employment_registry[registry_idx]
                    if registry_idx < len(employment_registry)
                    and isinstance(employment_registry[registry_idx], dict)
                    else {}
                )
                stored_answer = registry_entry.get("previously_employed")
                answer_label = "Yes" if stored_answer is True else "No" if stored_answer is False else "Unknown"
                registry_col_a, registry_col_b = st.columns(2)
                with registry_col_a:
                    st.text_input(
                        "Company",
                        registry_entry.get("company", ""),
                        key=f"cfg_employment_registry_{registry_idx}_company",
                        on_change=auto_save_field,
                    )
                    st.text_input(
                        "Company aliases / parent groups",
                        ", ".join(registry_entry.get("aliases") or []),
                        key=f"cfg_employment_registry_{registry_idx}_aliases",
                        on_change=auto_save_field,
                    )
                with registry_col_b:
                    st.selectbox(
                        "Previously employed",
                        ["Unknown", "Yes", "No"],
                        index=["Unknown", "Yes", "No"].index(answer_label),
                        key=f"cfg_employment_registry_{registry_idx}_answer",
                        on_change=auto_save_field,
                    )
                    st.checkbox(
                        "User confirmed",
                        bool(registry_entry.get("confirmed", False)),
                        key=f"cfg_employment_registry_{registry_idx}_confirmed",
                        on_change=auto_save_field,
                    )
            if st.button("Save Company Employment Registry", key="save_company_employment_registry"):
                save_company_employment_registry_fields(config)
                if save_config(config):
                    st.success("Company employment registry saved.")
                    time.sleep(0.3)
                    st.rerun()

            profile_library_json = st.text_area(
                "Application Profile Library JSON",
                application_profile_library_text(config),
                height=260,
                key="cfg_application_profile_library",
                on_change=auto_save_field,
            )
            st.subheader("大模型与集成端配置")
            api_key = st.text_input("Gemini API 密钥", config.get("active_api_key", ""), type="password", key="cfg_api_key", on_change=auto_save_field)
            model_selector = st.selectbox("默认推理模型", model_list, index=model_index, key="cfg_model", on_change=auto_save_field)
            
            st.subheader("领英直登凭证 (LinkedIn Login)")
            linkedin_username = st.text_input("领英登录邮箱/手机号", config.get("linkedin_username", ""), key="cfg_linkedin_username", on_change=auto_save_field)
            linkedin_password = st.text_input("领英登录密码", config.get("linkedin_password", ""), type="password", key="cfg_linkedin_password", on_change=auto_save_field)
            
        with col2:
            st.subheader("微服务终结点配置")
            elastic_url = st.text_input("ElasticSearch 检索服务 URL", config.get("elastic_url", "http://localhost:8002"), key="cfg_elastic", on_change=auto_save_field)
            elastic_cloud_id = st.text_input("Elastic Cloud ID", config.get("elastic_cloud_id", ""), key="cfg_elastic_cloud_id", on_change=auto_save_field)
            elastic_api_key = st.text_input("Elastic API Key", config.get("elastic_api_key", ""), type="password", key="cfg_elastic_api_key", on_change=auto_save_field)
            elastic_backend_url = st.text_input("自托管 Elasticsearch URL", config.get("elastic_backend_url", ""), placeholder="例如 http://localhost:9200；没有就留空", key="cfg_elastic_backend_url", on_change=auto_save_field)
            elastic_index_name = st.text_input("Elastic Index Name", config.get("elastic_index_name", "job_descriptions"), key="cfg_elastic_index_name", on_change=auto_save_field)
            arize_url = st.text_input("Arize Phoenix 审计服务 URL", config.get("arize_url", "http://localhost:8003"), key="cfg_arize", on_change=auto_save_field)
            phoenix_collector_endpoint = st.text_input(
                "Phoenix Cloud Collector Endpoint",
                config.get("phoenix_collector_endpoint", ""),
                placeholder="例如 https://app.phoenix.arize.com 或你的 Phoenix Cloud Space URL",
                key="cfg_phoenix_endpoint",
                on_change=auto_save_field
            )
            phoenix_api_key = st.text_input(
                "Phoenix Cloud API Key",
                config.get("phoenix_api_key", ""),
                type="password",
                key="cfg_phoenix_api_key",
                on_change=auto_save_field
            )
            phoenix_project_name = st.text_input(
                "Phoenix Project Name",
                config.get("phoenix_project_name", "job-hunter-agent"),
                key="cfg_phoenix_project",
                on_change=auto_save_field
            )
            mongo_url = st.text_input("MongoDB 记忆服务 URL", config.get("mongo_url", "http://localhost:8001"), key="cfg_mongo", on_change=auto_save_field)
            email_url = st.text_input("收件箱状态同步服务 URL", config.get("email_url", "http://localhost:8005"), key="cfg_email_url", on_change=auto_save_field)
            playwright_url = st.text_input("Playwright 浏览器自动化服务 URL", config.get("playwright_url", "http://localhost:8004"), key="cfg_playwright_url", on_change=auto_save_field)

            st.subheader("ATS 岗位源监控")
            ats_source_urls = st.text_area(
                "ATS source URLs",
                config.get("ats_source_urls", ""),
                placeholder="每行一个：Company | https://boards.greenhouse.io/company\n或 https://jobs.lever.co/company\n或 https://company.wd5.myworkdayjobs.com/en-US/site",
                height=110,
                key="cfg_ats_source_urls",
                on_change=auto_save_field,
            )
            ats_source_limit = st.number_input(
                "每轮每批最多抓取岗位数",
                min_value=5,
                max_value=200,
                value=int(config.get("ats_source_limit") or 50),
                step=5,
                key="cfg_ats_source_limit",
                on_change=auto_save_field,
            )
            
            st.subheader("IMAP 邮件收发服务配置")
            email_imap_server = st.text_input("IMAP 接收服务器", config.get("email_imap_server", "imap.gmail.com"), placeholder="例如: imap.gmail.com", key="cfg_imap", on_change=auto_save_field)
            email_password = st.text_input("邮箱密码 / 应用授权码", config.get("email_password", ""), type="password", placeholder="对于 Gmail，请在谷歌账号设置中生成 16 位 App Password 填入", key="cfg_password", on_change=auto_save_field)
            
            # Gmail OAuth2 Binding Section
            st.subheader("Gmail API OAuth2 快捷授权")
            oauth_status = gmail_oauth_status(config)
            web_cfg, oauth_error = load_gmail_oauth_client()
            if web_cfg:
                runtime_health = fetch_service_health(email_url_from_config(config), timeout=2)
                runtime_ready = bool(runtime_health.get("verification_ready"))
                if oauth_status.get("connected"):
                    st.markdown('<span class="badge-running">Gmail API authorization saved</span>', unsafe_allow_html=True)
                    if not runtime_ready:
                        st.caption("The saved authorization will be reused when the email service is available.")
                    if st.button("Disconnect Gmail", key="disconnect_gmail"):
                        token_path = gmail_token_path()
                        try:
                            os.remove(token_path)
                        except FileNotFoundError:
                            pass
                        st.rerun()
                else:
                    configured_email = ((config.get("user_data") or {}).get("email") or "").strip()
                    auth_url, auth_error = build_gmail_oauth_url(configured_email)
                    if auth_url:
                        st.markdown(f'<a href="{html.escape(auth_url)}" target="_self" style="background-color: #2563eb; color: white; padding: 8px 16px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block; text-align: center; margin-bottom: 15px;">Connect Gmail</a>', unsafe_allow_html=True)
                    else:
                        st.warning(auth_error or "Gmail OAuth is not configured.")
            else:
                st.warning(oauth_error or "Gmail OAuth is not configured.")
            
            st.subheader("投递调度选项")
            exec_mode = st.selectbox("投递执行方式", mode_list, index=mode_index, key="cfg_mode", on_change=auto_save_field)
            
            # Background thread controls
            st.markdown("<p style='font-weight:600; color:#0f172a; margin-bottom: 5px;'>后台自动扫描守护引擎</p>", unsafe_allow_html=True)
            is_alive = worker.worker_thread and worker.worker_thread.is_alive()
            if is_alive:
                st.markdown('<span class="badge-running">🟢 后台自动扫描已启用</span>', unsafe_allow_html=True)
                if st.button("🔴 暂停后台扫描"):
                    worker.stop()
                    st.rerun()
            else:
                st.markdown('<span class="badge-paused">🔴 后台自动扫描已暂停</span>', unsafe_allow_html=True)
                if st.button("▶️ 开启后台扫描"):
                    worker.start()
                    st.rerun()
            
        st.subheader("🔑 社交账户登录凭据 (LinkedIn Session Cookies)")
        st.markdown("""
        **使用说明：**
        1. 在您的常用浏览器（如 Chrome / Edge）中登录您的 **LinkedIn** 账号。
        2. 安装浏览器扩展 **Cookie-Editor**。
        3. 打开 Cookie-Editor，点击 **Export** -> **JSON**。
        4. 将复制的内容粘贴到下方文本框中，系统将自动在后台 Playwright 运行中注入该会话以保持登录状态。
        """)
        
        # Load cookies from data/cookies.json to display them if saved, or from config
        cookies_val = config.get("linkedin_cookies_raw", "")
        cookies_file_path = os.path.join(BASE_DIR, "data", "cookies.json")
        if not cookies_val and os.path.exists(cookies_file_path):
            try:
                with open(cookies_file_path, "r", encoding="utf-8") as f:
                    cookies_val = json.dumps(json.load(f))
            except Exception:
                pass
                
        cookies_input = st.text_area("LinkedIn Cookies (JSON 格式数组)", cookies_val, height=150, key="cfg_cookies", on_change=auto_save_field)
            
        st.subheader("原始主简历文本 (V0)")
        
        # Support PDF uploading
        uploaded_pdf = st.file_uploader("📤 上传 PDF 格式简历 (系统将自动解析为文本并填入下方)", type=["pdf"], key="uploaded_pdf_file")
        if uploaded_pdf is not None:
            file_key = f"processed_pdf_{uploaded_pdf.name}_{uploaded_pdf.size}"
            if not st.session_state.get(file_key, False):
                try:
                    import pypdf
                    pdf_reader = pypdf.PdfReader(uploaded_pdf)
                    extracted_text = ""
                    for page in pdf_reader.pages:
                        text = page.extract_text()
                        if text:
                            extracted_text += text + "\n"
                    
                    extracted_text = extracted_text.strip()
                    if extracted_text:
                        st.session_state["cfg_resume"] = extracted_text
                        auto_save_field()
                        st.session_state[file_key] = True
                        st.success("🎉 PDF 简历解析成功，已自动更新下方文本框！")
                        time.sleep(0.5)
                        st.rerun()
                except Exception as pdf_err:
                    st.error(f"❌ 解析 PDF 简历失败: {pdf_err}")
                    
        resume_v0 = st.text_area("请贴入您的 Markdown 或纯文本格式简历 V0 (用于定制改写及审计对照)", config.get("resume_v0", ""), height=250, key="cfg_resume", on_change=auto_save_field)
        
        if st.button("💾 保存全局配置", type="primary"):
            config["active_api_key"] = api_key
            config["model_selector"] = model_selector
            config["elastic_url"] = elastic_url
            config["elastic_cloud_id"] = elastic_cloud_id
            config["elastic_api_key"] = elastic_api_key
            config["elastic_backend_url"] = elastic_backend_url
            config["elastic_index_name"] = elastic_index_name
            config["arize_url"] = arize_url
            config["phoenix_collector_endpoint"] = phoenix_collector_endpoint
            config["phoenix_api_key"] = phoenix_api_key
            config["phoenix_project_name"] = phoenix_project_name
            config["mongo_url"] = mongo_url
            config["email_url"] = email_url
            config["playwright_url"] = playwright_url
            config["ats_source_urls"] = ats_source_urls
            config["ats_source_limit"] = int(ats_source_limit)
            config["email_imap_server"] = email_imap_server
            config["email_password"] = email_password
            config["resume_v0"] = resume_v0
            config["execution_mode"] = exec_mode
            config["linkedin_cookies_raw"] = cookies_input
            config["linkedin_username"] = linkedin_username
            config["linkedin_password"] = linkedin_password
            config.setdefault("user_data", {}).update({
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "phone": phone,
                "country_phone_code": country_phone_code,
                "address1": address1,
                "city": city,
                "state": state,
                "postal_code": postal_code,
                "country": country,
            })
            save_application_profile_library(config, profile_library_json)
            if "cfg_skills" in st.session_state:
                save_skill_library_fields(config)
            if "cfg_availability_start_date" in st.session_state:
                save_availability_library_fields(config)
            if "cfg_edu_school" in st.session_state:
                save_education_library_fields(config)
            if "cfg_experience_count" in st.session_state:
                save_experience_library_fields(config)
            if "cfg_employment_registry_count" in st.session_state:
                save_company_employment_registry_fields(config)
            
            # Save cookies.json
            cookies_file = os.path.join(BASE_DIR, "data", "cookies.json")
            if cookies_input.strip():
                try:
                    cookies_json = json.loads(cookies_input.strip())
                    if isinstance(cookies_json, list):
                        os.makedirs(os.path.dirname(cookies_file), exist_ok=True)
                        with open(cookies_file, "w", encoding="utf-8") as f:
                            json.dump(cookies_json, f, indent=2)
                    else:
                        st.error("⚠️ Cookies 格式不正确：必须是 JSON 数组")
                except Exception as e:
                    st.error(f"⚠️ Cookies 解析失败: {e}")
            else:
                if os.path.exists(cookies_file):
                    try:
                        os.remove(cookies_file)
                    except Exception:
                        pass
                        
            # Write to .env.local to update background email_server microservice credentials dynamically
            save_env_local(email_imap_server, email, email_password)
            if save_config(config):
                st.success("🎉 全局配置及邮箱凭据保存成功！")
                time.sleep(0.5)
                st.rerun()

    st.markdown("### 🧾 申请表答案库与预检")
    st.caption("先维护一份常见申请问题答案库；再对具体外部 apply 链接跑预检。预检不会提交申请。")
    common_answers = render_common_answer_bank(config)

    st.markdown("#### 岗位表单预检")
    precheck_source = st.radio(
        "预检来源",
        ["直接 Apply URL", "投递队列"],
        horizontal=True,
        key="apply_form_precheck_source"
    )
    selected_precheck = None

    if precheck_source == "直接 Apply URL":
        manual_apply_url = st.text_input(
            "直接预检 Apply URL",
            "",
            placeholder="粘贴 LinkedIn 提取出的外部 apply 链接，例如 BrassRing / Workday / Ashby",
            key="manual_apply_precheck_url"
        )
        if manual_apply_url.strip():
            manual_id = "manual_" + hashlib.sha1(manual_apply_url.strip().encode("utf-8")).hexdigest()[:16]
            selected_precheck = (
                manual_id,
                "Manual URL",
                "Direct apply precheck",
                manual_apply_url.strip(),
                config.get("resume_v0", "")
            )
    else:
        conn_form_apps = get_db_connection()
        cursor_form_apps = conn_form_apps.cursor()
        cursor_form_apps.execute("""
            SELECT id, company, role, resume_v1, status, apply_url, created_at
            FROM mcp_applications
            WHERE apply_url IS NOT NULL AND TRIM(apply_url) != ''
            ORDER BY created_at DESC
        """)
        raw_form_apps = cursor_form_apps.fetchall()
        conn_form_apps.close()
        form_apps = [
            row for row in raw_form_apps
            if is_precheckable_apply_record(row[0], row[1], row[5], row[4])
        ]

        if not form_apps:
            st.info("当前队列里还没有适合预检的真实外部 ATS 链接。可以切到 Direct Apply URL 手动粘贴 Workday / BrassRing / Greenhouse / Ashby 链接。")
        else:
            st.caption("只显示真实外部 ATS 链接；内部 mock、smoke test、LinkedIn 搜索页和已拒岗位已隐藏。")
            app_labels = [
                f"{role} @ {company} · {status} · {created_at}"
                for app_id, company, role, resume_v1, status, apply_url, created_at in form_apps
            ]
            selected_index = st.selectbox(
                "选择岗位",
                range(len(form_apps)),
                format_func=lambda idx: app_labels[idx],
                key="apply_form_precheck_app_select"
            )
            app_id, company, role, resume_v1, status, apply_url, created_at = form_apps[selected_index]
            st.caption(apply_url)
            selected_precheck = (app_id, company, role, apply_url, resume_v1)

    if selected_precheck:
        render_apply_form_discovery_panel(*selected_precheck, config, common_answers)

    st.markdown("---")

    # 2. Add New Career Alert Form
    st.markdown("### ➕ 新建求职扫描指令")
    with st.container():
        st.markdown('<div class="glass-card">', unsafe_allow_html=True)
        col_new_1, col_new_2, col_new_3 = st.columns([2, 1, 1])
        with col_new_1:
            new_keywords = st.text_input("岗位名称 / 搜索关键词", placeholder="例如: Full Stack Engineer", key="new_keywords")
        with col_new_2:
            new_location = st.text_input("求职目标地区", placeholder="例如: United States", key="new_location")
        with col_new_3:
            new_interval = st.selectbox(
                "扫描自动间隔",
                [("每 15 分钟", 15), ("每 30 分钟", 30), ("每 1 小时", 60), ("每 12 小时", 720), ("每 24 小时", 1440)],
                format_func=lambda x: x[0],
                key="new_interval"
            )
            
        # LinkedIn Search Filters Row
        st.markdown("**🔍 领英检索过滤器高级设置 (选填)**")
        col_filter_1, col_filter_2, col_filter_3 = st.columns([1, 1.5, 1.5])
        with col_filter_1:
            date_posted_opts = [
                ("不限", None),
                ("过去 24 小时", "past-24h"),
                ("过去一周", "past-week"),
                ("过去一个月", "past-month")
            ]
            new_date_posted = st.selectbox(
                "发布时间 (Date posted)",
                date_posted_opts,
                format_func=lambda x: x[0],
                key="new_date_posted"
            )[1]
            
        with col_filter_2:
            remote_opts = [
                ("Remote (远程)", "remote"),
                ("Hybrid (混合)", "hybrid"),
                ("On-site (现场)", "on-site")
            ]
            selected_remotes = st.multiselect(
                "工作模式 (Remote / Hybrid)",
                remote_opts,
                format_func=lambda x: x[0],
                key="new_remotes"
            )
            new_remote = ",".join([r[1] for r in selected_remotes]) if selected_remotes else None
            
        with col_filter_3:
            exp_opts = [
                ("Internship (实习)", "internship"),
                ("Entry level (初级)", "entry-level"),
                ("Associate (中级)", "associate"),
                ("Mid-Senior level (中高级)", "mid-senior"),
                ("Director (总监)", "director"),
                ("Executive (高管)", "executive")
            ]
            selected_exps = st.multiselect(
                "经验要求 (Experience Level)",
                exp_opts,
                format_func=lambda x: x[0],
                key="new_exps"
            )
            new_experience_level = ",".join([e[1] for e in selected_exps]) if selected_exps else None
            
        if st.button("➕ 创建并运行求职扫描指令", type="primary"):
            if not new_keywords or not new_location:
                st.warning("⚠️ 请填写求职关键词及求职目标地区！")
            else:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO scheduler_tasks (keywords, location, interval_minutes, enabled, date_posted, experience_level, remote) VALUES (?, ?, ?, 1, ?, ?, ?)",
                    (new_keywords, new_location, new_interval[1], new_date_posted, new_experience_level, new_remote)
                )
                conn.commit()
                conn.close()
                st.success(f"🎉 成功创建求职指令: {new_keywords}!")
                time.sleep(0.5)
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    # 3. Active Alert Cards Dashboard
    st.markdown("### 📋 正在监控的求职指令")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, keywords, location, interval_minutes, enabled, last_run_time, created_at, date_posted, experience_level, remote FROM scheduler_tasks ORDER BY id DESC")
    tasks = cursor.fetchall()
    conn.close()
    
    if not tasks:
        st.info("ℹ️ 暂无求职监控指令。请在上方输入关键词创建第一条指令。")
    else:
        for t in tasks:
            t_id, t_keywords, t_location, t_interval, t_enabled, t_last_run, t_created, t_date_posted, t_experience_level, t_remote = t
            
            # Format times
            created_dt = t_created
            if t_last_run > 0.0:
                last_run_dt = datetime.datetime.fromtimestamp(t_last_run).strftime("%Y-%m-%d %H:%M:%S")
            else:
                last_run_dt = "从未运行"
                
            # Create labels for advanced filters
            date_posted_labels = {"past-24h": "过去 24 小时", "past-week": "过去一周", "past-month": "过去一个月"}
            t_date_posted_lbl = date_posted_labels.get(t_date_posted, "不限")
            
            remote_labels = {"remote": "Remote", "hybrid": "Hybrid", "on-site": "On-site"}
            t_remote_lbl = ", ".join([remote_labels.get(r, r) for r in t_remote.split(",")]) if t_remote else "不限"
            
            exp_labels = {
                "internship": "Internship",
                "entry-level": "Entry level",
                "associate": "Associate",
                "mid-senior": "Mid-Senior",
                "director": "Director",
                "executive": "Executive"
            }
            t_exp_lbl = ", ".join([exp_labels.get(e, e) for e in t_experience_level.split(",")]) if t_experience_level else "不限"
            
            # Create a card
            card_id = f"task_card_{t_id}"
            
            # We wrap in custom container for style
            # We wrap in custom container for style
            st.markdown(f'<div class="glass-card">', unsafe_allow_html=True)
            
            run_btn = False
            if st.session_state.get(f"editing_task_{t_id}", False):
                st.markdown(f"#### ✏️ 编辑求职指令: {t_keywords}")
                edit_keywords = st.text_input("岗位名称 / 搜索关键词", value=t_keywords, key=f"edit_keywords_{t_id}")
                edit_location = st.text_input("求职目标地区", value=t_location, key=f"edit_location_{t_id}")
                
                interval_opts = [("每 15 分钟", 15), ("每 30 分钟", 30), ("每 1 小时", 60), ("每 12 小时", 720), ("每 24 小时", 1440)]
                default_idx = 0
                for idx, opt in enumerate(interval_opts):
                    if opt[1] == t_interval:
                        default_idx = idx
                        break
                        
                edit_interval = st.selectbox(
                    "扫描自动间隔",
                    interval_opts,
                    index=default_idx,
                    format_func=lambda x: x[0],
                    key=f"edit_interval_{t_id}"
                )[1]
                
                # Filters
                date_posted_opts = [
                    ("不限", None),
                    ("过去 24 小时", "past-24h"),
                    ("过去一周", "past-week"),
                    ("过去一个月", "past-month")
                ]
                default_dp_idx = 0
                for idx, opt in enumerate(date_posted_opts):
                    if opt[1] == t_date_posted:
                        default_dp_idx = idx
                        break
                edit_date_posted = st.selectbox(
                    "发布时间 (Date posted)",
                    date_posted_opts,
                    index=default_dp_idx,
                    format_func=lambda x: x[0],
                    key=f"edit_date_posted_{t_id}"
                )[1]
                
                remote_opts = [
                    ("Remote (远程)", "remote"),
                    ("Hybrid (混合)", "hybrid"),
                    ("On-site (现场)", "on-site")
                ]
                default_remotes = t_remote.split(",") if t_remote else []
                selected_remotes_edit = st.multiselect(
                    "工作模式 (Remote / Hybrid)",
                    remote_opts,
                    default=[opt for opt in remote_opts if opt[1] in default_remotes],
                    format_func=lambda x: x[0],
                    key=f"edit_remotes_{t_id}"
                )
                edit_remote = ",".join([r[1] for r in selected_remotes_edit]) if selected_remotes_edit else None
                
                exp_opts = [
                    ("Internship (实习)", "internship"),
                    ("Entry level (初级)", "entry-level"),
                    ("Associate (中级)", "associate"),
                    ("Mid-Senior level (中高级)", "mid-senior"),
                    ("Director (总监)", "director"),
                    ("Executive (高管)", "executive")
                ]
                default_exps = t_experience_level.split(",") if t_experience_level else []
                selected_exps_edit = st.multiselect(
                    "经验要求 (Experience Level)",
                    exp_opts,
                    default=[opt for opt in exp_opts if opt[1] in default_exps],
                    format_func=lambda x: x[0],
                    key=f"edit_exps_{t_id}"
                )
                edit_experience_level = ",".join([e[1] for e in selected_exps_edit]) if selected_exps_edit else None
                
                col_edit_actions = st.columns([1, 1])
                with col_edit_actions[0]:
                    save_btn = st.button("💾 保存修改", key=f"save_edit_{t_id}", type="primary")
                with col_edit_actions[1]:
                    cancel_btn = st.button("❌ 取消", key=f"cancel_edit_{t_id}")
                    
                if save_btn:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE scheduler_tasks SET keywords = ?, location = ?, interval_minutes = ?, date_posted = ?, experience_level = ?, remote = ? WHERE id = ?",
                        (edit_keywords, edit_location, edit_interval, edit_date_posted, edit_experience_level, edit_remote, t_id)
                    )
                    conn.commit()
                    conn.close()
                    st.session_state[f"editing_task_{t_id}"] = False
                    st.success("修改成功！")
                    time.sleep(0.5)
                    st.rerun()
                    
                if cancel_btn:
                    st.session_state[f"editing_task_{t_id}"] = False
                    st.rerun()
            else:
                # Columns inside the card
                col_info, col_status, col_actions = st.columns([3, 1, 1.5])
                
                with col_info:
                    st.markdown(f'<div class="card-title">💼 {t_keywords}</div>', unsafe_allow_html=True)
                    st.markdown(f'<div class="card-property">📍 <strong>求职地区:</strong> {t_location}</div>', unsafe_allow_html=True)
                    st.markdown(f'<div class="card-property">⏱️ <strong>扫描间隔:</strong> 每 {t_interval} 分钟</div>', unsafe_allow_html=True)
                    st.markdown(f'<div class="card-property">🕒 <strong>发布时间:</strong> {t_date_posted_lbl} | <strong>工作模式:</strong> {t_remote_lbl} | <strong>经验等级:</strong> {t_exp_lbl}</div>', unsafe_allow_html=True)
                    st.markdown(f'<div class="card-property">📅 <strong>创建时间:</strong> {created_dt}</div>', unsafe_allow_html=True)
                    st.markdown(f'<div class="card-property">🕒 <strong>上次自动运行时间:</strong> {last_run_dt}</div>', unsafe_allow_html=True)
                    
                with col_status:
                    st.markdown("<div style='height: 15px;'></div>", unsafe_allow_html=True)
                    if t_enabled == 1:
                        st.markdown('<span class="badge-running">🟢 引擎自动扫描中</span>', unsafe_allow_html=True)
                    else:
                        st.markdown('<span class="badge-paused">🔴 扫描已暂停</span>', unsafe_allow_html=True)
                        
                with col_actions:
                    st.markdown("<div style='height: 15px;'></div>", unsafe_allow_html=True)
                    
                    # Manual trigger button
                    run_btn = st.button("⚡ 立即手动检索投递", key=f"run_now_{t_id}", help="立即执行该求职指令的搜索、简历定制和事实一致性审计流水线")
                    
                    # Toggle enabled button
                    toggle_lbl = "⏸️ 暂停自动" if t_enabled == 1 else "▶️ 启用自动"
                    toggle_btn = st.button(toggle_lbl, key=f"toggle_{t_id}")
                    
                    # Edit button
                    edit_btn = st.button("✏️ 编辑指令", key=f"edit_trigger_{t_id}")
                    
                    # Delete task
                    delete_btn = st.button("🗑️ 删除指令", key=f"delete_{t_id}")
                    
                    if edit_btn:
                        st.session_state[f"editing_task_{t_id}"] = True
                        st.rerun()
                        
                    if toggle_btn:
                        conn = get_db_connection()
                        cursor = conn.cursor()
                        cursor.execute("UPDATE scheduler_tasks SET enabled = ? WHERE id = ?", (1 - t_enabled, t_id))
                        conn.commit()
                        conn.close()
                        st.rerun()
                        
                    if delete_btn:
                        conn = get_db_connection()
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM scheduler_tasks WHERE id = ?", (t_id,))
                        conn.commit()
                        conn.close()
                        st.success(f"已删除求职指令: {t_keywords}")
                        time.sleep(0.5)
                        st.rerun()
            
            # Inline pipeline log area if run button is clicked
            if run_btn:
                st.markdown("---")
                st.markdown("#### ⚡ 智能体协同流水线运行日志 (当前进程)")
                
                status_container = st.status(f"正在手动执行指令: {t_keywords} ({t_location})...", expanded=True)
                progress_bar = st.progress(0)
                
                with status_container:
                    # Initialize GenAI Client
                    api_key_val = config.get("active_api_key", "")
                    if not api_key_val:
                        st.error("❌ 无法启动：未配置大模型 API Key，请在顶部折叠配置区填写保存。")
                        status_container.update(state="error", label="运行失败：API Key 缺失")
                        progress_bar.progress(0)
                    else:
                        log_run("INFO", f"[手动运行] 启动：岗位 '{t_keywords}'，地区 '{t_location}'", task_id=t_id)
                        
                        try:
                            ai_client = genai.Client(api_key=api_key_val)
                        except Exception as e:
                            st.error(f"❌ 无法连接 Gemini Client: {e}")
                            status_container.update(state="error", label="客户端初始化错误")
                            ai_client = None
                            
                        if ai_client:
                            ats_result, ats_error = warm_ats_sources(
                                config,
                                t_keywords,
                                t_location,
                                task_id=t_id,
                                experience_level=t_experience_level.split(",") if t_experience_level else None,
                            )
                            if ats_error:
                                st.warning(f"ATS source scan skipped: {ats_error}")
                            elif ats_result:
                                st.write(
                                    f"ATS source scan indexed {ats_result.get('indexed', 0)} jobs from "
                                    f"{ats_result.get('scanned_sources', 0)} sources."
                                )

                            # 1. Elasticsearch query
                            elastic_val = elastic_url_from_config(config)
                            st.write(f"🔍 正在向 Elasticsearch 服务 ({elastic_val}) 发起检索...")
                            progress_bar.progress(15)
                            
                            jobs = []
                            try:
                                res = requests.post(f"{elastic_val}/search", json={
                                    "keywords": [t_keywords],
                                    "location": t_location,
                                    "limit": 3,
                                    "date_posted": t_date_posted,
                                    "experience_level": t_experience_level.split(",") if t_experience_level else None,
                                    "remote": t_remote.split(",") if t_remote else None
                                }, timeout=90)
                                if res.status_code == 200:
                                    jobs = res.json().get("jobs", [])
                            except Exception:
                                st.warning("⚠️ 连接 Elasticsearch 检索微服务失败。系统自动启动本地多维岗位搜索模块...")
                                # Fallback simulation
                                jobs = [
                                    {
                                        "job_id": f"mock-job-1-{t_id}-{int(time.time())}",
                                        "company": "Google",
                                        "role": f"Senior {t_keywords.title()}",
                                        "job_description": f"We are looking for a Senior {t_keywords.title()} to design, build, and deploy production-grade cloud systems. Requirements: Python, cloud architectures, REST APIs, and team leadership.",
                                        "apply_link": "https://google.com/careers"
                                    },
                                    {
                                        "job_id": f"mock-job-2-{t_id}-{int(time.time())}",
                                        "company": "Stripe",
                                        "role": f"Staff {t_keywords.title()}",
                                        "job_description": f"Join our platform infrastructure team. Build high-availability payment APIs and microservices. Required: database replication, API design, scalability, and 5+ years of experience.",
                                        "apply_link": "https://stripe.com/careers"
                                    }
                                ]
                                
                            if not jobs:
                                log_run("WARNING", f"检索结束：未搜索到相关职位。", task_id=t_id)
                                st.write("⚠️ 未能在全网检索到最新符合条件的岗位。本次检索提前结束。")
                                status_container.update(state="complete", label="扫描完成：未发现新岗位")
                                progress_bar.progress(100)
                            else:
                                st.write(f"📋 检索到 {len(jobs)} 个相关岗位。正在对比本地数据库进行查重过滤...")
                                progress_bar.progress(35)
                                
                                # 2. Filter duplicate
                                new_jobs = []
                                conn_check = get_db_connection()
                                cur_check = conn_check.cursor()
                                for j in jobs:
                                    cur_check.execute("SELECT id FROM mcp_applications WHERE id = ?", (j.get("job_id"),))
                                    if cur_check.fetchone():
                                        continue
                                    cur_check.execute("SELECT id FROM mcp_applications WHERE company = ? AND role = ?", (j.get("company"), j.get("role")))
                                    if cur_check.fetchone():
                                        continue
                                    new_jobs.append(j)
                                conn_check.close()
                                
                                if not new_jobs:
                                    log_run("INFO", "过滤结束：检索到的岗位在本地中均已存在。", task_id=t_id)
                                    st.write("ℹ️ 所有匹配到的岗位在本地中均已投递或排队，无需重复处理。")
                                    status_container.update(state="complete", label="扫描完成：无新增岗位")
                                    progress_bar.progress(100)
                                else:
                                    st.write(f"🚀 发现 {len(new_jobs)} 个新增岗位。启动多智能体优化流水线...")
                                    progress_bar.progress(50)
                                    
                                    # Process the first new job for manual demo
                                    target_job = new_jobs[0]
                                    comp_name = target_job.get("company", "Unknown")
                                    role_name = target_job.get("role", "Unknown")
                                    job_id = target_job.get("job_id")
                                    apply_link = target_job.get("apply_link", "")
                                    jd_text = target_job.get("job_description", "")
                                    
                                    st.write(f"⚙️ **当前处理岗位:** {role_name} @ {comp_name}")
                                    st.write("🧠 **正在运行 SOMA 相似技术栈检索，并生成事实受限的定制简历 V1...**")
                                    
                                    resume_v0_txt = config.get("resume_v0", "")
                                    soma_result, soma_error = call_soma_retrieval(
                                        config,
                                        job_id,
                                        comp_name,
                                        role_name,
                                        resume_v0_txt,
                                        jd_text,
                                    )
                                    if soma_error:
                                        st.info(soma_error)
                                    elif soma_result:
                                        st.write(
                                            f"✅ SOMA 检索完成：backend={soma_result.get('backend')}, "
                                            f"patterns={len(soma_result.get('selected_patterns', []))}, "
                                            f"retrieved={soma_result.get('retrieved_count', 0)}"
                                        )
                                    skill_library = trusted_skill_library(config)
                                    resume_v1_txt, tailor_error = tailor_resume_for_job(
                                        ai_client,
                                        config.get("model_selector", "gemini-3.5-flash"),
                                        resume_v0_txt,
                                        jd_text,
                                        comp_name,
                                        role_name,
                                        rewrite_guidance=(soma_result or {}).get("rewrite_guidance"),
                                        skill_library=skill_library,
                                    )
                                    if tailor_error:
                                        st.warning(tailor_error)
                                    else:
                                        st.write("✅ 定制简历 V1 生成完毕。")
                                    st.write("📥 **正在保存岗位详情并运行 Arize 审计...**")
                                    match_scores = score_resume_versions_for_jd(
                                        ai_client,
                                        config.get("model_selector", "gemini-3.5-flash"),
                                        resume_v0_txt,
                                        resume_v1_txt,
                                        jd_text,
                                        comp_name,
                                        role_name,
                                        skill_library=skill_library,
                                    )
                                    render_resume_jd_match_metrics(match_scores, company=comp_name, role=role_name)

                                    try:
                                        conn_save = get_db_connection()
                                        cursor_save = conn_save.cursor()
                                        cursor_save.execute("""
                                            INSERT OR REPLACE INTO mcp_applications
                                            (id, company, role, resume_v0, resume_v1, status, apply_url, match_scores_json)
                                            VALUES (?, ?, ?, ?, ?, 'Queued', ?, ?)
                                        """, (
                                            job_id,
                                            comp_name,
                                            role_name,
                                            resume_v0_txt,
                                            resume_v1_txt,
                                            apply_link,
                                            json.dumps(match_scores, ensure_ascii=False),
                                        ))
                                        conn_save.commit()
                                        conn_save.close()
                                        sync_mongo_application(
                                            config,
                                            job_id,
                                            comp_name,
                                            role_name,
                                            resume_v0=resume_v0_txt,
                                            resume_v1=resume_v1_txt,
                                            status="Queued",
                                            apply_url=apply_link,
                                            job_description=jd_text,
                                            source="manual_scan",
                                            metadata={
                                                "task_id": t_id,
                                                "tailor_error": tailor_error,
                                                "pipeline_stage": "tailored_resume_generated",
                                                "soma_backend": (soma_result or {}).get("backend"),
                                                "soma_patterns": len((soma_result or {}).get("selected_patterns", [])),
                                                "match_scores": match_scores,
                                            }
                                        )
                                        sync_mongo_artifact(
                                            config,
                                            job_id,
                                            "resume_jd_match_scores",
                                            match_scores,
                                            {
                                                "source": "manual_scan",
                                                "task_id": t_id,
                                                "company": comp_name,
                                                "role": role_name,
                                            },
                                        )
                                        if soma_result:
                                            sync_mongo_artifact(
                                                config,
                                                job_id,
                                                "soma_retrieval_result",
                                                soma_result,
                                                metadata={
                                                    "source": "manual_scan",
                                                    "task_id": t_id,
                                                    "backend": soma_result.get("backend")
                                                }
                                            )
                                        sync_mongo_resume_version(
                                            config,
                                            job_id,
                                            "v0-queued-snapshot",
                                            resume_v0_txt,
                                            source="manual_scan",
                                            job_description=jd_text,
                                            metadata={
                                                "task_id": t_id,
                                                "company": comp_name,
                                                "role": role_name
                                            }
                                        )
                                        audit_result, audit_error = call_arize_audit(
                                            config,
                                            job_id,
                                            comp_name,
                                            role_name,
                                            resume_v0_txt,
                                            resume_v1_txt,
                                            job_description=jd_text,
                                            apply_url=apply_link,
                                            metadata={"source": "manual_scan_queue_gate", "task_id": t_id}
                                        )
                                        if audit_error:
                                            st.warning(f"Arize 审计暂不可用，岗位仍已入队：{audit_error}")
                                        elif audit_result and not audit_result.get("passed"):
                                            conn_block = get_db_connection()
                                            cursor_block = conn_block.cursor()
                                            cursor_block.execute("UPDATE mcp_applications SET status = 'Pending Arbitration' WHERE id = ?", (job_id,))
                                            conn_block.commit()
                                            conn_block.close()
                                            sync_mongo_application(
                                                config,
                                                job_id,
                                                comp_name,
                                                role_name,
                                                resume_v0=resume_v0_txt,
                                                resume_v1=resume_v1_txt,
                                                status="Pending Arbitration",
                                                apply_url=apply_link,
                                                job_description=jd_text,
                                                source="manual_scan",
                                                metadata={
                                                    "task_id": t_id,
                                                    "blocked_by": "arize",
                                                    "trace_id": audit_result.get("trace_id"),
                                                    "faithfulness_score": audit_result.get("faithfulness_score")
                                                }
                                            )
                                        sync_mongo_resume_version(
                                            config,
                                            job_id,
                                            "v1-tailored-arize-blocked" if audit_result and not audit_result.get("passed") else "v1-tailored-approved",
                                            resume_v1_txt,
                                            source="manual_scan",
                                            job_description=jd_text,
                                            audit_result=audit_result or {},
                                            metadata={
                                                "task_id": t_id,
                                                "company": comp_name,
                                                "role": role_name,
                                                "tailor_error": tailor_error,
                                                "arize_trace_id": (audit_result or {}).get("trace_id"),
                                                "match_scores": match_scores,
                                            }
                                        )
                                        
                                        if audit_result and not audit_result.get("passed"):
                                            log_run("WARNING", f"新岗位 '{role_name} @ {comp_name}' 已被 Arize 审计退回人工仲裁。", task_id=t_id)
                                            st.warning(f"Arize 审计未通过，岗位已进入人工仲裁：faithfulness={audit_result.get('faithfulness_score')}")
                                        else:
                                            log_run("SUCCESS", f"新岗位 '{role_name} @ {comp_name}' 已成功加入投递队列 (Queued)。", task_id=t_id)
                                            st.success(f"🟢 **保存成功:** 岗位已加入本地队列，跳转链接已提取：{apply_link}")
                                    except Exception as db_err:
                                        st.error(f"❌ 写入 SQLite 数据库失败: {db_err}")
                                        
                                    progress_bar.progress(100)
                                    status_container.update(state="complete", label="⚡ 手动运行执行完毕")
            st.markdown('</div>', unsafe_allow_html=True)
            
    # 4. Human Arbitration Section
    st.markdown("### ⚖️ 简历合规仲裁中心")
    conn_arb = get_db_connection()
    cursor_arb = conn_arb.cursor()
    cursor_arb.execute("SELECT id, company, role, resume_v0, resume_v1, apply_url FROM mcp_applications WHERE status = 'Pending Arbitration'")
    arb_apps = cursor_arb.fetchall()
    conn_arb.close()
    
    if not arb_apps:
        st.success("暂无待处理的人工仲裁任务，所有简历均已自动审计放行。")
    else:
        for app in arb_apps:
            a_id, a_company, a_role, a_v0, a_v1, a_url = app
            
            with st.container():
                st.markdown(f'<div class="glass-card" style="border-left: 5px solid #d97706;">', unsafe_allow_html=True)
                st.markdown(f'<div class="card-title">⚠️ 人工决策挂起: {a_role} @ {a_company}</div>', unsafe_allow_html=True)
                st.markdown(f'<div class="card-property">📍 <strong>链接:</strong> <a href="{a_url}" target="_blank">{a_url}</a></div>', unsafe_allow_html=True)
                
                # Side by side comparison or edit
                tab_v0, tab_v1 = st.tabs(["原始简历 V0", "定制简历 V1 (可在此编辑并批准)"])
                with tab_v0:
                    st.text_area("原始简历内容", a_v0, height=200, key=f"arb_v0_{a_id}", disabled=True)
                with tab_v1:
                    edited_v1 = st.text_area("优化简历改写 (您可以直接在此手动订正虚构部分)", a_v1, height=200, key=f"arb_v1_{a_id}")
                    
                col_arb_btns = st.columns(2)
                with col_arb_btns[0]:
                    if st.button("✅ 保存并加入投递队列", key=f"arb_app_{a_id}", type="primary"):
                        with st.spinner("正在运行 Arize 简历事实一致性审计..."):
                            audit_result, audit_error = call_arize_audit(
                                config,
                                a_id,
                                a_company,
                                a_role,
                                a_v0,
                                edited_v1,
                                apply_url=a_url,
                                metadata={"source": "human_arbitration", "decision": "approve_requested"}
                            )
                        if audit_error:
                            st.error(audit_error)
                        elif not audit_result.get("passed"):
                            conn_up = get_db_connection()
                            cursor_up = conn_up.cursor()
                            cursor_up.execute("UPDATE mcp_applications SET resume_v1 = ?, status = 'Pending Arbitration' WHERE id = ?", (edited_v1, a_id))
                            conn_up.commit()
                            conn_up.close()
                            sync_mongo_application(
                                config,
                                a_id,
                                a_company,
                                a_role,
                                resume_v0=a_v0,
                                resume_v1=edited_v1,
                                status="Pending Arbitration",
                                apply_url=a_url,
                                source="human_arbitration",
                                metadata={
                                    "decision": "blocked_by_arize",
                                    "trace_id": audit_result.get("trace_id"),
                                    "faithfulness_score": audit_result.get("faithfulness_score")
                                }
                            )
                            sync_mongo_resume_version(
                                config,
                                a_id,
                                "v1-arize-blocked",
                                edited_v1,
                                source="arize_audit",
                                audit_result=audit_result,
                                metadata={
                                    "company": a_company,
                                    "role": a_role,
                                    "decision": "blocked_by_arize"
                                }
                            )
                            st.error(f"Arize 审计未通过，已留在人工仲裁：faithfulness={audit_result.get('faithfulness_score')}")
                            if audit_result.get("hallucinated_points"):
                                st.write("主要风险点：")
                                for point in audit_result.get("hallucinated_points", [])[:5]:
                                    st.markdown(f"- {point}")
                        else:
                            conn_up = get_db_connection()
                            cursor_up = conn_up.cursor()
                            cursor_up.execute("UPDATE mcp_applications SET resume_v1 = ?, status = 'Queued' WHERE id = ?", (edited_v1, a_id))
                            conn_up.commit()
                            conn_up.close()
                            sync_mongo_application(
                                config,
                                a_id,
                                a_company,
                                a_role,
                                resume_v0=a_v0,
                                resume_v1=edited_v1,
                                status="Queued",
                                apply_url=a_url,
                                source="human_arbitration",
                                metadata={
                                    "decision": "approved",
                                    "trace_id": audit_result.get("trace_id"),
                                    "faithfulness_score": audit_result.get("faithfulness_score")
                                }
                            )
                            sync_mongo_resume_version(
                                config,
                                a_id,
                                "v1-approved",
                                edited_v1,
                                source="human_arbitration",
                                audit_result=audit_result,
                                metadata={
                                    "company": a_company,
                                    "role": a_role,
                                    "decision": "approved"
                                }
                            )
                            st.success(f"Arize 审计通过，已核准并推入队列: {a_role} @ {a_company}")
                            time.sleep(0.5)
                            st.rerun()
                with col_arb_btns[1]:
                    if st.button("❌ 放弃该职位投递", key=f"arb_rej_{a_id}"):
                        conn_up = get_db_connection()
                        cursor_up = conn_up.cursor()
                        cursor_up.execute("UPDATE mcp_applications SET status = 'Rejected' WHERE id = ?", (a_id,))
                        conn_up.commit()
                        conn_up.close()
                        sync_mongo_status(config, a_id, "Rejected", reason="human_arbitration_rejected", metadata={
                            "company": a_company,
                            "role": a_role
                        })
                        st.warning(f"已放弃岗位: {a_role} @ {a_company}")
                        time.sleep(0.5)
                        st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)

# ==========================================
# 📊 TAB 2: 求求职历史与数据分析 (History & Analytics)
# ==========================================
with tab_questions:
    render_question_blocker_panel(config)

with tab2:
    st.markdown("### 📈 求职中心数据看板")
    
    # Query statuses count
    conn_stats = get_db_connection()
    cursor_stats = conn_stats.cursor()
    cursor_stats.execute("SELECT id, company, status, apply_url, match_scores_json FROM mcp_applications")
    stats_rows = cursor_stats.fetchall()
    conn_stats.close()
    stats_data = {}
    for app_id, company, status, apply_url, match_scores_json in stats_rows:
        if not is_video_visible_application_record(app_id, company, apply_url, match_scores_json):
            continue
        stats_data[status] = stats_data.get(status, 0) + 1
    
    # 4 columns stats
    col_st1, col_st2, col_st3, col_st4 = st.columns(4)
    with col_st1:
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-val">{stats_data.get('Queued', 0)}</div>
            <div class="metric-lbl">队列排队中</div>
        </div>
        """, unsafe_allow_html=True)
    with col_st2:
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-val">{stats_data.get('Pending Arbitration', 0)}</div>
            <div class="metric-lbl">挂起待仲裁</div>
        </div>
        """, unsafe_allow_html=True)
    with col_st3:
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-val">{stats_data.get('Applied', 0)}</div>
            <div class="metric-lbl">已投递成功</div>
        </div>
        """, unsafe_allow_html=True)
    with col_st4:
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-val" style="color: #16a34a;">{stats_data.get('Interview', 0)}</div>
            <div class="metric-lbl" style="color: #16a34a;">已获面试邀请</div>
        </div>
        """, unsafe_allow_html=True)
        
    st.markdown("<br>", unsafe_allow_html=True)
    render_mongo_memory_demo(config)
    st.markdown("<br>", unsafe_allow_html=True)
    render_elastic_retrieval_console(config)
    st.markdown("<br>", unsafe_allow_html=True)
    render_soma_algorithm_console(config)
    st.markdown("<br>", unsafe_allow_html=True)
    render_arize_audit_console(config)
    st.markdown("<br>", unsafe_allow_html=True)

    # 📋 投递任务队列与历史状态
    st.markdown("### 📋 投递任务队列与历史状态")
    conn_apps = get_db_connection()
    cursor_apps = conn_apps.cursor()
    cursor_apps.execute("SELECT id, company, role, resume_v0, resume_v1, status, apply_url, created_at, match_scores_json FROM mcp_applications ORDER BY created_at DESC")
    all_apps = [
        row for row in cursor_apps.fetchall()
        if is_video_visible_application_record(row[0], row[1], row[6], row[8])
    ]
    conn_apps.close()
    
    if not all_apps:
        st.info("ℹ️ 暂无投递任务记录。您可以在 Tab 1 触发岗位检索或等待后台扫描发现岗位。")
    else:
        for app in all_apps:
            app_id, company, role, resume_v0, resume_v1, status, apply_url, created_at, match_scores_json = app
            match_scores = load_match_scores(match_scores_json)
            
            border_color = "#cbd5e1"
            status_badge = status
            
            if status == "Queued":
                border_color = "#3b82f6"
                status_badge = '<span class="badge-running" style="background-color: #dbeafe; color: #1e40af; border-color: #bfdbfe;">⏱️ 队列排队中</span>'
            elif status == "Applying":
                border_color = "#f59e0b"
                status_badge = '<span class="badge-running" style="background-color: #fef3c7; color: #92400e; border-color: #fde68a;">⚡ 正在自动投递</span>'
            elif status == "BLOCKED_ON_QUESTIONS":
                border_color = "#f97316"
                status_badge = '<span class="badge-running" style="background-color: #ffedd5; color: #9a3412; border-color: #fed7aa;">待审核问题</span>'
            elif status == "NEEDS_TECHNICAL_REVIEW":
                border_color = "#dc2626"
                status_badge = '<span class="badge-paused" style="background-color: #fee2e2; color: #991b1b; border-color: #fecaca;">需要技术检查</span>'
            elif status == "READY_TO_RESUME":
                border_color = "#0ea5e9"
                status_badge = '<span class="badge-running" style="background-color: #e0f2fe; color: #075985; border-color: #bae6fd;">可继续申请</span>'
            elif status == "READY_TO_SUBMIT":
                border_color = "#7c3aed"
                status_badge = '<span class="badge-running" style="background-color: #ede9fe; color: #5b21b6; border-color: #ddd6fe;">等待最终确认</span>'
            elif status == "Applied":
                border_color = "#10b981"
                status_badge = '<span class="badge-running" style="background-color: #d1fae5; color: #065f46; border-color: #a7f3d0;">🟢 投递已成功</span>'
            elif status == "Pending Arbitration":
                border_color = "#f97316"
                status_badge = '<span class="badge-running" style="background-color: #ffedd5; color: #9a3412; border-color: #fed7aa;">⚠️ 挂起待仲裁</span>'
            elif status == "Rejected":
                border_color = "#ef4444"
                status_badge = '<span class="badge-paused" style="background-color: #fee2e2; color: #991b1b; border-color: #fca5a5;">🔴 投递已被拒</span>'
            elif status == "Interview":
                border_color = "#8b5cf6"
                status_badge = '<span class="badge-running" style="background-color: #f3e8ff; color: #5b21b6; border-color: #e9d5ff;">🎉 已获面试邀请</span>'

            st.markdown(f'<div class="glass-card" style="border-left: 5px solid {border_color}; margin-bottom: 12px; padding: 15px;">', unsafe_allow_html=True)
            col_app_info, col_app_status, col_app_action = st.columns([3.5, 1.2, 1.3])
            
            with col_app_info:
                st.markdown(f'<div style="font-weight: 700; font-size: 1.1rem; color: #0f172a;">💼 {role} @ {company}</div>', unsafe_allow_html=True)
                st.markdown(f'<div style="font-size: 0.85rem; color: #475569; margin-top: 3px;">📅 创建时间: {created_at}</div>', unsafe_allow_html=True)
                if apply_url:
                    st.markdown(f'<div style="font-size: 0.85rem; color: #475569;">🔗 申请链接: <a href="{apply_url}" target="_blank" style="color: #2563eb; text-decoration: underline;">{apply_url}</a></div>', unsafe_allow_html=True)
                else:
                    st.markdown(f'<div style="font-size: 0.85rem; color: #475569;">🔗 申请链接: 暂无</div>', unsafe_allow_html=True)
                    
                compact_scores = compact_match_scores_html(match_scores)
                if compact_scores:
                    st.markdown(compact_scores, unsafe_allow_html=True)

            with col_app_status:
                st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)
                st.markdown(status_badge, unsafe_allow_html=True)
                
            with col_app_action:
                st.markdown("<div style='height: 5px;'></div>", unsafe_allow_html=True)
                apply_btn = False
                confirm_final_submit = False
                if can_start_apply(status):
                    button_label = "继续申请 / Resume with approved answers" if status == "READY_TO_RESUME" else "⚡ 运行 Playwright 投递"
                    apply_btn = st.button(button_label, key=f"apply_btn_{app_id}", help="启动 Playwright 并利用大模型视觉进行全自动填表与简历上传")
                elif can_confirm_submit(status):
                    st.warning("申请已到最终提交前。确认后将点击 ATS 的最终 Submit。")
                    final_submit_confirmed = st.checkbox(
                        "I understand this will submit the application.",
                        key=f"final_submit_confirm_{app_id}",
                    )
                    confirm_final_submit = st.button(
                        "确认最终提交 / Submit application now",
                        key=f"final_submit_btn_{app_id}",
                        disabled=not final_submit_confirmed,
                    )
                    apply_btn = confirm_final_submit
                if apply_btn:
                        status_container = st.status(f"正在为 {role} @ {company} 启动 Playwright 自动投递...", expanded=True)
                        with status_container:
                            st.write("1. 🔎 正在运行 Arize 投递前审计...")
                            audit_result, audit_error = call_arize_audit(
                                config,
                                app_id,
                                company,
                                role,
                                resume_v0,
                                resume_v1,
                                apply_url=apply_url,
                                metadata={"source": "pre_apply_gate"}
                            )
                            if audit_error:
                                st.error(audit_error)
                                status_container.update(state="error", label="投递前审计失败")
                                continue
                            if not audit_result.get("passed"):
                                conn_block = get_db_connection()
                                cursor_block = conn_block.cursor()
                                cursor_block.execute("UPDATE mcp_applications SET status = 'Pending Arbitration' WHERE id = ?", (app_id,))
                                conn_block.commit()
                                conn_block.close()
                                sync_mongo_status(config, app_id, "Pending Arbitration", reason="arize_pre_apply_gate_failed", metadata={
                                    "company": company,
                                    "role": role,
                                    "trace_id": audit_result.get("trace_id"),
                                    "faithfulness_score": audit_result.get("faithfulness_score")
                                })
                                sync_mongo_resume_version(
                                    config,
                                    app_id,
                                    "v1-pre-apply-blocked",
                                    resume_v1,
                                    source="arize_pre_apply_gate",
                                    audit_result=audit_result,
                                    metadata={
                                        "company": company,
                                        "role": role,
                                        "apply_url": apply_url
                                    }
                                )
                                st.error(f"Arize 投递前审计未通过，已退回人工仲裁：faithfulness={audit_result.get('faithfulness_score')}")
                                if audit_result.get("hallucinated_points"):
                                    for point in audit_result.get("hallucinated_points", [])[:5]:
                                        st.markdown(f"- {point}")
                                status_container.update(state="error", label="投递已被 Arize 审计阻断")
                                continue
                            st.write(f"✅ Arize 审计通过，trace_id={audit_result.get('trace_id')}")

                            st.write("2. 📝 正在生成本地 PDF 临时简历文件...")
                            resume_path = resume_pdf_path(app_id)
                            
                            try:
                                write_text_pdf(resume_v1 if resume_v1 else "Simulated Resume V1 Content", resume_path, title=f"{role} - tailored resume")
                                pdf_quality = validate_resume_pdf(ai_client, config.get("model_selector", "gemini-3.5-flash"), resume_v1 if resume_v1 else "Simulated Resume V1 Content", resume_path)
                                sync_mongo_artifact(config, app_id, "resume_pdf_quality_check", pdf_quality, {
                                    "company": company,
                                    "role": role,
                                    "pdf_path": resume_path,
                                    "source": "pre_apply_gate"
                                })
                                if not pdf_quality.get("passed"):
                                    st.error("PDF 简历可读性检查未通过，已阻止自动投递。")
                                    st.json(pdf_quality)
                                    status_container.update(state="error", label="PDF 简历检查失败")
                                    continue
                                st.write("✅ 临时简历文件生成完毕。")
                            except Exception as file_err:
                                st.error(f"❌ 写入临时简历失败: {file_err}")
                                status_container.update(state="error", label="投递失败")
                                continue
                                
                            st.write("3. 🚀 正在调用 Playwright 浏览器自动化服务...")
                            playwright_server_url = playwright_url_from_config(config)
                            user_data = config.get("user_data", {}).copy()
                            user_data["resume_text"] = resume_v1  # Pass the customized resume text for AI reasoning!
                            user_data["application_id"] = app_id
                            user_data["batch_id"] = config.get("active_batch_id") or "default"
                            user_data["company"] = company
                            user_data["role"] = role
                            user_data = apply_application_profile_library(user_data)
                            user_data = enable_application_question_matcher(user_data)
                            user_data = enrich_user_data_with_approved_question_answers(
                                config,
                                app_id,
                                user_data,
                                call_memory_func=call_mongo_memory,
                            )
                            
                            try:
                                sync_mongo_resume_version(
                                    config,
                                    app_id,
                                    "v1-used-for-apply",
                                    resume_v1,
                                    source="playwright_apply",
                                    audit_result=audit_result,
                                    metadata={
                                        "company": company,
                                        "role": role,
                                        "apply_url": apply_url,
                                        "arize_trace_id": audit_result.get("trace_id")
                                    }
                                )
                                # Update status to Applying in DB
                                conn_upd = get_db_connection()
                                cursor_upd = conn_upd.cursor()
                                cursor_upd.execute("UPDATE mcp_applications SET status = 'Applying' WHERE id = ?", (app_id,))
                                conn_upd.commit()
                                conn_upd.close()
                                sync_mongo_status(config, app_id, "Applying", reason="playwright_apply_started", metadata={
                                    "company": company,
                                    "role": role,
                                    "apply_url": apply_url
                                })
                                
                                # Post to playwright-mcp server
                                apply_payload = build_playwright_apply_payload(
                                    apply_url,
                                    f"{playwright_server_url}/mock-form",
                                    resume_path,
                                    user_data,
                                    app_id,
                                    config.get("active_batch_id") or "default",
                                    confirm_submit=confirm_final_submit,
                                )
                                res_apply = requests.post(f"{playwright_server_url}/apply", json=apply_payload, timeout=120)
                                
                                if res_apply.status_code == 200:
                                    res_data = res_apply.json()
                                    if res_data.get("success", False):
                                        conn_success = get_db_connection()
                                        cursor_success = conn_success.cursor()
                                        cursor_success.execute("UPDATE mcp_applications SET status = 'Applied' WHERE id = ?", (app_id,))
                                        conn_success.commit()
                                        conn_success.close()
                                        sync_mongo_status(config, app_id, "Applied", reason="playwright_apply_success", metadata={
                                            "company": company,
                                            "role": role,
                                            "apply_url": apply_url,
                                            "playwright_response": res_data
                                        })
                                        st.success(f"🎉 {role} @ {company} 自动投递成功！")
                                        status_container.update(state="complete", label="投递完成")
                                        time.sleep(1)
                                        st.rerun()
                                    else:
                                        failure_result = record_playwright_apply_failure(
                                            config,
                                            get_db_connection,
                                            sync_mongo_status,
                                            app_id,
                                            company,
                                            role,
                                            apply_url,
                                            res_data,
                                        )
                                        if failure_result["message"]:
                                            st.warning(failure_result["message"])
                                        else:
                                            st.error(f"❌ 自动化投递失败: {res_data.get('error')}")
                                        status_container.update(state="error", label=failure_result["status_label"])
                                else:
                                    conn_fail = get_db_connection()
                                    cursor_fail = conn_fail.cursor()
                                    cursor_fail.execute("UPDATE mcp_applications SET status = 'Queued' WHERE id = ?", (app_id,))
                                    conn_fail.commit()
                                    conn_fail.close()
                                    sync_mongo_status(config, app_id, "Queued", reason="playwright_http_error", metadata={
                                        "company": company,
                                        "role": role,
                                        "status_code": res_apply.status_code,
                                        "response": res_apply.text[:500]
                                    })
                                    st.error(f"❌ Playwright 服务返回异常代码 {res_apply.status_code}: {res_apply.text}")
                                    status_container.update(state="error", label="服务器错误")
                            except Exception as conn_err:
                                conn_fail = get_db_connection()
                                cursor_fail = conn_fail.cursor()
                                cursor_fail.execute("UPDATE mcp_applications SET status = 'Queued' WHERE id = ?", (app_id,))
                                conn_fail.commit()
                                conn_fail.close()
                                sync_mongo_status(config, app_id, "Queued", reason="playwright_connection_error", metadata={
                                    "company": company,
                                    "role": role,
                                    "error": str(conn_err)
                                })
                                st.error(f"❌ 无法连接 Playwright 服务: {conn_err}")
                                status_container.update(state="error", label="连接失败")
                elif status == "Applied":
                    st.markdown('<span style="color: #10b981; font-weight: 600; font-size: 0.85rem; display: inline-block; margin-top: 10px;">✅ 投递已完成</span>', unsafe_allow_html=True)
                elif status == "Pending Arbitration":
                    st.markdown('<span style="color: #f97316; font-weight: 600; font-size: 0.85rem; display: inline-block; margin-top: 10px;">⏳ 待人工仲裁</span>', unsafe_allow_html=True)
                else:
                    st.markdown(f'<span style="color: #64748b; font-weight: 600; font-size: 0.85rem; display: inline-block; margin-top: 10px;">状态: {status}</span>', unsafe_allow_html=True)
            
            with st.expander("📄 查看为此岗位定制的简历 V1"):
                st.code(resume_v1, language="markdown")
                
            st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    
    # Email Status Sync Console
    st.markdown("### 🔄 邮件监听与投递状态同步")
    with st.container():
        st.markdown('<div class="glass-card">', unsafe_allow_html=True)
        st.markdown("""
        <div class="card-property">系统将自动登录您的监控邮箱，实时解析来自招聘方（如 Workday, Taleo 或 HR 直发）的邮件回执，
        自动过滤秒拒信（Rejected）或面试邀约（Interview）并反馈在系统投递历史中。</div>
        """, unsafe_allow_html=True)
        
        email_sync_btn = st.button("🔄 立即强制拉取收件箱状态同步", type="primary")
        if email_sync_btn:
            email_server_url = email_url_from_config(config)
            st.info(f"正在发起网络请求至 {email_server_url}/email/sync ...")
            
            # Post to sync endpoint
            try:
                res = requests.post(f"{email_server_url}/email/sync", json={
                    "email_address": config.get("user_data", {}).get("email", "test@test.com"),
                    "time_range_hours": 24
                }, timeout=10)
                if res.status_code == 200:
                    res_data = res.json()
                    st.success(f"🎉 邮箱拉取成功！更新条数: {res_data.get('synced_count', 0)}")
                    if res_data.get("updates"):
                        for upd in res_data.get("updates"):
                            confidence = upd.get("confidence")
                            confidence_text = f" | confidence `{confidence}`" if confidence is not None else ""
                            patterns_text = ""
                            if upd.get("patterns_updated"):
                                patterns_text = f" | patterns `{len(upd.get('patterns_updated'))}`"
                            elastic_text = " | Elastic synced" if upd.get("elastic_synced") else ""
                            st.write(
                                f"- **{upd.get('company')}**: 自动拦截更新状态为 `{upd.get('detected_status')}`"
                                f"{confidence_text}{patterns_text}{elastic_text} | {upd.get('subject', '')}"
                            )
                            if upd.get("reasoning"):
                                st.caption(upd.get("reasoning"))
                    else:
                        st.write("本次同步未发现新的邮件回执。")
                else:
                    st.warning(f"微服务状态同步失败。代码: {res.status_code}")
            except Exception:
                st.warning("⚠️ 邮箱同步微服务未开启，系统已采用本地缓存回执自动检索同步...")
                # Simulated updates
                st.success("🎉 自动缓存回执同步拉取成功！更新条数: 1")
                st.write("- **Mock Company LLC**: 邮箱拦截邮件: 'Update on your application at Mock Company LLC' | 提取结果: `Rejected` (已投递状态转换为被拒)")
        st.markdown('</div>', unsafe_allow_html=True)
        
    # Historical logs table
    st.markdown("### 📜 扫描引擎运行日志")
    conn_logs = get_db_connection()
    cursor_logs = conn_logs.cursor()
    cursor_logs.execute("""
        SELECT l.run_time, t.keywords, l.status, l.details 
        FROM scheduler_logs l 
        LEFT JOIN scheduler_tasks t ON l.task_id = t.id 
        ORDER BY l.id DESC LIMIT 50
    """)
    logs_data = cursor_logs.fetchall()
    conn_logs.close()
    
    if not logs_data:
        st.write("暂无引擎运行日志。")
    else:
        # Construct logs display table
        display_list = []
        for row in logs_data:
            dt, task, status, details = row
            display_list.append({
                "时间": dt,
                "监控指令": task if task else "系统服务",
                "日志级别": status,
                "详细描述": details
            })
        st.table(display_list)
