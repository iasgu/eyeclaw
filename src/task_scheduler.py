from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock, Thread
from time import sleep
from typing import Callable
from uuid import uuid4


TaskHandler = Callable[[dict], list[str]]


@dataclass
class ScheduledTask:
    id: str
    name: str
    run_at_iso: str
    task_type: str
    payload: dict
    status: str = "scheduled"
    logs: list[str] = field(default_factory=list)
    last_error: str | None = None
    created_at_iso: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class TaskScheduler:
    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._lock = Lock()
        self._thread = Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def add_task(self, name: str, run_at_iso: str, task_type: str, payload: dict) -> ScheduledTask:
        task = ScheduledTask(
            id=uuid4().hex[:12],
            name=name,
            run_at_iso=run_at_iso,
            task_type=task_type,
            payload=payload,
        )
        with self._lock:
            self._tasks[task.id] = task
        return task

    def list_tasks(self) -> list[ScheduledTask]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda task: task.run_at_iso)

    def update_status(self, task_id: str, status: str, logs: list[str] | None = None, error: str | None = None) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = status
            if logs is not None:
                task.logs = logs
            if error is not None:
                task.last_error = error

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
                        task.status = "running"
                        due_tasks.append(task)
            for task in due_tasks:
                handler = TASK_HANDLERS.get(task.task_type)
                if handler is None:
                    self.update_status(task.id, "failed", error=f"Unknown task type: {task.task_type}")
                    continue
                try:
                    logs = handler(task.payload)
                except Exception as exc:
                    self.update_status(task.id, "failed", error=str(exc))
                else:
                    self.update_status(task.id, "completed", logs=logs)


TASK_HANDLERS: dict[str, TaskHandler] = {}


def register_task_handler(task_type: str, handler: TaskHandler) -> None:
    TASK_HANDLERS[task_type] = handler
