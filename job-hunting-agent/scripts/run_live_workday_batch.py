from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"


DEFAULT_JOBS = [
    {
        "company": "Boeing",
        "role": "Entry-Level Software Engineer/Developer",
        "posted": "Posted 2 Days Ago",
        "url": "https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/job/USA---Richardson-TX/Entry-Level-Software-Engineer-Developer_JR2026512717-2",
    },
    {
        "company": "Stord",
        "role": "Software Engineer - New Grad",
        "posted": "Posted 2 Days Ago",
        "url": "https://stord.wd503.myworkdayjobs.com/en-US/Stord_External_Career/job/HQ---Atlanta-GA/Software-Engineer---New-Grad_JR102585",
    },
    {
        "company": "Northrop Grumman",
        "role": "2026 Associate Software Engineer / Software Engineer",
        "posted": "Posted 3 Days Ago",
        "url": "https://ngc.wd1.myworkdayjobs.com/en-US/northrop_grumman_external_site/job/United-States-Florida-Melbourne/XMLNAME-2026-Associate-Software-Engineer---Software-Engineer_R10233692",
    },
    {
        "company": "State Street",
        "role": "Software Engineer, CRD- New Graduate",
        "posted": "Posted 3 Days Ago",
        "url": "https://statestreet.wd1.myworkdayjobs.com/en-US/Global/job/Burlington-Massachusetts/Software-Engineer--CRD--New-Graduate_R-792647",
    },
    {
        "company": "State Street",
        "role": "Software Engineer, CRD- New Graduate",
        "posted": "Posted 3 Days Ago",
        "url": "https://statestreet.wd1.myworkdayjobs.com/en-US/Global/job/Burlington-Massachusetts/Software-Engineer--CRD--New-Graduate_R-792654",
    },
    {
        "company": "State Street",
        "role": "Software Engineer I, Officer",
        "posted": "Posted 3 Days Ago",
        "url": "https://statestreet.wd1.myworkdayjobs.com/en-US/Global/job/Burlington-Massachusetts/Software-Engineer-I--Officer_R-789697",
    },
    {
        "company": "Boeing",
        "role": "Associate and Mid-Level Software Engineers",
        "posted": "Posted 4 Days Ago",
        "url": "https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/job/USA---Berkeley-MO/Associate-and-Mid-Level-Software-Engineers_JR2026502220-1",
    },
    {
        "company": "Boeing",
        "role": "Associate Software Test & Verification Engineer",
        "posted": "Posted 9 Days Ago",
        "url": "https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/job/USA---Tukwila-WA/Associate-Software-Test---Verification-Engineer_JR2026500660-1",
    },
    {
        "company": "Boeing",
        "role": "Entry-Level Software Engineer",
        "posted": "Posted 11 Days Ago",
        "url": "https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/job/USA---Tukwila-WA/Entry-Level-Software-Engineer_JR2026508309-1",
    },
    {
        "company": "CDW",
        "role": "Software Engineer I - Frontend",
        "posted": "Posted 30+ Days Ago",
        "url": "https://cdw.wd5.myworkdayjobs.com/en-US/Careers/job/Virtual---Illinois/Software-Engineer-I---Frontend_R26_00001465",
    },
]


def now_timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def append(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def load_result(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def summarize_result(data: dict) -> dict:
    question_blocker = data.get("question_blocker") or (data.get("discovery") or {}).get("question_blocker") or {}
    questions = question_blocker.get("questions") or []
    return {
        "success": data.get("success"),
        "status": data.get("status"),
        "stage": data.get("stage"),
        "blocked_reason": data.get("blocked_reason"),
        "current_url": data.get("current_url"),
        "screenshot_path": data.get("screenshot_path"),
        "question_count": len(questions),
        "questions": [
            {
                "text": (item.get("raw_text") or item.get("normalized_text") or "")[:240],
                "status": item.get("status"),
                "canonical_key": item.get("canonical_key"),
            }
            for item in questions[:8]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timestamp", default=now_timestamp())
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--max-jobs", type=int, default=10)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--stage-timeout-seconds", type=int, default=90)
    args = parser.parse_args()

    run_id = f"workday_batch_{args.timestamp}"
    batch_log = LOG_DIR / f"{run_id}.log"
    summary_path = LOG_DIR / f"{run_id}.summary.json"
    summaries = []

    append(batch_log, f"batch_start run_id={run_id} max_jobs={args.max_jobs}")
    selected_jobs = DEFAULT_JOBS[max(0, args.start_index - 1): args.max_jobs]
    for index, job in enumerate(selected_jobs, start=max(1, args.start_index)):
        job_ts = f"{args.timestamp}_{index:02d}"
        result_path = LOG_DIR / f"hp_smoke_{job_ts}.result.json"
        command = [
            sys.executable,
            "scripts/run_live_hp_smoke.py",
            "--timestamp",
            job_ts,
            "--max-seconds",
            str(args.max_seconds),
            "--stage-timeout-seconds",
            str(args.stage_timeout_seconds),
            "--heartbeat-seconds",
            "30",
            "--url",
            job["url"],
            "--company",
            job["company"],
            "--role",
            job["role"],
        ]
        append(batch_log, f"job_start index={index} company={job['company']} role={job['role']} posted={job['posted']}")
        started = time.time()
        try:
            proc = subprocess.run(
                command,
                cwd=str(ROOT),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=args.max_seconds + 120,
            )
            for line in (proc.stdout or "").splitlines():
                append(batch_log, f"job_{index:02d} {line}")
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            for line in (exc.stdout or "").splitlines():
                append(batch_log, f"job_{index:02d} {line}")
            exit_code = 124
            append(batch_log, f"job_timeout index={index}")

        data = load_result(result_path)
        item = {
            **job,
            "index": index,
            "exit_code": exit_code,
            "elapsed_seconds": round(time.time() - started, 1),
            "result_path": str(result_path),
            **summarize_result(data),
        }
        summaries.append(item)
        summary_path.write_text(json.dumps({"run_id": run_id, "jobs": summaries}, indent=2, ensure_ascii=False), encoding="utf-8")
        append(batch_log, "job_done " + json.dumps(item, ensure_ascii=True))

    append(batch_log, f"batch_done summary_path={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
