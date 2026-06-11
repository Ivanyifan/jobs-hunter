import os
import time
import json
import sqlite3
import requests
import threading
from datetime import datetime
from google import genai
from google.genai import types

class ScheduledApplyWorker:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ScheduledApplyWorker, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.worker_thread = None
        self.stop_event = threading.Event()
        
        # Files and databases paths
        self.workspace_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) # job-hunting-agent/
        self.config_path = os.path.join(self.workspace_dir, "data", "scheduler_config.json")
        self.sqlite_db_path = os.path.join(self.workspace_dir, "data", "applications.db")
        
        # Initialize SQLite tables
        self.init_db()

    def init_db(self):
        try:
            os.makedirs(os.path.dirname(self.sqlite_db_path), exist_ok=True)
            conn = sqlite3.connect(self.sqlite_db_path)
            cursor = conn.cursor()
            # Applications table
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
            try:
                cursor.execute("ALTER TABLE mcp_applications ADD COLUMN apply_url TEXT")
            except sqlite3.OperationalError:
                pass # Already exists

            # Scheduler tasks table
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

            # Scheduler logs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS scheduler_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER,
                    run_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT NOT NULL,
                    details TEXT
                )
            """)
            try:
                cursor.execute("ALTER TABLE scheduler_logs ADD COLUMN task_id INTEGER")
            except sqlite3.OperationalError:
                pass # Already exists

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

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[Scheduler Worker] DB Init Error: {e}")

    def log_run(self, status, details, task_id=None):
        try:
            conn = sqlite3.connect(self.sqlite_db_path)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO scheduler_logs (status, details, task_id) VALUES (?, ?, ?)", (status, details, task_id))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[Scheduler Worker] Log Write Error: {e}")

    def sync_mongo_application(self, config, app_id, company, role, resume_v0="", resume_v1=None, status="Queued", apply_url=None, job_description=None, source="scheduler_worker", metadata=None):
        mongo_url = (os.getenv("MONGO_URL") or config.get("mongo_url") or "http://localhost:8001").rstrip("/")
        payload = {
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
        }
        try:
            res = requests.post(f"{mongo_url}/applications/upsert", json=payload, timeout=3)
            if res.status_code >= 400:
                print(f"[Scheduler Worker] Mongo sync failed: {res.status_code} {res.text[:300]}")
        except Exception as e:
            print(f"[Scheduler Worker] Mongo sync skipped: {e}")

    def sync_mongo_resume_version(self, config, app_id, version_label, content, source=None, job_description=None, metadata=None):
        if not content:
            return
        mongo_url = (os.getenv("MONGO_URL") or config.get("mongo_url") or "http://localhost:8001").rstrip("/")
        payload = {
            "version_label": version_label,
            "content": content,
            "source": source,
            "job_description": job_description,
            "audit_result": (metadata or {}).get("audit_result") or {},
            "metadata": metadata or {}
        }
        try:
            res = requests.post(f"{mongo_url}/applications/{app_id}/resume-versions", json=payload, timeout=3)
            if res.status_code >= 400:
                print(f"[Scheduler Worker] Mongo resume version sync failed: {res.status_code} {res.text[:300]}")
        except Exception as e:
            print(f"[Scheduler Worker] Mongo resume version sync skipped: {e}")

    def sync_mongo_artifact(self, config, app_id, artifact_type, payload, metadata=None):
        mongo_url = (os.getenv("MONGO_URL") or config.get("mongo_url") or "http://localhost:8001").rstrip("/")
        try:
            res = requests.post(f"{mongo_url}/applications/{app_id}/artifacts", json={
                "artifact_type": artifact_type,
                "payload": payload or {},
                "metadata": metadata or {}
            }, timeout=3)
            if res.status_code >= 400:
                print(f"[Scheduler Worker] Mongo artifact sync failed: {res.status_code} {res.text[:300]}")
        except Exception as e:
            print(f"[Scheduler Worker] Mongo artifact sync skipped: {e}")

    def sync_mongo_event(self, config, app_id, event_type, payload=None):
        mongo_url = (os.getenv("MONGO_URL") or config.get("mongo_url") or "http://localhost:8001").rstrip("/")
        try:
            res = requests.post(f"{mongo_url}/applications/{app_id}/events", json={
                "event_type": event_type,
                "payload": payload or {}
            }, timeout=3)
            if res.status_code >= 400:
                print(f"[Scheduler Worker] Mongo event sync failed: {res.status_code} {res.text[:300]}")
        except Exception as e:
            print(f"[Scheduler Worker] Mongo event sync skipped: {e}")

    def call_arize_audit(self, config, app_id, company, role, resume_v0, resume_v1, job_description=None, apply_url=None, source="scheduler_worker"):
        if not resume_v0 or not resume_v1:
            return None
        arize_url = (os.getenv("ARIZE_URL") or config.get("arize_url") or "http://localhost:8003").rstrip("/")
        payload = {
            "app_id": str(app_id),
            "company": company,
            "role": role,
            "apply_url": apply_url,
            "resume_v0": resume_v0,
            "resume_v1": resume_v1,
            "job_description": job_description,
            "model": config.get("model_selector"),
            "metadata": {"source": source}
        }
        try:
            res = requests.post(f"{arize_url}/audit", json=payload, timeout=45)
            if res.status_code >= 400:
                print(f"[Scheduler Worker] Arize audit failed: {res.status_code} {res.text[:300]}")
                return None
            audit_result = res.json()
            self.sync_mongo_artifact(config, app_id, "arize_resume_audit", audit_result, metadata={
                "company": company,
                "role": role,
                "trace_id": audit_result.get("trace_id"),
                "source": source
            })
            self.sync_mongo_event(config, app_id, "arize_audit_completed", {
                "passed": audit_result.get("passed"),
                "faithfulness_score": audit_result.get("faithfulness_score"),
                "jd_match_score": audit_result.get("jd_match_score"),
                "risk_score": audit_result.get("risk_score"),
                "trace_id": audit_result.get("trace_id"),
                "source": source
            })
            return audit_result
        except Exception as e:
            print(f"[Scheduler Worker] Arize audit skipped: {e}")
            return None

    def call_soma_retrieval(self, config, app_id, company, role, resume_v0, job_description):
        if not resume_v0 or not job_description:
            return None
        elastic_url = (os.getenv("ELASTIC_URL_API") or config.get("elastic_url") or "http://localhost:8002").rstrip("/")
        mongo_url = (os.getenv("MONGO_URL") or config.get("mongo_url") or "http://localhost:8001").rstrip("/")
        try:
            res = requests.post(f"{elastic_url}/retrieve-similar-episodes", json={
                "application_id": str(app_id),
                "mongo_url": mongo_url,
                "company": company,
                "role": role,
                "job_description": job_description,
                "resume_v0": resume_v0,
                "limit": 8,
                "retrieve_limit": 50,
            }, timeout=45)
            if res.status_code >= 400:
                print(f"[Scheduler Worker] SOMA retrieval failed: {res.status_code} {res.text[:300]}")
                return None
            result = res.json()
            self.sync_mongo_artifact(config, app_id, "soma_retrieval_result", result, metadata={
                "source": "scheduler_worker",
                "backend": result.get("backend"),
            })
            return result
        except Exception as e:
            print(f"[Scheduler Worker] SOMA retrieval skipped: {e}")
            return None

    def clean_generated_resume(self, text):
        import re
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:markdown|md|text)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()

    def tailor_resume_for_job(self, ai_client, model_name, resume_v0, job_description, company, role, rewrite_guidance=None):
        if not resume_v0:
            return "", "Missing resume_v0."
        if not job_description:
            return resume_v0, "Missing JD; fell back to V0."
        prompt = f"""
You are the resume optimizer inside a job application agent.

Goal:
Rewrite the original resume into a targeted V1 for this job while staying factually faithful.

Hard constraints:
1. Do not invent new companies, schools, dates, degrees, projects, tools, metrics, awards, citizenship, work authorization, or achievements.
2. You may reorder, compress, emphasize, and rephrase facts that are clearly present in V0.
3. You may use JD language only when V0 already supports that skill or experience.
4. Keep concrete metrics from V0, but do not create new numbers.
5. Return only the resume text in clean Markdown/plain text. No explanation, no JSON, no code fence.

Target:
Company: {company}
Role: {role}

SOMA Stack-Outcome Guidance:
\"\"\"
{rewrite_guidance or "No historical stack-outcome guidance is available. Use only direct JD and resume evidence."}
\"\"\"

Job Description:
\"\"\"
{job_description}
\"\"\"

Original Resume V0:
\"\"\"
{resume_v0}
\"\"\"
"""
        try:
            response = ai_client.models.generate_content(
                model=model_name or "gemini-3.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.15),
            )
            resume_v1 = self.clean_generated_resume(response.text)
            if len(resume_v1) < 200:
                return resume_v0, "Model returned too little content; fell back to V0."
            return resume_v1, None
        except Exception as e:
            return resume_v0, f"Tailor failed; fell back to V0: {e}"

    def start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            print("[Scheduler Worker] Thread already running.")
            return
        self.stop_event.clear()
        self.worker_thread = threading.Thread(target=self.run_loop, daemon=True)
        self.worker_thread.start()
        print("[Scheduler Worker] Background thread started.")

    def stop(self):
        self.stop_event.set()
        if self.worker_thread:
            self.worker_thread.join(timeout=2)
        print("[Scheduler Worker] Background thread stopped.")

    def run_loop(self):
        print("[Scheduler Worker] Starting main worker loop...")
        while not self.stop_event.is_set():
            try:
                # Sleep in small intervals to allow responsive shutdown
                for _ in range(5):
                    if self.stop_event.is_set():
                        return
                    time.sleep(1)

                # Load global config for credentials and urls
                global_config = {}
                if os.path.exists(self.config_path):
                    try:
                        with open(self.config_path, "r", encoding="utf-8") as f:
                            global_config = json.load(f)
                    except Exception as e:
                        print(f"[Scheduler Worker] Error reading global config: {e}")

                # Poll enabled tasks from SQLite
                tasks = []
                try:
                    conn = sqlite3.connect(self.sqlite_db_path)
                    cursor = conn.cursor()
                    cursor.execute("SELECT id, keywords, location, interval_minutes, last_run_time, date_posted, experience_level, remote FROM scheduler_tasks WHERE enabled = 1")
                    tasks = cursor.fetchall()
                    conn.close()
                except Exception as e:
                    print(f"[Scheduler Worker] Error reading tasks: {e}")
                    time.sleep(5)
                    continue

                now = time.time()
                for task in tasks:
                    task_id, keywords, location, interval_min, last_run, date_posted, experience_level, remote = task
                    if now - last_run >= (interval_min * 60):
                        # Execute scan for this specific task
                        self.execute_scan(task_id, keywords, location, global_config, date_posted, experience_level, remote)
                        
                        # Update last_run_time for this task
                        try:
                            conn = sqlite3.connect(self.sqlite_db_path)
                            cursor = conn.cursor()
                            cursor.execute("UPDATE scheduler_tasks SET last_run_time = ? WHERE id = ?", (now, task_id))
                            conn.commit()
                            conn.close()
                        except Exception as e:
                            print(f"[Scheduler Worker] Error updating last_run_time for task {task_id}: {e}")

            except Exception as e:
                print(f"[Scheduler Worker] Error in loop: {e}")
                time.sleep(10)

    def execute_scan(self, task_id, keywords, location, config, date_posted=None, experience_level=None, remote=None):
        user_data = config.get("user_data", {})
        active_api_key = config.get("active_api_key") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
        model_selector = config.get("model_selector", "gemini-3.5-flash")
        elastic_url = (os.getenv("ELASTIC_URL_API") or config.get("elastic_url") or "http://localhost:8002").rstrip("/")
        arize_url = (os.getenv("ARIZE_URL") or config.get("arize_url") or "http://localhost:8003").rstrip("/")
        resume_v0 = config.get("resume_v0", "")

        self.log_run("INFO", f"定时扫描启动：正在检索关键词为 '{keywords}'，地点为 '{location}' 的职位...", task_id=task_id)

        # Initialize GenAI Client
        if not active_api_key:
            self.log_run("ERROR", "扫描终止：未配置 GEMINI_API_KEY", task_id=task_id)
            return
        try:
            ai_client = genai.Client(api_key=active_api_key)
        except Exception as e:
            self.log_run("ERROR", f"扫描终止：无法初始化 Gemini 客户端: {e}", task_id=task_id)
            return

        # 1. Query Elasticsearch server for jobs
        jobs = []
        try:
            res = requests.post(f"{elastic_url}/search", json={
                "keywords": [keywords],
                "location": location,
                "limit": 3,
                "date_posted": date_posted,
                "experience_level": experience_level.split(",") if experience_level else None,
                "remote": remote.split(",") if remote else None
            }, timeout=60)
            if res.status_code == 200:
                jobs = res.json().get("jobs", [])
        except Exception as e:
            self.log_run("ERROR", f"检索职位失败：无法连接检索服务 {elastic_url}。错误: {e}", task_id=task_id)
            return

        if not jobs:
            self.log_run("WARNING", f"检索结束：未能在互联网搜索到与关键词 '{keywords}' 相关的最新岗位。", task_id=task_id)
            return

        self.log_run("INFO", f"检索到 {len(jobs)} 个相关岗位，正在进行查重过滤...", task_id=task_id)

        # 2. Filter already applied/queued jobs
        new_jobs = []
        try:
            conn = sqlite3.connect(self.sqlite_db_path)
            cursor = conn.cursor()
            for j in jobs:
                # Check ID
                cursor.execute("SELECT id FROM mcp_applications WHERE id = ?", (j.get("job_id"),))
                if cursor.fetchone():
                    continue
                # Check Company + Role
                cursor.execute("SELECT id FROM mcp_applications WHERE company = ? AND role = ?", (j.get("company"), j.get("role")))
                if cursor.fetchone():
                    continue
                new_jobs.append(j)
            conn.close()
        except Exception as filter_err:
            self.log_run("ERROR", f"过滤岗位查重失败: {filter_err}", task_id=task_id)
            return

        if not new_jobs:
            self.log_run("INFO", "过滤结束：所有检索到的岗位在本地数据库中均已投递或排队。本次无新增。", task_id=task_id)
            return

        self.log_run("INFO", f"过滤后新增 {len(new_jobs)} 个待处理岗位。开始生成 V1 简历并运行 Arize 审计...", task_id=task_id)

        # 3. Process each new job
        for job in new_jobs[:2]: # Limit to max 2 new jobs per run for safety
            company = job.get("company", "Unknown")
            role = job.get("role", "Unknown")
            job_id = job.get("job_id") if job.get("job_id") else f"job-{int(time.time())}"
            apply_link = job.get("apply_link", "")
            jd_text = job.get("job_description", "")
            soma_result = self.call_soma_retrieval(config, job_id, company, role, resume_v0, jd_text)
            if soma_result:
                self.log_run(
                    "INFO",
                    f"SOMA 检索完成：{role} @ {company}, backend={soma_result.get('backend')}, patterns={len(soma_result.get('selected_patterns', []))}",
                    task_id=task_id,
                )
            resume_v1, tailor_error = self.tailor_resume_for_job(
                ai_client,
                model_selector,
                resume_v0,
                jd_text,
                company,
                role,
                rewrite_guidance=(soma_result or {}).get("rewrite_guidance")
            )
            if tailor_error:
                self.log_run("WARNING", f"岗位 '{role} @ {company}' 简历 V1 生成告警：{tailor_error}", task_id=task_id)

            try:
                conn = sqlite3.connect(self.sqlite_db_path)
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO mcp_applications (id, company, role, resume_v0, resume_v1, status, apply_url)
                    VALUES (?, ?, ?, ?, ?, 'Queued', ?)
                """, (job_id, company, role, resume_v0, resume_v1, apply_link))
                conn.commit()
                conn.close()
                self.sync_mongo_application(
                    config,
                    job_id,
                    company,
                    role,
                    resume_v0=resume_v0,
                    resume_v1=resume_v1,
                    status="Queued",
                    apply_url=apply_link,
                    job_description=jd_text,
                    source="scheduler_worker",
                    metadata={
                        "task_id": task_id,
                        "keywords": keywords,
                        "location": location,
                        "tailor_error": tailor_error,
                        "pipeline_stage": "tailored_resume_generated",
                        "soma_backend": (soma_result or {}).get("backend"),
                        "soma_patterns": len((soma_result or {}).get("selected_patterns", []))
                    }
                )
                self.sync_mongo_resume_version(
                    config,
                    job_id,
                    "v0-queued-snapshot",
                    resume_v0,
                    source="scheduler_worker",
                    job_description=jd_text,
                    metadata={"task_id": task_id}
                )
                audit_result = self.call_arize_audit(
                    config,
                    job_id,
                    company,
                    role,
                    resume_v0,
                    resume_v1,
                    job_description=jd_text,
                    apply_url=apply_link,
                    source="scheduler_worker_queue_gate"
                )
                if audit_result and not audit_result.get("passed"):
                    conn_block = sqlite3.connect(self.sqlite_db_path)
                    cursor_block = conn_block.cursor()
                    cursor_block.execute("UPDATE mcp_applications SET status = 'Pending Arbitration' WHERE id = ?", (job_id,))
                    conn_block.commit()
                    conn_block.close()
                    self.sync_mongo_application(
                        config,
                        job_id,
                        company,
                        role,
                        resume_v0=resume_v0,
                        resume_v1=resume_v1,
                        status="Pending Arbitration",
                        apply_url=apply_link,
                        job_description=jd_text,
                        source="scheduler_worker",
                        metadata={
                            "task_id": task_id,
                            "blocked_by": "arize",
                            "trace_id": audit_result.get("trace_id"),
                            "faithfulness_score": audit_result.get("faithfulness_score")
                        }
                    )
                self.sync_mongo_resume_version(
                    config,
                    job_id,
                    "v1-tailored-arize-blocked" if audit_result and not audit_result.get("passed") else "v1-tailored-approved",
                    resume_v1,
                    source="scheduler_worker",
                    job_description=jd_text,
                    metadata={
                        "task_id": task_id,
                        "tailor_error": tailor_error,
                        "audit_result": audit_result or {},
                        "arize_trace_id": (audit_result or {}).get("trace_id")
                    }
                )
                if audit_result and not audit_result.get("passed"):
                    self.log_run("WARNING", f"新岗位 '{role} @ {company}' 已被 Arize 审计退回人工仲裁。", task_id=task_id)
                else:
                    self.log_run("SUCCESS", f"新岗位 '{role} @ {company}' 已成功加入排队队列 (Queued)，Arize trace={audit_result.get('trace_id') if audit_result else 'unavailable'}。", task_id=task_id)
            except Exception as db_err:
                self.log_run("ERROR", f"写入 SQLite 数据库失败: {db_err}", task_id=task_id)
