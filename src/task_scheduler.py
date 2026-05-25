from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
from threading import Lock, Thread
from time import sleep
from typing import Callable
from uuid import uuid4


TaskHandler = Callable[..., list[str]]


class ManualTaskRequired(RuntimeError):
    def __init__(self, message: str, logs: list[str] | None = None) -> None:
        super().__init__(message)
        self.logs = logs or []


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


class TaskScheduler:
    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._lock = Lock()
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
        return task

    def list_tasks(self) -> list[ScheduledTask]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda task: task.run_at_iso)

    def get_task(self, task_id: str) -> ScheduledTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def update_status(self, task_id: str, status: str, logs: list[str] | None = None, error: str | None = None) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = status
            if logs is not None:
                task.logs = logs
            task.last_error = error

    def append_log(self, task_id: str, message: str) -> None:
        if not message:
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.logs.append(message)

    def run_task_now(self, task_id: str) -> ScheduledTask:
        with self._lock:
            task = self._tasks[task_id]
        self._execute_task(task.id)
        with self._lock:
            return self._tasks[task_id]

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
            status="running",
        )
        with self._lock:
            self._tasks[task.id] = task
        self._execute_task(task.id)
        with self._lock:
            return self._tasks[task.id]

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
            status="running",
        )
        with self._lock:
            self._tasks[task.id] = task
        Thread(target=self._execute_task, args=(task.id,), daemon=True).start()
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

    def _execute_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = "running"
            task.last_error = None
            task.last_run_at_iso = datetime.now(timezone.utc).isoformat()
            task.run_count += 1
        handler = TASK_HANDLERS.get(task.task_type)
        if handler is None:
            self.update_status(task_id, "failed", error=f"Unknown task type: {task.task_type}")
            return
        progress_callback = lambda message: self.append_log(task_id, message)
        try:
            signature = inspect.signature(handler)
            accepts_progress = (
                "progress_callback" in signature.parameters
                or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
            )
            if accepts_progress:
                logs = handler(task.payload, progress_callback=progress_callback)
            else:
                logs = handler(task.payload)
        except ManualTaskRequired as exc:
            with self._lock:
                current = self._tasks[task_id]
                current.logs = exc.logs
                current.status = "manual"
                current.last_error = str(exc)
            return
        except Exception as exc:
            self.update_status(task_id, "failed", error=str(exc))
            return

        with self._lock:
            current = self._tasks[task_id]
            current.logs = logs
            if current.frequency == "daily" or current.frequency == "weekly":
                self._schedule_next_run(current)
            elif current.frequency == "manual":
                current.status = "manual"
            else:
                current.status = "completed"
            current.last_error = None

    def _run_loop(self) -> None:
        while True:
            sleep(1.0)
            due_tasks: list[ScheduledTask] = []
            now = datetime.now(timezone.utc)
            with self._lock:
                for task in self._tasks.values():
                    if task.status != "scheduled":
                        continue
                    run_at = datetime.fromisoformat(task.run_at_iso)
                    if run_at.tzinfo is None:
                        run_at = run_at.replace(tzinfo=timezone.utc)
                    if run_at <= now:
                        due_tasks.append(task)
            for task in due_tasks:
                self._execute_task(task.id)


TASK_HANDLERS: dict[str, TaskHandler] = {}


def register_task_handler(task_type: str, handler: TaskHandler) -> None:
    TASK_HANDLERS[task_type] = handler
