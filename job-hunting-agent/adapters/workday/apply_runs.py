from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
import concurrent.futures
import re
import threading
import time
import traceback
import uuid
from typing import Any, Callable

from .contracts import OutcomeType, StageResult, safe_diagnostic_value


QUEUED = "queued"
RUNNING = "running"
BLOCKED = "blocked"
COMPLETE = "complete"
FAILED = "failed"
CANCELED = "canceled"
TIMED_OUT = "timed_out"
TERMINAL_STATUSES = {BLOCKED, COMPLETE, FAILED, CANCELED, TIMED_OUT}

DEFAULT_PER_STAGE_TIMEOUT_SECONDS = 90.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 420.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0
DEFAULT_STAGE_CANCEL_GRACE_SECONDS = 1.0
APPLY_RUNNER_NOT_CONFIGURED = "APPLY_RUNNER_NOT_CONFIGURED"

_UNSET = object()
_TERMINAL = object()
_READY_TO_SUBMIT = object()


class ApplyRunCanceled(Exception):
    """Raised inside a cooperative apply stage when cancellation is requested."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip())
    return slug.strip("._") or "screenshot"


def _coerce_seconds(payload: dict[str, Any], keys: tuple[str, ...], default: float) -> float:
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        return max(0.001, seconds)
    return default


def _stage_name(stage_runner: Any) -> str:
    for attr in ("stage", "name"):
        value = getattr(stage_runner, attr, "")
        if value:
            return _normalize_stage(value)
    name = getattr(stage_runner, "__name__", "")
    return _normalize_stage(name or "apply")


def _normalize_stage(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip() or "UNKNOWN"


def _normalize_outcome(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _last_action_and_field(result: StageResult) -> tuple[str, str]:
    if not result.actions:
        return "", ""
    action = result.actions[-1]
    return str(action.action or ""), str(action.field or action.target or "")


def _result_url(result: StageResult) -> str:
    if result.snapshot is not None:
        return str(result.snapshot.url or "")
    return ""


def _result_blocker(result: StageResult) -> Any:
    if result.blockers:
        return list(result.blockers)
    unresolved = result.to_dict().get("unresolved_required_fields") or []
    if unresolved:
        return unresolved
    return result.blocked_reason or result.needs_user_action or result.message or result.normalized_outcome()


@dataclass
class TraceEvent:
    timestamp: str = dataclass_field(default_factory=_utc_now)
    stage: str = ""
    current_url: str = ""
    action: str = ""
    field: str = ""
    outcome: str = ""
    blocker: Any = None
    screenshot_path: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "stage": self.stage,
            "current_url": self.current_url,
            "action": self.action,
            "field": self.field,
            "outcome": self.outcome,
            "blocker": safe_diagnostic_value(self.blocker),
            "screenshot_path": self.screenshot_path,
            "message": self.message,
        }


@dataclass
class ApplyRun:
    run_id: str
    status: str = QUEUED
    stage: str = ""
    current_url: str = ""
    last_action: str = ""
    last_field: str = ""
    outcome: str = ""
    blocker: Any = None
    screenshot_path: str = ""
    trace_events: list[TraceEvent] = dataclass_field(default_factory=list)
    heartbeat_at: str = dataclass_field(default_factory=_utc_now)
    created_at: str = dataclass_field(default_factory=_utc_now)
    updated_at: str = dataclass_field(default_factory=_utc_now)
    completed_at: str = ""
    cancel_requested: bool = False
    error: str = ""
    confirm_submit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stage": self.stage,
            "current_url": self.current_url,
            "last_action": self.last_action,
            "last_field": self.last_field,
            "outcome": self.outcome,
            "blocker": safe_diagnostic_value(self.blocker),
            "screenshot_path": self.screenshot_path,
            "trace_events": [event.to_dict() for event in self.trace_events],
            "heartbeat_at": self.heartbeat_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "cancel_requested": self.cancel_requested,
            "error": self.error,
            "confirm_submit": self.confirm_submit,
        }


class ApplyRunRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, ApplyRun] = {}

    def add(self, run: ApplyRun) -> None:
        with self._lock:
            self._runs[run.run_id] = run

    def get(self, run_id: str) -> ApplyRun:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            return run

    def to_dict(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return self.get(run_id).to_dict()

    def update(self, run_id: str, **updates: Any) -> ApplyRun:
        with self._lock:
            run = self.get(run_id)
            for key, value in updates.items():
                if value is _UNSET:
                    continue
                setattr(run, key, value)
            run.updated_at = _utc_now()
            return run

    def heartbeat(self, run_id: str) -> ApplyRun:
        now = _utc_now()
        return self.update(run_id, heartbeat_at=now, updated_at=now)

    def append_trace(self, run_id: str, event: TraceEvent) -> ApplyRun:
        with self._lock:
            run = self.get(run_id)
            run.trace_events.append(event)
            run.updated_at = _utc_now()
            return run

    def request_cancel(self, run_id: str) -> ApplyRun:
        with self._lock:
            run = self.get(run_id)
            run.cancel_requested = True
            run.updated_at = _utc_now()
            return run


class ApplyRunContext:
    def __init__(
        self,
        service: "ApplyRunService",
        run_id: str,
        payload: dict[str, Any],
        artifact_root: Path,
        screenshot_writer: Callable[..., str | None] | None = None,
    ) -> None:
        self.service = service
        self.run_id = run_id
        self.payload = payload
        self.artifact_root = artifact_root
        self.screenshot_writer = screenshot_writer
        self.page: Any = None
        self.context: Any = None
        self.browser: Any = None
        self._cleanup_callbacks: list[Callable[[], Any]] = []
        self._cleanup_lock = threading.Lock()

    def run_status(self) -> dict[str, Any]:
        return self.service.get_run(self.run_id)

    def cancel_requested(self) -> bool:
        return bool(self.service.registry.get(self.run_id).cancel_requested)

    def check_canceled(self) -> None:
        if self.cancel_requested():
            raise ApplyRunCanceled("apply run canceled")

    def heartbeat(self) -> None:
        self.service.registry.heartbeat(self.run_id)

    def register_cleanup(self, callback: Callable[[], Any]) -> None:
        with self._cleanup_lock:
            self._cleanup_callbacks.append(callback)

    def set_page(self, page: Any) -> None:
        self.page = page

    def set_context(self, context: Any) -> None:
        self.context = context

    def set_browser(self, browser: Any) -> None:
        self.browser = browser

    def update(
        self,
        *,
        status: Any = _UNSET,
        stage: Any = _UNSET,
        current_url: Any = _UNSET,
        last_action: Any = _UNSET,
        last_field: Any = _UNSET,
        outcome: Any = _UNSET,
        blocker: Any = _UNSET,
        screenshot_path: Any = _UNSET,
        error: Any = _UNSET,
        completed_at: Any = _UNSET,
        message: str = "",
        trace: bool = False,
    ) -> None:
        if self.service.registry.get(self.run_id).status in TERMINAL_STATUSES:
            return
        updates = {
            "status": status,
            "stage": _normalize_stage(stage) if stage is not _UNSET else _UNSET,
            "current_url": str(current_url or "") if current_url is not _UNSET else _UNSET,
            "last_action": str(last_action or "") if last_action is not _UNSET else _UNSET,
            "last_field": str(last_field or "") if last_field is not _UNSET else _UNSET,
            "outcome": _normalize_outcome(outcome) if outcome is not _UNSET else _UNSET,
            "blocker": blocker,
            "screenshot_path": str(screenshot_path or "") if screenshot_path is not _UNSET else _UNSET,
            "error": str(error or "") if error is not _UNSET else _UNSET,
            "completed_at": str(completed_at or "") if completed_at is not _UNSET else _UNSET,
        }
        run = self.service.registry.update(self.run_id, **updates)
        if trace:
            self.append_trace(
                stage=run.stage,
                current_url=run.current_url,
                action=run.last_action,
                field=run.last_field,
                outcome=run.outcome,
                blocker=run.blocker,
                screenshot_path=run.screenshot_path,
                message=message,
                _allow_after_terminal=True,
            )

    def append_trace(
        self,
        *,
        stage: str = "",
        current_url: str = "",
        action: str = "",
        field: str = "",
        outcome: str = "",
        blocker: Any = None,
        screenshot_path: str = "",
        message: str = "",
        _allow_after_terminal: bool = False,
    ) -> None:
        current = self.service.registry.get(self.run_id)
        if current.status in TERMINAL_STATUSES and not _allow_after_terminal:
            return
        event = TraceEvent(
            stage=_normalize_stage(stage or current.stage),
            current_url=current_url or current.current_url,
            action=action or current.last_action,
            field=field or current.last_field,
            outcome=outcome or current.outcome,
            blocker=blocker if blocker is not None else current.blocker,
            screenshot_path=screenshot_path or current.screenshot_path,
            message=message,
        )
        self.service.registry.append_trace(self.run_id, event)

    def capture_screenshot(self, reason: str = "terminal") -> str:
        run_dir = self.artifact_root / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{_safe_slug(reason)}_{int(time.time() * 1000)}.png"
        screenshot_path = ""
        if self.screenshot_writer is not None:
            screenshot_path = self._call_screenshot_writer(str(path), reason)
        elif self.page is not None and callable(getattr(self.page, "screenshot", None)):
            self.page.screenshot(path=str(path), full_page=True)
            screenshot_path = str(path)
        if screenshot_path:
            self.update(screenshot_path=screenshot_path)
        return screenshot_path

    def _call_screenshot_writer(self, path: str, reason: str) -> str:
        writer = self.screenshot_writer
        if writer is None:
            return ""
        try:
            result = writer(self, path, reason)
        except TypeError:
            try:
                result = writer(self.page, path)
            except TypeError:
                result = writer(path)
        return str(result or path)

    def close_resources(self) -> None:
        callbacks: list[Callable[[], Any]] = []
        seen: set[int] = set()
        for resource in (self.page, self.context, self.browser):
            close = getattr(resource, "close", None)
            if callable(close) and id(close) not in seen:
                seen.add(id(close))
                callbacks.append(close)
        with self._cleanup_lock:
            callbacks.extend(self._cleanup_callbacks)
            self._cleanup_callbacks = []
        for callback in callbacks:
            try:
                callback()
            except Exception as err:
                self.append_trace(action="cleanup", outcome="CLEANUP_ERROR", message=str(err))


class _FinalSubmitStage:
    stage = "FINAL_SUBMIT"

    def __init__(self, callback: Callable[[ApplyRunContext, dict[str, Any]], Any]) -> None:
        self.callback = callback

    def run(self, ctx: ApplyRunContext, payload: dict[str, Any]) -> Any:
        return self.callback(ctx, payload)


class ApplyRunService:
    def __init__(
        self,
        *,
        registry: ApplyRunRegistry | None = None,
        artifact_root: str | Path | None = None,
        stage_runners: list[Any] | None = None,
        final_submit_callback: Callable[[ApplyRunContext, dict[str, Any]], Any] | None = None,
        screenshot_writer: Callable[..., str | None] | None = None,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.registry = registry or ApplyRunRegistry()
        self.artifact_root = Path(artifact_root or Path("artifacts") / "apply-runs")
        self.stage_runners = list(stage_runners or [])
        self.final_submit_callback = final_submit_callback
        self.screenshot_writer = screenshot_writer
        self.run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex)
        self._threads: dict[str, threading.Thread] = {}
        self._thread_lock = threading.Lock()

    def create_run(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(payload or {})
        confirm_submit = bool(payload.get("confirm_submit", False))
        run_id = str(payload.get("run_id") or self.run_id_factory())
        now = _utc_now()
        run = ApplyRun(
            run_id=run_id,
            status=QUEUED,
            current_url=str(payload.get("job_url") or payload.get("url") or ""),
            heartbeat_at=now,
            created_at=now,
            updated_at=now,
            confirm_submit=confirm_submit,
        )
        self.registry.add(run)
        thread = threading.Thread(
            target=self._worker,
            args=(run_id, payload),
            name=f"apply-run-{run_id}",
            daemon=True,
        )
        with self._thread_lock:
            self._threads[run_id] = thread
        thread.start()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self.registry.to_dict(run_id)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        self.registry.request_cancel(run_id)
        run = self.registry.get(run_id)
        self.registry.append_trace(
            run_id,
            TraceEvent(
                stage=run.stage,
                current_url=run.current_url,
                action="cancel_requested",
                field=run.last_field,
                outcome=run.outcome,
                blocker=run.blocker,
                screenshot_path=run.screenshot_path,
                message="cancel requested",
            ),
        )
        return self.get_run(run_id)

    def wait_for_terminal(self, run_id: str, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.get_run(run_id)
            if run["status"] in TERMINAL_STATUSES:
                return run
            time.sleep(0.01)
        return self.get_run(run_id)

    def is_worker_alive(self, run_id: str) -> bool:
        with self._thread_lock:
            thread = self._threads.get(run_id)
        return bool(thread and thread.is_alive())

    def _worker(self, run_id: str, payload: dict[str, Any]) -> None:
        ctx = ApplyRunContext(
            self,
            run_id,
            payload,
            self.artifact_root,
            screenshot_writer=self.screenshot_writer,
        )
        per_stage_timeout = _coerce_seconds(
            payload,
            ("per_stage_timeout_seconds", "per_stage_timeout", "stage_timeout_seconds", "stage_timeout"),
            DEFAULT_PER_STAGE_TIMEOUT_SECONDS,
        )
        total_timeout = _coerce_seconds(
            payload,
            ("total_timeout_seconds", "total_timeout"),
            DEFAULT_TOTAL_TIMEOUT_SECONDS,
        )
        heartbeat_interval = _coerce_seconds(
            payload,
            ("heartbeat_interval_seconds", "heartbeat_interval"),
            DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        )
        stage_cancel_grace = _coerce_seconds(
            payload,
            ("stage_cancel_grace_seconds", "cancel_grace_seconds", "stage_stop_grace_seconds"),
            DEFAULT_STAGE_CANCEL_GRACE_SECONDS,
        )
        total_deadline = time.monotonic() + total_timeout
        ctx.update(status=RUNNING, stage="queued", last_action="start_run", trace=True, message="apply run started")
        try:
            stage_runners = self._stage_runners(payload)
            if not stage_runners:
                self._finish_not_configured(ctx)
                return
            stage_ran = False
            for stage_runner in stage_runners:
                ctx.check_canceled()
                if time.monotonic() >= total_deadline:
                    self._finish_timeout(ctx, "TOTAL_TIMEOUT")
                    return
                stage_ran = True
                result = self._run_stage_with_timeout(
                    ctx,
                    stage_runner,
                    payload,
                    per_stage_timeout,
                    total_deadline,
                    heartbeat_interval,
                    stage_cancel_grace,
                )
                if result is _TERMINAL:
                    return
                if self._handle_stage_result(ctx, result) is _READY_TO_SUBMIT:
                    self._handle_final_submit_gate(
                        ctx,
                        payload,
                        per_stage_timeout,
                        total_deadline,
                        heartbeat_interval,
                        stage_cancel_grace,
                    )
                    return
                if self.registry.get(run_id).status in TERMINAL_STATUSES:
                    return
            if not stage_ran:
                self._finish_not_configured(ctx)
                return
            self._handle_final_submit_gate(
                ctx,
                payload,
                per_stage_timeout,
                total_deadline,
                heartbeat_interval,
                stage_cancel_grace,
            )
        except ApplyRunCanceled:
            self._finish_canceled(ctx)
        except Exception as err:
            self._finish_failed(ctx, err)
        finally:
            ctx.close_resources()

    def _stage_runners(self, payload: dict[str, Any]) -> list[Any]:
        runners = payload.get("stage_runners")
        if isinstance(runners, list):
            return runners
        return list(self.stage_runners)

    def _call_stage(self, stage_runner: Any, ctx: ApplyRunContext, payload: dict[str, Any]) -> Any:
        if hasattr(stage_runner, "run") and callable(stage_runner.run):
            return stage_runner.run(ctx, payload)
        return stage_runner(ctx, payload)

    def _run_stage_with_timeout(
        self,
        ctx: ApplyRunContext,
        stage_runner: Any,
        payload: dict[str, Any],
        per_stage_timeout: float,
        total_deadline: float,
        heartbeat_interval: float,
        stage_cancel_grace: float,
    ) -> Any:
        stage = _stage_name(stage_runner)
        ctx.update(stage=stage, last_action="stage_start", trace=True, message=f"{stage} started")
        stage_deadline = min(time.monotonic() + per_stage_timeout, total_deadline)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"apply-stage-{ctx.run_id}")
        future = executor.submit(self._call_stage, stage_runner, ctx, payload)
        next_heartbeat = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                if future.done():
                    result = future.result()
                    executor.shutdown(wait=True)
                    return result
                if ctx.cancel_requested():
                    self._request_stage_cancellation(
                        ctx,
                        outcome="CANCELED",
                        message="stage cancellation requested",
                    )
                    ctx.close_resources()
                    self._wait_for_stage_exit(
                        ctx,
                        future,
                        executor,
                        stage_cancel_grace,
                        outcome="CANCELED",
                    )
                    self._finish_canceled(ctx)
                    executor.shutdown(wait=False, cancel_futures=True)
                    return _TERMINAL
                if now >= stage_deadline:
                    outcome = "TOTAL_TIMEOUT" if now >= total_deadline else "STAGE_TIMEOUT"
                    self._request_stage_cancellation(
                        ctx,
                        outcome=outcome,
                        message=f"cancellation requested due to {outcome}",
                    )
                    screenshot_path = ctx.capture_screenshot(_safe_slug(outcome.lower()))
                    ctx.close_resources()
                    self._wait_for_stage_exit(
                        ctx,
                        future,
                        executor,
                        stage_cancel_grace,
                        outcome=outcome,
                    )
                    self._finish_timeout(ctx, outcome, screenshot_path=screenshot_path)
                    executor.shutdown(wait=False, cancel_futures=True)
                    return _TERMINAL
                if now >= next_heartbeat:
                    ctx.heartbeat()
                    next_heartbeat = now + heartbeat_interval
                time.sleep(min(0.05, max(0.005, heartbeat_interval / 2)))
        except ApplyRunCanceled:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        except Exception:
            executor.shutdown(wait=False, cancel_futures=True)
            raise

    def _request_stage_cancellation(self, ctx: ApplyRunContext, *, outcome: str, message: str) -> None:
        self.registry.request_cancel(ctx.run_id)
        run = self.registry.get(ctx.run_id)
        self.registry.append_trace(
            ctx.run_id,
            TraceEvent(
                stage=run.stage,
                current_url=run.current_url,
                action="cancel_requested",
                field=run.last_field,
                outcome=outcome,
                blocker=run.blocker,
                screenshot_path=run.screenshot_path,
                message=message,
            ),
        )

    def _wait_for_stage_exit(
        self,
        ctx: ApplyRunContext,
        future: concurrent.futures.Future,
        executor: concurrent.futures.ThreadPoolExecutor,
        grace_seconds: float,
        *,
        outcome: str,
    ) -> bool:
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if future.done():
                try:
                    future.result()
                except ApplyRunCanceled:
                    pass
                except Exception as err:
                    run = self.registry.get(ctx.run_id)
                    self.registry.append_trace(
                        ctx.run_id,
                        TraceEvent(
                            stage=run.stage,
                            current_url=run.current_url,
                            action="stage_exit_error_after_cancel",
                            field=run.last_field,
                            outcome=outcome,
                            blocker=run.blocker,
                            screenshot_path=run.screenshot_path,
                            message="stage exited with error after cancellation request: "
                            + "".join(traceback.format_exception_only(type(err), err)).strip(),
                        ),
                    )
                executor.shutdown(wait=True)
                return True
            time.sleep(min(0.02, max(0.005, grace_seconds / 4)))
        if not future.done():
            run = self.registry.get(ctx.run_id)
            self.registry.append_trace(
                ctx.run_id,
                TraceEvent(
                    stage=run.stage,
                    current_url=run.current_url,
                    action="stage_thread_still_running",
                    field=run.last_field,
                    outcome=outcome,
                    blocker=run.blocker,
                    screenshot_path=run.screenshot_path,
                    message="stage runner did not exit after cancellation grace; stage runners must call ctx.check_canceled",
                ),
            )
            return False
        return True

    def _handle_stage_result(self, ctx: ApplyRunContext, result: Any) -> Any:
        if result is None:
            ctx.update(last_action="stage_complete", outcome=OutcomeType.COMPLETE.value, trace=True, message="stage completed")
            return None
        if isinstance(result, StageResult):
            return self._handle_contract_stage_result(ctx, result)
        if isinstance(result, dict):
            return self._handle_dict_stage_result(ctx, result)
        ctx.append_trace(action="stage_complete", outcome=OutcomeType.COMPLETE.value, message=str(result))
        return None

    def _handle_contract_stage_result(self, ctx: ApplyRunContext, result: StageResult) -> Any:
        action, field = _last_action_and_field(result)
        normalized = result.normalized_outcome()
        outcome = "WORKDAY_LOADING_STUCK" if result.status == "WORKDAY_LOADING_STUCK" else normalized
        ctx.update(
            stage=_normalize_stage(result.stage),
            current_url=_result_url(result),
            last_action=action or "stage_result",
            last_field=field,
            outcome=outcome,
            blocker=_result_blocker(result) if normalized not in {OutcomeType.COMPLETE.value, OutcomeType.READY_TO_SUBMIT.value} else "",
            screenshot_path=result.screenshot_path or "",
            trace=True,
            message=result.message or result.blocked_reason or outcome,
        )
        if normalized == OutcomeType.READY_TO_SUBMIT.value:
            return _READY_TO_SUBMIT
        if normalized == OutcomeType.SUBMITTED.value:
            self._finish_complete(ctx, outcome=OutcomeType.SUBMITTED.value, message="application submitted")
            return _TERMINAL
        if normalized == OutcomeType.COMPLETE.value:
            return None
        if normalized == OutcomeType.RETRYABLE.value:
            return None
        screenshot_path = result.screenshot_path or ctx.capture_screenshot(_safe_slug(outcome.lower()))
        self._finish_blocked(ctx, outcome=outcome, blocker=_result_blocker(result), screenshot_path=screenshot_path)
        return _TERMINAL

    def _handle_dict_stage_result(self, ctx: ApplyRunContext, result: dict[str, Any]) -> Any:
        status = str(result.get("status") or "").lower()
        outcome = _normalize_outcome(result.get("outcome") or result.get("outcome_type"))
        stage = result.get("stage") or _UNSET
        current_url = result.get("current_url") or result.get("url") or _UNSET
        last_action = result.get("last_action") or result.get("action") or _UNSET
        last_field = result.get("last_field") or result.get("field") or _UNSET
        blocker = result.get("blocker") or result.get("blocked_reason") or result.get("missing_required") or _UNSET
        screenshot_path = result.get("screenshot_path") or _UNSET
        ctx.update(
            status=status if status in {RUNNING, QUEUED} else _UNSET,
            stage=stage,
            current_url=current_url,
            last_action=last_action,
            last_field=last_field,
            outcome=outcome or _UNSET,
            blocker=blocker,
            screenshot_path=screenshot_path,
            trace=True,
            message=str(result.get("message") or outcome or status or "stage result"),
        )
        if status in {BLOCKED, FAILED, CANCELED, TIMED_OUT, COMPLETE}:
            if status == BLOCKED:
                shot = str(result.get("screenshot_path") or "") or ctx.capture_screenshot(_safe_slug((outcome or "blocked").lower()))
                self._finish_blocked(ctx, outcome=outcome or "BLOCKED", blocker=blocker if blocker is not _UNSET else "", screenshot_path=shot)
                return _TERMINAL
            if status == FAILED:
                self._finish_failed(ctx, RuntimeError(str(result.get("error") or result.get("message") or "apply stage failed")))
                return _TERMINAL
            if status == CANCELED:
                self._finish_canceled(ctx)
                return _TERMINAL
            if status == TIMED_OUT:
                self._finish_timeout(ctx, outcome or "STAGE_TIMEOUT")
                return _TERMINAL
            if status == COMPLETE:
                self._finish_complete(ctx, outcome=outcome or COMPLETE, message=str(result.get("message") or "apply run complete"))
                return _TERMINAL
        if outcome == OutcomeType.READY_TO_SUBMIT.value or result.get("ready_to_submit"):
            return _READY_TO_SUBMIT
        if outcome in {
            "WORKDAY_LOADING_STUCK",
            OutcomeType.LOADING_STUCK.value,
            OutcomeType.AUTH_BLOCKED.value,
            OutcomeType.NAVIGATION_BLOCKED.value,
            OutcomeType.MY_INFORMATION_BLOCKED.value,
            OutcomeType.MY_EXPERIENCE_BLOCKED.value,
            OutcomeType.BLOCKED_ON_QUESTIONS.value,
            OutcomeType.NEEDS_TECHNICAL_REVIEW.value,
        }:
            shot = str(result.get("screenshot_path") or "") or ctx.capture_screenshot(_safe_slug(outcome.lower()))
            self._finish_blocked(ctx, outcome=outcome, blocker=blocker if blocker is not _UNSET else outcome, screenshot_path=shot)
            return _TERMINAL
        return None

    def _handle_final_submit_gate(
        self,
        ctx: ApplyRunContext,
        payload: dict[str, Any],
        per_stage_timeout: float,
        total_deadline: float,
        heartbeat_interval: float,
        stage_cancel_grace: float,
    ) -> None:
        if not bool(payload.get("confirm_submit", False)):
            self._finish_complete(
                ctx,
                outcome="pre_submit_review",
                message="final submit skipped by confirm_submit=false",
                action="skip_final_submit",
            )
            return
        if self.final_submit_callback is None:
            self._finish_complete(
                ctx,
                outcome="submit_confirmation_allowed",
                message="confirm_submit=true and no final submit callback configured",
                action="confirm_submit_true",
            )
            return
        result = self._run_stage_with_timeout(
            ctx,
            _FinalSubmitStage(self.final_submit_callback),
            payload,
            per_stage_timeout,
            total_deadline,
            heartbeat_interval,
            stage_cancel_grace,
        )
        if result is _TERMINAL:
            return
        handled = self._handle_stage_result(ctx, result)
        if handled is _TERMINAL:
            return
        if self.registry.get(ctx.run_id).status not in TERMINAL_STATUSES:
            self._finish_complete(ctx, outcome=OutcomeType.SUBMITTED.value, message="final submit callback completed")

    def _finish_complete(
        self,
        ctx: ApplyRunContext,
        *,
        outcome: str,
        message: str,
        action: Any = _UNSET,
    ) -> None:
        ctx.update(
            status=COMPLETE,
            last_action=action,
            outcome=outcome,
            blocker="",
            completed_at=_utc_now(),
            trace=True,
            message=message,
        )

    def _finish_blocked(self, ctx: ApplyRunContext, *, outcome: str, blocker: Any, screenshot_path: str = "") -> None:
        ctx.update(
            status=BLOCKED,
            outcome=outcome,
            blocker=blocker,
            screenshot_path=screenshot_path or ctx.run_status().get("screenshot_path") or "",
            completed_at=_utc_now(),
            trace=True,
            message=f"apply run blocked: {outcome}",
        )

    def _finish_not_configured(self, ctx: ApplyRunContext) -> None:
        self._finish_blocked(
            ctx,
            outcome=APPLY_RUNNER_NOT_CONFIGURED,
            blocker={
                "reason": APPLY_RUNNER_NOT_CONFIGURED,
                "message": "no Workday apply stage runner configured",
            },
        )

    def _finish_canceled(self, ctx: ApplyRunContext) -> None:
        ctx.update(
            status=CANCELED,
            last_action="cancel",
            outcome="CANCELED",
            completed_at=_utc_now(),
            trace=True,
            message="apply run canceled",
        )

    def _finish_timeout(self, ctx: ApplyRunContext, outcome: str, screenshot_path: str = "") -> None:
        run = ctx.run_status()
        blocker = {
            "reason": outcome,
            "stage": run.get("stage") or "",
            "current_url": run.get("current_url") or "",
            "last_action": run.get("last_action") or "",
            "last_field": run.get("last_field") or "",
        }
        screenshot_path = screenshot_path or ctx.capture_screenshot(_safe_slug(outcome.lower()))
        ctx.update(
            status=TIMED_OUT,
            outcome=outcome,
            blocker=blocker,
            screenshot_path=screenshot_path,
            completed_at=_utc_now(),
            trace=True,
            message=f"apply run timed out: {outcome}",
        )

    def _finish_failed(self, ctx: ApplyRunContext, err: Exception) -> None:
        screenshot_path = ctx.capture_screenshot("failed")
        ctx.update(
            status=FAILED,
            outcome="FAILED",
            error="".join(traceback.format_exception_only(type(err), err)).strip(),
            screenshot_path=screenshot_path,
            completed_at=_utc_now(),
            trace=True,
            message="apply run failed",
        )
