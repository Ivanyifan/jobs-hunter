import os
import json
import sqlite3
from typing import Any, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from adapters.workday.apply_runs import ApplyRunService

app = FastAPI(title="SaaS WebSocket Gateway Server", version="1.0.0")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Active connections of Chrome Extensions
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"Chrome Extension connected! Total active extensions: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            print(f"Chrome Extension disconnected! Remaining: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"Error sending message to extension: {e}")

manager = ConnectionManager()

# SQLite Database path for recording statuses
sqlite_db_path = os.path.join(os.path.dirname(__file__), "..", "data", "applications.db")
cookies_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "cookies.json")

class CookiesPayload(BaseModel):
    cookies: list

class ApplyPayload(BaseModel):
    url: str
    company: str
    role: str
    resumeV1Text: str
    userData: dict
    jobId: str

class ApplyRunCreatePayload(BaseModel):
    job_url: Optional[str] = None
    url: Optional[str] = None
    profile: dict[str, Any] = {}
    context: dict[str, Any] = {}
    user_data: dict[str, Any] = {}
    confirm_submit: bool = False
    per_stage_timeout_seconds: Optional[float] = None
    total_timeout_seconds: Optional[float] = None
    heartbeat_interval_seconds: Optional[float] = None

    class Config:
        extra = "allow"


apply_run_service = ApplyRunService()


def _payload_dict(payload: BaseModel) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    return payload.dict()

@app.post("/api/cookies")
def save_cookies(payload: CookiesPayload):
    try:
        os.makedirs(os.path.dirname(cookies_file_path), exist_ok=True)
        with open(cookies_file_path, "w", encoding="utf-8") as f:
            json.dump(payload.cookies, f, indent=2)
        print(f"Saved {len(payload.cookies)} cookies successfully.")
        return {"status": "success", "message": f"Saved {len(payload.cookies)} cookies."}
    except Exception as e:
        print(f"Failed to save cookies: {e}")
        raise HTTPException(status_code=500, detail=str(e))

import asyncio

async def queue_dispatcher_loop():
    print("Starting background Queue Dispatcher loop...")
    while True:
        try:
            await asyncio.sleep(5)
            if os.getenv("AUTO_DISPATCH_QUEUE", "false").lower() != "true":
                continue
            if manager.active_connections:
                conn = sqlite3.connect(sqlite_db_path)
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
                # Try table schema migration for apply_url column
                try:
                    cursor.execute("ALTER TABLE mcp_applications ADD COLUMN apply_url TEXT")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
                
                # Fetch first queued job
                cursor.execute("SELECT id, company, role, resume_v1, apply_url FROM mcp_applications WHERE status = 'Queued' ORDER BY created_at ASC LIMIT 1")
                row = cursor.fetchone()
                if row:
                    job_id, company, role, resume_v1, apply_url = row
                    print(f"Queue Dispatcher: Found queued job {job_id} for {company} ({role}). Dispatching to extension...")
                    
                    cursor.execute("UPDATE mcp_applications SET status = 'Applying' WHERE id = ?", (job_id,))
                    conn.commit()
                    
                    # Read user data from scheduler config
                    user_data = {
                        "first_name": "Yifan",
                        "last_name": "Ivan",
                        "email": "yifan.ivan@example.com",
                        "phone": "+1 (555) 304-2900"
                    }
                    config_path = os.path.join(os.path.dirname(__file__), "..", "data", "scheduler_config.json")
                    if os.path.exists(config_path):
                        try:
                            with open(config_path, "r", encoding="utf-8") as cf:
                                cfg = json.load(cf)
                                if "user_data" in cfg:
                                    user_data = cfg["user_data"].copy()
                                    user_data["resume_text"] = resume_v1
                        except Exception:
                            pass
                            
                    directive = {
                        "type": "APPLY_JOB",
                        "url": apply_url if apply_url else "http://localhost:8004/mock-form",
                        "company": company,
                        "role": role,
                        "resumeV1Text": resume_v1 if resume_v1 else "Resume Content",
                        "userData": user_data,
                        "jobId": job_id
                    }
                    await manager.broadcast(directive)
                conn.close()
        except Exception as e:
            print(f"Error in queue dispatcher loop: {e}")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(queue_dispatcher_loop())

@app.post("/api/apply")
async def trigger_extension_apply(payload: ApplyPayload):
    """
    Called by the website frontend (Streamlit / Next.js) to delegate 
    the browser automation task to the Chrome Extension via WebSocket.
    """
    if not manager.active_connections:
        raise HTTPException(
            status_code=400, 
            detail="No Chrome Extension is currently connected to the server. Please load the extension and click 'Connect' first."
        )
    
    directive = {
        "type": "APPLY_JOB",
        "url": payload.url,
        "company": payload.company,
        "role": payload.role,
        "resumeV1Text": payload.resumeV1Text,
        "userData": payload.userData,
        "jobId": payload.jobId
    }
    
    # Broadcast apply job instruction to the extensions
    await manager.broadcast(directive)
    return {"status": "Job dispatched to Chrome Extension successfully", "role": payload.role}


@app.post("/apply-runs")
def create_apply_run(payload: ApplyRunCreatePayload):
    data = _payload_dict(payload)
    if not data.get("job_url") and data.get("url"):
        data["job_url"] = data["url"]
    if not data.get("job_url"):
        raise HTTPException(status_code=422, detail="job_url is required")
    return apply_run_service.create_run(data)


@app.get("/apply-runs/{run_id}")
def get_apply_run(run_id: str):
    try:
        return apply_run_service.get_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="apply run not found")


@app.post("/apply-runs/{run_id}/cancel")
def cancel_apply_run(run_id: str):
    try:
        return apply_run_service.cancel_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="apply run not found")

@app.websocket("/ws/extension")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Receive status updates from the Chrome Extension
            data = await websocket.receive_text()
            message = json.loads(data)
            print(f"Received from extension: {message}")
            
            if message.get("type") == "APPLICATION_RESULT":
                # Update status in SQLite application database
                try:
                    conn = sqlite3.connect(sqlite_db_path)
                    cursor = conn.cursor()
                    # Check if table exists
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS mcp_applications (
                            id TEXT PRIMARY KEY,
                            company TEXT NOT NULL,
                            role TEXT NOT NULL,
                            resume_v0 TEXT NOT NULL,
                            resume_v1 TEXT,
                            status TEXT NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    
                    status = message.get("status", "Applied")
                    job_id = message.get("jobId")
                    company = message.get("company", "Unknown")
                    role = message.get("role", "Unknown")
                    
                    # Update status if present, otherwise insert record
                    cursor.execute("SELECT id FROM mcp_applications WHERE id = ?", (job_id,))
                    row = cursor.fetchone()
                    if row:
                        cursor.execute("UPDATE mcp_applications SET status = ? WHERE id = ?", (status, job_id))
                        print(f"Updated job {job_id} status to {status} in SQLite.")
                    else:
                        cursor.execute(
                            "INSERT INTO mcp_applications (id, company, role, resume_v0, resume_v1, status) VALUES (?, ?, ?, ?, ?, ?)",
                            (job_id, company, role, "Submitted via Chrome Extension", "Revised via Extension", status)
                        )
                        print(f"Inserted new job {job_id} as {status} in SQLite.")
                    
                    conn.commit()
                    conn.close()
                except Exception as db_err:
                    print(f"Database update failed in gateway: {db_err}")
                    
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("GATEWAY_PORT", 8000))
    print(f"WebSocket Gateway Server starting on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
