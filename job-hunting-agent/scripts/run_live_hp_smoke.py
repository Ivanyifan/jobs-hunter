from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"


def now_timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def pids_listening_on_port(port: int) -> list[int]:
    command = (
        f"Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty OwningProcess"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def stop_pid(pid: int, log_path: Path, reason: str) -> None:
    try:
        append_log(log_path, f"stopping pid={pid} reason={reason}")
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception as exc:
        append_log(log_path, f"failed_to_stop pid={pid} error={exc!r}")


def stop_process(proc: subprocess.Popen | None, log_path: Path, reason: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    stop_pid(proc.pid, log_path, reason)


def start_server(progress_path: Path, run_id: str, log_path: Path) -> subprocess.Popen:
    for pid in pids_listening_on_port(8004):
        stop_pid(pid, log_path, "restart_live_smoke_server")

    env = os.environ.copy()
    env["PLAYWRIGHT_HEADLESS"] = "true"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PLAYWRIGHT_PROGRESS_FILE"] = str(progress_path)
    env["PLAYWRIGHT_PROGRESS_RUN_ID"] = run_id
    env["PLAYWRIGHT_PROGRESS_STARTED_AT"] = str(time.time())
    env["PLAYWRIGHT_PROGRESS_SCREENSHOT_DIR"] = str(LOG_DIR)

    log_handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "mcp_servers/playwright_server.py"],
        cwd=str(ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    append_log(log_path, f"server_started pid={proc.pid}")
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"playwright_server exited early code={proc.returncode}")
        if port_open(8004):
            append_log(log_path, "server_ready port=8004")
            return proc
        time.sleep(1)
    stop_process(proc, log_path, "server_start_timeout")
    raise RuntimeError("playwright_server did not listen on 8004 within 45 seconds")


def start_worker(result_path: Path, run_id: str, log_path: Path, url: str | None = None, company: str | None = None, role: str | None = None) -> subprocess.Popen:
    log_handle = log_path.open("a", encoding="utf-8")
    command = [
        sys.executable,
        "scripts/live_hp_smoke_worker.py",
        "--result-path",
        str(result_path),
        "--application-id",
        f"{run_id}-hp",
        "--batch-id",
        run_id,
    ]
    if url:
        command.extend(["--url", url])
    if company:
        command.extend(["--company", company])
    if role:
        command.extend(["--role", role])
    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    append_log(log_path, f"worker_started pid={proc.pid}")
    return proc


def heartbeat(progress: dict[str, Any], log_path: Path, started_at: float) -> None:
    payload = {
        "elapsed_seconds": round(time.time() - started_at, 1),
        "current_stage": progress.get("current_stage") or "unknown",
        "current_url": progress.get("current_url") or "",
        "last_action": progress.get("last_action") or "",
        "last_field": progress.get("last_field") or "",
        "screenshot_path": progress.get("screenshot_path") or "",
    }
    append_log(log_path, "HEARTBEAT " + json.dumps(payload, ensure_ascii=True))


def write_timeout_result(result_path: Path, log_path: Path, reason: str, progress: dict[str, Any], started_at: float) -> None:
    failure = {
        "success": False,
        "status": "timeout",
        "blocked_reason": reason,
        "elapsed_seconds": round(time.time() - started_at, 1),
        "current_stage": progress.get("current_stage") or "unknown",
        "current_url": progress.get("current_url") or "",
        "last_action": progress.get("last_action") or "",
        "last_field": progress.get("last_field") or "",
        "screenshot_path": progress.get("screenshot_path") or "",
        "dom_excerpt": progress.get("dom_excerpt") or "",
        "progress": progress,
    }
    result_path.write_text(json.dumps(failure, indent=2, ensure_ascii=False), encoding="utf-8")
    append_log(log_path, "TIMEOUT_JSON " + json.dumps(failure, ensure_ascii=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timestamp", default=now_timestamp())
    parser.add_argument("--max-seconds", type=int, default=480)
    parser.add_argument("--stage-timeout-seconds", type=int, default=90)
    parser.add_argument("--heartbeat-seconds", type=int, default=30)
    parser.add_argument("--url", default="")
    parser.add_argument("--company", default="")
    parser.add_argument("--role", default="")
    args = parser.parse_args()

    run_id = f"hp_smoke_{args.timestamp}"
    log_path = LOG_DIR / f"{run_id}.log"
    progress_path = LOG_DIR / f"{run_id}.progress.json"
    result_path = LOG_DIR / f"{run_id}.result.json"
    server_proc: subprocess.Popen | None = None
    worker_proc: subprocess.Popen | None = None
    started_at = time.time()
    last_heartbeat = 0.0
    observed_stage = "unknown"
    observed_stage_started_at = started_at

    append_log(log_path, f"run_start run_id={run_id}")
    try:
        server_proc = start_server(progress_path, run_id, log_path)
        worker_proc = start_worker(result_path, run_id, log_path, args.url, args.company, args.role)
        while True:
            progress = read_json(progress_path)
            elapsed = time.time() - started_at
            current_stage = progress.get("current_stage") or "unknown"
            if current_stage != observed_stage:
                observed_stage = current_stage
                observed_stage_started_at = time.time()
            if time.time() - last_heartbeat >= args.heartbeat_seconds:
                heartbeat(progress, log_path, started_at)
                last_heartbeat = time.time()

            if worker_proc.poll() is not None:
                append_log(log_path, f"worker_exit code={worker_proc.returncode}")
                progress = read_json(progress_path)
                heartbeat(progress, log_path, started_at)
                return worker_proc.returncode or 0

            if elapsed > args.max_seconds:
                progress = read_json(progress_path)
                write_timeout_result(result_path, log_path, "total_timeout", progress, started_at)
                stop_process(worker_proc, log_path, "total_timeout")
                stop_process(server_proc, log_path, "total_timeout")
                for pid in pids_listening_on_port(8004):
                    stop_pid(pid, log_path, "total_timeout_port_cleanup")
                return 124

            if observed_stage != "unknown" and time.time() - observed_stage_started_at > args.stage_timeout_seconds:
                progress = read_json(progress_path)
                write_timeout_result(result_path, log_path, "stage_timeout", progress, started_at)
                stop_process(worker_proc, log_path, "stage_timeout")
                stop_process(server_proc, log_path, "stage_timeout")
                for pid in pids_listening_on_port(8004):
                    stop_pid(pid, log_path, "stage_timeout_port_cleanup")
                return 124

            time.sleep(5)
    finally:
        stop_process(worker_proc, log_path, "runner_cleanup")
        stop_process(server_proc, log_path, "runner_cleanup")
        for pid in pids_listening_on_port(8004):
            stop_pid(pid, log_path, "runner_port_cleanup")
        append_log(log_path, f"run_done result_path={result_path}")


if __name__ == "__main__":
    raise SystemExit(main())
