from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import json
import shutil
from pathlib import Path
import re
from threading import Lock, Thread
from time import sleep
from typing import Any, Callable
from urllib.parse import quote, urlparse
from uuid import uuid4


TaskHandler = Callable[..., list[str]]
ACTIVE_TASK_STATUSES = {"queued", "running", "cancelling"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_RUN_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "task_runs"
HTTP_URL_PATTERN = re.compile(r"https?://[^\s<>)\]\"']+", flags=re.IGNORECASE)
TEMP_DOWNLOAD_SUFFIXES = {".crdownload", ".download", ".part", ".tmp"}
TOKEN_USAGE_LOG_PREFIX = "token_usage:"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def empty_token_usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_cached_tokens": 0,
        "entry_count": 0,
        "cost": 0.0,
        "by_model": {},
    }


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _normalize_token_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return empty_token_usage()
    by_model: dict[str, dict[str, Any]] = {}
    raw_by_model = value.get("by_model")
    if isinstance(raw_by_model, dict):
        for raw_model, raw_stats in raw_by_model.items():
            model = str(raw_model or "").strip()
            if not model or not isinstance(raw_stats, dict):
                continue
            by_model[model] = {
                "model": str(raw_stats.get("model") or model),
                "prompt_tokens": _safe_int(raw_stats.get("prompt_tokens")),
                "completion_tokens": _safe_int(raw_stats.get("completion_tokens")),
                "total_tokens": _safe_int(raw_stats.get("total_tokens")),
                "prompt_cached_tokens": _safe_int(raw_stats.get("prompt_cached_tokens")),
                "cost": _safe_float(raw_stats.get("cost")),
                "invocations": _safe_int(raw_stats.get("invocations")),
            }
    return {
        "prompt_tokens": _safe_int(value.get("prompt_tokens") or value.get("total_prompt_tokens")),
        "completion_tokens": _safe_int(value.get("completion_tokens") or value.get("total_completion_tokens")),
        "total_tokens": _safe_int(value.get("total_tokens")),
        "prompt_cached_tokens": _safe_int(value.get("prompt_cached_tokens") or value.get("total_prompt_cached_tokens")),
        "entry_count": _safe_int(value.get("entry_count")),
        "cost": _safe_float(value.get("cost") or value.get("total_cost")),
        "by_model": by_model,
    }


def _merge_token_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left = _normalize_token_usage(left)
    right = _normalize_token_usage(right)
    merged = empty_token_usage()
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cached_tokens", "entry_count"):
        merged[key] = _safe_int(left.get(key)) + _safe_int(right.get(key))
    merged["cost"] = round(_safe_float(left.get("cost")) + _safe_float(right.get("cost")), 8)
    by_model: dict[str, dict[str, Any]] = {}
    for source in (left.get("by_model"), right.get("by_model")):
        if not isinstance(source, dict):
            continue
        for model, raw_stats in source.items():
            if not isinstance(raw_stats, dict):
                continue
            current = by_model.setdefault(
                str(model),
                {
                    "model": str(raw_stats.get("model") or model),
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "prompt_cached_tokens": 0,
                    "cost": 0.0,
                    "invocations": 0,
                },
            )
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cached_tokens", "invocations"):
                current[key] = _safe_int(current.get(key)) + _safe_int(raw_stats.get(key))
            current["cost"] = round(_safe_float(current.get("cost")) + _safe_float(raw_stats.get("cost")), 8)
    merged["by_model"] = by_model
    return merged


def extract_token_usage_from_logs(logs: list[str]) -> dict[str, Any]:
    usage = empty_token_usage()
    for line in logs:
        text = str(line or "")
        marker_index = text.find(TOKEN_USAGE_LOG_PREFIX)
        if marker_index < 0:
            continue
        payload_text = text[marker_index + len(TOKEN_USAGE_LOG_PREFIX):].strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        usage = _merge_token_usage(usage, payload)
    return usage


def token_usage_has_activity(usage: dict[str, Any]) -> bool:
    normalized = _normalize_token_usage(usage)
    return any(_safe_int(normalized.get(key)) > 0 for key in ("total_tokens", "prompt_tokens", "completion_tokens", "entry_count"))


class ManualTaskRequired(RuntimeError):
    def __init__(self, message: str, logs: list[str] | None = None) -> None:
        super().__init__(message)
        self.logs = logs or []


class TaskCancelled(RuntimeError):
    def __init__(self, message: str = "Task cancelled by user.") -> None:
        super().__init__(message)


@dataclass
class ScheduledTask:
    id: str
    name: str
    run_at_iso: str
    task_type: str
    payload: dict
    frequency: str = "once"
    status: str = "scheduled"
    logs: list[str] = field(default_factory=list)
    last_error: str | None = None
    run_count: int = 0
    last_run_at_iso: str | None = None
    created_at_iso: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cancel_requested: bool = False
    cancelled_at_iso: str | None = None
    progress_events: list[dict[str, Any]] = field(default_factory=list)
    artifact_dir: str | None = None
    deliverables: list[dict[str, str]] = field(default_factory=list)
    downloaded_files: list[str] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=empty_token_usage)


class TaskScheduler:
    def __init__(self, max_concurrent_tasks: int = 3) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._lock = Lock()
        self.max_concurrent_tasks = max(1, int(max_concurrent_tasks))
        self._active_task_ids: set[str] = set()
        self._persisted_history_loaded = False
        self._load_persisted_task_artifacts()
        self._thread = Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def add_task(
        self,
        name: str,
        run_at_iso: str,
        task_type: str,
        payload: dict,
        *,
        frequency: str = "once",
    ) -> ScheduledTask:
        status = "manual" if frequency == "manual" else "scheduled"
        task = ScheduledTask(
            id=uuid4().hex[:12],
            name=name,
            run_at_iso=run_at_iso,
            task_type=task_type,
            payload=payload,
            frequency=frequency,
            status=status,
        )
        with self._lock:
            self._tasks[task.id] = task
            self._append_event_locked(task, "created", f"Task created with status {task.status}.")
        self._persist_task_snapshot(task.id)
        return task

    def list_tasks(self) -> list[ScheduledTask]:
        with self._lock:
            return sorted(self._tasks.values(), key=self._task_sort_key)

    def _task_sort_key(self, task: ScheduledTask) -> tuple[int, float]:
        ranks = {
            "running": 0,
            "cancelling": 1,
            "queued": 2,
            "manual": 3,
            "scheduled": 4,
            "failed": 5,
            "cancelled": 6,
            "completed": 7,
        }
        sort_time = task.last_run_at_iso or task.created_at_iso or task.run_at_iso
        return (ranks.get(task.status, 8), -self._iso_timestamp(sort_time))

    def _iso_timestamp(self, value: str | None) -> float:
        if not value:
            return 0
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return 0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    def get_task(self, task_id: str) -> ScheduledTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def _load_persisted_task_artifacts(self) -> None:
        if self._persisted_history_loaded:
            return
        root = TASK_RUN_ARTIFACT_ROOT
        if not root.exists():
            self._persisted_history_loaded = True
            return

        loaded: list[ScheduledTask] = []
        for run_json_path in sorted(root.glob("*/run.json")):
            try:
                payload = json.loads(run_json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue

            task_id = str(payload.get("id") or run_json_path.parent.name).strip()
            if not task_id:
                continue
            with self._lock:
                if task_id in self._tasks:
                    continue

            task = self._task_from_snapshot(payload, run_json_path.parent, fallback_task_id=task_id)
            if task is not None:
                loaded.append(task)

        if not loaded:
            self._persisted_history_loaded = True
            return
        with self._lock:
            for task in loaded:
                self._tasks.setdefault(task.id, task)
        self._persisted_history_loaded = True

    def _task_from_snapshot(
        self,
        snapshot: dict[str, Any],
        artifact_dir: Path,
        *,
        fallback_task_id: str,
    ) -> ScheduledTask | None:
        task_id = str(snapshot.get("id") or fallback_task_id).strip()
        if not task_id:
            return None
        status = str(snapshot.get("status") or "completed").strip() or "completed"
        interrupted = status in ACTIVE_TASK_STATUSES
        if status in ACTIVE_TASK_STATUSES:
            status = "failed"
        payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
        existing_deliverables = [item for item in snapshot.get("deliverables") or [] if isinstance(item, dict)]
        file_deliverables = [item for item in existing_deliverables if item.get("kind") != "link"]
        if self._snapshot_has_invalid_pdf_fallback(snapshot):
            file_deliverables = [
                item
                for item in file_deliverables
                if not str(item.get("filename") or item.get("label") or "").lower().endswith(".pdf")
            ]
        task = ScheduledTask(
            id=task_id,
            name=str(snapshot.get("name") or "历史任务"),
            run_at_iso=str(snapshot.get("run_at_iso") or snapshot.get("last_run_at_iso") or snapshot.get("created_at_iso") or utc_now_iso()),
            task_type=str(snapshot.get("task_type") or payload.get("task_type") or "unknown"),
            payload=payload,
            frequency=str(snapshot.get("frequency") or "once"),
            status=status,
            logs=[str(item) for item in snapshot.get("logs") or []],
            last_error=str(snapshot.get("last_error")) if snapshot.get("last_error") else None,
            run_count=int(snapshot.get("run_count") or 0),
            last_run_at_iso=str(snapshot.get("last_run_at_iso")) if snapshot.get("last_run_at_iso") else None,
            created_at_iso=str(snapshot.get("created_at_iso") or utc_now_iso()),
            cancel_requested=bool(snapshot.get("cancel_requested")),
            cancelled_at_iso=str(snapshot.get("cancelled_at_iso")) if snapshot.get("cancelled_at_iso") else None,
            progress_events=[event for event in snapshot.get("progress_events") or [] if isinstance(event, dict)],
            artifact_dir=str(artifact_dir.resolve()),
            deliverables=file_deliverables + self._extract_web_link_deliverables(snapshot),
            downloaded_files=[str(item) for item in snapshot.get("downloaded_files") or []],
            token_usage=self._snapshot_token_usage(snapshot),
        )
        if interrupted and not task.last_error:
            task.last_error = "Task was interrupted before completion; restored from saved history."
        return task

    def update_status(self, task_id: str, status: str, logs: list[str] | None = None, error: str | None = None) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = status
            if logs is not None:
                task.logs = logs
                task.token_usage = extract_token_usage_from_logs(logs)
            task.last_error = error
            self._append_event_locked(task, "status", error or f"Task status changed to {status}.")
        self._persist_task_snapshot(task_id)

    def append_log(self, task_id: str, message: str) -> None:
        if not message:
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.logs.append(message)
        self._persist_task_snapshot(task_id)

    def append_progress(self, task_id: str, message: str) -> None:
        if not message:
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.logs.append(message)
            self._append_event_locked(task, "progress", message)
        self._persist_task_snapshot(task_id)

    def request_cancel(self, task_id: str) -> ScheduledTask:
        should_finalize = False
        with self._lock:
            task = self._tasks[task_id]
            if task.status in {"completed", "failed", "cancelled"}:
                return task
            task.cancel_requested = True
            task.cancelled_at_iso = utc_now_iso()
            if task.status == "running":
                task.status = "cancelling"
                task.logs.append("Stop requested by user. The current browser action will stop at the next safe checkpoint.")
                self._append_event_locked(task, "cancelling", "Stop requested by user.")
            elif task.status == "cancelling":
                pass
            else:
                task.status = "cancelled"
                task.last_error = "Task cancelled before execution."
                task.logs.append("Task cancelled before execution.")
                self._append_event_locked(task, "cancelled", task.last_error)
                should_finalize = True
        self._persist_task_snapshot(task_id)
        if should_finalize:
            self._write_task_artifacts(task_id)
        with self._lock:
            return task

    def is_cancel_requested(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            return bool(task and task.cancel_requested)

    def raise_if_cancelled(self, task_id: str) -> None:
        if self.is_cancel_requested(task_id):
            raise TaskCancelled()

    def ensure_task_artifacts(self, task_id: str) -> ScheduledTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            should_write = bool(
                task
                and not task.artifact_dir
                and task.status in {"completed", "failed", "cancelled", "manual", "scheduled"}
                and (task.run_count > 0 or task.status == "cancelled")
            )
        if should_write:
            self._write_task_artifacts(task_id)
        with self._lock:
            return self._tasks.get(task_id)

    def run_task_now(self, task_id: str) -> ScheduledTask:
        with self._lock:
            task = self._tasks[task_id]
            if task.status in {"manual", "scheduled"}:
                task.status = "queued"
                task.last_error = None
                self._append_event_locked(task, "queued", "Task queued for immediate execution.")
        self._persist_task_snapshot(task_id)
        self._start_eligible_tasks()
        return self._wait_for_task_to_finish(task_id)

    def create_and_run_task(
        self,
        *,
        name: str,
        task_type: str,
        payload: dict,
        run_at_iso: str | None = None,
    ) -> ScheduledTask:
        immediate_run_at = run_at_iso or datetime.now(timezone.utc).isoformat()
        task = ScheduledTask(
            id=uuid4().hex[:12],
            name=name,
            run_at_iso=immediate_run_at,
            task_type=task_type,
            payload=payload,
            frequency="once",
            status="queued",
        )
        with self._lock:
            self._tasks[task.id] = task
            self._append_event_locked(task, "queued", "Task queued for immediate execution.")
        self._persist_task_snapshot(task.id)
        self._start_eligible_tasks()
        return self._wait_for_task_to_finish(task.id)

    def create_and_start_task(
        self,
        *,
        name: str,
        task_type: str,
        payload: dict,
        run_at_iso: str | None = None,
    ) -> ScheduledTask:
        immediate_run_at = run_at_iso or datetime.now(timezone.utc).isoformat()
        task = ScheduledTask(
            id=uuid4().hex[:12],
            name=name,
            run_at_iso=immediate_run_at,
            task_type=task_type,
            payload=payload,
            frequency="once",
            status="queued",
        )
        with self._lock:
            self._tasks[task.id] = task
            self._append_event_locked(task, "queued", "Task queued for background execution.")
        self._persist_task_snapshot(task.id)
        self._start_eligible_tasks()
        return task

    def _schedule_next_run(self, task: ScheduledTask) -> None:
        run_at = datetime.fromisoformat(task.run_at_iso)
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        if task.frequency == "daily":
            next_run = run_at.timestamp() + 86400
        elif task.frequency == "weekly":
            next_run = run_at.timestamp() + (86400 * 7)
        else:
            return
        task.run_at_iso = datetime.fromtimestamp(next_run, tz=timezone.utc).isoformat()
        task.status = "scheduled"
        self._append_event_locked(task, "scheduled", f"Next run scheduled at {task.run_at_iso}.")

    def _active_execution_count_locked(self) -> int:
        return len(self._active_task_ids)

    def _task_is_due(self, task: ScheduledTask, now: datetime) -> bool:
        if task.status == "queued":
            return True
        if task.status != "scheduled":
            return False
        run_at = datetime.fromisoformat(task.run_at_iso)
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        return run_at <= now

    def _start_eligible_tasks(self) -> None:
        threads: list[Thread] = []
        started_task_ids: list[str] = []
        now = datetime.now(timezone.utc)
        with self._lock:
            eligible = [
                task
                for task in sorted(self._tasks.values(), key=lambda item: item.run_at_iso)
                if self._task_is_due(task, now) and not task.cancel_requested
            ]
            for task in eligible:
                if self._active_execution_count_locked() >= self.max_concurrent_tasks:
                    break
                task.status = "running"
                task.last_error = None
                task.last_run_at_iso = utc_now_iso()
                task.run_count += 1
                self._active_task_ids.add(task.id)
                self._append_event_locked(task, "started", f"Task run {task.run_count} started.")
                started_task_ids.append(task.id)
                threads.append(Thread(target=self._execute_task, args=(task.id,), daemon=True))

        for task_id in started_task_ids:
            self._persist_task_snapshot(task_id)
        for thread in threads:
            thread.start()

    def _wait_for_task_to_finish(self, task_id: str) -> ScheduledTask:
        while True:
            with self._lock:
                task = self._tasks[task_id]
                if task.status not in ACTIVE_TASK_STATUSES:
                    return task
            sleep(0.05)

    def _execute_task(self, task_id: str) -> None:
        download_dir = self._prepare_task_download_dir(task_id)
        download_snapshot = self._snapshot_download_files(download_dir)
        completion_status: str | None = None
        try:
            completion_status = self._execute_task_body(task_id)
        finally:
            self._record_downloaded_files(task_id, download_dir, download_snapshot)
            if completion_status is not None:
                self._finalize_successful_task(task_id)
            should_write_artifacts = False
            with self._lock:
                self._active_task_ids.discard(task_id)
                task = self._tasks.get(task_id)
                should_write_artifacts = bool(
                    task
                    and task.run_count > 0
                    and task.status in {"completed", "failed", "cancelled", "manual", "scheduled"}
                )
            if should_write_artifacts:
                self._write_task_artifacts(task_id)
            self._start_eligible_tasks()

    def _execute_task_body(self, task_id: str) -> str | None:
        with self._lock:
            task = self._tasks[task_id]
            if task.status == "cancelled" or task.cancel_requested:
                task.status = "cancelled"
                task.cancelled_at_iso = task.cancelled_at_iso or datetime.now(timezone.utc).isoformat()
                task.last_error = "Task cancelled by user." if task.cancel_requested else task.last_error or "Task cancelled before execution."
                if not task.logs or task.logs[-1] != task.last_error:
                    task.logs.append(task.last_error)
                self._append_event_locked(task, "cancelled", task.last_error)
                return None
        handler = TASK_HANDLERS.get(task.task_type)
        if handler is None:
            self.update_status(task_id, "failed", error=f"Unknown task type: {task.task_type}")
            return None
        def progress_callback(message: str) -> None:
            self.raise_if_cancelled(task_id)
            self.append_progress(task_id, message)
            self.raise_if_cancelled(task_id)

        try:
            self.raise_if_cancelled(task_id)
            signature = inspect.signature(handler)
            accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
            accepts_progress = "progress_callback" in signature.parameters or accepts_kwargs
            accepts_should_stop = "should_stop_callback" in signature.parameters or accepts_kwargs
            handler_kwargs: dict[str, Any] = {}
            if accepts_progress:
                handler_kwargs["progress_callback"] = progress_callback
            if accepts_should_stop:
                handler_kwargs["should_stop_callback"] = lambda: self.is_cancel_requested(task_id)
            if handler_kwargs:
                logs = handler(task.payload, **handler_kwargs)
            else:
                logs = handler(task.payload)
            self.raise_if_cancelled(task_id)
        except ManualTaskRequired as exc:
            with self._lock:
                current = self._tasks[task_id]
                if current.cancel_requested:
                    current.status = "cancelled"
                    current.cancelled_at_iso = current.cancelled_at_iso or datetime.now(timezone.utc).isoformat()
                    current.last_error = "Task cancelled by user."
                    if not current.logs or current.logs[-1] != current.last_error:
                        current.logs.append(current.last_error)
                    self._append_event_locked(current, "cancelled", current.last_error)
                else:
                    current.logs = exc.logs
                    current.token_usage = extract_token_usage_from_logs(exc.logs)
                    current.status = "manual"
                    current.last_error = str(exc)
                    self._append_event_locked(current, "manual", str(exc))
            return None
        except TaskCancelled as exc:
            with self._lock:
                current = self._tasks[task_id]
                current.status = "cancelled"
                current.cancel_requested = True
                current.cancelled_at_iso = current.cancelled_at_iso or datetime.now(timezone.utc).isoformat()
                current.last_error = str(exc)
                if not current.logs or current.logs[-1] != str(exc):
                    current.logs.append(str(exc))
                self._append_event_locked(current, "cancelled", str(exc))
            return None
        except Exception as exc:
            if self.is_cancel_requested(task_id):
                with self._lock:
                    current = self._tasks[task_id]
                    current.status = "cancelled"
                    current.cancelled_at_iso = current.cancelled_at_iso or datetime.now(timezone.utc).isoformat()
                    current.last_error = "Task cancelled by user."
                    if not current.logs or current.logs[-1] != current.last_error:
                        current.logs.append(current.last_error)
                    self._append_event_locked(current, "cancelled", current.last_error)
            else:
                self.update_status(task_id, "failed", error=str(exc))
            return None

        with self._lock:
            current = self._tasks[task_id]
            if current.cancel_requested:
                current.status = "cancelled"
                current.cancelled_at_iso = current.cancelled_at_iso or datetime.now(timezone.utc).isoformat()
                current.last_error = "Task cancelled by user."
                if not current.logs or current.logs[-1] != current.last_error:
                    current.logs.append(current.last_error)
                self._append_event_locked(current, "cancelled", current.last_error)
                return None
            current.logs = logs
            current.last_error = None
            current.token_usage = extract_token_usage_from_logs(logs)
            self._append_event_locked(current, "finalizing", "Task actions finished; collecting final files and links.")
            return "success"

    def _finalize_successful_task(self, task_id: str) -> None:
        with self._lock:
            current = self._tasks[task_id]
            if current.cancel_requested:
                current.status = "cancelled"
                current.cancelled_at_iso = current.cancelled_at_iso or datetime.now(timezone.utc).isoformat()
                current.last_error = "Task cancelled by user."
                if not current.logs or current.logs[-1] != current.last_error:
                    current.logs.append(current.last_error)
                self._append_event_locked(current, "cancelled", current.last_error)
                return
            if current.frequency == "daily" or current.frequency == "weekly":
                self._schedule_next_run(current)
            elif current.frequency == "manual":
                current.status = "manual"
                self._append_event_locked(current, "manual", "Task returned to manual state after execution.")
            else:
                current.status = "completed"
                self._append_event_locked(current, "completed", "Task completed.")
            current.last_error = None

    def _append_event_locked(self, task: ScheduledTask, event_type: str, message: str) -> None:
        task.progress_events.append(
            {
                "sequence": len(task.progress_events) + 1,
                "event_type": event_type,
                "status": task.status,
                "message": message,
                "timestamp_iso": utc_now_iso(),
            }
        )

    def _snapshot_token_usage(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        token_usage = _normalize_token_usage(snapshot.get("token_usage"))
        if token_usage_has_activity(token_usage):
            return token_usage
        return extract_token_usage_from_logs([str(item) for item in snapshot.get("logs") or []])

    def _current_task_token_usage(self, task: ScheduledTask) -> dict[str, Any]:
        log_usage = extract_token_usage_from_logs(list(task.logs))
        if token_usage_has_activity(log_usage):
            return log_usage
        return _normalize_token_usage(getattr(task, "token_usage", None))

    def _task_snapshot_locked(self, task: ScheduledTask, deliverables: list[dict[str, str]]) -> dict[str, Any]:
        token_usage = self._current_task_token_usage(task)
        return {
            "id": task.id,
            "name": task.name,
            "run_at_iso": task.run_at_iso,
            "task_type": task.task_type,
            "frequency": task.frequency,
            "status": task.status,
            "logs": list(task.logs),
            "last_error": task.last_error,
            "created_at_iso": task.created_at_iso,
            "run_count": task.run_count,
            "last_run_at_iso": task.last_run_at_iso,
            "cancel_requested": task.cancel_requested,
            "cancelled_at_iso": task.cancelled_at_iso,
            "payload": task.payload,
            "progress_events": list(task.progress_events),
            "deliverables": deliverables,
            "downloaded_files": list(task.downloaded_files),
            "token_usage": token_usage,
        }

    def _write_task_artifacts(self, task_id: str) -> None:
        artifact_dir = TASK_RUN_ARTIFACT_ROOT / task_id
        artifact_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            downloaded_files = list(task.downloaded_files)
            snapshot = self._task_snapshot_locked(task, [])

        deliverables = self._downloaded_file_deliverables(task_id, artifact_dir, downloaded_files)
        deliverables.extend(self._extract_web_link_deliverables(snapshot))
        snapshot["deliverables"] = deliverables

        (artifact_dir / "logs.txt").write_text("\n".join(snapshot["logs"]) + ("\n" if snapshot["logs"] else ""), encoding="utf-8")
        (artifact_dir / "run.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        (artifact_dir / "report.md").write_text(self._render_task_report(snapshot), encoding="utf-8")

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.artifact_dir = str(artifact_dir.resolve())
            task.deliverables = deliverables
            task.token_usage = snapshot["token_usage"]

    def _persist_task_snapshot(self, task_id: str) -> None:
        artifact_dir = TASK_RUN_ARTIFACT_ROOT / task_id
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.artifact_dir = str(artifact_dir.resolve())
            snapshot = self._task_snapshot_locked(task, list(task.deliverables))

        target = artifact_dir / "run.json"
        temp_target = artifact_dir / "run.json.tmp"
        try:
            temp_target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_target.replace(target)
        except OSError:
            try:
                temp_target.unlink(missing_ok=True)
            except OSError:
                pass

    def _prepare_task_download_dir(self, task_id: str) -> Path:
        default_download_dir = TASK_RUN_ARTIFACT_ROOT / task_id / "downloads"
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return default_download_dir
            raw_download_dir = task.payload.get("downloads_path") if isinstance(task.payload, dict) else None
            download_dir = Path(str(raw_download_dir)) if raw_download_dir else default_download_dir
            if isinstance(task.payload, dict):
                task.payload["downloads_path"] = str(download_dir.resolve())
            return download_dir

    def _snapshot_download_files(self, download_dir: Path) -> dict[str, tuple[int, int]]:
        snapshot: dict[str, tuple[int, int]] = {}
        if not download_dir.exists():
            return snapshot
        for path in download_dir.rglob("*"):
            if not self._is_completed_download_file(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    def _record_downloaded_files(self, task_id: str, download_dir: Path, before: dict[str, tuple[int, int]]) -> None:
        downloaded_files: list[str] = []
        if download_dir.exists():
            candidates: list[tuple[int, Path]] = []
            for path in download_dir.rglob("*"):
                if not self._is_completed_download_file(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                key = str(path.resolve())
                if before.get(key) == (stat.st_mtime_ns, stat.st_size):
                    continue
                candidates.append((stat.st_mtime_ns, path))
            downloaded_files = [str(path.resolve()) for _, path in sorted(candidates)]
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            existing = set(task.downloaded_files)
            task.downloaded_files.extend(path for path in downloaded_files if path not in existing)

    def _is_completed_download_file(self, path: Path) -> bool:
        if not path.is_file() or path.suffix.lower() in TEMP_DOWNLOAD_SUFFIXES:
            return False
        try:
            return path.stat().st_size > 0
        except OSError:
            return False

    def _downloaded_file_deliverables(
        self,
        task_id: str,
        artifact_dir: Path,
        downloaded_files: list[str],
    ) -> list[dict[str, str]]:
        deliverables: list[dict[str, str]] = []
        used_names: set[str] = set()
        artifact_root = artifact_dir.resolve()
        download_target_dir = artifact_dir / "downloads"

        for raw_path in downloaded_files:
            source = Path(raw_path)
            if not self._is_completed_download_file(source):
                continue
            display_name = source.name
            safe_name = self._unique_filename(display_name, used_names)
            try:
                source_path = source.resolve()
                relative_path = source_path.relative_to(artifact_root)
            except ValueError:
                download_target_dir.mkdir(parents=True, exist_ok=True)
                target_path = download_target_dir / safe_name
                try:
                    if source.resolve() != target_path.resolve():
                        shutil.copy2(source, target_path)
                except OSError:
                    continue
                relative_path = target_path.resolve().relative_to(artifact_root)

            filename = relative_path.as_posix()
            deliverables.append(
                {
                    "kind": "file",
                    "label": display_name,
                    "filename": filename,
                    "download_url": f"/api/tasks/{task_id}/artifacts/{quote(filename, safe='/')}",
                }
            )
        return deliverables

    def _unique_filename(self, filename: str, used_names: set[str]) -> str:
        candidate = filename or "downloaded-file"
        stem = Path(candidate).stem or "downloaded-file"
        suffix = Path(candidate).suffix
        index = 2
        while candidate.lower() in used_names:
            candidate = f"{stem}-{index}{suffix}"
            index += 1
        used_names.add(candidate.lower())
        return candidate

    def _extract_web_link_deliverables(self, snapshot: dict[str, Any]) -> list[dict[str, str]]:
        if snapshot.get("status") != "completed":
            return []

        candidates = self._final_link_url_candidates(snapshot)
        if not candidates:
            return []

        expected_targets = self._expected_final_url_targets(snapshot)
        if expected_targets:
            candidates = self._candidates_matching_expected_targets(candidates, expected_targets)

        candidates = self._remove_start_url_candidates(snapshot, candidates)
        if not candidates:
            return []
        url = candidates[-1].strip().rstrip(".,;:，。；）】")
        if not self._is_external_http_url(url):
            return []
        return [
            {
                "kind": "link",
                "label": self._web_link_label(url),
                "filename": "",
                "url": url,
                "download_url": url,
            }
        ]

    def _candidates_matching_expected_targets(self, candidates: list[str], expected_targets: list[str]) -> list[str]:
        for expected in reversed(expected_targets):
            matching = [candidate for candidate in candidates if self._url_matches_expected_target(candidate, expected)]
            if matching:
                return matching
        return []

    def _remove_start_url_candidates(self, snapshot: dict[str, Any], candidates: list[str]) -> list[str]:
        start_urls = self._start_url_candidates(snapshot)
        if not start_urls:
            return candidates
        return [
            candidate
            for candidate in candidates
            if not any(self._urls_represent_same_location(candidate, start_url) for start_url in start_urls)
        ]

    def _start_url_candidates(self, snapshot: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        for value in (
            plan.get("site_url") if isinstance(plan, dict) else None,
            payload.get("site_url"),
        ):
            if isinstance(value, str) and self._is_external_http_url(value):
                candidates.append(value)

        for line in snapshot.get("logs") or []:
            text = str(line).strip()
            normalized = text.lower()
            if not (
                normalized.startswith("browser use start_url:")
                or normalized.startswith("selenium start_url:")
                or normalized.startswith("autoglm task start_url:")
                or normalized.startswith("opening target site:")
            ):
                continue
            candidates.extend(match.rstrip(".,;:，。；）】") for match in HTTP_URL_PATTERN.findall(text))

        return [url for url in _dedupe_preserving_order(candidates) if self._is_external_http_url(url)]

    def _snapshot_has_invalid_pdf_fallback(self, snapshot: dict[str, Any]) -> bool:
        logs = [str(item) for item in snapshot.get("logs") or []]
        combined = "\n".join(logs).lower()
        if "browser use pdf fallback saved current preview page" not in combined:
            return False
        expected_targets = self._expected_final_url_targets(snapshot)
        if not expected_targets:
            return False
        candidates = self._final_link_url_candidates(snapshot)
        return not bool(self._candidates_matching_expected_targets(candidates, expected_targets))

    def _final_link_url_candidates(self, snapshot: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        successful = self._snapshot_indicates_success(snapshot)

        for line in snapshot.get("logs") or []:
            text = str(line).strip()
            normalized = text.lower()
            if normalized.startswith("final_url:"):
                candidates.extend(match.rstrip(".,;:，。；）】") for match in HTTP_URL_PATTERN.findall(text))
            elif normalized.startswith("final_result:"):
                explicit_final_url = self._extract_named_final_url(text)
                if explicit_final_url:
                    candidates.append(explicit_final_url)
                elif successful:
                    candidates.extend(match.rstrip(".,;:，。；）】") for match in HTTP_URL_PATTERN.findall(text))
            elif successful and normalized.startswith("urls:"):
                candidates.extend(match.rstrip(".,;:，。；）】") for match in HTTP_URL_PATTERN.findall(text))

        return [url for url in candidates if self._is_external_http_url(url)]

    def _extract_named_final_url(self, text: str) -> str | None:
        match = re.search(
            r"(?:final_url|finalUrl|final url)\s*[:=]\s*['\"]?(https?://[^\s<>)\]\"']+)",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return match.group(1).rstrip(".,;:，。；）】")

    def _snapshot_indicates_success(self, snapshot: dict[str, Any]) -> bool:
        logs = [str(item).strip() for item in snapshot.get("logs") or []]
        is_done = ""
        is_successful = ""
        for line in logs:
            normalized = line.lower()
            if normalized.startswith("is_done:"):
                is_done = line.split(":", 1)[1].strip().lower()
            elif normalized.startswith("is_successful:"):
                is_successful = line.split(":", 1)[1].strip().lower()
        if is_successful in {"false", "none", "null"} or is_done in {"false", "none", "null"}:
            return False
        return snapshot.get("status") == "completed"

    def _expected_final_url_targets(self, snapshot: dict[str, Any]) -> list[str]:
        payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
        if not plan:
            return []
        targets: list[str] = []
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            action = str(step.get("action") or "").strip().lower()
            if action not in {"wait", "open", "click", "operate"}:
                continue
            text = " ".join(str(step.get(key) or "") for key in ("target", "value", "notes"))
            urls = [match.rstrip(".,;:，。；）】") for match in HTTP_URL_PATTERN.findall(text)]
            targets.extend(url for url in urls if self._is_external_http_url(url))
        return targets[-1:]

    def _url_matches_expected_target(self, candidate: str, expected: str) -> bool:
        try:
            candidate_parsed = urlparse(candidate)
            expected_parsed = urlparse(expected)
        except ValueError:
            return False
        if candidate_parsed.hostname != expected_parsed.hostname:
            return False
        expected_path = expected_parsed.path.rstrip("/")
        candidate_path = candidate_parsed.path.rstrip("/")
        if expected_path and candidate_path != expected_path:
            return False
        expected_fragment_path = expected_parsed.fragment.split("?", 1)[0].rstrip("/")
        candidate_fragment_path = candidate_parsed.fragment.split("?", 1)[0].rstrip("/")
        if expected_fragment_path and candidate_fragment_path != expected_fragment_path:
            return False
        return True

    def _urls_represent_same_location(self, left: str, right: str) -> bool:
        return self._normalize_url_for_dedupe(left) == self._normalize_url_for_dedupe(right)

    def _normalize_url_for_dedupe(self, raw_url: str) -> str:
        try:
            parsed = urlparse(raw_url)
        except ValueError:
            return raw_url.strip().lower()
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        path = parsed.path.rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        fragment = f"#{parsed.fragment.rstrip('/')}" if parsed.fragment else ""
        return f"{scheme}://{netloc}{path}{query}{fragment}"

    def _is_external_http_url(self, raw_url: str) -> bool:
        try:
            parsed = urlparse(raw_url)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        host = parsed.hostname or ""
        return host not in {"127.0.0.1", "localhost", "::1"}

    def _web_link_label(self, url: str) -> str:
        host = urlparse(url).netloc or "网页链接"
        return f"最终网页: {host}"

    def _render_task_report(self, snapshot: dict[str, Any]) -> str:
        payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
        skill_names = payload.get("skill_names") or []
        objective = payload.get("objective") or payload.get("user_request") or ""
        lines = [
            f"# EyeClaw Task Run Report",
            "",
            f"- Task ID: `{snapshot.get('id')}`",
            f"- Name: {snapshot.get('name')}",
            f"- Status: `{snapshot.get('status')}`",
            f"- Task type: `{snapshot.get('task_type')}`",
            f"- Run count: {snapshot.get('run_count')}",
            f"- Created at: {snapshot.get('created_at_iso')}",
            f"- Last run at: {snapshot.get('last_run_at_iso') or ''}",
            f"- Objective: {objective}",
            f"- Skills: {', '.join(skill_names) if skill_names else 'None'}",
            f"- Last error: {snapshot.get('last_error') or ''}",
            "",
            "## Deliverables",
            "",
        ]
        for item in snapshot.get("deliverables") or []:
            label = item.get("label") or item.get("filename") or "交付物"
            href = item.get("url") or item.get("download_url") or ""
            if href:
                lines.append(f"- [{label}]({href})")
            else:
                lines.append(f"- {label}")
        if not snapshot.get("deliverables"):
            lines.append("- No deliverables recorded.")
        token_usage = _normalize_token_usage(snapshot.get("token_usage"))
        lines.extend(
            [
                "",
                "## Token Usage",
                "",
                f"- Total tokens: {token_usage['total_tokens']}",
                f"- Prompt tokens: {token_usage['prompt_tokens']}",
                f"- Completion tokens: {token_usage['completion_tokens']}",
                f"- Cached prompt tokens: {token_usage['prompt_cached_tokens']}",
                f"- Model calls: {token_usage['entry_count']}",
            ]
        )
        if token_usage.get("cost"):
            lines.append(f"- Estimated cost: {token_usage['cost']:.6f}")
        by_model = token_usage.get("by_model")
        if isinstance(by_model, dict) and by_model:
            lines.append("")
            lines.append("### By Model")
            for model, stats in by_model.items():
                if not isinstance(stats, dict):
                    continue
                lines.append(
                    f"- {model}: total={_safe_int(stats.get('total_tokens'))}, "
                    f"prompt={_safe_int(stats.get('prompt_tokens'))}, "
                    f"completion={_safe_int(stats.get('completion_tokens'))}, "
                    f"calls={_safe_int(stats.get('invocations'))}"
                )
        lines.extend(
            [
                "",
            "## Progress Events",
            "",
            ]
        )
        for event in snapshot.get("progress_events") or []:
            lines.append(
                f"- [{event.get('timestamp_iso')}] `{event.get('status')}` / `{event.get('event_type')}`: {event.get('message')}"
            )
        if not snapshot.get("progress_events"):
            lines.append("- No structured progress events recorded.")
        lines.extend(["", "## Logs", ""])
        for log in snapshot.get("logs") or []:
            lines.append(f"- {log}")
        if not snapshot.get("logs"):
            lines.append("- No logs recorded.")
        return "\n".join(lines) + "\n"

    def _run_loop(self) -> None:
        while True:
            sleep(1.0)
            self._start_eligible_tasks()


TASK_HANDLERS: dict[str, TaskHandler] = {}


def register_task_handler(task_type: str, handler: TaskHandler) -> None:
    TASK_HANDLERS[task_type] = handler
