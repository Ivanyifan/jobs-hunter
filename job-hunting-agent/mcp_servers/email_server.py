import os
import re
import imaplib
import email
import base64
import hashlib
import time
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from html import unescape
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn
import requests
from google import genai
from google.genai import types

# Load environment variables manually from project root (.env and .env.local)
def load_env_manually():
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for env_file in [".env", ".env.local"]:
        env_path = os.path.join(base_dir, env_file)
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            os.environ[k] = v
            except Exception as e:
                print(f"Failed to parse {env_file}: {e}")

load_env_manually()

app = FastAPI(title="Email Integration & Sync Server", version="1.0.0")

class OtpRequest(BaseModel):
    email_address: str
    sender_filter: str = None

class VerificationRequest(BaseModel):
    email_address: str
    sender_filter: Optional[str] = None
    time_range_minutes: int = Field(default=30, ge=1, le=120)
    tenant_host: str
    not_before_epoch: float
    correlation_id: str

class SyncRequest(BaseModel):
    email_address: str
    time_range_hours: int = 24

# Configuration variables
IMAP_SERVER = os.getenv("EMAIL_IMAP_SERVER")
EMAIL_USER = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
MONGO_URL = os.getenv("MONGO_URL", "http://localhost:8001")
api_key = os.getenv("GEMINI_API_KEY")
GMAIL_OAUTH_APP_ID = "__global_email_oauth__"
GMAIL_OAUTH_ARTIFACT_TYPE = "gmail_oauth_token"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
ALLOW_LOCAL_GMAIL_TOKEN = (os.getenv("ALLOW_LOCAL_GMAIL_TOKEN") or "false").strip().lower() in {"1", "true", "yes", "on"}

client = None
if api_key:
    try:
        client = genai.Client(api_key=api_key)
        print("Gemini GenAI client initialized successfully for email parsing!")
    except Exception as e:
        print(f"Failed to initialize Gemini client in email server: {e}")

@app.get("/health")
def health():
    gmail_token, _gmail_token_source = load_configured_gmail_token()
    gmail_oauth_configured = bool(gmail_token)
    gmail_oauth_ready = bool(get_gmail_api_token()) if gmail_oauth_configured else False
    imap_configured = bool(configured_imap_credentials())
    return {
        "ok": True,
        "service": "email-integration",
        "mongo_url_configured": bool(configured_mongo_url()),
        "gmail_oauth_configured": gmail_oauth_configured,
        "gmail_oauth_ready": gmail_oauth_ready,
        "imap_configured": imap_configured,
        "verification_ready": bool(gmail_oauth_ready or imap_configured),
        "gemini_configured": bool(client),
    }

def sqlite_db_path():
    return os.path.join(os.path.dirname(__file__), "..", "data", "applications.db")

def load_project_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scheduler_config.json")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as err:
        print(f"[Email Config] Failed to read scheduler_config.json: {err}")
        return {}

def configured_imap_credentials():
    config = load_project_config()
    server = config.get("email_imap_server") or IMAP_SERVER
    username = ((config.get("user_data") or {}).get("email") or EMAIL_USER or "").strip()
    password = config.get("email_password") or EMAIL_PASSWORD
    if not server or not username or not password or password == "your-imap-app-password":
        return None
    return {
        "server": server,
        "username": username,
        "password": password,
    }

def configured_mongo_url():
    return (os.getenv("MONGO_URL") or load_project_config().get("mongo_url") or MONGO_URL or "http://localhost:8001").rstrip("/")

def configured_elastic_url():
    return (os.getenv("ELASTIC_URL_API") or load_project_config().get("elastic_url") or "http://localhost:8002").rstrip("/")

def compact_text(text, limit=700):
    return re.sub(r"\s+", " ", text or "").strip()[:limit]

def clean_json_response(text):
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.replace("```json", "", 1).replace("```", "").strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned.replace("```", "").strip()
    return cleaned

def call_mongo_memory(method, path, payload=None, timeout=5):
    url = f"{configured_mongo_url()}{path}"
    try:
        response = requests.request(method, url, json=payload, timeout=timeout)
        if response.status_code >= 400:
            print(f"[Mongo Memory] {method} {url} failed: {response.status_code} {response.text[:300]}")
            return None
        return response.json() if response.text else {}
    except Exception as err:
        print(f"[Mongo Memory] {method} {url} skipped: {err}")
        return None

def sync_elastic_episodes():
    try:
        response = requests.post(
            f"{configured_elastic_url()}/sync-mongo-episodes",
            json={"mongo_url": configured_mongo_url(), "limit": 200, "persist_artifacts_to_mongo": True},
            timeout=45,
        )
        if response.status_code >= 400:
            print(f"[Elastic Sync] Failed: {response.status_code} {response.text[:300]}")
            return None
        return response.json()
    except Exception as err:
        print(f"[Elastic Sync] Skipped: {err}")
        return None

def get_applied_jobs():
    applied_jobs = []
    try:
        seen = set()
        for status in ["Applied", "Queued", "Applying", "Pending Arbitration"]:
            payload = call_mongo_memory("GET", f"/applications?status={status}&limit=200", timeout=6)
            for item in (payload or {}).get("applications", []):
                app_id = item.get("id") or item.get("_id")
                if not app_id or app_id in seen:
                    continue
                seen.add(app_id)
                applied_jobs.append({
                    "id": app_id,
                    "company": item.get("company"),
                    "role": item.get("role"),
                    "status": item.get("status"),
                })
        if applied_jobs:
            return applied_jobs
    except Exception as e:
        print(f"Failed to fetch Mongo jobs list: {e}")

    try:
        path = sqlite_db_path()
        if os.path.exists(path):
            import sqlite3
            conn = sqlite3.connect(path)
            cursor = conn.cursor()
            cursor.execute("SELECT id, company, role, status FROM mcp_applications WHERE status = 'Applied'")
            applied_jobs = [
                {"id": row[0], "company": row[1], "role": row[2], "status": row[3]}
                for row in cursor.fetchall()
            ]
            conn.close()
    except Exception as e:
        print(f"Failed to fetch applied jobs list: {e}")
    return applied_jobs

def update_local_application_status(app_id, status):
    try:
        path = sqlite_db_path()
        if not os.path.exists(path):
            return
        import sqlite3
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        cursor.execute("UPDATE mcp_applications SET status = ? WHERE id = ?", (status, app_id))
        conn.commit()
        conn.close()
    except Exception as err:
        print(f"Failed to update local application status: {err}")

def match_job_by_company(applied_jobs, detected_company):
    detected = (detected_company or "").lower().strip()
    if not detected:
        return None
    for job in applied_jobs:
        company = (job.get("company") or "").lower()
        if company and (company in detected or detected in company):
            return job
    return None

def record_email_classification(matched_job, detected_status, classification, email_data, source):
    app_id = matched_job["id"]
    subject = email_data.get("subject", "")
    sender = email_data.get("from", "")
    body = email_data.get("body", "")
    payload = {
        "classification": detected_status,
        "confidence": classification.get("confidence"),
        "reasoning": classification.get("reasoning") or classification.get("reason"),
        "next_action": classification.get("next_action"),
        "detected_company": classification.get("company"),
        "matched_company": matched_job.get("company"),
        "matched_role": matched_job.get("role"),
        "sender": sender,
        "subject": subject,
        "received_at": email_data.get("date") or email_data.get("received_at") or "",
        "message_id": email_data.get("id", ""),
        "body_snippet": compact_text(body),
        "source": source
    }
    call_mongo_memory("POST", f"/applications/{app_id}/events", {
        "event_type": "email_classified",
        "payload": payload
    })
    call_mongo_memory("POST", f"/applications/{app_id}/artifacts", {
        "artifact_type": "latest_email_signal",
        "payload": payload,
        "metadata": {"source": source}
    })
    outcome_label = "INTERVIEW" if detected_status == "Interview" else "REJECTION" if detected_status == "Rejected" else "APPLIED"
    outcome_res = call_mongo_memory("POST", f"/applications/{app_id}/outcome", {
        "outcome_label": outcome_label,
        "confidence": classification.get("confidence"),
        "source": source,
        "observed_at": email_data.get("date") or email_data.get("received_at") or datetime.utcnow().isoformat() + "Z",
        "message_id": email_data.get("id", ""),
        "subject": subject,
        "sender": sender,
        "metadata": payload
    }, timeout=10)
    if not outcome_res:
        outcome_res = call_mongo_memory("PATCH", f"/applications/{app_id}/status", {
            "status": detected_status,
            "reason": "email_classified",
            "metadata": payload
        })
    elastic_sync = sync_elastic_episodes()
    update_local_application_status(app_id, detected_status)
    return {
        "company": matched_job.get("company"),
        "role": matched_job.get("role"),
        "detected_status": detected_status,
        "subject": subject,
        "sender": sender,
        "matched_id": app_id,
        "confidence": classification.get("confidence"),
        "reasoning": classification.get("reasoning") or classification.get("reason"),
        "next_action": classification.get("next_action"),
        "mongo_synced": bool(outcome_res),
        "patterns_updated": outcome_res.get("updated_patterns", []) if isinstance(outcome_res, dict) else [],
        "elastic_synced": bool(elastic_sync)
    }

def classify_application_email(sender, subject, body):
    if not client:
        return None
    prompt = f"""
Classify this job application update email.
Sender: {sender}
Subject: {subject}
Content:
\"\"\"
{(body or "")[:2500]}
\"\"\"

Task:
1. Determine which company this email is from.
2. Determine whether this is an interview invitation, rejection, application confirmation, still pending/follow-up, or unrelated.
3. Be conservative: only use Interview when the email asks the candidate to schedule, attend, or continue an interview process. Only use Rejected when it clearly says they will not move forward.

Status options: "Interview", "Rejected", "Applied", or "Unknown".

Return ONLY a JSON object:
{{
  "company": "Company Name",
  "status": "Interview" | "Rejected" | "Applied" | "Unknown",
  "confidence": 0.0,
  "next_action": "schedule_interview" | "wait" | "none" | "review_manually",
  "reasoning": "brief evidence-based explanation"
}}
"""
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0
        )
    )
    result = json.loads(clean_json_response(response.text))
    if "confidence" not in result:
        result["confidence"] = None
    return result

def decode_mime_words(s):
    """Safely decode email header MIME encodings."""
    if not s:
        return ""
    parts = []
    for word, encoding in decode_header(s):
        if isinstance(word, bytes):
            try:
                parts.append(word.decode(encoding or 'utf-8', errors='ignore'))
            except Exception:
                parts.append(word.decode('latin1', errors='ignore'))
        else:
            parts.append(word)
    return "".join(parts)

def gmail_token_path():
    configured = (os.getenv("GMAIL_OAUTH_TOKEN_PATH") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "gmail_token.json")

def gmail_secret_path():
    configured = (os.getenv("GMAIL_OAUTH_CLIENT_SECRET_PATH") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "client_secret.json")

def load_local_gmail_token():
    path = gmail_token_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as err:
        print(f"[Gmail OAuth] Failed to read local token: {err}")
        return None

def save_local_gmail_token(token_data):
    try:
        os.makedirs(os.path.dirname(gmail_token_path()), exist_ok=True)
        with open(gmail_token_path(), "w", encoding="utf-8") as out:
            json.dump(token_data, out, indent=2)
    except Exception as err:
        print(f"[Gmail OAuth] Failed to write local token: {err}")

def load_gmail_token_from_mongo():
    memory = call_mongo_memory("GET", f"/applications/{GMAIL_OAUTH_APP_ID}/memory", timeout=5)
    for artifact in (memory or {}).get("artifacts") or []:
        if artifact.get("artifact_type") == GMAIL_OAUTH_ARTIFACT_TYPE:
            payload = artifact.get("payload") or {}
            if payload.get("access_token") or payload.get("refresh_token"):
                return payload
    return None

def load_configured_gmail_token():
    explicit_local_path = bool((os.getenv("GMAIL_OAUTH_TOKEN_PATH") or "").strip())
    if ALLOW_LOCAL_GMAIL_TOKEN and explicit_local_path:
        token_data = load_local_gmail_token()
        if token_data:
            return token_data, "local"
    token_data = load_gmail_token_from_mongo()
    if token_data:
        return token_data, "mongo"
    if ALLOW_LOCAL_GMAIL_TOKEN:
        token_data = load_local_gmail_token()
        if token_data:
            return token_data, "local"
    return None, "none"

def save_gmail_token_to_mongo(token_data):
    if not token_data:
        return
    call_mongo_memory(
        "POST",
        f"/applications/{GMAIL_OAUTH_APP_ID}/artifacts",
        {
            "artifact_type": GMAIL_OAUTH_ARTIFACT_TYPE,
            "payload": token_data,
            "metadata": {
                "email_address": token_data.get("email_address", ""),
                "scope": token_data.get("scope", GMAIL_READONLY_SCOPE),
                "source": "email_server_refresh",
            },
        },
        timeout=5,
    )

def load_gmail_client_secret():
    env_client_id = (os.getenv("GMAIL_OAUTH_CLIENT_ID") or "").strip()
    env_client_secret = (os.getenv("GMAIL_OAUTH_CLIENT_SECRET") or "").strip()
    if env_client_id and env_client_secret:
        return {
            "client_id": env_client_id,
            "client_secret": env_client_secret,
        }

    path = gmail_secret_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("web", {})
    except Exception as err:
        print(f"[Gmail OAuth] Failed to read client_secret.json: {err}")
        return None

def get_gmail_api_token():
    try:
        token_data, token_source = load_configured_gmail_token()
        if not token_data:
            return None

        expires_at = float(token_data.get("expires_at") or 0)
        refresh_token = token_data.get("refresh_token")
        if time.time() > expires_at - 60:
            if not refresh_token:
                print("[Gmail OAuth] Token expired and no refresh_token is available.")
                return None
            web_cfg = load_gmail_client_secret()
            if not web_cfg:
                print("[Gmail OAuth] Token refresh skipped because client_secret.json is missing.")
                return None
            res = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": web_cfg.get("client_id"),
                    "client_secret": web_cfg.get("client_secret"),
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=10,
            )
            if res.status_code == 200:
                new_data = res.json()
                token_data["access_token"] = new_data["access_token"]
                token_data["expires_at"] = time.time() + new_data.get("expires_in", 3600)
                if token_source == "mongo":
                    save_gmail_token_to_mongo(token_data)
                else:
                    save_local_gmail_token(token_data)
            else:
                print(f"[Gmail Token Refresh Error] Status: {res.status_code}, Body: {res.text}")
                return None

        return token_data.get("access_token")
    except Exception as e:
        print(f"[Gmail OAuth Helper Error] {e}")
        return None

def fetch_latest_email_body_gmail_api(sender_filter=None):
    access_token = get_gmail_api_token()
    if not access_token:
        return None
        
    try:
        q = "after:" + (datetime.now() - timedelta(days=1)).strftime("%Y/%m/%d")
        if sender_filter:
            q += f" from:{sender_filter}"
            
        res = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": q, "maxResults": 1},
            timeout=10
        )
        if res.status_code != 200:
            print(f"[Gmail List Error] Status: {res.status_code}, Body: {res.text}")
            return None
            
        messages = res.json().get("messages", [])
        if not messages:
            return None
            
        msg_id = messages[0]["id"]
        res_msg = requests.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"format": "full"},
            timeout=10
        )
        if res_msg.status_code != 200:
            return None
            
        msg_data = res_msg.json()
        payload = msg_data.get("payload", {})
        headers = payload.get("headers", [])
        
        subject = ""
        sender = ""
        date_str = ""
        for h in headers:
            name = h.get("name", "").lower()
            if name == "subject":
                subject = h.get("value", "")
            elif name == "from":
                sender = h.get("value", "")
            elif name == "date":
                date_str = h.get("value", "")
                
        def get_body(part):
            if "body" in part and "data" in part["body"] and part["body"]["data"]:
                return part["body"]["data"]
            if "parts" in part:
                for p in part["parts"]:
                    data = get_body(p)
                    if data:
                        return data
            return None
            
        body = ""
        b64_body = get_body(payload)
        if b64_body:
            padding = '=' * (4 - len(b64_body) % 4)
            try:
                decoded_bytes = base64.urlsafe_b64decode(b64_body + padding)
                body = decoded_bytes.decode('utf-8', errors='ignore')
            except Exception:
                pass
        if not body:
            body = msg_data.get("snippet", "")
            
        return {
            "subject": subject,
            "from": sender,
            "body": body,
            "date": date_str
        }
    except Exception as e:
        print(f"[Gmail API Fetch Error] {e}")
        return None

def extract_verification_links(body_text):
    if not body_text:
        return []

    text = unescape(body_text)
    links = []
    for match in re.finditer(r'href=["\']([^"\']+)["\']', text, flags=re.IGNORECASE):
        links.append(match.group(1).strip())
    for match in re.finditer(r'https?://[^\s<>"\']+', text, flags=re.IGNORECASE):
        links.append(match.group(0).strip().rstrip(").,;"))

    cleaned = []
    seen = set()
    for link in links:
        if not link or link in seen:
            continue
        low = link.lower()
        if low.startswith("mailto:"):
            continue
        if any(token in low for token in ["unsubscribe", "privacy", "terms", "preferences"]):
            continue
        seen.add(link)
        cleaned.append(link)
    return cleaned[:10]

def extract_otp_code(body_text):
    if not body_text:
        return None
    otp_match = re.search(r'\b(\d{4,8})\b', body_text)
    if otp_match:
        return otp_match.group(1)
    alpha_match = re.search(r'\b([A-Z0-9]{6,10})\b', body_text)
    if alpha_match:
        candidate = alpha_match.group(1)
        if any(ch.isdigit() for ch in candidate):
            return candidate
    return None

def fetch_recent_emails_gmail_api(hours=24):
    access_token = get_gmail_api_token()
    if not access_token:
        return []
        
    try:
        since_dt = datetime.now() - timedelta(hours=hours)
        q = "after:" + since_dt.strftime("%Y/%m/%d")
        q += " subject:(interview OR application OR career OR hiring OR recruit OR rejection OR unfortunately)"
        
        res = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": q, "maxResults": 20},
            timeout=10
        )
        if res.status_code != 200:
            return []
            
        messages = res.json().get("messages", [])
        results = []
        for m in messages:
            msg_id = m["id"]
            res_msg = requests.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"format": "full"},
                timeout=10
            )
            if res_msg.status_code != 200:
                continue
                
            msg_data = res_msg.json()
            payload = msg_data.get("payload", {})
            headers = payload.get("headers", [])
            
            subject = ""
            sender = ""
            received_at = ""
            for h in headers:
                name = h.get("name", "").lower()
                if name == "subject":
                    subject = h.get("value", "")
                elif name == "from":
                    sender = h.get("value", "")
                elif name == "date":
                    received_at = h.get("value", "")
                    
            def get_body(part):
                if "body" in part and "data" in part["body"] and part["body"]["data"]:
                    return part["body"]["data"]
                if "parts" in part:
                    for p in part["parts"]:
                        data = get_body(p)
                        if data:
                            return data
                return None
                
            body = ""
            b64_body = get_body(payload)
            if b64_body:
                padding = '=' * (4 - len(b64_body) % 4)
                try:
                    decoded_bytes = base64.urlsafe_b64decode(b64_body + padding)
                    body = decoded_bytes.decode('utf-8', errors='ignore')
                except Exception:
                    pass
            if not body:
                body = msg_data.get("snippet", "")
                
            results.append({
                "subject": subject,
                "from": sender,
                "body": body,
                "id": msg_id,
                "date": received_at
            })
        return results
    except Exception as e:
        print(f"[Gmail API Sync Fetch Error] {e}")
        return []

def fetch_latest_email_body_imap(sender_filter=None, imap_server=None, email_user=None, email_password=None):
    """Retrieve raw email body from IMAP inbox."""
    srv = imap_server or IMAP_SERVER
    usr = email_user or EMAIL_USER
    pwd = email_password or EMAIL_PASSWORD
    
    if not srv or not usr or not pwd:
        print(f"[Email IMAP] Missing configuration. server={srv}, user={usr}")
        return None
        
    try:
        # Connect to IMAP server
        mail = imaplib.IMAP4_SSL(srv)
        mail.login(usr, pwd)
        mail.select("inbox")
        
        # Search queries
        search_criteria = 'UNSEEN'
        if sender_filter:
            search_criteria = f'(FROM "{sender_filter}")'
            
        status, data = mail.search(None, search_criteria)
        if status != 'OK':
            # Fallback to search all recent emails if specific filter failed
            status, data = mail.search(None, 'ALL')
            
        email_ids = data[0].split()
        if not email_ids:
            mail.logout()
            return None
            
        # Get the latest email
        latest_id = email_ids[-1]
        status, data = mail.fetch(latest_id, '(RFC822)')
        if status != 'OK':
            mail.logout()
            return None
            
        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)
        
        # Extract body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                if content_type == "text/plain" and "attachment" not in content_disposition:
                    body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    break
                elif content_type == "text/html" and "attachment" not in content_disposition:
                    body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
        else:
            body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            
        mail.logout()
        return {
            "subject": decode_mime_words(msg["subject"]),
            "from": decode_mime_words(msg["from"]),
            "body": body,
            "date": msg["date"]
        }
    except Exception as e:
        print(f"IMAP fetch error: {e}")
        return None


WORKDAY_VERIFICATION_INTENT_RE = re.compile(
    r"(verify|verification|confirm|activate).{0,80}(account|email|identity)|"
    r"(account|email|identity).{0,80}(verify|verification|confirm|activate)|"
    r"one[- ]time (?:passcode|password|code)|verification code",
    re.IGNORECASE | re.DOTALL,
)


def normalize_email_address(value):
    return str(value or "").strip().lower()


def message_recipient_addresses(candidate):
    values = candidate.get("recipient_headers") or []
    return {
        normalize_email_address(address)
        for _name, address in getaddresses([str(value or "") for value in values])
        if normalize_email_address(address)
    }


def message_received_epoch(candidate):
    internal = candidate.get("received_at_epoch")
    try:
        if internal is not None:
            return float(internal)
    except Exception:
        pass
    try:
        parsed = parsedate_to_datetime(candidate.get("date") or "")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        return 0.0


def normalized_tenant_host(value):
    raw = str(value or "").strip().lower()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").lower()


def trusted_workday_verification_links(links, tenant_host):
    expected_host = normalized_tenant_host(tenant_host)
    if not expected_host or not expected_host.endswith(".myworkdayjobs.com"):
        return []
    trusted = []
    for raw_link in links or []:
        link = unescape(str(raw_link or "").strip())
        try:
            parsed = urlparse(link)
        except Exception:
            continue
        if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != expected_host:
            continue
        trusted.append(link)
    return trusted


def extract_verification_otp_code(body_text):
    text = re.sub(r"\s+", " ", body_text or "")
    patterns = (
        r"(?:verification|security|one[- ]time|confirmation)\s+(?:passcode|password|code)\D{0,24}([A-Z0-9]{4,10})\b",
        r"\bcode\D{0,16}([A-Z0-9]{4,10})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match and any(character.isdigit() for character in match.group(1)):
            return match.group(1)
    return None


def verification_candidate_correlation(candidate, req):
    expected_email = normalize_email_address(req.email_address)
    recipients = message_recipient_addresses(candidate)
    recipient_match = bool(expected_email and expected_email in recipients)
    received_epoch = message_received_epoch(candidate)
    now = time.time()
    not_before = max(
        float(req.not_before_epoch or 0),
        now - int(req.time_range_minutes or 0) * 60,
    )
    time_match = bool(received_epoch and received_epoch >= not_before - 90 and received_epoch <= now + 300)
    sender_text = str(candidate.get("from") or "").lower()
    sender_filter = str(req.sender_filter or "").strip().lower()
    if sender_filter == "workday":
        sender_match = "workday" in sender_text
    else:
        sender_match = bool(sender_filter and sender_filter in sender_text)
    subject = str(candidate.get("subject") or "")
    body = str(candidate.get("body") or "")
    verification_intent_match = bool(WORKDAY_VERIFICATION_INTENT_RE.search(f"{subject}\n{body}"))
    all_links = extract_verification_links(body)
    trusted_links = trusted_workday_verification_links(all_links, req.tenant_host)
    otp_code = extract_verification_otp_code(body)
    tenant_slug = normalized_tenant_host(req.tenant_host).split(".", 1)[0]
    tenant_text = f"{sender_text}\n{subject}\n{body}".lower()
    tenant_match = bool(trusted_links or (tenant_slug and tenant_slug in tenant_text))
    artifact_match = bool(trusted_links or otp_code)
    checks = {
        "recipient_match": recipient_match,
        "time_match": time_match,
        "sender_match": sender_match,
        "verification_intent_match": verification_intent_match,
        "tenant_match": tenant_match,
        "artifact_match": artifact_match,
    }
    reasons = [name for name, passed in checks.items() if not passed]
    return {
        "verified": all(checks.values()),
        "checks": checks,
        "reasons": reasons,
        "trusted_links": trusted_links,
        "otp_code": otp_code,
        "received_at_epoch": received_epoch,
    }


GMAIL_API_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def gmail_api_get(url, access_token, *, params=None, timeout=10, attempts=3):
    for attempt in range(max(1, attempts)):
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
                timeout=timeout,
            )
        except requests.RequestException as err:
            print(f"[Gmail API] transient request error: {err.__class__.__name__}")
            response = None
        if response is not None and response.status_code not in GMAIL_API_RETRYABLE_STATUSES:
            return response
        if attempt + 1 < max(1, attempts):
            time.sleep(0.25 * (attempt + 1))
    return response


def gmail_message_record(access_token, message_id):
    if not message_id:
        return None
    response = gmail_api_get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
        access_token,
        params={"format": "full"},
    )
    if response is None or response.status_code != 200:
        return None
    try:
        message = response.json() or {}
    except ValueError:
        return None
    payload = message.get("payload") or {}
    headers = {}
    for header in payload.get("headers") or []:
        name = str(header.get("name") or "").lower()
        headers.setdefault(name, []).append(header.get("value") or "")

    def encoded_body(part):
        body = part.get("body") or {}
        if body.get("data"):
            return body.get("data")
        preferred = sorted(
            part.get("parts") or [],
            key=lambda item: 0 if item.get("mimeType") == "text/plain" else 1,
        )
        for child in preferred:
            value = encoded_body(child)
            if value:
                return value
        return None

    body_text = ""
    encoded = encoded_body(payload)
    if encoded:
        try:
            body_text = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode(
                "utf-8", errors="ignore"
            )
        except Exception:
            body_text = ""
    if not body_text:
        body_text = message.get("snippet") or ""
    try:
        received_epoch = float(message.get("internalDate") or 0) / 1000.0
    except Exception:
        received_epoch = 0.0
    return {
        "id": message.get("id") or message_id,
        "thread_id": message.get("threadId") or "",
        "subject": (headers.get("subject") or [""])[0],
        "from": (headers.get("from") or [""])[0],
        "date": (headers.get("date") or [""])[0],
        "recipient_headers": (
            (headers.get("to") or [])
            + (headers.get("delivered-to") or [])
            + (headers.get("x-original-to") or [])
            + (headers.get("envelope-to") or [])
        ),
        "body": body_text,
        "received_at_epoch": received_epoch,
    }


def fetch_verification_candidates_gmail_api(req, max_results=50):
    access_token = get_gmail_api_token()
    if not access_token:
        return []
    after_date = datetime.fromtimestamp(max(0, req.not_before_epoch - 120), timezone.utc).strftime("%Y/%m/%d")
    expected_email = normalize_email_address(req.email_address)
    query = f"after:{after_date}"
    if expected_email:
        query += f" to:{expected_email}"
    response = gmail_api_get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        access_token,
        params={"q": query, "maxResults": max_results},
    )
    if response is None or response.status_code != 200:
        print(f"[Gmail Verification Search] status={getattr(response, 'status_code', 'unavailable')}")
        return []
    candidates = []
    for item in (response.json() or {}).get("messages") or []:
        record = gmail_message_record(access_token, item.get("id"))
        if record:
            candidates.append(record)
    return sorted(candidates, key=message_received_epoch, reverse=True)


def _imap_message_body(message):
    if message.is_multipart():
        parts = list(message.walk())
        parts.sort(key=lambda part: 0 if part.get_content_type() == "text/plain" else 1)
        for part in parts:
            disposition = str(part.get("Content-Disposition") or "")
            if part.get_content_type() not in {"text/plain", "text/html"} or "attachment" in disposition:
                continue
            payload = part.get_payload(decode=True)
            if payload:
                return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        return ""
    payload = message.get_payload(decode=True)
    return payload.decode(message.get_content_charset() or "utf-8", errors="ignore") if payload else ""


def fetch_verification_candidates_imap(req, max_results=30):
    credentials = configured_imap_credentials()
    if not credentials:
        return []
    mailbox = None
    try:
        mailbox = imaplib.IMAP4_SSL(credentials["server"])
        mailbox.login(credentials["username"], credentials["password"])
        mailbox.select("inbox", readonly=True)
        since = datetime.fromtimestamp(max(0, req.not_before_epoch - 120), timezone.utc).strftime("%d-%b-%Y")
        status, data = mailbox.search(None, "SINCE", since)
        if status != "OK":
            return []
        candidates = []
        for message_id in reversed((data[0] or b"").split()[-max_results:]):
            status, content = mailbox.fetch(message_id, "(BODY.PEEK[])")
            if status != "OK" or not content or not isinstance(content[0], tuple):
                continue
            message = email.message_from_bytes(content[0][1])
            candidates.append({
                "id": decode_mime_words(message.get("Message-ID") or message_id.decode(errors="ignore")),
                "subject": decode_mime_words(message.get("Subject")),
                "from": decode_mime_words(message.get("From")),
                "date": message.get("Date") or "",
                "recipient_headers": [
                    decode_mime_words(message.get(name) or "")
                    for name in ("To", "Delivered-To", "X-Original-To", "Envelope-To")
                    if message.get(name)
                ],
                "body": _imap_message_body(message),
                "received_at_epoch": 0.0,
            })
        return sorted(candidates, key=message_received_epoch, reverse=True)
    except Exception as err:
        print(f"[IMAP Verification Search] {err}")
        return []
    finally:
        if mailbox is not None:
            try:
                mailbox.logout()
            except Exception:
                pass

@app.post("/email/otp")
def get_latest_otp(req: OtpRequest):
    # Try Gmail API first
    email_data = fetch_latest_email_body_gmail_api(req.sender_filter)
    
    # Fallback to IMAP if Gmail token is not available
    if not email_data:
        # Load credentials dynamically from scheduler_config.json if available
        imap_server = IMAP_SERVER
        email_user = EMAIL_USER
        email_password = EMAIL_PASSWORD
        
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scheduler_config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8-sig") as f:
                    config_data = json.load(f)
                    if config_data.get("email_imap_server"):
                        imap_server = config_data.get("email_imap_server")
                    if config_data.get("email_password"):
                        email_password = config_data.get("email_password")
                    user_email = config_data.get("user_data", {}).get("email")
                    if user_email:
                        email_user = user_email
            except Exception as read_err:
                print(f"Failed to read scheduler_config.json dynamically: {read_err}")
                
        # Also check .env.local dynamically
        env_local_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env.local")
        if os.path.exists(env_local_path):
            try:
                with open(env_local_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k == "EMAIL_IMAP_SERVER" and v:
                                imap_server = v
                            elif k == "EMAIL_USERNAME" and v:
                                email_user = v
                            elif k == "EMAIL_PASSWORD" and v:
                                email_password = v
            except Exception as e:
                print(f"Failed to parse .env.local dynamically: {e}")
                
        if not imap_server or not email_user or not email_password or email_password == "your-imap-app-password":
            mock_code = "982314"
            print(f"[Mock Email] Mock Email Mode: Generated OTP verification code '{mock_code}' for testing.")
            return {
                "otp_code": mock_code,
                "received_at": datetime.now().isoformat()
            }
        email_data = fetch_latest_email_body_imap(req.sender_filter, imap_server, email_user, email_password)
        
    if not email_data:
        raise HTTPException(status_code=404, detail="No recent emails found in the inbox")
        
    body_text = email_data["body"]
    
    # 1. Regex parsing attempt (4-8 digit numbers)
    otp_match = re.search(r'\b(\d{4,8})\b', body_text)
    if otp_match:
        return {
            "otp_code": otp_match.group(1),
            "received_at": email_data["date"]
        }
        
    # 2. Gemini GenAI parsing fallback if regex missed (e.g. alpha-numeric OTPs)
    if client:
        prompt = f"""
Analyze the following email and extract the One-Time Password (OTP) or Verification Code.

Email Text:
\"\"\"
{body_text}
\"\"\"

Output ONLY the extracted verification code (numbers/letters). Do not output any other text or markdown.
"""
        try:
            response = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.0)
            )
            extracted_code = response.text.strip()
            if extracted_code:
                return {
                    "otp_code": extracted_code,
                    "received_at": email_data["date"]
                }
        except Exception as e:
            print(f"Gemini OTP parsing failed: {e}")
            
    raise HTTPException(status_code=400, detail="Could not parse an OTP code from the latest email")

@app.post("/email/verification")
def get_latest_verification_artifacts(req: VerificationRequest):
    tenant_host = normalized_tenant_host(req.tenant_host)
    if not tenant_host.endswith(".myworkdayjobs.com"):
        raise HTTPException(status_code=422, detail="tenant_host_must_be_workday")
    if not normalize_email_address(req.email_address):
        raise HTTPException(status_code=422, detail="email_address_required")
    if req.not_before_epoch <= 0 or req.not_before_epoch > time.time() + 300:
        raise HTTPException(status_code=422, detail="invalid_not_before_epoch")
    if not str(req.correlation_id or "").strip():
        raise HTTPException(status_code=422, detail="correlation_id_required")

    candidates = fetch_verification_candidates_gmail_api(req)
    source = "gmail_oauth"
    if not candidates:
        candidates = fetch_verification_candidates_imap(req)
        source = "imap"
    if not candidates:
        raise HTTPException(status_code=404, detail={
            "reason": "no_recent_verification_email",
            "correlation_id": req.correlation_id,
        })

    rejected = []
    for candidate in candidates:
        correlation = verification_candidate_correlation(candidate, req)
        message_digest = hashlib.sha256(str(candidate.get("id") or "").encode("utf-8")).hexdigest()[:16]
        if not correlation.get("verified"):
            rejected.append({
                "message_digest": message_digest,
                "reasons": correlation.get("reasons") or [],
                "received_at_epoch": correlation.get("received_at_epoch") or 0,
            })
            continue
        trusted_links = correlation.get("trusted_links") or []
        otp_code = correlation.get("otp_code")
        return {
            "trusted": True,
            "source": source,
            "correlation_id": req.correlation_id,
            "message_digest": message_digest,
            "received_at_epoch": correlation.get("received_at_epoch") or 0,
            "artifact_type": "link" if trusted_links else "otp",
            "otp_code": otp_code,
            "links": trusted_links,
            "correlation": {
                "verified": True,
                "checks": correlation.get("checks") or {},
                "tenant_host": tenant_host,
            },
        }

    raise HTTPException(status_code=409, detail={
        "reason": "verification_email_correlation_failed",
        "correlation_id": req.correlation_id,
        "rejected_candidates": rejected[:10],
    })

@app.post("/email/sync")
def sync_inbox_updates(req: SyncRequest):
    # Try Gmail API first
    access_token = get_gmail_api_token()
    if access_token:
        print("[Gmail API] Active token found. Synching using Google Gmail REST API...")
        recent_emails = fetch_recent_emails_gmail_api(req.time_range_hours)
        updates = []
        
        applied_jobs = get_applied_jobs()
            
        if not applied_jobs:
            return {"status": "Success", "synced_count": 0, "updates": []}
            
        for email_data in recent_emails:
            subject = email_data["subject"]
            sender = email_data["from"]
            body = email_data["body"]
            
            try:
                result = classify_application_email(sender, subject, body)
                if not result:
                    continue
                detected_company = result.get("company", "").strip()
                detected_status = result.get("status", "Unknown")
                if detected_status in ["Interview", "Rejected"] and detected_company:
                    matched_job = match_job_by_company(applied_jobs, detected_company)
                    if matched_job:
                        updates.append(record_email_classification(
                            matched_job,
                            detected_status,
                            result,
                            email_data,
                            "gmail_api"
                        ))
            except Exception as gen_err:
                print(f"Gemini email classify error: {gen_err}")
                    
        return {
            "status": "Success",
            "synced_count": len(updates),
            "updates": updates
        }

    # Simulated updates fallback
    if not IMAP_SERVER or not EMAIL_USER or not EMAIL_PASSWORD:
        print("[Mock Email] Mock Email Mode: Simulating inbox sync updates.")
        mock_updates = []
        try:
            import sqlite3
            sqlite_db_path = os.path.join(os.path.dirname(__file__), "..", "data", "applications.db")
            if os.path.exists(sqlite_db_path):
                conn = sqlite3.connect(sqlite_db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT id, company, role FROM mcp_applications WHERE status = 'Applied' LIMIT 2")
                rows = cursor.fetchall()
                conn.close()
                
                statuses = ["Interview", "Rejected"]
                for i, row in enumerate(rows):
                    app_id = row[0]
                    company = row[1]
                    role = row[2]
                    status = statuses[i % len(statuses)]
                    mock_email = {
                        "subject": f"Update on your application at {company}",
                        "from": f"careers@{re.sub(r'[^a-z0-9]+', '', company.lower()) or 'company'}.example",
                        "body": "Mock interview invitation." if status == "Interview" else "Mock rejection notice.",
                        "date": datetime.utcnow().isoformat() + "Z",
                        "id": f"mock-{app_id}-{status.lower()}"
                    }
                    mock_classification = {
                        "company": company,
                        "status": status,
                        "confidence": 0.99,
                        "next_action": "schedule_interview" if status == "Interview" else "none",
                        "reasoning": "Mock email sync classification for local demo."
                    }
                    mock_updates.append(record_email_classification(
                        {"id": app_id, "company": company, "role": role},
                        status,
                        mock_classification,
                        mock_email,
                        "mock"
                    ))
        except Exception as err:
            print(f"Mock sync SQLite error: {err}")
            
        return {
            "status": "Success (Mock)",
            "synced_count": len(mock_updates),
            "updates": mock_updates
        }

    # Real IMAP sync
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_USER, EMAIL_PASSWORD)
        mail.select("inbox")
        
        # Look for emails received in the time range
        since_date = (datetime.now() - timedelta(hours=req.time_range_hours)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f'SINCE "{since_date}"')
        
        email_ids = data[0].split()
        updates = []
        
        if email_ids:
            applied_jobs = get_applied_jobs()
                
            if not applied_jobs:
                mail.logout()
                return {"status": "Success", "synced_count": 0, "updates": []}

            # Scan the latest 20 emails
            for eid in email_ids[-20:]:
                status, edata = mail.fetch(eid, '(RFC822)')
                if status != 'OK':
                    continue
                raw_email = edata[0][1]
                msg = email.message_from_bytes(raw_email)
                subject = decode_mime_words(msg["subject"])
                sender = decode_mime_words(msg["from"])
                received_at = decode_mime_words(msg["date"])
                
                # Check for indicators in subject
                subject_lower = subject.lower()
                is_career_related = any(k in subject_lower or k in sender.lower() 
                                        for k in ["interview", "application", "career", "hiring", "recruit", "thank you for applying", "rejection", "unfortunately"])
                
                if not is_career_related:
                    continue
                
                # Retrieve body text
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                            break
                else:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                
                try:
                    result = classify_application_email(sender, subject, body)
                    if not result:
                        continue
                    detected_company = result.get("company", "").strip()
                    detected_status = result.get("status", "Unknown")
                    if detected_status in ["Interview", "Rejected"] and detected_company:
                        matched_job = match_job_by_company(applied_jobs, detected_company)
                        if matched_job:
                            updates.append(record_email_classification(
                                matched_job,
                                detected_status,
                                result,
                                {
                                    "subject": subject,
                                    "from": sender,
                                    "body": body,
                                    "date": received_at,
                                    "id": eid.decode("utf-8", errors="ignore") if isinstance(eid, bytes) else str(eid)
                                },
                                "imap"
                            ))
                except Exception as gen_err:
                    print(f"Gemini email classify error: {gen_err}")
                        
        mail.logout()
        return {
            "status": "Success",
            "synced_count": len(updates),
            "updates": updates
        }
    except Exception as e:
        print(f"IMAP Sync failed: {e}")
        raise HTTPException(status_code=500, detail=f"Email sync execution failed: {e}")

if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("EMAIL_SERVER_PORT", 8005))
    uvicorn.run(app, host="0.0.0.0", port=port)

