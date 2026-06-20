import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, UpdateOne
from pymongo.errors import PyMongoError
import uvicorn

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from application_questions import (
    APPLICATION_WORKFLOW_STATUSES,
    APPLIED,
    APPROVED,
    BLOCKED_ON_QUESTIONS,
    DISMISSED,
    NEEDS_TECHNICAL_REVIEW,
    READY_TO_RESUME,
    READY_TO_SUBMIT,
    SUBMITTED,
    SUBMITTING,
    SUBMIT_UNKNOWN,
    TECHNICAL_REVIEW,
    UNANSWERED,
    fingerprint_question,
    normalize_options,
    normalize_question_text,
    options_compatible,
)

app = FastAPI(title="MongoDB Application Memory Server", version="1.1.0")

def load_env_manually():
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for env_file in [".env", ".env.local"]:
        env_path = os.path.join(base_dir, env_file)
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            os.environ[key.strip()] = value.strip().strip('"').strip("'")
            except Exception as err:
                print(f"Failed to parse {env_file}: {err}")

load_env_manually()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_CONFIGURED = bool(MONGO_URI)
mongo_startup_error = ""
DB_NAME = os.getenv("MONGO_DB_NAME", "jobs_hunter")
sqlite_db_path = os.path.join(os.path.dirname(__file__), "..", "data", "applications.db")

ALLOWED_STATUSES = {
    "Pending",
    "Pending Arbitration",
    "Queued",
    "Applying",
    "Applied",
    "Interview",
    "Rejected",
    "Failed",
} | APPLICATION_WORKFLOW_STATUSES

mongo_client = None


def connect_mongodb():
    global mongo_startup_error
    if not MONGO_URI:
        return None

    retries = int(os.getenv("MONGO_CONNECT_RETRIES", "3"))
    delay_seconds = float(os.getenv("MONGO_CONNECT_RETRY_DELAY_SECONDS", "2"))
    server_timeout_ms = int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "20000"))
    socket_timeout_ms = int(os.getenv("MONGO_SOCKET_TIMEOUT_MS", "10000"))

    for attempt in range(1, retries + 1):
        try:
            client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=server_timeout_ms,
                connectTimeoutMS=socket_timeout_ms,
                socketTimeoutMS=socket_timeout_ms,
            )
            client.admin.command("ping")
            mongo_startup_error = ""
            print(f"Connected to MongoDB successfully on attempt {attempt}.")
            return client
        except Exception as e:
            mongo_startup_error = str(e)
            print(
                f"MongoDB connection attempt {attempt}/{retries} failed: {e}. "
                "MongoDB mode is required; SQLite fallback is disabled."
            )
            if attempt < retries:
                time.sleep(delay_seconds)
    return None


def ensure_mongo_client():
    global mongo_client
    if MONGO_CONFIGURED and not mongo_client:
        mongo_client = connect_mongodb()
    return mongo_client


if MONGO_URI:
    mongo_client = connect_mongodb()


class SaveAppRequest(BaseModel):
    company: str
    role: str
    resume_v0: str
    resume_v1: Optional[str] = None
    status: str = "Pending"
    apply_url: Optional[str] = None
    job_description: Optional[str] = None
    source: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class UpsertApplicationRequest(SaveAppRequest):
    id: Optional[str] = None


class UpdateStatusRequest(BaseModel):
    status: str
    reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EventRequest(BaseModel):
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class ArtifactRequest(BaseModel):
    artifact_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ResumeVersionRequest(BaseModel):
    version_label: str = "v1"
    content: str
    source: Optional[str] = None
    job_description: Optional[str] = None
    audit_result: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class OutcomeRequest(BaseModel):
    outcome_label: str
    outcome_score: Optional[float] = None
    confidence: Optional[float] = None
    source: str = "manual"
    observed_at: Optional[str] = None
    message_id: Optional[str] = None
    subject: Optional[str] = None
    sender: Optional[str] = None
    patterns_used: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class QuestionBlockerCreateRequest(BaseModel):
    batch_id: Optional[str] = None
    ats: str = "unknown"
    tenant: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None
    job_url: Optional[str] = None
    page_name: Optional[str] = None
    stage: str = "unknown"
    raw_text: str
    normalized_text: Optional[str] = None
    fingerprint: Optional[str] = None
    canonical_key: Optional[str] = None
    required: bool = True
    control_type: str = "text"
    options: List[str] = Field(default_factory=list)
    validation_message: Optional[str] = None
    locator_hints: Dict[str, Any] = Field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    status: str = UNANSWERED


class QuestionBlockerApproveRequest(BaseModel):
    answer: Any
    scope: str = "application"
    approved_by: str
    batch_id: Optional[str] = None


class QuestionReviewBundleApproveRequest(BaseModel):
    answers: Dict[str, Any] = Field(default_factory=dict)
    approved_by: str


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def backend_name() -> str:
    if mongo_client:
        return "mongodb"
    if MONGO_CONFIGURED:
        return "mongodb_unavailable"
    return "sqlite"


def require_mongodb_if_configured() -> None:
    if MONGO_CONFIGURED and not ensure_mongo_client():
        detail = "MongoDB Atlas is configured but unavailable. Check Atlas Network Access/IP allowlist and restart 8001."
        if mongo_startup_error:
            detail = f"{detail} Last connection error: {mongo_startup_error[:600]}"
        raise HTTPException(status_code=503, detail=detail)


def model_dump(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def init_sqlite() -> None:
    os.makedirs(os.path.dirname(sqlite_db_path), exist_ok=True)
    conn = sqlite3.connect(sqlite_db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mcp_applications (
            id TEXT PRIMARY KEY,
            company TEXT NOT NULL,
            role TEXT NOT NULL,
            resume_v0 TEXT,
            resume_v1 TEXT,
            status TEXT NOT NULL,
            apply_url TEXT,
            job_description TEXT,
            source TEXT,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        )
    """)
    for column, ddl in [
        ("apply_url", "ALTER TABLE mcp_applications ADD COLUMN apply_url TEXT"),
        ("job_description", "ALTER TABLE mcp_applications ADD COLUMN job_description TEXT"),
        ("source", "ALTER TABLE mcp_applications ADD COLUMN source TEXT"),
        ("metadata_json", "ALTER TABLE mcp_applications ADD COLUMN metadata_json TEXT"),
        ("updated_at", "ALTER TABLE mcp_applications ADD COLUMN updated_at TEXT"),
    ]:
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS application_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS application_artifacts (
            app_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            payload_json TEXT,
            metadata_json TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (app_id, artifact_type)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS application_question_blockers (
            id TEXT PRIMARY KEY,
            batch_id TEXT,
            application_id TEXT NOT NULL,
            ats TEXT,
            tenant TEXT,
            company TEXT,
            role TEXT,
            job_url TEXT,
            page_name TEXT,
            stage TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            normalized_text TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            canonical_key TEXT,
            required INTEGER NOT NULL,
            control_type TEXT NOT NULL,
            options_json TEXT,
            validation_message TEXT,
            locator_hints_json TEXT,
            artifacts_json TEXT,
            metadata_json TEXT,
            status TEXT NOT NULL,
            approved_answer_json TEXT,
            approval_scope TEXT,
            approved_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(application_id, stage, fingerprint)
        )
    """)
    try:
        cursor.execute("ALTER TABLE application_question_blockers ADD COLUMN metadata_json TEXT")
    except sqlite3.OperationalError:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS application_resume_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT NOT NULL,
            version_label TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT,
            job_description TEXT,
            audit_result_json TEXT,
            metadata_json TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


if not mongo_client and not MONGO_CONFIGURED:
    print(f"Using SQLite database at: {sqlite_db_path}")
    init_sqlite()


def db():
    client = ensure_mongo_client()
    if not client:
        return None
    return client[DB_NAME]


def run_mongo_transaction(callback):
    require_mongodb_if_configured()
    if not mongo_client or not hasattr(mongo_client, "start_session"):
        raise HTTPException(status_code=503, detail="MongoDB transactions require a replica-set deployment with session support")
    try:
        with mongo_client.start_session() as session:
            return session.with_transaction(lambda txn_session: callback(txn_session))
    except HTTPException:
        raise
    except PyMongoError as err:
        raise HTTPException(status_code=503, detail=f"MongoDB transaction failed or is unsupported: {str(err)[:400]}")


def normalize_application(data: Dict[str, Any], app_id: Optional[str] = None) -> Dict[str, Any]:
    resolved_id = app_id or data.get("id") or str(uuid.uuid4())
    status = data.get("status") or "Pending"
    if status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    return {
        "id": resolved_id,
        "company": data.get("company") or "Unknown",
        "role": data.get("role") or "Unknown",
        "resume_v0": data.get("resume_v0") or "",
        "resume_v1": data.get("resume_v1"),
        "status": status,
        "apply_url": data.get("apply_url"),
        "job_description": data.get("job_description"),
        "source": data.get("source"),
        "metadata": data.get("metadata") or {},
        "updated_at": now_iso(),
    }


def write_event(app_id: str, event_type: str, payload: Dict[str, Any], mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> None:
    require_mongodb_if_configured()
    created_at = now_iso()
    if mongo_client:
        db().application_events.insert_one({
            "app_id": app_id,
            "event_type": event_type,
            "payload": payload or {},
            "created_at": created_at,
        }, session=mongo_session)
        return
    if sqlite_conn is not None:
        sqlite_conn.execute(
            "INSERT INTO application_events (app_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (app_id, event_type, json.dumps(payload or {}, ensure_ascii=False), created_at),
        )
        return
    conn = sqlite3.connect(sqlite_db_path)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO application_events (app_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (app_id, event_type, json.dumps(payload or {}, ensure_ascii=False), created_at),
    )
    conn.commit()
    conn.close()


def clamp_score(value: Any, default: float = 0.0) -> float:
    try:
        score = float(value)
    except Exception:
        score = default
    return round(max(0.0, min(1.0, score)), 3)


def normalize_outcome_label(label: str) -> str:
    normalized = re.sub(r"[^A-Z_]+", "_", (label or "").upper()).strip("_")
    aliases = {
        "INTERVIEW": "INTERVIEW",
        "OFFER": "OFFER",
        "ONLINE_ASSESSMENT": "ONLINE_ASSESSMENT",
        "OA": "ONLINE_ASSESSMENT",
        "RECRUITER_REPLY": "RECRUITER_REPLY",
        "REPLIED": "RECRUITER_REPLY",
        "APPLIED": "APPLIED",
        "REJECTION": "REJECTION",
        "REJECTED": "REJECTION",
        "NO_RESPONSE": "NO_RESPONSE",
    }
    return aliases.get(normalized, normalized or "UNKNOWN")


def outcome_score(label: str, explicit: Optional[float] = None) -> float:
    if explicit is not None:
        return clamp_score(explicit)
    normalized = normalize_outcome_label(label)
    return {
        "OFFER": 1.0,
        "INTERVIEW": 1.0,
        "ONLINE_ASSESSMENT": 0.7,
        "RECRUITER_REPLY": 0.5,
        "APPLIED": 0.35,
        "NO_RESPONSE": 0.2,
        "REJECTION": 0.0,
    }.get(normalized, 0.2)


def status_from_outcome(label: str) -> str:
    normalized = normalize_outcome_label(label)
    if normalized in {"OFFER", "INTERVIEW", "ONLINE_ASSESSMENT", "RECRUITER_REPLY"}:
        return "Interview"
    if normalized == "REJECTION":
        return "Rejected"
    if normalized == "APPLIED":
        return "Applied"
    return "Applied"


def extract_patterns_from_memory(app_id: str) -> List[Dict[str, Any]]:
    if mongo_client:
        artifact = db().application_artifacts.find_one({"app_id": app_id, "artifact_type": "soma_retrieval_result"}, {"_id": 0})
        payload = (artifact or {}).get("payload") or {}
        return payload.get("selected_patterns") or []

    conn = sqlite3.connect(sqlite_db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT payload_json FROM application_artifacts WHERE app_id = ? AND artifact_type = ?",
        (app_id, "soma_retrieval_result"),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return []
    try:
        payload = json.loads(row[0] or "{}")
        return payload.get("selected_patterns") or []
    except Exception:
        return []


def update_pattern_stats(patterns: List[Dict[str, Any]], outcome: Dict[str, Any], app_id: str) -> List[str]:
    updated = []
    if not patterns:
        return updated
    score = clamp_score(outcome.get("score"), 0.0)
    now = now_iso()
    for pattern in patterns:
        pattern_id = pattern.get("pattern_id")
        if not pattern_id:
            continue
        doc = {
            "pattern_id": pattern_id,
            "name": pattern.get("name") or pattern_id,
            "stack_cluster": pattern.get("stack_cluster"),
            "rewrite_instruction": pattern.get("rewrite_instruction"),
            "risk_constraints": pattern.get("risk_constraints") or [],
            "applicability_conditions": pattern.get("applicability_conditions") or {},
            "last_updated_at": now,
        }
        stats_inc = {
            "stats.attempts": 1,
            "stats.weighted_successes": score,
            "stats.weighted_failures": 1.0 - score,
            "stats.total_outcome_score": score,
            "stats.total_confidence": clamp_score(outcome.get("confidence"), 0.5),
        }
        push_event = {
            "app_id": app_id,
            "outcome_label": outcome.get("label"),
            "outcome_score": score,
            "source": outcome.get("source"),
            "observed_at": outcome.get("observed_at"),
        }
        if mongo_client:
            db().rewrite_patterns.update_one(
                {"_id": pattern_id},
                {
                    "$set": doc,
                    "$inc": stats_inc,
                    "$push": {"recent_outcomes": {"$each": [push_event], "$slice": -20}},
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
        else:
            conn = sqlite3.connect(sqlite_db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rewrite_patterns (
                    pattern_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cursor.execute("SELECT payload_json FROM rewrite_patterns WHERE pattern_id = ?", (pattern_id,))
            row = cursor.fetchone()
            payload = json.loads(row[0]) if row else {**doc, "stats": {}, "recent_outcomes": []}
            payload.update(doc)
            stats = payload.setdefault("stats", {})
            stats["attempts"] = stats.get("attempts", 0) + 1
            stats["weighted_successes"] = stats.get("weighted_successes", 0.0) + score
            stats["weighted_failures"] = stats.get("weighted_failures", 0.0) + (1.0 - score)
            stats["total_outcome_score"] = stats.get("total_outcome_score", 0.0) + score
            stats["total_confidence"] = stats.get("total_confidence", 0.0) + clamp_score(outcome.get("confidence"), 0.5)
            payload["recent_outcomes"] = (payload.get("recent_outcomes") or [])[-19:] + [push_event]
            cursor.execute(
                "INSERT OR REPLACE INTO rewrite_patterns (pattern_id, payload_json, updated_at) VALUES (?, ?, ?)",
                (pattern_id, json.dumps(payload, ensure_ascii=False), now),
            )
            conn.commit()
            conn.close()
        updated.append(pattern_id)
    return updated


question_blocker_indexes_ready = False
TERMINAL_APPLICATION_STATUSES = {READY_TO_SUBMIT, SUBMITTING, SUBMITTED, SUBMIT_UNKNOWN, "Applied", "Interview", "Rejected"}
BLOCKING_APPLICATION_STATUSES = {BLOCKED_ON_QUESTIONS, NEEDS_TECHNICAL_REVIEW}
APPROVAL_FINAL_QUESTION_STATUSES = {APPROVED, APPLIED, DISMISSED}
MAX_TEXT_ANSWER_LENGTH = 2000
CHECKBOX_TRUE_VALUES = {"true", "yes", "checked", "1", "on"}
CHECKBOX_FALSE_VALUES = {"false", "no", "unchecked", "0", "off"}
SUPPORTED_APPROVAL_CONTROL_TYPES = {"radio", "select", "checkbox", "text", "textarea"}
APPROVAL_BEFORE_READY_UPDATE_HOOK = None


def ensure_question_blocker_storage() -> None:
    global question_blocker_indexes_ready
    require_mongodb_if_configured()
    if mongo_client:
        if not question_blocker_indexes_ready:
            db().application_question_blockers.create_index(
                [("application_id", 1), ("stage", 1), ("fingerprint", 1)],
                unique=True,
                name="uniq_application_stage_fingerprint",
            )
            db().application_question_blockers.create_index([("fingerprint", 1), ("batch_id", 1), ("status", 1)])
            question_blocker_indexes_ready = True
        return
    init_sqlite()


def deterministic_blocker_id(application_id: str, stage: str, fingerprint: str) -> str:
    seed = f"{application_id}|{stage}|{fingerprint}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def safe_artifacts(artifacts: Any) -> List[Dict[str, Any]]:
    sensitive = re.compile(r"(cookie|password|token|secret|storage|resume|captcha)", re.IGNORECASE)
    clean_items = []
    for item in artifacts or []:
        if not isinstance(item, dict):
            continue
        safe_item = {}
        for key, value in item.items():
            if sensitive.search(str(key)):
                continue
            if key in {"type", "artifact_type", "path", "url", "label", "created_at", "screenshot_path"}:
                safe_item[key] = str(value)[:500] if value is not None else value
        if safe_item:
            clean_items.append(safe_item)
    return clean_items


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def sqlite_application_exists(app_id: str, sqlite_conn: Optional[sqlite3.Connection] = None) -> bool:
    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM mcp_applications WHERE id = ?", (app_id,))
    exists = cursor.fetchone() is not None
    if sqlite_conn is None:
        conn.close()
    return exists


def application_exists(app_id: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> bool:
    require_mongodb_if_configured()
    if mongo_client:
        return db().applications.count_documents({"_id": app_id}, limit=1, session=mongo_session) > 0
    return sqlite_application_exists(app_id, sqlite_conn=sqlite_conn)


def get_application_status_internal(app_id: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    require_mongodb_if_configured()
    if mongo_client:
        doc = db().applications.find_one({"_id": app_id}, {"status": 1}, session=mongo_session)
        return doc.get("status") if doc else None
    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM mcp_applications WHERE id = ?", (app_id,))
    row = cursor.fetchone()
    if sqlite_conn is None:
        conn.close()
    return row[0] if row else None


def set_application_status_internal(app_id: str, status: str) -> None:
    updated_at = now_iso()
    if mongo_client:
        result = db().applications.update_one({"_id": app_id}, {"$set": {"status": status, "updated_at": updated_at}})
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Application not found")
        return
    conn = sqlite3.connect(sqlite_db_path)
    cursor = conn.cursor()
    cursor.execute("UPDATE mcp_applications SET status = ?, updated_at = ? WHERE id = ?", (status, updated_at, app_id))
    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Application not found")
    conn.commit()
    conn.close()


def effective_blocking_status(application_id: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    if mongo_client:
        statuses = db().application_question_blockers.distinct(
            "status",
            {
                "application_id": application_id,
                "status": {"$in": [UNANSWERED, TECHNICAL_REVIEW]},
            },
            session=mongo_session,
        )
    else:
        conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT status FROM application_question_blockers WHERE application_id = ? AND status IN (?, ?)",
            (application_id, UNANSWERED, TECHNICAL_REVIEW),
        )
        statuses = [row[0] for row in cursor.fetchall()]
        if sqlite_conn is None:
            conn.close()
    if TECHNICAL_REVIEW in statuses:
        return NEEDS_TECHNICAL_REVIEW
    if UNANSWERED in statuses:
        return BLOCKED_ON_QUESTIONS
    return None


def transition_to_effective_blocking_status(app_id: str, reason: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    desired_status = effective_blocking_status(app_id, mongo_session=mongo_session, sqlite_conn=sqlite_conn)
    current = get_application_status_internal(app_id, mongo_session=mongo_session, sqlite_conn=sqlite_conn)
    if current is None:
        raise HTTPException(status_code=404, detail="Application not found")
    if desired_status is None:
        return {"changed": False, "status": current, "reason": "no_unresolved_blockers"}
    if current in TERMINAL_APPLICATION_STATUSES:
        return {"changed": False, "status": current, "reason": "terminal_status_preserved"}
    if current == desired_status:
        if mongo_client:
            db().applications.update_one(
                {"_id": app_id, "status": desired_status},
                {"$set": {"updated_at": now_iso()}, "$inc": {"question_blocker_revision": 1}},
                session=mongo_session,
            )
        return {"changed": False, "status": current, "reason": "already_in_status"}
    updated_at = now_iso()
    if mongo_client:
        result = db().applications.update_one(
            {"_id": app_id, "status": {"$nin": list(TERMINAL_APPLICATION_STATUSES)}},
            {
                "$set": {"status": desired_status, "updated_at": updated_at},
                "$inc": {"question_blocker_revision": 1},
            },
            session=mongo_session,
        )
        latest = get_application_status_internal(app_id, mongo_session=mongo_session)
        if result.matched_count == 0:
            return {"changed": False, "status": latest, "reason": "conditional_update_not_matched"}
        return {"changed": result.modified_count > 0, "status": latest, "previous_status": current, "reason": reason}

    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    try:
        cursor = conn.cursor()
        terminal_placeholders = ",".join("?" for _ in TERMINAL_APPLICATION_STATUSES)
        cursor.execute(
            f"UPDATE mcp_applications SET status = ?, updated_at = ? "
            f"WHERE id = ? AND status NOT IN ({terminal_placeholders})",
            [desired_status, updated_at, app_id, *TERMINAL_APPLICATION_STATUSES],
        )
        changed = cursor.rowcount > 0
        if sqlite_conn is None:
            conn.commit()
    finally:
        if sqlite_conn is None:
            conn.close()
    latest = get_application_status_internal(app_id, sqlite_conn=sqlite_conn)
    return {"changed": changed, "status": latest, "previous_status": current, "reason": reason if changed else "conditional_update_not_matched"}


def transition_to_ready_to_resume(app_id: str, reason: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    revision = None
    if mongo_client:
        app_doc = db().applications.find_one(
            {"_id": app_id},
            {"status": 1, "question_blocker_revision": 1},
            session=mongo_session,
        )
        current = app_doc.get("status") if app_doc else None
        revision = int((app_doc or {}).get("question_blocker_revision") or 0)
    else:
        current = get_application_status_internal(app_id, sqlite_conn=sqlite_conn)
    if current is None:
        raise HTTPException(status_code=404, detail="Application not found")
    if current not in BLOCKING_APPLICATION_STATUSES:
        return {"changed": False, "status": current, "reason": "not_in_blocking_status"}
    updated_at = now_iso()
    if mongo_client:
        if count_unresolved_blockers(app_id, mongo_session=mongo_session) != 0:
            return transition_to_effective_blocking_status(app_id, "unresolved_blockers_remain", mongo_session=mongo_session)
        if APPROVAL_BEFORE_READY_UPDATE_HOOK:
            APPROVAL_BEFORE_READY_UPDATE_HOOK(app_id, mongo_session)
        result = db().applications.update_one(
            {
                "_id": app_id,
                "status": {"$in": list(BLOCKING_APPLICATION_STATUSES)},
                "question_blocker_revision": revision,
            },
            {
                "$set": {"status": READY_TO_RESUME, "updated_at": updated_at},
                "$inc": {"question_blocker_revision": 1},
            },
            session=mongo_session,
        )
        latest = get_application_status_internal(app_id, mongo_session=mongo_session)
        if result.matched_count == 0:
            return {"changed": False, "status": latest, "reason": "conditional_update_not_matched"}
        return {"changed": result.modified_count > 0, "status": latest, "previous_status": current, "reason": reason}

    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    try:
        cursor = conn.cursor()
        if sqlite_conn is None:
            cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT status FROM mcp_applications WHERE id = ?", (app_id,))
        row = cursor.fetchone()
        if not row:
            if sqlite_conn is None:
                conn.rollback()
            raise HTTPException(status_code=404, detail="Application not found")
        current = row[0]
        if current not in BLOCKING_APPLICATION_STATUSES:
            if sqlite_conn is None:
                conn.commit()
            return {"changed": False, "status": current, "reason": "not_in_blocking_status"}
        cursor.execute(
            "SELECT COUNT(*) FROM application_question_blockers WHERE application_id = ? AND status IN (?, ?)",
            (app_id, UNANSWERED, TECHNICAL_REVIEW),
        )
        if int(cursor.fetchone()[0]) != 0:
            transition = transition_to_effective_blocking_status(app_id, "unresolved_blockers_remain", sqlite_conn=conn)
            if sqlite_conn is None:
                conn.commit()
            return transition
        cursor.execute("""
            UPDATE mcp_applications
            SET status = ?, updated_at = ?
            WHERE id = ?
              AND status IN (?, ?)
              AND NOT EXISTS (
                  SELECT 1 FROM application_question_blockers
                  WHERE application_id = ? AND status IN (?, ?)
              )
        """, (
            READY_TO_RESUME,
            updated_at,
            app_id,
            BLOCKED_ON_QUESTIONS,
            NEEDS_TECHNICAL_REVIEW,
            app_id,
            UNANSWERED,
            TECHNICAL_REVIEW,
        ))
        changed = cursor.rowcount > 0
        cursor.execute("SELECT status FROM mcp_applications WHERE id = ?", (app_id,))
        latest = cursor.fetchone()[0]
        if sqlite_conn is None:
            conn.commit()
        return {"changed": changed, "status": latest, "previous_status": current, "reason": reason if changed else "conditional_update_not_matched"}
    finally:
        if sqlite_conn is None:
            conn.close()


def transition_application_status(app_id: str, desired_status: str, reason: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    if desired_status in {BLOCKED_ON_QUESTIONS, NEEDS_TECHNICAL_REVIEW}:
        return transition_to_effective_blocking_status(app_id, reason, mongo_session=mongo_session, sqlite_conn=sqlite_conn)
    if desired_status == READY_TO_RESUME:
        return transition_to_ready_to_resume(app_id, reason, mongo_session=mongo_session, sqlite_conn=sqlite_conn)
    raise HTTPException(status_code=422, detail=f"Unsupported workflow transition target: {desired_status}")


def sqlite_blocker_row_to_doc(row: sqlite3.Row | tuple) -> Dict[str, Any]:
    if not isinstance(row, sqlite3.Row):
        raise TypeError("sqlite row_factory must be sqlite3.Row")
    item = dict(row)
    item["required"] = bool(item.get("required"))
    item["options"] = json_loads(item.pop("options_json", None), [])
    item["locator_hints"] = json_loads(item.pop("locator_hints_json", None), {})
    item["artifacts"] = json_loads(item.pop("artifacts_json", None), [])
    item["metadata"] = json_loads(item.pop("metadata_json", None), {})
    item["approved_answer"] = json_loads(item.pop("approved_answer_json", None), None)
    return item


def blocker_projection() -> Dict[str, int]:
    return {"_id": 0}


def get_question_blocker_by_id(blocker_id: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    ensure_question_blocker_storage()
    if mongo_client:
        return db().application_question_blockers.find_one({"id": blocker_id}, blocker_projection(), session=mongo_session)
    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM application_question_blockers WHERE id = ?", (blocker_id,))
    row = cursor.fetchone()
    if sqlite_conn is None:
        conn.close()
    return sqlite_blocker_row_to_doc(row) if row else None


def get_question_blocker_by_unique(application_id: str, stage: str, fingerprint: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    ensure_question_blocker_storage()
    if mongo_client:
        return db().application_question_blockers.find_one(
            {"application_id": application_id, "stage": stage, "fingerprint": fingerprint},
            blocker_projection(),
            session=mongo_session,
        )
    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM application_question_blockers WHERE application_id = ? AND stage = ? AND fingerprint = ?",
        (application_id, stage, fingerprint),
    )
    row = cursor.fetchone()
    if sqlite_conn is None:
        conn.close()
    return sqlite_blocker_row_to_doc(row) if row else None


def count_unresolved_blockers(application_id: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> int:
    ensure_question_blocker_storage()
    statuses = [UNANSWERED, TECHNICAL_REVIEW]
    if mongo_client:
        return db().application_question_blockers.count_documents({
            "application_id": application_id,
            "status": {"$in": statuses},
        }, session=mongo_session)
    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM application_question_blockers WHERE application_id = ? AND status IN (?, ?)",
        (application_id, UNANSWERED, TECHNICAL_REVIEW),
    )
    count = int(cursor.fetchone()[0])
    if sqlite_conn is None:
        conn.close()
    return count


def count_question_blockers(query: Dict[str, Any]) -> int:
    ensure_question_blocker_storage()
    if mongo_client:
        return db().application_question_blockers.count_documents(query)
    clauses = []
    params: List[Any] = []
    for key, value in query.items():
        clauses.append(f"{key} = ?")
        params.append(value)
    sql = "SELECT COUNT(*) FROM application_question_blockers"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    conn = sqlite3.connect(sqlite_db_path)
    cursor = conn.cursor()
    cursor.execute(sql, params)
    count = int(cursor.fetchone()[0])
    conn.close()
    return count


def blocker_doc_from_request(application_id: str, req: QuestionBlockerCreateRequest) -> Dict[str, Any]:
    normalized_text = normalize_question_text(req.raw_text)
    normalized_options = normalize_options(req.options)
    fingerprint = fingerprint_question(normalized_text, req.control_type, normalized_options)
    if req.normalized_text and req.normalized_text != normalized_text:
        raise HTTPException(status_code=422, detail="normalized_text does not match server canonical value")
    if req.fingerprint and req.fingerprint != fingerprint:
        raise HTTPException(status_code=422, detail="fingerprint does not match server canonical value")
    created_at = now_iso()
    status = req.status or UNANSWERED
    if status == TECHNICAL_REVIEW:
        app_status = NEEDS_TECHNICAL_REVIEW
    elif status == NEEDS_TECHNICAL_REVIEW:
        status = TECHNICAL_REVIEW
        app_status = NEEDS_TECHNICAL_REVIEW
    else:
        status = UNANSWERED
        app_status = BLOCKED_ON_QUESTIONS
    doc = {
        "id": deterministic_blocker_id(application_id, req.stage, fingerprint),
        "batch_id": req.batch_id,
        "application_id": application_id,
        "ats": req.ats,
        "tenant": req.tenant,
        "company": req.company,
        "role": req.role,
        "job_url": req.job_url,
        "page_name": req.page_name,
        "stage": req.stage,
        "raw_text": req.raw_text,
        "normalized_text": normalized_text,
        "fingerprint": fingerprint,
        "canonical_key": req.canonical_key,
        "required": bool(req.required),
        "control_type": req.control_type,
        "options": list(req.options or []),
        "validation_message": req.validation_message,
        "locator_hints": dict(req.locator_hints or {}),
        "artifacts": safe_artifacts(req.artifacts),
        "metadata": dict(req.metadata or {}),
        "status": status,
        "approved_answer": None,
        "approval_scope": None,
        "approved_by": None,
        "created_at": created_at,
        "updated_at": created_at,
    }
    doc["_application_status"] = app_status
    return doc


def create_question_blocker(application_id: str, req: QuestionBlockerCreateRequest) -> Dict[str, Any]:
    ensure_question_blocker_storage()
    doc = blocker_doc_from_request(application_id, req)
    app_status = doc.pop("_application_status")
    if mongo_client:
        def txn(session):
            if not application_exists(application_id, mongo_session=session):
                raise HTTPException(status_code=404, detail="Application not found")
            result = db().application_question_blockers.update_one(
                {"application_id": application_id, "stage": doc["stage"], "fingerprint": doc["fingerprint"]},
                {"$setOnInsert": doc},
                upsert=True,
                session=session,
            )
            created = bool(result.upserted_id)
            stored = get_question_blocker_by_unique(application_id, doc["stage"], doc["fingerprint"], mongo_session=session) or doc
            transition = {"changed": False, "status": get_application_status_internal(application_id, mongo_session=session), "reason": "existing_blocker_not_reopened"}
            if created:
                transition = transition_application_status(application_id, app_status, "new_question_blocker", mongo_session=session)
                write_event(application_id, "application_question_blocker_created", {
                    "blocker_id": stored["id"],
                    "created": created,
                    "status": stored["status"],
                    "workflow_status": transition.get("status"),
                    "fingerprint": stored["fingerprint"],
                    "stage": stored["stage"],
                }, mongo_session=session)
            return stored, created, transition

        stored, created, transition = run_mongo_transaction(txn)
        return {
            "blocker": stored,
            "created": created,
            "application_status": transition.get("status"),
            "workflow_transition": transition,
            "backend": backend_name(),
        }

    conn = sqlite3.connect(sqlite_db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        if not application_exists(application_id, sqlite_conn=conn):
            conn.rollback()
            raise HTTPException(status_code=404, detail="Application not found")
        cursor.execute("""
            INSERT OR IGNORE INTO application_question_blockers
            (id, batch_id, application_id, ats, tenant, company, role, job_url, page_name, stage,
             raw_text, normalized_text, fingerprint, canonical_key, required, control_type, options_json,
             validation_message, locator_hints_json, artifacts_json, metadata_json, status, approved_answer_json,
             approval_scope, approved_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            doc["id"], doc["batch_id"], doc["application_id"], doc["ats"], doc["tenant"], doc["company"],
            doc["role"], doc["job_url"], doc["page_name"], doc["stage"], doc["raw_text"],
            doc["normalized_text"], doc["fingerprint"], doc["canonical_key"], int(doc["required"]),
            doc["control_type"], json_dumps(doc["options"]), doc["validation_message"],
            json_dumps(doc["locator_hints"]), json_dumps(doc["artifacts"]), json_dumps(doc["metadata"]), doc["status"],
            json_dumps(doc["approved_answer"]), doc["approval_scope"], doc["approved_by"],
            doc["created_at"], doc["updated_at"],
        ))
        created = cursor.rowcount > 0
        stored = get_question_blocker_by_unique(application_id, doc["stage"], doc["fingerprint"], sqlite_conn=conn) or doc
        transition = {"changed": False, "status": get_application_status_internal(application_id, sqlite_conn=conn), "reason": "existing_blocker_not_reopened"}
        if created:
            transition = transition_application_status(application_id, app_status, "new_question_blocker", sqlite_conn=conn)
            write_event(application_id, "application_question_blocker_created", {
                "blocker_id": stored["id"],
                "created": created,
                "status": stored["status"],
                "workflow_status": transition.get("status"),
                "fingerprint": stored["fingerprint"],
                "stage": stored["stage"],
            }, sqlite_conn=conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "blocker": stored,
        "created": created,
        "application_status": transition.get("status"),
        "workflow_transition": transition,
        "backend": backend_name(),
    }


def question_blocker_query(status: Optional[str] = None, batch_id: Optional[str] = None, application_id: Optional[str] = None, fingerprint: Optional[str] = None) -> Dict[str, Any]:
    query = {}
    for key, value in {
        "status": status,
        "batch_id": batch_id,
        "application_id": application_id,
        "fingerprint": fingerprint,
    }.items():
        if value:
            query[key] = value
    return query


def list_question_blockers(status: Optional[str] = None, batch_id: Optional[str] = None, application_id: Optional[str] = None, fingerprint: Optional[str] = None, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    ensure_question_blocker_storage()
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    query = question_blocker_query(status=status, batch_id=batch_id, application_id=application_id, fingerprint=fingerprint)
    total = count_question_blockers(query)
    if mongo_client:
        items = list(
            db().application_question_blockers.find(query, {"_id": 0})
            .sort("created_at", -1)
            .skip(offset)
            .limit(limit)
        )
        return {"items": items, "total": total, "limit": limit, "offset": offset, "has_more": offset + len(items) < total}

    clauses = []
    params: List[Any] = []
    for key, value in query.items():
        clauses.append(f"{key} = ?")
        params.append(value)
    sql = "SELECT * FROM application_question_blockers"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    conn = sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(sql, params)
    rows = [sqlite_blocker_row_to_doc(row) for row in cursor.fetchall()]
    conn.close()
    return {"items": rows, "total": total, "limit": limit, "offset": offset, "has_more": offset + len(rows) < total}


def list_group_occurrences(fingerprint: str, batch_id: Optional[str] = None, status: Optional[str] = None, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    page = list_question_blockers(
        status=status,
        batch_id=batch_id,
        fingerprint=fingerprint,
        limit=limit,
        offset=offset,
    )
    occurrences = []
    for item in page["items"]:
        occurrences.append({
            "id": item.get("id"),
            "application_id": item.get("application_id"),
            "company": item.get("company"),
            "role": item.get("role"),
            "batch_id": item.get("batch_id"),
            "status": item.get("status"),
            "artifacts": safe_artifacts(item.get("artifacts")),
        })
    return {**page, "items": occurrences}


def get_application_doc_by_id(application_id: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    require_mongodb_if_configured()
    if mongo_client:
        doc = db().applications.find_one({"_id": application_id}, session=mongo_session)
        if not doc:
            return None
        doc = dict(doc)
        doc["id"] = doc.pop("_id")
        return doc
    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM mcp_applications WHERE id = ?", (application_id,))
    row = cursor.fetchone()
    if sqlite_conn is None:
        conn.close()
    if not row:
        return None
    item = dict(row)
    item["metadata"] = json_loads(item.pop("metadata_json", None), {})
    return item


def list_blockers_for_application(application_id: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    ensure_question_blocker_storage()
    if mongo_client:
        return list(
            db().application_question_blockers.find(
                {"application_id": application_id},
                {"_id": 0},
                session=mongo_session,
            ).sort("created_at", -1)
        )
    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM application_question_blockers WHERE application_id = ? ORDER BY created_at DESC", (application_id,))
    rows = [sqlite_blocker_row_to_doc(row) for row in cursor.fetchall()]
    if sqlite_conn is None:
        conn.close()
    return rows


def review_question_from_blocker(blocker: Dict[str, Any]) -> Dict[str, Any]:
    metadata = dict(blocker.get("metadata") or {})
    return {
        "blocker_id": blocker.get("id"),
        "fingerprint": blocker.get("fingerprint"),
        "raw_text": blocker.get("raw_text"),
        "normalized_text": blocker.get("normalized_text"),
        "canonical_key": blocker.get("canonical_key"),
        "control_type": blocker.get("control_type"),
        "options": blocker.get("options") or [],
        "status": blocker.get("status"),
        "probe_answer": metadata.get("probe_answer"),
        "suggested_answer": metadata.get("suggested_answer"),
        "approved_answer": blocker.get("approved_answer"),
        "metadata": metadata,
        "artifacts": safe_artifacts(blocker.get("artifacts")),
    }


def application_question_review_bundle(application_id: str) -> Dict[str, Any]:
    ensure_question_blocker_storage()
    app_doc = get_application_doc_by_id(application_id)
    if not app_doc:
        raise HTTPException(status_code=404, detail="Application not found")
    blockers = [
        item for item in list_blockers_for_application(application_id)
        if item.get("status") in {UNANSWERED, TECHNICAL_REVIEW, APPROVED}
        or (item.get("metadata") or {}).get("probe_answer_applied")
        or (item.get("metadata") or {}).get("suggested_answer")
    ]
    questions = [review_question_from_blocker(item) for item in blockers]
    summary = {
        "unanswered_count": sum(1 for item in blockers if item.get("status") == UNANSWERED),
        "technical_review_count": sum(1 for item in blockers if item.get("status") == TECHNICAL_REVIEW),
        "approved_count": sum(1 for item in blockers if item.get("status") == APPROVED),
        "probe_answer_count": sum(1 for item in blockers if (item.get("metadata") or {}).get("probe_answer_applied")),
        "suggested_answer_count": sum(1 for item in blockers if (item.get("metadata") or {}).get("suggested_answer") is not None),
    }
    return {
        "backend": backend_name(),
        "application_id": application_id,
        "company": app_doc.get("company"),
        "role": app_doc.get("role"),
        "batch_id": (app_doc.get("metadata") or {}).get("batch_id"),
        "status": app_doc.get("status"),
        "questions": questions,
        "summary": summary,
    }


def application_ids_with_question_blockers(batch_id: Optional[str] = None) -> List[str]:
    ensure_question_blocker_storage()
    if mongo_client:
        query = {"batch_id": batch_id} if batch_id else {}
        return sorted(db().application_question_blockers.distinct("application_id", query))
    conn = sqlite3.connect(sqlite_db_path)
    cursor = conn.cursor()
    if batch_id:
        cursor.execute("SELECT DISTINCT application_id FROM application_question_blockers WHERE batch_id = ? ORDER BY application_id", (batch_id,))
    else:
        cursor.execute("SELECT DISTINCT application_id FROM application_question_blockers ORDER BY application_id")
    rows = [row[0] for row in cursor.fetchall()]
    conn.close()
    return rows


def list_application_question_review_bundles(batch_id: Optional[str] = None, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    app_ids = application_ids_with_question_blockers(batch_id=batch_id)
    bundles = []
    for app_id in app_ids:
        try:
            bundle = application_question_review_bundle(app_id)
        except HTTPException:
            continue
        if status and bundle.get("status") != status:
            continue
        if batch_id and bundle.get("batch_id") not in {None, batch_id}:
            continue
        bundles.append(bundle)
    bundles.sort(key=lambda item: item.get("application_id") or "")
    total = len(bundles)
    page = bundles[offset:offset + limit]
    return {
        "backend": backend_name(),
        "bundles": page,
        "pagination": {
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page) < total,
        },
    }


def grouped_question_blockers(limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    ensure_question_blocker_storage()
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    groups: List[Dict[str, Any]] = []
    if mongo_client:
        total_groups = len(db().application_question_blockers.distinct("fingerprint"))
        pipeline = [
            {"$sort": {"created_at": -1}},
            {"$group": {
                "_id": "$fingerprint",
                "question": {"$first": "$raw_text"},
                "normalized_text": {"$first": "$normalized_text"},
                "control_type": {"$first": "$control_type"},
                "options": {"$first": "$options"},
                "occurrence_count": {"$sum": 1},
                "application_ids": {"$addToSet": "$application_id"},
                "companies": {"$addToSet": "$company"},
                "roles": {"$addToSet": "$role"},
                "batch_ids": {"$addToSet": "$batch_id"},
                "blocker_ids": {"$addToSet": "$id"},
                "validation_messages": {"$addToSet": "$validation_message"},
                "occurrences": {"$push": {
                    "id": "$id",
                    "application_id": "$application_id",
                    "company": "$company",
                    "role": "$role",
                    "batch_id": "$batch_id",
                    "status": "$status",
                    "artifacts": "$artifacts",
                }},
                "statuses": {"$push": "$status"},
            }},
            {"$sort": {"occurrence_count": -1}},
            {"$skip": offset},
            {"$limit": limit},
        ]
        for row in db().application_question_blockers.aggregate(pipeline):
            counts = {}
            for status_value in row.pop("statuses", []) or []:
                counts[status_value or "UNKNOWN"] = counts.get(status_value or "UNKNOWN", 0) + 1
            occurrences = []
            for occurrence in row.get("occurrences", []) or []:
                occurrence["artifacts"] = safe_artifacts(occurrence.get("artifacts"))
                occurrences.append(occurrence)
            groups.append({
                "fingerprint": row.pop("_id"),
                **row,
                "status_counts": counts,
                "occurrences": occurrences,
            })
        return {"groups": groups, "total": total_groups, "limit": limit, "offset": offset, "has_more": offset + len(groups) < total_groups}

    conn = sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM (SELECT fingerprint FROM application_question_blockers GROUP BY fingerprint)")
    total_groups = int(cursor.fetchone()[0])
    cursor.execute("""
        SELECT fingerprint, COUNT(*) AS occurrence_count, MAX(created_at) AS newest
        FROM application_question_blockers
        GROUP BY fingerprint
        ORDER BY occurrence_count DESC, newest DESC
        LIMIT ? OFFSET ?
    """, (limit, offset))
    group_rows = cursor.fetchall()
    for group_row in group_rows:
        fingerprint = group_row["fingerprint"]
        cursor.execute("SELECT * FROM application_question_blockers WHERE fingerprint = ? ORDER BY created_at DESC", (fingerprint,))
        items = [sqlite_blocker_row_to_doc(row) for row in cursor.fetchall()]
        group: Dict[str, Any] = {
            "fingerprint": fingerprint,
            "question": items[0]["raw_text"],
            "normalized_text": items[0]["normalized_text"],
            "control_type": items[0]["control_type"],
            "options": items[0]["options"],
            "occurrence_count": len(items),
            "application_ids": [],
            "companies": [],
            "roles": [],
            "validation_messages": [],
            "status_counts": {},
            "batch_ids": [],
            "blocker_ids": [],
            "occurrences": [],
        }
        for item in items:
            for key, value in [
                ("application_ids", item.get("application_id")),
                ("companies", item.get("company")),
                ("roles", item.get("role")),
                ("validation_messages", item.get("validation_message")),
                ("batch_ids", item.get("batch_id")),
                ("blocker_ids", item.get("id")),
            ]:
                if value and value not in group[key]:
                    group[key].append(value)
            status_value = item.get("status") or "UNKNOWN"
            group["status_counts"][status_value] = group["status_counts"].get(status_value, 0) + 1
            group["occurrences"].append({
                "id": item.get("id"),
                "application_id": item.get("application_id"),
                "company": item.get("company"),
                "role": item.get("role"),
                "batch_id": item.get("batch_id"),
                "status": item.get("status"),
                "artifacts": safe_artifacts(item.get("artifacts")),
            })
        groups.append(group)
    conn.close()
    return {"groups": groups, "total": total_groups, "limit": limit, "offset": offset, "has_more": offset + len(groups) < total_groups}


def validate_approved_answer(blocker: Dict[str, Any], answer: Any) -> Any:
    control_type = str(blocker.get("control_type") or "").lower().strip()
    if control_type not in SUPPORTED_APPROVAL_CONTROL_TYPES:
        raise HTTPException(status_code=422, detail=f"Unsupported control_type for approval: {control_type or 'unknown'}")
    if control_type in {"radio", "select"}:
        answer_norm = normalize_options([str(answer or "")])
        if not answer_norm:
            raise HTTPException(status_code=422, detail="Answer is required for select/radio approval")
        option_norms = normalize_options(blocker.get("options") or [])
        if answer_norm[0] not in option_norms:
            raise HTTPException(status_code=422, detail="Answer must match one available option")
        for option in blocker.get("options") or []:
            if normalize_options([option]) == answer_norm:
                return option
        return answer_norm[0]
    if control_type == "checkbox":
        if isinstance(answer, bool):
            return answer
        if isinstance(answer, str):
            value = answer.strip().lower()
            if value in CHECKBOX_TRUE_VALUES:
                return True
            if value in CHECKBOX_FALSE_VALUES:
                return False
        raise HTTPException(status_code=422, detail="Checkbox answer must be boolean or an explicit checkbox value")
    if not isinstance(answer, str):
        raise HTTPException(status_code=422, detail="Text answer must be a string")
    value = answer.strip()
    if not value:
        raise HTTPException(status_code=422, detail="Text answer cannot be empty")
    if len(value) > MAX_TEXT_ANSWER_LENGTH:
        raise HTTPException(status_code=422, detail=f"Text answer exceeds {MAX_TEXT_ANSWER_LENGTH} characters")
    return value


def update_blockers_approved(blockers: List[Dict[str, Any]], answer: Any, scope: str, approved_by: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> int:
    updated_at = now_iso()
    ids = [item["id"] for item in blockers]
    if mongo_client:
        result = db().application_question_blockers.update_many(
            {"id": {"$in": ids}, "status": UNANSWERED},
            {"$set": {
                "status": APPROVED,
                "approved_answer": answer,
                "approval_scope": scope,
                "approved_by": approved_by,
                "metadata.probe_answer_applied": False,
                "metadata.requires_user_review": False,
                "metadata.approved_answer_replaces_probe": True,
                "updated_at": updated_at,
            }},
            session=mongo_session,
        )
        return result.modified_count
    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    try:
        cursor = conn.cursor()
        modified = 0
        blockers_by_id = {item["id"]: item for item in blockers}
        for blocker_id in ids:
            metadata = dict((blockers_by_id.get(blocker_id) or {}).get("metadata") or {})
            metadata["probe_answer_applied"] = False
            metadata["requires_user_review"] = False
            metadata["approved_answer_replaces_probe"] = True
            cursor.execute("""
                UPDATE application_question_blockers
                SET status = ?, approved_answer_json = ?, approval_scope = ?, approved_by = ?, metadata_json = ?, updated_at = ?
                WHERE id = ? AND status = ?
            """, (APPROVED, json_dumps(answer), scope, approved_by, json_dumps(metadata), updated_at, blocker_id, UNANSWERED))
            modified += cursor.rowcount
        if sqlite_conn is None:
            conn.commit()
        return modified
    finally:
        if sqlite_conn is None:
            conn.close()


def update_blockers_approved_individually(approvals: List[Dict[str, Any]], approved_by: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> int:
    updated_at = now_iso()
    if mongo_client:
        modified = 0
        for item in approvals:
            result = db().application_question_blockers.update_one(
                {"id": item["blocker"]["id"], "status": UNANSWERED},
                {"$set": {
                    "status": APPROVED,
                    "approved_answer": item["answer"],
                    "approval_scope": "application_bundle",
                    "approved_by": approved_by,
                    "metadata.probe_answer_applied": False,
                    "metadata.requires_user_review": False,
                    "metadata.approved_answer_replaces_probe": True,
                    "updated_at": updated_at,
                }},
                session=mongo_session,
            )
            modified += result.modified_count
        return modified
    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    try:
        cursor = conn.cursor()
        modified = 0
        for item in approvals:
            blocker = item["blocker"]
            metadata = dict(blocker.get("metadata") or {})
            metadata["probe_answer_applied"] = False
            metadata["requires_user_review"] = False
            metadata["approved_answer_replaces_probe"] = True
            cursor.execute("""
                UPDATE application_question_blockers
                SET status = ?, approved_answer_json = ?, approval_scope = ?, approved_by = ?, metadata_json = ?, updated_at = ?
                WHERE id = ? AND status = ?
            """, (
                APPROVED,
                json_dumps(item["answer"]),
                "application_bundle",
                approved_by,
                json_dumps(metadata),
                updated_at,
                blocker["id"],
                UNANSWERED,
            ))
            modified += cursor.rowcount
        if sqlite_conn is None:
            conn.commit()
        return modified
    finally:
        if sqlite_conn is None:
            conn.close()


def list_unanswered_batch_blockers(batch_id: str, fingerprint: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    ensure_question_blocker_storage()
    if mongo_client:
        return list(db().application_question_blockers.find({
            "batch_id": batch_id,
            "fingerprint": fingerprint,
            "status": UNANSWERED,
        }, {"_id": 0}, session=mongo_session))
    conn = sqlite_conn or sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM application_question_blockers WHERE batch_id = ? AND fingerprint = ? AND status = ?",
        (batch_id, fingerprint, UNANSWERED),
    )
    rows = [sqlite_blocker_row_to_doc(row) for row in cursor.fetchall()]
    if sqlite_conn is None:
        conn.close()
    return rows


def unresolved_blockers_for_application(application_id: str) -> int:
    return count_unresolved_blockers(application_id)


def finish_question_blocker_approval(affected: List[Dict[str, Any]], approved_answer: Any, scope: str, approved_by: str, mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    modified_count = update_blockers_approved(affected, approved_answer, scope, approved_by, mongo_session=mongo_session, sqlite_conn=sqlite_conn)
    if modified_count == 0:
        raise HTTPException(status_code=409, detail="No unanswered blockers matched approval scope")
    ready_apps = []
    affected_app_ids = sorted({item["application_id"] for item in affected})
    for app_id in affected_app_ids:
        write_event(app_id, "application_question_blocker_approved", {
            "blocker_ids": [item["id"] for item in affected if item["application_id"] == app_id],
            "scope": scope,
            "approved_by": approved_by,
        }, mongo_session=mongo_session, sqlite_conn=sqlite_conn)
        transition = transition_application_status(app_id, READY_TO_RESUME, "all_question_blockers_approved", mongo_session=mongo_session, sqlite_conn=sqlite_conn)
        if transition.get("changed"):
            write_event(app_id, "application_ready_to_resume", {"reason": "all_question_blockers_approved"}, mongo_session=mongo_session, sqlite_conn=sqlite_conn)
            ready_apps.append(app_id)
    updated = [
        get_question_blocker_by_id(row["id"], mongo_session=mongo_session, sqlite_conn=sqlite_conn)
        for row in affected
    ]
    return {
        "backend": backend_name(),
        "approved_count": modified_count,
        "affected_blocker_count": modified_count,
        "affected_application_count": len(affected_app_ids),
        "ready_to_resume_applications": ready_apps,
        "ready_to_resume_count": len(ready_apps),
        "blockers": [item for item in updated if item],
    }


def approve_question_blocker(blocker_id: str, req: QuestionBlockerApproveRequest) -> Dict[str, Any]:
    ensure_question_blocker_storage()
    scope = (req.scope or "application").lower().strip()
    if scope not in {"application", "batch"}:
        raise HTTPException(status_code=400, detail="scope must be application or batch")
    if scope == "batch":
        raise HTTPException(status_code=400, detail="Batch scope approval must use /question-blocker-groups/{fingerprint}/approve")

    def validate_and_finish(target: Dict[str, Any], mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        if not application_exists(target["application_id"], mongo_session=mongo_session, sqlite_conn=sqlite_conn):
            raise HTTPException(status_code=404, detail="Application not found")
        if target.get("status") == TECHNICAL_REVIEW:
            raise HTTPException(status_code=409, detail="Technical review blockers cannot be approved with an answer")
        if target.get("status") != UNANSWERED:
            raise HTTPException(status_code=409, detail="Question blocker is not unanswered")
        approved_answer = validate_approved_answer(target, req.answer)
        return finish_question_blocker_approval([target], approved_answer, scope, req.approved_by, mongo_session=mongo_session, sqlite_conn=sqlite_conn)

    if mongo_client:
        def txn(session):
            target = get_question_blocker_by_id(blocker_id, mongo_session=session)
            if not target:
                raise HTTPException(status_code=404, detail="Question blocker not found")
            return validate_and_finish(target, mongo_session=session)
        return run_mongo_transaction(txn)

    conn = sqlite3.connect(sqlite_db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        target = get_question_blocker_by_id(blocker_id, sqlite_conn=conn)
        if not target:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Question blocker not found")
        result = validate_and_finish(target, sqlite_conn=conn)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def approve_question_blocker_group(fingerprint: str, req: QuestionBlockerApproveRequest) -> Dict[str, Any]:
    ensure_question_blocker_storage()
    batch_id = req.batch_id
    if not batch_id:
        raise HTTPException(status_code=400, detail="batch_id is required for group approval")

    def validate_and_finish(affected: List[Dict[str, Any]], mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        target = affected[0]
        approved_answer = validate_approved_answer(target, req.answer)
        for item in affected[1:]:
            if not options_compatible(item.get("options"), target.get("options")):
                raise HTTPException(status_code=409, detail="Options are not compatible for batch approval")
            validate_approved_answer(item, req.answer)
        return finish_question_blocker_approval(affected, approved_answer, "batch", req.approved_by, mongo_session=mongo_session, sqlite_conn=sqlite_conn)

    if mongo_client:
        def txn(session):
            total_for_batch = db().application_question_blockers.count_documents(
                {"fingerprint": fingerprint, "batch_id": batch_id},
                session=session,
            )
            if total_for_batch == 0:
                raise HTTPException(status_code=404, detail="No blockers found for fingerprint and batch_id")
            affected = list_unanswered_batch_blockers(batch_id, fingerprint, mongo_session=session)
            if not affected:
                raise HTTPException(status_code=409, detail="No unanswered blockers matched approval scope")
            return validate_and_finish(affected, mongo_session=session)
        return run_mongo_transaction(txn)

    conn = sqlite3.connect(sqlite_db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM application_question_blockers WHERE fingerprint = ? AND batch_id = ?",
            (fingerprint, batch_id),
        )
        if int(cursor.fetchone()[0]) == 0:
            conn.rollback()
            raise HTTPException(status_code=404, detail="No blockers found for fingerprint and batch_id")
        affected = list_unanswered_batch_blockers(batch_id, fingerprint, sqlite_conn=conn)
        if not affected:
            conn.rollback()
            raise HTTPException(status_code=409, detail="No unanswered blockers matched approval scope")
        result = validate_and_finish(affected, sqlite_conn=conn)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def approve_question_review_bundle(application_id: str, req: QuestionReviewBundleApproveRequest) -> Dict[str, Any]:
    ensure_question_blocker_storage()

    def validate_and_finish(mongo_session=None, sqlite_conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        if not application_exists(application_id, mongo_session=mongo_session, sqlite_conn=sqlite_conn):
            raise HTTPException(status_code=404, detail="Application not found")
        blockers = [
            item for item in list_blockers_for_application(application_id, mongo_session=mongo_session, sqlite_conn=sqlite_conn)
            if item.get("status") == UNANSWERED
        ]
        if not blockers:
            raise HTTPException(status_code=409, detail="No unanswered blockers matched application bundle")
        approvals = []
        for blocker in blockers:
            blocker_id = blocker["id"]
            if blocker_id not in req.answers:
                raise HTTPException(status_code=422, detail=f"Missing approved answer for blocker {blocker_id}")
            approvals.append({
                "blocker": blocker,
                "answer": validate_approved_answer(blocker, req.answers[blocker_id]),
            })
        modified_count = update_blockers_approved_individually(
            approvals,
            req.approved_by,
            mongo_session=mongo_session,
            sqlite_conn=sqlite_conn,
        )
        if modified_count != len(approvals):
            raise HTTPException(status_code=409, detail="Application bundle approval conflicted with blocker state")
        write_event(application_id, "application_question_bundle_approved", {
            "blocker_ids": [item["blocker"]["id"] for item in approvals],
            "approved_by": req.approved_by,
        }, mongo_session=mongo_session, sqlite_conn=sqlite_conn)
        transition = transition_application_status(
            application_id,
            READY_TO_RESUME,
            "all_application_bundle_questions_approved",
            mongo_session=mongo_session,
            sqlite_conn=sqlite_conn,
        )
        if transition.get("changed"):
            write_event(application_id, "application_ready_to_resume", {
                "reason": "all_application_bundle_questions_approved",
            }, mongo_session=mongo_session, sqlite_conn=sqlite_conn)
        updated = [
            get_question_blocker_by_id(item["blocker"]["id"], mongo_session=mongo_session, sqlite_conn=sqlite_conn)
            for item in approvals
        ]
        return {
            "backend": backend_name(),
            "approved_count": modified_count,
            "affected_blocker_count": modified_count,
            "affected_application_count": 1,
            "ready_to_resume_applications": [application_id] if transition.get("changed") else [],
            "ready_to_resume_count": 1 if transition.get("changed") else 0,
            "workflow_transition": transition,
            "blockers": [item for item in updated if item],
        }

    if mongo_client:
        def txn(session):
            return validate_and_finish(mongo_session=session)
        return run_mongo_transaction(txn)

    conn = sqlite3.connect(sqlite_db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        result = validate_and_finish(sqlite_conn=conn)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_application_doc(data: Dict[str, Any]) -> Dict[str, Any]:
    require_mongodb_if_configured()
    doc = normalize_application(data)
    app_id = doc["id"]
    if mongo_client:
        db().applications.update_one(
            {"_id": app_id},
            {
                "$set": {k: v for k, v in doc.items() if k != "id"},
                "$setOnInsert": {"created_at": now_iso(), "question_blocker_revision": 0},
            },
            upsert=True,
        )
    else:
        conn = sqlite3.connect(sqlite_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id, created_at FROM mcp_applications WHERE id = ?", (app_id,))
        existing = cursor.fetchone()
        metadata_json = json.dumps(doc.get("metadata") or {}, ensure_ascii=False)
        if existing:
            cursor.execute("""
                UPDATE mcp_applications
                SET company = ?, role = ?, resume_v0 = ?, resume_v1 = ?, status = ?,
                    apply_url = ?, job_description = ?, source = ?, metadata_json = ?, updated_at = ?
                WHERE id = ?
            """, (
                doc["company"], doc["role"], doc["resume_v0"], doc.get("resume_v1"), doc["status"],
                doc.get("apply_url"), doc.get("job_description"), doc.get("source"), metadata_json,
                doc["updated_at"], app_id,
            ))
        else:
            cursor.execute("""
                INSERT INTO mcp_applications
                (id, company, role, resume_v0, resume_v1, status, apply_url, job_description, source, metadata_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                app_id, doc["company"], doc["role"], doc["resume_v0"], doc.get("resume_v1"), doc["status"],
                doc.get("apply_url"), doc.get("job_description"), doc.get("source"), metadata_json, doc["updated_at"],
            ))
        conn.commit()
        conn.close()
    write_event(app_id, "application_upserted", {"status": doc["status"], "source": doc.get("source")})
    return {"id": app_id, "status": doc["status"], "backend": backend_name()}


@app.get("/health")
def health():
    ok = bool(ensure_mongo_client()) if MONGO_CONFIGURED else True
    response = {"ok": ok, "backend": backend_name(), "database": DB_NAME}
    if MONGO_CONFIGURED and not mongo_client:
        response["error"] = "MongoDB Atlas is configured but unavailable. Check Atlas Network Access/IP allowlist and restart 8001."
        if mongo_startup_error:
            response["last_error"] = mongo_startup_error[:600]
    return response


@app.post("/applications")
def save_application(req: SaveAppRequest):
    data = model_dump(req)
    data["id"] = str(uuid.uuid4())
    return upsert_application_doc(data)


@app.post("/applications/upsert")
def upsert_application(req: UpsertApplicationRequest):
    return upsert_application_doc(model_dump(req))


@app.get("/applications")
def list_applications(status: Optional[str] = Query(None), limit: int = Query(50, le=200)):
    require_mongodb_if_configured()
    if mongo_client:
        query = {"status": status} if status else {}
        rows = []
        for doc in db().applications.find(query).sort("updated_at", -1).limit(limit):
            doc["id"] = doc.pop("_id")
            rows.append(doc)
        return {"backend": backend_name(), "applications": rows}

    conn = sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if status:
        cursor.execute("SELECT * FROM mcp_applications WHERE status = ? ORDER BY COALESCE(updated_at, created_at) DESC LIMIT ?", (status, limit))
    else:
        cursor.execute("SELECT * FROM mcp_applications ORDER BY COALESCE(updated_at, created_at) DESC LIMIT ?", (limit,))
    rows = []
    for row in cursor.fetchall():
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        rows.append(item)
    conn.close()
    return {"backend": backend_name(), "applications": rows}


@app.get("/applications/question-review-bundles")
def get_application_question_review_bundles(
    batch_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    return list_application_question_review_bundles(
        batch_id=batch_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@app.get("/applications/{application_id}/question-review-bundle")
def get_application_question_review_bundle(application_id: str):
    return application_question_review_bundle(application_id)


@app.post("/applications/{application_id}/question-review-bundle/approve")
def approve_application_question_review_bundle(application_id: str, req: QuestionReviewBundleApproveRequest):
    return approve_question_review_bundle(application_id, req)


@app.patch("/applications/{id}/status")
def update_status(id: str, req: UpdateStatusRequest):
    require_mongodb_if_configured()
    if req.status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {req.status}")

    updated_at = now_iso()
    if mongo_client:
        result = db().applications.update_one(
            {"_id": id},
            {"$set": {"status": req.status, "updated_at": updated_at}},
        )
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Application not found")
    else:
        conn = sqlite3.connect(sqlite_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM mcp_applications WHERE id = ?", (id,))
        if not cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=404, detail="Application not found")
        cursor.execute("UPDATE mcp_applications SET status = ?, updated_at = ? WHERE id = ?", (req.status, updated_at, id))
        conn.commit()
        conn.close()
    write_event(id, "status_changed", {"status": req.status, "reason": req.reason, "metadata": req.metadata})
    return {"id": id, "status": req.status, "backend": backend_name()}


@app.post("/applications/{application_id}/question-blockers")
def create_application_question_blocker(application_id: str, req: QuestionBlockerCreateRequest):
    return create_question_blocker(application_id, req)


@app.get("/question-blockers")
def get_question_blockers(
    status: Optional[str] = Query(None),
    batch_id: Optional[str] = Query(None),
    application_id: Optional[str] = Query(None),
    fingerprint: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
):
    page = list_question_blockers(
        status=status,
        batch_id=batch_id,
        application_id=application_id,
        fingerprint=fingerprint,
        limit=limit,
        offset=offset,
    )
    return {
        "backend": backend_name(),
        "blockers": page["items"],
        "pagination": {key: page[key] for key in ["total", "limit", "offset", "has_more"]},
    }


@app.get("/question-blockers/groups")
def get_question_blocker_groups(limit: int = Query(100, le=500), offset: int = Query(0, ge=0)):
    page = grouped_question_blockers(limit=limit, offset=offset)
    return {"backend": backend_name(), **page}


@app.get("/question-blocker-groups/{fingerprint}/occurrences")
def get_question_blocker_group_occurrences(
    fingerprint: str,
    batch_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
):
    page = list_group_occurrences(
        fingerprint=fingerprint,
        batch_id=batch_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {
        "backend": backend_name(),
        "fingerprint": fingerprint,
        "occurrences": page["items"],
        "pagination": {key: page[key] for key in ["total", "limit", "offset", "has_more"]},
    }


@app.post("/question-blockers/{blocker_id}/approve")
def approve_application_question_blocker(blocker_id: str, req: QuestionBlockerApproveRequest):
    return approve_question_blocker(blocker_id, req)


@app.post("/question-blocker-groups/{fingerprint}/approve")
def approve_application_question_blocker_group(fingerprint: str, req: QuestionBlockerApproveRequest):
    return approve_question_blocker_group(fingerprint, req)


@app.post("/applications/{id}/events")
def add_event(id: str, req: EventRequest):
    write_event(id, req.event_type, req.payload)
    return {"id": id, "event_type": req.event_type, "backend": backend_name()}


@app.post("/applications/{id}/artifacts")
def save_artifact(id: str, req: ArtifactRequest):
    require_mongodb_if_configured()
    updated_at = now_iso()
    if mongo_client:
        db().application_artifacts.update_one(
            {"app_id": id, "artifact_type": req.artifact_type},
            {"$set": {
                "payload": req.payload,
                "metadata": req.metadata,
                "updated_at": updated_at,
            }},
            upsert=True,
        )
    else:
        conn = sqlite3.connect(sqlite_db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO application_artifacts
            (app_id, artifact_type, payload_json, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            id,
            req.artifact_type,
            json.dumps(req.payload or {}, ensure_ascii=False),
            json.dumps(req.metadata or {}, ensure_ascii=False),
            updated_at,
        ))
        conn.commit()
        conn.close()
    write_event(id, "artifact_saved", {"artifact_type": req.artifact_type})
    return {"id": id, "artifact_type": req.artifact_type, "backend": backend_name()}


@app.post("/applications/{id}/resume-versions")
def save_resume_version(id: str, req: ResumeVersionRequest):
    require_mongodb_if_configured()
    created_at = now_iso()
    payload = model_dump(req)
    payload["created_at"] = created_at
    payload["app_id"] = id

    if mongo_client:
        db().application_resume_versions.insert_one(payload)
        db().applications.update_one(
            {"_id": id},
            {"$set": {"resume_v1": req.content, "updated_at": created_at}},
            upsert=False,
        )
    else:
        conn = sqlite3.connect(sqlite_db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO application_resume_versions
            (app_id, version_label, content, source, job_description, audit_result_json, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            id,
            req.version_label,
            req.content,
            req.source,
            req.job_description,
            json.dumps(req.audit_result or {}, ensure_ascii=False),
            json.dumps(req.metadata or {}, ensure_ascii=False),
            created_at,
        ))
        cursor.execute("UPDATE mcp_applications SET resume_v1 = ?, updated_at = ? WHERE id = ?", (req.content, created_at, id))
        conn.commit()
        conn.close()

    write_event(id, "resume_version_saved", {
        "version_label": req.version_label,
        "source": req.source,
        "metadata": req.metadata,
    })
    return {"id": id, "version_label": req.version_label, "backend": backend_name()}


@app.post("/applications/{id}/outcome")
def record_outcome(id: str, req: OutcomeRequest):
    require_mongodb_if_configured()
    label = normalize_outcome_label(req.outcome_label)
    score = outcome_score(label, req.outcome_score)
    status = status_from_outcome(label)
    observed_at = req.observed_at or now_iso()
    outcome = {
        "label": label,
        "score": score,
        "confidence": clamp_score(req.confidence, 0.5),
        "source": req.source,
        "observed_at": observed_at,
        "message_id": req.message_id,
        "subject": req.subject,
        "sender": req.sender,
        "metadata": req.metadata,
    }

    existing_patterns = req.patterns_used or extract_patterns_from_memory(id)
    updated_patterns = update_pattern_stats(existing_patterns, outcome, id)
    updated_at = now_iso()

    if mongo_client:
        result = db().applications.update_one(
            {"_id": id},
            {"$set": {
                "status": status,
                "outcome": outcome,
                "updated_at": updated_at,
            }},
        )
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Application not found")
        db().application_artifacts.update_one(
            {"app_id": id, "artifact_type": "application_outcome"},
            {"$set": {
                "payload": {**outcome, "patterns_used": existing_patterns, "updated_patterns": updated_patterns},
                "metadata": {"source": req.source},
                "updated_at": updated_at,
            }},
            upsert=True,
        )
    else:
        conn = sqlite3.connect(sqlite_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM mcp_applications WHERE id = ?", (id,))
        if not cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=404, detail="Application not found")
        cursor.execute("UPDATE mcp_applications SET status = ?, updated_at = ? WHERE id = ?", (status, updated_at, id))
        cursor.execute("""
            INSERT OR REPLACE INTO application_artifacts
            (app_id, artifact_type, payload_json, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            id,
            "application_outcome",
            json.dumps({**outcome, "patterns_used": existing_patterns, "updated_patterns": updated_patterns}, ensure_ascii=False),
            json.dumps({"source": req.source}, ensure_ascii=False),
            updated_at,
        ))
        conn.commit()
        conn.close()

    write_event(id, "outcome_recorded", {
        **outcome,
        "status": status,
        "updated_patterns": updated_patterns,
    })
    return {
        "id": id,
        "status": status,
        "outcome": outcome,
        "patterns_used": len(existing_patterns),
        "updated_patterns": updated_patterns,
        "backend": backend_name(),
    }


@app.get("/applications/{id}/memory")
def get_application_memory(id: str):
    require_mongodb_if_configured()
    if mongo_client:
        application = db().applications.find_one({"_id": id})
        if application:
            application["id"] = application.pop("_id")
        events = list(db().application_events.find({"app_id": id}, {"_id": 0}).sort("created_at", 1))
        artifacts = list(db().application_artifacts.find({"app_id": id}, {"_id": 0}))
        resume_versions = list(db().application_resume_versions.find({"app_id": id}, {"_id": 0}).sort("created_at", 1))
        return {
            "backend": backend_name(),
            "application": application,
            "events": events,
            "artifacts": artifacts,
            "resume_versions": resume_versions,
        }

    conn = sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM mcp_applications WHERE id = ?", (id,))
    row = cursor.fetchone()
    application = dict(row) if row else None
    if application:
        application["metadata"] = json.loads(application.pop("metadata_json") or "{}")
    cursor.execute("SELECT event_type, payload_json, created_at FROM application_events WHERE app_id = ? ORDER BY created_at", (id,))
    events = [
        {"event_type": event_type, "payload": json.loads(payload_json or "{}"), "created_at": created_at}
        for event_type, payload_json, created_at in cursor.fetchall()
    ]
    cursor.execute("SELECT artifact_type, payload_json, metadata_json, updated_at FROM application_artifacts WHERE app_id = ?", (id,))
    artifacts = [
        {
            "artifact_type": artifact_type,
            "payload": json.loads(payload_json or "{}"),
            "metadata": json.loads(metadata_json or "{}"),
            "updated_at": updated_at,
        }
        for artifact_type, payload_json, metadata_json, updated_at in cursor.fetchall()
    ]
    cursor.execute("""
        SELECT version_label, content, source, job_description, audit_result_json, metadata_json, created_at
        FROM application_resume_versions
        WHERE app_id = ?
        ORDER BY created_at
    """, (id,))
    resume_versions = [
        {
            "version_label": version_label,
            "content": content,
            "source": source,
            "job_description": job_description,
            "audit_result": json.loads(audit_result_json or "{}"),
            "metadata": json.loads(metadata_json or "{}"),
            "created_at": created_at,
        }
        for version_label, content, source, job_description, audit_result_json, metadata_json, created_at in cursor.fetchall()
    ]
    conn.close()
    return {
        "backend": backend_name(),
        "application": application,
        "events": events,
        "artifacts": artifacts,
        "resume_versions": resume_versions,
    }


@app.get("/applications/patterns")
def get_patterns(role_type: str = Query(None)):
    success_patterns = [
        "Highlight Docker & Kubernetes container orchestration",
        "Quantify performance improvements",
        "Explicitly mention LLM APIs for AI agent roles",
    ]
    failure_patterns = [
        "Vague summaries without metrics",
        "Overly academic descriptions without shipped systems",
        "Missing TypeScript/Next.js details for fullstack roles",
    ]
    return {"success_patterns": success_patterns, "failure_patterns": failure_patterns}


@app.get("/memory/summary")
def memory_summary():
    require_mongodb_if_configured()
    if mongo_client:
        pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
        status_counts = {row["_id"]: row["count"] for row in db().applications.aggregate(pipeline)}
        return {
            "backend": backend_name(),
            "status_counts": status_counts,
            "applications": db().applications.count_documents({}),
            "events": db().application_events.count_documents({}),
            "artifacts": db().application_artifacts.count_documents({}),
            "resume_versions": db().application_resume_versions.count_documents({}),
        }

    conn = sqlite3.connect(sqlite_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT status, COUNT(*) FROM mcp_applications GROUP BY status")
    status_counts = dict(cursor.fetchall())
    cursor.execute("SELECT COUNT(*) FROM mcp_applications")
    applications = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM application_events")
    events = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM application_artifacts")
    artifacts = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM application_resume_versions")
    resume_versions = cursor.fetchone()[0]
    conn.close()
    return {
        "backend": backend_name(),
        "status_counts": status_counts,
        "applications": applications,
        "events": events,
        "artifacts": artifacts,
        "resume_versions": resume_versions,
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("MONGO_SERVER_PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
