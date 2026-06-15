from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from queue import Queue
from threading import Thread
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse
from uuid import uuid4

from pydantic import BaseModel, ValidationError
import requests
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from src.analyze import build_replay_plan
from src.browser_listener import (
    BrowserEventBatchIn,
    BrowserEventStore,
    choose_site_url,
    is_eyeclaw_console_url,
    plan_listener_guided_frames,
    save_session_recording,
    summarize_browser_event,
)
from src.config import load_config_status
from src.eia_workflow import (
    DEFAULT_CDP_URL,
    ManualCheckpointRequired,
    detect_eia_state,
    parse_eia_request,
    run_eia_live_workflow,
)
from src.dsl import ReplayPlan
from src.glm_client import DEFAULT_FRAME_BATCH_SIZE
from src.replay import close_replay_session, connect_over_cdp, dedupe_preserving_order, run_replay_plan
from src.skill_library import SkillLibrary
from src.task_scheduler import (
    ManualTaskRequired,
    TASK_HANDLERS,
    TaskScheduler,
    empty_token_usage,
    extract_token_usage_from_logs,
    register_task_handler,
    token_usage_has_activity,
)
from src.video import extract_frames, get_video_metadata, save_uploaded_video


INDEX_HTML = Path("web/index.html")
APP_HTML = Path("web/app.html")
MCPORTER_CONFIG_PATH = Path("config/mcporter.json")
EXECUTION_ADAPTERS_CONFIG_PATH = Path("config/execution_adapters.json")
AUTOGLM_BROWSER_AGENT_NAME = "autoglm-browser-agent"
PRIMARY_EXECUTION_TASK_TYPE = "browser_use_live_workflow"
LEGACY_REPLAY_TASK_TYPE = "generic_live_workflow"
HYBRID_REPLAY_TASK_TYPE = "playwright_browser_use_live_workflow"
SMART_ROUTER_TASK_TYPE = "smart_router_live_workflow"
BENCHMARK_TASK_TYPE = "execution_adapter_benchmark"
SELENIUM_TASK_TYPE = "selenium_live_workflow"
USER_VISIBLE_TASK_TYPES = {
    SMART_ROUTER_TASK_TYPE,
    BENCHMARK_TASK_TYPE,
    PRIMARY_EXECUTION_TASK_TYPE,
    LEGACY_REPLAY_TASK_TYPE,
    HYBRID_REPLAY_TASK_TYPE,
    SELENIUM_TASK_TYPE,
    "autoglm_live_workflow",
    "eia_live_workflow",
}
EXECUTION_ADAPTER_TASK_TYPE_ALIASES = {
    "browser_use": PRIMARY_EXECUTION_TASK_TYPE,
    "browser-use": PRIMARY_EXECUTION_TASK_TYPE,
    "browser_use_live_workflow": PRIMARY_EXECUTION_TASK_TYPE,
    "playwright": HYBRID_REPLAY_TASK_TYPE,
    "playwright_hybrid": HYBRID_REPLAY_TASK_TYPE,
    "playwright_browser_use": HYBRID_REPLAY_TASK_TYPE,
    "playwright_live_workflow": HYBRID_REPLAY_TASK_TYPE,
    "playwright_browser_use_live_workflow": HYBRID_REPLAY_TASK_TYPE,
    "smart": SMART_ROUTER_TASK_TYPE,
    "auto": SMART_ROUTER_TASK_TYPE,
    "router": SMART_ROUTER_TASK_TYPE,
    "smart_router": SMART_ROUTER_TASK_TYPE,
    "smart_router_live_workflow": SMART_ROUTER_TASK_TYPE,
    "benchmark": BENCHMARK_TASK_TYPE,
    "adapter_benchmark": BENCHMARK_TASK_TYPE,
    "execution_benchmark": BENCHMARK_TASK_TYPE,
    "execution_adapter_benchmark": BENCHMARK_TASK_TYPE,
    "selenium": SELENIUM_TASK_TYPE,
    "selenium_live_workflow": SELENIUM_TASK_TYPE,
    "generic": LEGACY_REPLAY_TASK_TYPE,
    "replay": LEGACY_REPLAY_TASK_TYPE,
    "cdp": LEGACY_REPLAY_TASK_TYPE,
    "cdp_replay": LEGACY_REPLAY_TASK_TYPE,
    "deterministic_replay": LEGACY_REPLAY_TASK_TYPE,
    "generic_live_workflow": LEGACY_REPLAY_TASK_TYPE,
    "autoglm": "autoglm_live_workflow",
    "autoglm_browser_agent": "autoglm_live_workflow",
    "autoglm_live_workflow": "autoglm_live_workflow",
    "eia": "eia_live_workflow",
    "eia_live_workflow": "eia_live_workflow",
}
SCHEDULER = TaskScheduler()
BROWSER_EVENT_STORE = BrowserEventStore()
SKILL_LIBRARY = SkillLibrary()
PREVIEW_CACHE_TTL_SECONDS = 0.8
PREVIEW_CACHE_LOCK = Lock()
PREVIEW_CACHE: dict[str, tuple[float, bytes, str]] = {}
BROWSER_USE_FAST_MODE_DEFAULT = True
BROWSER_USE_PREFLIGHT_REPLAY_DEFAULT = True
BROWSER_USE_DEEPSEEK_TOOL_MODEL = "deepseek-chat"
BROWSER_USE_DEEPSEEK_NATIVE_TOOLS_DEFAULT = False
BROWSER_USE_VISION_MODE_DEFAULT = "auto"
BROWSER_USE_VISUAL_RETRY_DEFAULT = True


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _env_text(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


@dataclass
class UploadResponse:
    video_path: str
    duration_seconds: float
    fps: float
    width: int
    height: int


class ListenerAnalysisRequest(BaseModel):
    session_id: str | None = None
    user_request: str | None = None
    max_events: int = 8


class CreateSkillRequest(BaseModel):
    name: str
    description: str = ""
    source_type: str = "analysis"
    steps: list[dict[str, Any]]
    site_url: str | None = None
    user_request: str = ""
    video_path: str | None = None
    listener_session_id: str | None = None


class UpdateSkillRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    source_type: str | None = None
    steps: list[dict[str, Any]] | None = None
    site_url: str | None = None
    user_request: str | None = None
    video_path: str | None = None
    listener_session_id: str | None = None


class RunUserTaskRequest(BaseModel):
    name: str | None = None
    objective: str = ""
    cdp_url: str = DEFAULT_CDP_URL
    task_type: str = PRIMARY_EXECUTION_TASK_TYPE
    skill_ids: list[str] = []
    benchmark_task_types: list[str] = []
    benchmark_runs: int = 1


class RunLiveWorkflowRequest(BaseModel):
    cdp_url: str = DEFAULT_CDP_URL
    user_request: str = ""
    plan: dict[str, Any] | None = None
    task_type: str = PRIMARY_EXECUTION_TASK_TYPE


@dataclass
class AnalysisJob:
    id: str
    job_type: str
    status: str
    progress_percent: int
    stage: str
    created_at_iso: str
    updated_at_iso: str
    created_at_monotonic: float
    updated_at_monotonic: float
    stage_started_at_monotonic: float
    result: dict[str, Any] | None = None
    error: str | None = None


class AnalysisJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, AnalysisJob] = {}
        self._lock = Lock()

    def create_job(self, job_type: str, *, stage: str) -> AnalysisJob:
        now_iso = datetime.now(timezone.utc).isoformat()
        now_monotonic = time.monotonic()
        job = AnalysisJob(
            id=uuid4().hex[:12],
            job_type=job_type,
            status="running",
            progress_percent=0,
            stage=stage,
            created_at_iso=now_iso,
            updated_at_iso=now_iso,
            created_at_monotonic=now_monotonic,
            updated_at_monotonic=now_monotonic,
            stage_started_at_monotonic=now_monotonic,
        )
        with self._lock:
            self._jobs[job.id] = job
        return job

    def update(
        self,
        job_id: str,
        *,
        progress_percent: int | None = None,
        stage: str | None = None,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            now_monotonic = time.monotonic()
            next_progress = (
                max(0, min(100, int(progress_percent)))
                if progress_percent is not None
                else job.progress_percent
            )
            next_stage = stage if stage is not None else job.stage
            if next_progress != job.progress_percent or next_stage != job.stage:
                job.stage_started_at_monotonic = now_monotonic
            if progress_percent is not None:
                job.progress_percent = next_progress
            if stage is not None:
                job.stage = stage
            if status is not None:
                job.status = status
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error
            job.updated_at_iso = datetime.now(timezone.utc).isoformat()
            job.updated_at_monotonic = now_monotonic

    def get(self, job_id: str) -> AnalysisJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def as_dict(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        if job is None:
            return None
        display_progress = infer_display_progress(job)
        progress_mode = "activity" if job.status == "running" else "determinate"
        return {
            "id": job.id,
            "job_type": job.job_type,
            "status": job.status,
            "progress_percent": display_progress,
            "reported_progress_percent": job.progress_percent,
            "progress_estimated": progress_mode == "activity",
            "progress_mode": progress_mode,
            "stage": job.stage,
            "created_at_iso": job.created_at_iso,
            "updated_at_iso": job.updated_at_iso,
            "result": job.result,
            "error": job.error,
        }


ANALYSIS_JOBS = AnalysisJobStore()


def infer_display_progress(job: AnalysisJob) -> int:
    if job.status == "completed":
        return 100
    return job.progress_percent


LIVE_WORKFLOW_PROGRESS_STAGES: list[tuple[str, int, str]] = [
    ("Executing Browser Use live workflow", 6, "正在启动 Browser Use"),
    ("Browser Use is the primary execution engine", 8, "正在启动 Browser Use"),
    ("Browser Use task:", 12, "正在生成智能执行任务"),
    ("Browser Use result summary:", 90, "正在汇总 Browser Use 结果"),
    ("final_result:", 96, "正在校验最终结果"),
    ("Executing generic replay plan", 12, "正在准备执行计划"),
    ("Running generic plan with", 16, "正在载入步骤"),
    ("Step ", 22, "正在执行步骤"),
    (": success", 88, "步骤执行中"),
    ("Detected page state:", 10, "正在识别当前页面"),
    ("Clicked the 藏经阁 entry", 24, "正在进入首页入口"),
    ("Clicked the post-login bridge button.", 42, "正在完成登录后跳转"),
    ("Opened the 环评藏经阁 route", 58, "正在打开功能页面"),
    ("Applied filters:", 78, "正在应用筛选条件"),
    ("Opened the report preview tab.", 90, "正在打开预览页"),
    ("Triggered the PDF download", 100, "已触发 PDF 下载"),
]


def infer_live_workflow_progress(logs: list[str], fallback_message: str | None = None) -> tuple[int, str]:
    percent = 3
    stage = fallback_message or "正在连接浏览器"
    for log in logs:
        for marker, mapped_percent, mapped_stage in LIVE_WORKFLOW_PROGRESS_STAGES:
            if marker in log:
                if mapped_percent >= percent:
                    percent = mapped_percent
                    stage = mapped_stage
    return percent, stage


def _task_progress_report(logs: list[str], progress_callback: Callable[[str], None] | None, message: str) -> None:
    logs.append(message)
    if progress_callback is not None:
        progress_callback(message)


def _extract_logged_step_number(logs: list[str]) -> int | None:
    values = [
        value
        for value in [_extract_replay_step_number(logs), _extract_browser_use_round_number(logs)]
        if value is not None
    ]
    return max(values) if values else None


def _extract_replay_step_number(logs: list[str]) -> int | None:
    pattern = re.compile(r"\bStep\s+(\d+)\b", flags=re.IGNORECASE)
    latest: int | None = None
    for log in logs:
        match = pattern.search(log)
        if match:
            step_number = int(match.group(1))
            latest = step_number if latest is None else max(latest, step_number)
    return latest


def _extract_browser_use_round_number(logs: list[str]) -> int | None:
    pattern = re.compile(r"\bBrowser Use agent step\s+(\d+)\b", flags=re.IGNORECASE)
    latest: int | None = None
    for log in logs:
        match = pattern.search(log)
        if match:
            step_number = int(match.group(1))
            latest = step_number if latest is None else max(latest, step_number)
    return latest


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _seconds_since(value: Any, *, now: datetime) -> int | None:
    parsed = _parse_iso_datetime(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _seconds_between(start: Any, end: Any) -> int | None:
    parsed_start = _parse_iso_datetime(start)
    parsed_end = _parse_iso_datetime(end)
    if parsed_start is None or parsed_end is None:
        return None
    return max(0, int((parsed_end - parsed_start).total_seconds()))


def _latest_progress_event(task: Any) -> dict[str, Any] | None:
    events = list(getattr(task, "progress_events", []) or [])
    if not events:
        return None
    latest = events[-1]
    return latest if isinstance(latest, dict) else None


def infer_task_execution_snapshot(task: Any) -> dict[str, Any]:
    logs = list(getattr(task, "logs", []) or [])
    fallback_stage = "正在准备任务"
    total_steps = 0
    payload = getattr(task, "payload", {}) or {}
    if isinstance(payload, dict):
        plan = payload.get("plan")
        if isinstance(plan, dict):
            raw_steps = plan.get("steps")
            if isinstance(raw_steps, list):
                total_steps = len([step for step in raw_steps if isinstance(step, dict)])

    task_type = str(getattr(task, "task_type", "") or "")
    is_browser_use_task = task_type == PRIMARY_EXECUTION_TASK_TYPE
    is_hybrid_replay_task = task_type == HYBRID_REPLAY_TASK_TYPE
    is_benchmark_task = task_type == BENCHMARK_TASK_TYPE
    replay_step = _extract_replay_step_number(logs)
    browser_round = _extract_browser_use_round_number(logs)
    current_step = (browser_round or replay_step) if (is_browser_use_task or is_hybrid_replay_task) else _extract_logged_step_number(logs)
    percent, stage = infer_live_workflow_progress(logs, fallback_stage)
    estimated = False
    progress_mode = "determinate"
    status = str(getattr(task, "status", "") or "")
    now = datetime.now(timezone.utc)
    latest_event = _latest_progress_event(task)
    run_started_at = getattr(task, "last_run_at_iso", None) or getattr(task, "created_at_iso", None)
    if status in {"completed", "failed", "manual", "cancelled"} and latest_event:
        elapsed_seconds = _seconds_between(run_started_at, latest_event.get("timestamp_iso"))
    else:
        elapsed_seconds = _seconds_since(run_started_at, now=now)
    last_event_age_seconds = _seconds_since(latest_event.get("timestamp_iso") if latest_event else None, now=now)
    if is_benchmark_task:
        benchmark_total, benchmark_completed, benchmark_stage = _extract_benchmark_progress(logs)
        if benchmark_total:
            total_steps = benchmark_total
            current_step = min(benchmark_completed, benchmark_total)
            stage = benchmark_stage or stage
    if status == "completed":
        percent = 100
        stage = "执行完成"
        if total_steps:
            current_step = total_steps
    elif status == "failed":
        stage = getattr(task, "last_error", None) or stage or "执行失败"
        percent = max(percent, 8)
    elif status == "manual":
        stage = getattr(task, "last_error", None) or "等待人工处理"
        percent = max(percent, 92)
    elif status == "cancelled":
        stage = getattr(task, "last_error", None) or "任务已取消"
        percent = max(percent, 8)
    elif status == "cancelling":
        stage = "正在停止任务"
        percent = 0
        progress_mode = "activity"
    elif status == "queued":
        stage = "等待空闲执行槽"
        percent = 0
        progress_mode = "queued"
    elif status == "running":
        if is_benchmark_task:
            benchmark_total, benchmark_completed, benchmark_stage = _extract_benchmark_progress(logs)
            if benchmark_total:
                current_step = min(benchmark_completed, benchmark_total)
                total_steps = benchmark_total
                percent = min(95, round((benchmark_completed / max(1, benchmark_total)) * 100))
                stage = benchmark_stage or f"Benchmark completed {benchmark_completed}/{benchmark_total} attempts."
            else:
                progress_mode = "activity"
                percent = 0
                stage = benchmark_stage or "Benchmark is running adapter comparisons."
        elif is_browser_use_task or (is_hybrid_replay_task and browser_round is not None):
            progress_mode = "activity"
            percent = 0
            if browser_round is not None:
                stage = f"Browser Use 已完成 {browser_round} 轮，继续等待页面或模型返回"
            elif replay_step is not None:
                stage = f"快速执行已完成 {replay_step} 步，继续处理当前页面"
            elif logs:
                stage = stage or "Browser Use 运行中"
        elif total_steps and current_step is not None:
            current_step = min(current_step, total_steps)
            percent = max(percent, min(95, 10 + round((current_step / max(total_steps, 1)) * 80)))
            stage = f"正在执行第 {current_step}/{total_steps} 步"
        elif current_step is not None:
            progress_mode = "activity"
            percent = 0
            stage = f"已完成 {current_step} 轮，继续执行中"
        elif total_steps:
            percent = max(percent, 8)
            stage = f"正在准备 {total_steps} 步计划"

    return {
        "progress_percent": percent,
        "progress_stage": stage,
        "progress_estimated": estimated,
        "progress_mode": progress_mode,
        "current_step": current_step,
        "total_steps": total_steps or None,
        "planned_step_count": total_steps or None,
        "browser_round": browser_round if (is_browser_use_task or is_hybrid_replay_task) else None,
        "elapsed_seconds": elapsed_seconds,
        "last_event_age_seconds": last_event_age_seconds,
        "latest_event_message": latest_event.get("message") if latest_event else None,
    }


def _normalize_execution_task_type(raw_task_type: str | None) -> str:
    normalized = (raw_task_type or "").strip()
    if not normalized:
        return PRIMARY_EXECUTION_TASK_TYPE
    return EXECUTION_ADAPTER_TASK_TYPE_ALIASES.get(normalized.lower(), normalized)


def _load_execution_adapter_registry() -> dict[str, Any]:
    try:
        payload = json.loads(EXECUTION_ADAPTERS_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    adapters = payload.get("adapters")
    if not isinstance(adapters, dict):
        adapters = {}
    default_order = payload.get("default_order")
    if not isinstance(default_order, list):
        default_order = list(adapters)
    return {"default_order": default_order, "adapters": adapters}


def _serialize_execution_adapter(adapter_id: str, raw_adapter: Any) -> dict[str, Any]:
    adapter = raw_adapter if isinstance(raw_adapter, dict) else {}
    task_type = _normalize_execution_task_type(str(adapter.get("task_type") or adapter_id))
    enabled = bool(adapter.get("enabled"))
    label = str(adapter.get("label") or adapter_id).strip() or adapter_id
    summary = str(adapter.get("summary") or "").strip()
    status = str(adapter.get("status") or "unknown").strip() or "unknown"
    disabled_reason = ""
    if not enabled:
        disabled_reason = summary or "This execution adapter is installed or documented, but not wired into the scheduler yet."
    return {
        "id": adapter_id,
        "label": label,
        "summary": summary,
        "status": status,
        "enabled": enabled,
        "task_type": task_type,
        "role": str(adapter.get("role") or "").strip(),
        "runtime": str(adapter.get("runtime") or "").strip(),
        "skill_doc": str(adapter.get("skill_doc") or "").strip(),
        "source_path": str(adapter.get("source_path") or "").strip(),
        "disabled_reason": disabled_reason,
    }


def _execution_adapter_payload() -> dict[str, Any]:
    registry = _load_execution_adapter_registry()
    raw_adapters = registry["adapters"]
    serialized_by_id = {
        str(adapter_id): _serialize_execution_adapter(str(adapter_id), adapter)
        for adapter_id, adapter in raw_adapters.items()
    }
    ordered_ids = [str(adapter_id) for adapter_id in registry["default_order"] if str(adapter_id) in serialized_by_id]
    ordered_ids.extend(adapter_id for adapter_id in serialized_by_id if adapter_id not in ordered_ids)
    adapters = [serialized_by_id[adapter_id] for adapter_id in ordered_ids]
    default_adapter = next((adapter for adapter in adapters if adapter["enabled"]), None)
    return {
        "default_adapter": default_adapter["id"] if default_adapter else "browser_use",
        "default_task_type": default_adapter["task_type"] if default_adapter else PRIMARY_EXECUTION_TASK_TYPE,
        "adapters": adapters,
    }


def _enabled_router_candidate_types() -> list[str]:
    payload = _execution_adapter_payload()
    candidates: list[str] = []
    for adapter in payload.get("adapters") or []:
        if not isinstance(adapter, dict) or not adapter.get("enabled"):
            continue
        task_type = _normalize_execution_task_type(str(adapter.get("task_type") or adapter.get("id") or ""))
        if task_type in {SMART_ROUTER_TASK_TYPE, BENCHMARK_TASK_TYPE} or task_type not in TASK_HANDLERS:
            continue
        candidates.append(task_type)
    return dedupe_preserving_order(candidates)


def _router_order_from_text(raw_text: str, candidates: list[str]) -> list[str]:
    try:
        payload = json.loads(_extract_json_object_from_text(raw_text))
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    raw_order = payload.get("order") if isinstance(payload, dict) else None
    if not isinstance(raw_order, list):
        return []
    allowed = set(candidates)
    ordered: list[str] = []
    for item in raw_order:
        normalized = _normalize_execution_task_type(str(item))
        if normalized in allowed and normalized not in ordered:
            ordered.append(normalized)
    return ordered


def _extract_json_object_from_text(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    raise ValueError("No JSON object found.")


def _summarize_plan_for_router(plan: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {"site_url": None, "steps": []}
    steps: list[dict[str, Any]] = []
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        steps.append(
            {
                "action": str(step.get("action") or ""),
                "target": str(step.get("target") or "")[:80],
                "value": str(step.get("value") or "")[:80],
            }
        )
        if len(steps) >= 12:
            break
    return {
        "site_url": plan.get("site_url"),
        "step_count": len(plan.get("steps") or []) if isinstance(plan.get("steps"), list) else len(steps),
        "steps": steps,
    }


def _plan_step_count(plan: dict[str, Any] | None) -> int:
    if not isinstance(plan, dict):
        return 0
    steps = plan.get("steps")
    return len(steps) if isinstance(steps, list) else 0


def _expected_final_url_targets_from_plan(plan: dict[str, Any] | None) -> list[str]:
    if not isinstance(plan, dict):
        return []
    targets: list[str] = []
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action") or "").strip().lower()
        if action not in {"wait", "open", "click", "operate"}:
            continue
        for url in _extract_http_urls_from_step(step):
            if _is_http_url(url) and not is_eyeclaw_console_url(url):
                targets.append(url)
    return dedupe_preserving_order(targets[-1:])


def _build_task_acceptance_criteria(
    *,
    objective: str,
    plan: dict[str, Any] | None,
    skill_names: list[str] | None = None,
) -> dict[str, Any]:
    payload_for_detection = {
        "objective": objective,
        "user_request": objective,
        "skill_names": skill_names or [],
    }
    requires_file = _task_requests_file_delivery(payload_for_detection, plan)
    requires_preview = _plan_requires_preview_completion(plan)
    expected_url_targets = _expected_final_url_targets_from_plan(plan)
    criteria: list[str] = []
    if requires_file:
        criteria.append("real_downloaded_file")
    if requires_preview and not requires_file:
        criteria.append("preview_or_document_state")
    if expected_url_targets and not requires_file:
        criteria.append("final_url_matches_recorded_target")
    if not criteria:
        criteria.append("executor_completed_without_incomplete_self_report")
    return {
        "goal": str(objective or "").strip(),
        "skill_names": skill_names or [],
        "recorded_step_count": _plan_step_count(plan),
        "requires_file": requires_file,
        "requires_preview": requires_preview,
        "expected_url_targets": expected_url_targets,
        "criteria": criteria,
    }


def _payload_acceptance_criteria(payload: dict[str, Any], plan: dict[str, Any] | None) -> dict[str, Any]:
    criteria = payload.get("acceptance_criteria") if isinstance(payload, dict) else None
    if isinstance(criteria, dict):
        normalized = dict(criteria)
        normalized.setdefault("goal", str(payload.get("objective") or payload.get("user_request") or "").strip())
        normalized.setdefault("skill_names", payload.get("skill_names") or [])
        normalized.setdefault("recorded_step_count", _plan_step_count(plan))
        normalized.setdefault("requires_file", _task_requests_file_delivery(payload, plan))
        normalized.setdefault("requires_preview", _plan_requires_preview_completion(plan))
        normalized.setdefault("expected_url_targets", _expected_final_url_targets_from_plan(plan))
        normalized.setdefault("criteria", [])
        return normalized
    return _build_task_acceptance_criteria(
        objective=str(payload.get("objective") or payload.get("user_request") or ""),
        plan=plan,
        skill_names=[str(name) for name in payload.get("skill_names") or []],
    )


def _heuristic_router_order(payload: dict[str, Any], candidates: list[str]) -> list[str]:
    allowed = set(candidates)
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    objective_text = " ".join(
        [
            str(payload.get("objective") or ""),
            str(payload.get("user_request") or ""),
            json.dumps(_summarize_plan_for_router(plan), ensure_ascii=False),
        ]
    ).lower()
    has_plan_steps = bool(isinstance(plan, dict) and isinstance(plan.get("steps"), list) and plan.get("steps"))
    order: list[str] = []

    # Stage 2 is deterministic by default: Playwright replay first, then Selenium
    # for enterprise forms and custom dropdowns. Browser Use is intentionally
    # delayed until deterministic replay cannot satisfy final validation.
    if has_plan_steps and HYBRID_REPLAY_TASK_TYPE in allowed:
        order.append(HYBRID_REPLAY_TASK_TYPE)
    if has_plan_steps and SELENIUM_TASK_TYPE in allowed:
        order.append(SELENIUM_TASK_TYPE)
    if PRIMARY_EXECUTION_TASK_TYPE in allowed:
        order.append(PRIMARY_EXECUTION_TASK_TYPE)
    if "autoglm_live_workflow" in allowed:
        order.append("autoglm_live_workflow")
    for candidate in candidates:
        if candidate not in order:
            order.append(candidate)
    return order


def _llm_router_order(payload: dict[str, Any], candidates: list[str]) -> tuple[list[str], str]:
    status = load_config_status()
    if not status.is_ready or status.config is None:
        return [], f"LLM router skipped: configuration is incomplete ({status.missing_fields})."

    config = status.config
    endpoint = _normalize_openai_compatible_base_url(config.deepseek_base_url).rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"
    model = _env_text("EXECUTION_ROUTER_LLM", str(config.deepseek_model))
    timeout_seconds = _env_int("EXECUTION_ROUTER_LLM_TIMEOUT", 12, minimum=3, maximum=60)
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    router_payload = {
        "objective": str(payload.get("objective") or ""),
        "user_request": str(payload.get("user_request") or "")[:4000],
        "skill_names": payload.get("skill_names") or [],
        "plan": _summarize_plan_for_router(plan),
        "candidates": candidates,
        "candidate_guidance": {
            HYBRID_REPLAY_TASK_TYPE: "Use first when recorded skill steps exist; fastest deterministic replay with Browser Use fallback.",
            PRIMARY_EXECUTION_TASK_TYPE: "Use for open-ended tasks, page changes, custom dropdowns, and flexible recovery.",
            SELENIUM_TASK_TYPE: "Use for recorded enterprise form workflows, native selects, and WebDriver-compatible pages.",
            "autoglm_live_workflow": "Use as external MCP/OpenClaw-style agent fallback.",
        },
    }
    request_body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You route browser automation tasks. Return only JSON with keys "
                    "`order` (array of candidate task_type strings) and `reason` (short Chinese sentence). "
                    "Use only task types from the provided candidates."
                ),
            },
            {"role": "user", "content": json.dumps(router_payload, ensure_ascii=False)},
        ],
    }
    try:
        response = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {config.deepseek_api_key}", "Content-Type": "application/json"},
            json=request_body,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        response_json = response.json()
        content = str(response_json["choices"][0]["message"]["content"])
    except Exception as exc:
        return [], f"LLM router skipped: {_compact_error_text(exc)}"
    order = _router_order_from_text(content, candidates)
    if not order:
        return [], "LLM router returned no usable adapter order."
    reason = ""
    try:
        parsed = json.loads(_extract_json_object_from_text(content))
        reason = str(parsed.get("reason") or "").strip()
    except Exception:
        reason = ""
    return order, reason or f"LLM router selected {len(order)} candidate adapters."


def _enforce_smart_router_stage_order(payload: dict[str, Any], order: list[str]) -> list[str]:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    has_plan_steps = bool(isinstance(plan, dict) and isinstance(plan.get("steps"), list) and plan.get("steps"))
    deduped = dedupe_preserving_order(order)
    if not has_plan_steps:
        return deduped

    priority = {
        HYBRID_REPLAY_TASK_TYPE: 0,
        SELENIUM_TASK_TYPE: 1,
        LEGACY_REPLAY_TASK_TYPE: 2,
        PRIMARY_EXECUTION_TASK_TYPE: 3,
        "autoglm_live_workflow": 4,
    }
    original_index = {task_type: index for index, task_type in enumerate(deduped)}
    return sorted(deduped, key=lambda task_type: (priority.get(task_type, 10), original_index[task_type]))


def resolve_smart_router_order(payload: dict[str, Any]) -> tuple[list[str], str]:
    candidates = _enabled_router_candidate_types()
    if not candidates:
        return [], "No enabled execution adapters are available for smart routing."
    if _env_bool("EXECUTION_ROUTER_USE_LLM", False):
        llm_order, reason = _llm_router_order(payload, candidates)
        if llm_order:
            remaining = [candidate for candidate in candidates if candidate not in llm_order]
            return _enforce_smart_router_stage_order(payload, llm_order + remaining), f"LLM route: {reason}"
    else:
        reason = "LLM router disabled by EXECUTION_ROUTER_USE_LLM."
    return _enforce_smart_router_stage_order(payload, _heuristic_router_order(payload, candidates)), f"Heuristic route: {reason}"


def _call_execution_handler(
    handler: Callable[..., list[str]],
    payload: dict[str, Any],
    *,
    progress_callback: Callable[[str], None] | None = None,
    should_stop_callback: Callable[[], bool] | None = None,
) -> list[str]:
    signature = inspect.signature(handler)
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
    kwargs: dict[str, Any] = {}
    if accepts_kwargs or "progress_callback" in signature.parameters:
        kwargs["progress_callback"] = progress_callback
    if accepts_kwargs or "should_stop_callback" in signature.parameters:
        kwargs["should_stop_callback"] = should_stop_callback
    if kwargs:
        return handler(payload, **kwargs)
    return handler(payload)


def _browser_use_cdp_version_url(cdp_url: str) -> str | None:
    raw = (cdp_url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return None
    path = parsed.path or ""
    if path.rstrip("/").endswith("/json/version"):
        return raw
    normalized_path = path.rstrip("/")
    next_path = f"{normalized_path}/json/version" if normalized_path else "/json/version"
    return parsed._replace(path=next_path, params="", query="", fragment="").geturl()


def _ensure_browser_use_cdp_available(cdp_url: str) -> None:
    version_url = _browser_use_cdp_version_url(cdp_url)
    if version_url is None:
        return
    try:
        response = requests.get(version_url, timeout=5)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"浏览器调试地址不可用：{cdp_url}。请先用带 --remote-debugging-port=9222 的 Edge 启动目标网页，"
            "或在任务执行页改成正确的 CDP 地址。"
        ) from exc
    except ValueError as exc:
        raise RuntimeError(
            f"浏览器调试地址返回了无法识别的内容：{cdp_url}。请确认这个地址对应的是 Chrome/Edge 的远程调试端口。"
        ) from exc

    if not isinstance(payload, dict) or not payload.get("webSocketDebuggerUrl"):
        raise RuntimeError(
            f"浏览器调试地址没有返回有效的调试信息：{cdp_url}。请确认 Edge 已用远程调试模式启动。"
        )


def scheduler_run_eia_live_workflow(payload: dict, progress_callback: Callable[[str], None] | None = None) -> list[str]:
    cdp_url = payload.get("cdp_url") or DEFAULT_CDP_URL
    filter_spec = parse_eia_request(payload.get("user_request") or "")
    session = connect_over_cdp(cdp_url)
    logs: list[str] = []

    def report(message: str) -> None:
        _task_progress_report(logs, progress_callback, message)

    try:
        run_eia_live_workflow(session, progress_callback=report, filter_spec=filter_spec)
    except ManualCheckpointRequired as exc:
        logs.append(f"Manual checkpoint required: {exc}")
        raise ManualTaskRequired(str(exc), logs) from exc
    finally:
        close_replay_session(session)
    return logs


def scheduler_run_generic_live_workflow(payload: dict, progress_callback: Callable[[str], None] | None = None) -> list[str]:
    cdp_url = payload.get("cdp_url") or DEFAULT_CDP_URL
    raw_plan = payload.get("plan")
    if not isinstance(raw_plan, dict):
        raise ValueError("A generic live workflow requires a valid plan payload.")

    replay_plan = ReplayPlan.model_validate(raw_plan)
    session = connect_over_cdp(cdp_url)
    logs: list[str] = []

    def report(message: str) -> None:
        _task_progress_report(logs, progress_callback, message)

    try:
        report(f"Running generic plan with {len(replay_plan.steps)} steps.")
        run_replay_plan(session, replay_plan, progress_callback=report)
    finally:
        close_replay_session(session)
    return logs


def scheduler_run_fast_skill_preflight(
    payload: dict,
    *,
    plan: dict[str, Any] | None,
    downloads_path: Path,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[list[str], bool]:
    logs: list[str] = []
    if not plan or not _env_bool("BROWSER_USE_PREFLIGHT_REPLAY", BROWSER_USE_PREFLIGHT_REPLAY_DEFAULT):
        return logs, False

    def report(message: str) -> None:
        _task_progress_report(logs, progress_callback, message)

    cdp_url = str(payload.get("cdp_url") or DEFAULT_CDP_URL).strip()
    try:
        replay_plan = ReplayPlan.model_validate(_compact_plan_for_fast_preflight(plan))
    except Exception as exc:
        report(f"Fast preflight skipped: invalid recorded plan ({_compact_error_text(exc)}).")
        return logs, False
    if not replay_plan.steps:
        report("Fast preflight skipped: no executable recorded steps.")
        return logs, False

    wants_file_delivery = _task_requests_file_delivery(payload, plan)
    download_snapshot = _snapshot_preflight_download_files(downloads_path) if wants_file_delivery else {}

    report(f"Fast preflight: replaying {len(replay_plan.steps)} recorded skill steps with local CDP.")
    session = connect_over_cdp(cdp_url)
    try:
        run_replay_plan(session, replay_plan, progress_callback=report)
        final_url = _safe_replay_page_url(session)
        if final_url and _is_http_url(final_url) and not is_eyeclaw_console_url(final_url):
            report(f"final_url: {final_url}")

        if wants_file_delivery:
            downloaded_files = _new_preflight_download_files(downloads_path, download_snapshot)
            if not downloaded_files:
                downloaded_files = _try_trigger_fast_preflight_download(session, downloads_path, report)
            if not downloaded_files:
                report("Fast preflight reached a page but did not produce a real downloaded file; falling back to Browser Use.")
                return logs, False
            for downloaded_file in downloaded_files:
                report(f"downloaded_file: {downloaded_file}")

        report("Fast preflight completed; Browser Use fallback skipped.")
        return logs, True
    except Exception as exc:
        report(f"Fast preflight failed; falling back to Browser Use: {_compact_error_text(exc)}")
        return logs, False
    finally:
        close_replay_session(session)


def _compact_plan_for_fast_preflight(plan: dict[str, Any]) -> dict[str, Any]:
    compacted = dict(plan)
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list):
        return compacted

    steps = _normalize_dropdown_replay_steps(
        _repair_misordered_location_child_steps(_filter_internal_replay_steps([step for step in raw_steps if isinstance(step, dict)]))
    )
    compacted_steps: list[dict[str, Any]] = []
    previous_action = ""
    previous_target = ""
    for step in steps:
        action = str(step.get("action") or "").strip().lower()
        target = str(step.get("target") or "").strip()
        if action == "wait":
            wait_after_navigation_action = previous_action in {"click", "open"}
            if _is_low_value_wait_target(target) and not wait_after_navigation_action:
                continue
        if action == "click" and previous_action == "click" and target and target == previous_target:
            continue
        compacted_steps.append(dict(step))
        previous_action = action
        previous_target = target

    compacted["steps"] = _renumber_plan_steps(compacted_steps)
    return compacted


def _normalize_dropdown_replay_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    index = 0
    while index < len(steps):
        step = steps[index]
        next_step = steps[index + 1] if index + 1 < len(steps) else None
        after_next_step = steps[index + 2] if index + 2 < len(steps) else None

        if (
            next_step is not None
            and after_next_step is not None
            and _is_dropdown_trigger_step(step)
            and _is_dropdown_trigger_step(next_step)
            and _is_dropdown_option_step(after_next_step)
        ):
            index += 1
            continue

        if next_step is not None and _is_dropdown_trigger_step(step) and _is_dropdown_option_step(next_step):
            normalized.append(_merge_dropdown_trigger_and_option(step, next_step))
            index += 2
            continue

        normalized.append(step)
        index += 1
    return _renumber_plan_steps(normalized) if normalized else steps


def _merge_dropdown_trigger_and_option(trigger_step: dict[str, Any], option_step: dict[str, Any]) -> dict[str, Any]:
    merged = dict(trigger_step)
    option_text = str(option_step.get("value") or option_step.get("target") or "").strip()
    merged["action"] = "select"
    if option_text:
        merged["value"] = option_text
    if not _has_dropdown_trigger_selector(merged):
        merged.pop("selector_hint", None)
    notes = [str(trigger_step.get("notes") or "").strip(), str(option_step.get("notes") or "").strip()]
    notes = [note for note in notes if note]
    if notes:
        merged["notes"] = " / ".join(notes)
    return merged


def _is_dropdown_trigger_step(step: dict[str, Any] | None) -> bool:
    if not isinstance(step, dict):
        return False
    action = str(step.get("action") or "").strip().lower()
    if action not in {"click", "select"}:
        return False
    if _has_dropdown_option_selector(step):
        return False
    target = str(step.get("target") or "").strip()
    selector_hint = str(step.get("selector_hint") or "").strip()
    return _has_dropdown_trigger_selector(step) or _looks_like_dropdown_trigger_text(target)


def _is_dropdown_option_step(step: dict[str, Any] | None) -> bool:
    if not isinstance(step, dict):
        return False
    if str(step.get("action") or "").strip().lower() != "click":
        return False
    target = str(step.get("value") or step.get("target") or "").strip()
    if not target or _is_generic_dropdown_placeholder(target):
        return False
    has_option_selector = _has_dropdown_option_selector(step)
    if _has_dropdown_trigger_selector(step):
        return False
    if not has_option_selector and _looks_like_dropdown_trigger_text(target):
        return False
    notes = str(step.get("notes") or "").strip()
    return has_option_selector or _looks_like_dropdown_option_notes(notes)


def _has_dropdown_option_selector(step: dict[str, Any]) -> bool:
    selector_hint = str(step.get("selector_hint") or "").strip().lower()
    option_selector_markers = (
        "dropdown__item",
        "select-dropdown",
        "cascader",
        "select2-results__option",
        "ant-select-item-option",
        "ant-cascader-menu-item",
        "[role=\"option\"]",
        "[role='option']",
        "> option",
    )
    return any(marker in selector_hint for marker in option_selector_markers)


def _has_dropdown_trigger_selector(step: dict[str, Any]) -> bool:
    if _has_dropdown_option_selector(step):
        return False
    selector_hint = str(step.get("selector_hint") or "").strip().lower()
    trigger_selector_markers = (
        "el-select",
        "ant-select",
        "select2",
        "combobox",
        "[role=\"combobox\"]",
        "[role='combobox']",
        "select[",
        "select.",
    )
    return any(marker in selector_hint for marker in trigger_selector_markers)


def _looks_like_dropdown_trigger_text(value: str) -> bool:
    text = value.strip().lower()
    if not text:
        return False
    if _is_generic_dropdown_placeholder(text):
        return True
    dropdown_markers = (
        "\u4e0b\u62c9",
        "\u9009\u62e9\u6846",
        "\u7ea7\u8054",
        "dropdown",
        "combobox",
    )
    return any(marker in text for marker in dropdown_markers)


def _is_generic_dropdown_placeholder(value: str) -> bool:
    text = value.strip().lower()
    generic_values = {
        "\u8bf7\u9009\u62e9",
        "\u4e0d\u9650",
        "\u5168\u90e8",
        "\u9009\u62e9",
        "please select",
        "select",
        "all",
    }
    return text in generic_values


def _looks_like_dropdown_option_notes(value: str) -> bool:
    text = value.strip().lower()
    option_markers = (
        "\u9009\u9879",
        "\u9009\u4e2d",
        "\u5b50\u9879",
        "option",
        "dropdown item",
    )
    return any(marker in text for marker in option_markers)


def _is_low_value_wait_target(target: str) -> bool:
    text = target.strip().lower()
    if not text:
        return True
    if text.startswith(("http://", "https://")):
        return True
    low_value_markers = [
        "页面加载",
        "加载完成",
        "page loaded",
        "load complete",
    ]
    return any(marker in text for marker in low_value_markers)


def _task_requests_file_delivery(payload: dict, plan: dict[str, Any] | None) -> bool:
    values: list[str] = [
        str(payload.get("objective") or ""),
        str(payload.get("user_request") or ""),
    ]
    if isinstance(plan, dict):
        values.append(str(plan.get("site_url") or ""))
        for step in plan.get("steps") or []:
            if isinstance(step, dict):
                values.extend(
                    [
                        str(step.get("action") or ""),
                        str(step.get("target") or ""),
                        str(step.get("value") or ""),
                        str(step.get("notes") or ""),
                    ]
                )
    text = " ".join(values).lower()
    markers = [
        "下载",
        "保存",
        "导出",
        "文件",
        "pdf",
        "ctrl+s",
        "control+s",
        "meta+s",
        "download",
        "save",
        "export",
    ]
    return any(marker in text for marker in markers)


def _snapshot_preflight_download_files(downloads_path: Path) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    if not downloads_path.exists():
        return snapshot
    for path in downloads_path.rglob("*"):
        if not _is_completed_preflight_download(path):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _new_preflight_download_files(downloads_path: Path, before: dict[str, tuple[int, int]]) -> list[str]:
    if not downloads_path.exists():
        return []
    candidates: list[tuple[int, Path]] = []
    for path in downloads_path.rglob("*"):
        if not _is_completed_preflight_download(path):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        key = str(path.resolve())
        if before.get(key) == (stat.st_mtime_ns, stat.st_size):
            continue
        candidates.append((stat.st_mtime_ns, path))
    return [str(path.resolve()) for _, path in sorted(candidates)]


def _is_completed_preflight_download(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() in {".crdownload", ".download", ".part", ".tmp"}:
        return False
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _try_trigger_fast_preflight_download(
    session: Any,
    downloads_path: Path,
    report: Callable[[str], None],
) -> list[str]:
    page = getattr(session, "page", None)
    if page is None:
        return []
    downloads_path.mkdir(parents=True, exist_ok=True)
    before = _snapshot_preflight_download_files(downloads_path)
    if _click_common_download_control(page, downloads_path, report):
        downloaded_files = _wait_for_new_preflight_downloads(downloads_path, before, timeout_seconds=8.0)
        if downloaded_files:
            return downloaded_files
    return _new_preflight_download_files(downloads_path, before)


def _click_common_download_control(page: Any, downloads_path: Path, report: Callable[[str], None]) -> bool:
    selectors = [
        "#download",
        "#secondaryDownload",
        "a[download]",
        "button[aria-label*='Download' i]",
        "a[aria-label*='Download' i]",
        "button[title*='Download' i]",
        "a[title*='Download' i]",
    ]
    text_targets = ["下载", "导出", "保存", "Download", "Export", "Save", "PDF"]
    locators: list[Any] = []
    for selector in selectors:
        try:
            locators.append(page.locator(selector).first)
        except Exception:
            continue
    for text in text_targets:
        try:
            locators.append(page.get_by_text(text, exact=False).first)
        except Exception:
            continue

    for locator in locators:
        try:
            if locator.count() <= 0 or not locator.is_visible(timeout=500):
                continue
        except Exception:
            continue
        try:
            with page.expect_download(timeout=8_000) as download_info:
                locator.scroll_into_view_if_needed(timeout=1_500)
                locator.click(timeout=3_000)
            download = download_info.value
            filename = _safe_artifact_filename(download.suggested_filename or "downloaded-file")
            target = _unique_path(downloads_path / filename)
            download.save_as(str(target))
            report(f"Fast preflight triggered browser download: {target}")
            return True
        except Exception as exc:
            try:
                locator.click(timeout=2_000)
                report(f"Fast preflight clicked a likely download control without a download event: {_compact_error_text(exc)}")
                return True
            except Exception:
                continue
    return False


def _wait_for_new_preflight_downloads(
    downloads_path: Path,
    before: dict[str, tuple[int, int]],
    *,
    timeout_seconds: float,
) -> list[str]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        files = _new_preflight_download_files(downloads_path, before)
        if files:
            return files
        time.sleep(0.25)
    return []


def _task_pdf_filename_hint(payload: dict, final_url: str | None = None) -> str:
    skill_names = [str(name).strip() for name in payload.get("skill_names") or [] if str(name).strip()]
    objective = str(payload.get("objective") or "").strip()
    user_request = str(payload.get("user_request") or "").strip()
    host = urlparse(final_url or "").netloc
    raw = skill_names[0] if skill_names else objective or user_request or host or "eyeclaw-task-result"
    return _safe_artifact_filename(raw, suffix=".pdf")


def _safe_artifact_filename(raw_name: str, *, suffix: str = "") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", str(raw_name or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned[:80].strip(" .") or "eyeclaw-task-result"
    if suffix and not cleaned.lower().endswith(suffix.lower()):
        cleaned = f"{cleaned}{suffix}"
    return cleaned


def _safe_replay_page_url(session: Any) -> str:
    try:
        return str(session.page.url or "")
    except Exception:
        return ""


def _save_replay_page_as_pdf(session: Any, *, downloads_path: Path, filename_hint: str) -> str:
    downloads_path.mkdir(parents=True, exist_ok=True)
    target_path = _unique_path(downloads_path / _safe_artifact_filename(filename_hint, suffix=".pdf"))
    page = session.page
    cdp_session = page.context.new_cdp_session(page)
    result = cdp_session.send(
        "Page.printToPDF",
        {
            "printBackground": True,
            "landscape": False,
            "paperWidth": 8.27,
            "paperHeight": 11.69,
            "preferCSSPageSize": True,
        },
    )
    pdf_data = result.get("data") if isinstance(result, dict) else None
    if not pdf_data:
        raise RuntimeError("CDP Page.printToPDF returned no PDF data.")
    target_path.write_bytes(base64.b64decode(pdf_data))
    return str(target_path.resolve())


def _unique_path(path: Path) -> Path:
    candidate = path
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        index += 1
    return candidate


def _compact_error_text(exc: Exception, *, limit: int = 220) -> str:
    text = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
    return text[:limit] + ("..." if len(text) > limit else "")


def _normalize_openai_compatible_base_url(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def _is_deepseek_endpoint(base_url: str, model: str) -> bool:
    return "deepseek" in base_url.lower() or model.lower().startswith("deepseek")


def _is_glm_endpoint(base_url: str, model: str) -> bool:
    normalized_base_url = base_url.lower()
    normalized_model = model.lower()
    return (
        normalized_model.startswith("glm")
        or "bigmodel.cn" in normalized_base_url
        or "zhipu" in normalized_base_url
    )


def _is_deepseek_tool_choice_incompatible_model(model: str) -> bool:
    normalized = model.strip().lower().replace("_", "-")
    markers = ["reasoner", "thinking", "r1", "v4-pro"]
    return normalized.startswith("deepseek") and any(marker in normalized for marker in markers)


def _resolve_browser_use_llm_settings(config: Any, *, prefer_vision: bool = False) -> dict[str, str | bool]:
    if prefer_vision:
        configured_model = str(getattr(config, "glm_model", None) or getattr(config, "deepseek_model", ""))
        configured_base_url = str(getattr(config, "glm_base_url", None) or getattr(config, "deepseek_base_url", ""))
        configured_api_key = str(getattr(config, "glm_api_key", None) or getattr(config, "deepseek_api_key", ""))
        model = _env_text("BROWSER_USE_VISION_LLM", configured_model)
        base_url = _normalize_openai_compatible_base_url(_env_text("BROWSER_USE_VISION_LLM_BASE_URL", configured_base_url))
        api_key = _env_text("BROWSER_USE_VISION_LLM_API_KEY", configured_api_key)
    else:
        configured_model = str(config.deepseek_model)
        model = _env_text("BROWSER_USE_LLM", configured_model)
        explicit_base_url = os.getenv("BROWSER_USE_LLM_BASE_URL")
        explicit_api_key = os.getenv("BROWSER_USE_LLM_API_KEY")
        base_url = _normalize_openai_compatible_base_url(
            _env_text("BROWSER_USE_LLM_BASE_URL", str(config.deepseek_base_url))
        )
        if explicit_base_url is None and _is_glm_endpoint(base_url, model):
            base_url = _normalize_openai_compatible_base_url(str(config.glm_base_url))
        api_key = _env_text("BROWSER_USE_LLM_API_KEY", str(config.deepseek_api_key))
        if explicit_api_key is None and explicit_base_url is None and _is_glm_endpoint(base_url, model):
            api_key = str(config.glm_api_key)
    switched_model = False

    if (
        not prefer_vision
        and not os.getenv("BROWSER_USE_LLM")
        and _is_deepseek_endpoint(base_url, model)
        and _is_deepseek_tool_choice_incompatible_model(model)
    ):
        model = BROWSER_USE_DEEPSEEK_TOOL_MODEL
        switched_model = True

    return {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "switched_model": switched_model,
        "prefer_vision": prefer_vision,
    }


def _build_browser_use_llm(config: Any, *, fast_mode: bool, llm_timeout: int, prefer_vision: bool = False) -> Any:
    settings = _resolve_browser_use_llm_settings(config, prefer_vision=prefer_vision)
    base_url = str(settings["base_url"])
    model = str(settings["model"])
    api_key = str(settings["api_key"])
    max_tokens = _env_int(
        "BROWSER_USE_MAX_COMPLETION_TOKENS",
        1536 if fast_mode else 2048,
        minimum=512,
        maximum=4096,
    )
    use_deepseek_native_tools = _env_bool(
        "BROWSER_USE_DEEPSEEK_NATIVE_TOOLS",
        BROWSER_USE_DEEPSEEK_NATIVE_TOOLS_DEFAULT,
    )
    if (
        use_deepseek_native_tools
        and _is_deepseek_endpoint(base_url, model)
        and not _is_deepseek_tool_choice_incompatible_model(model)
    ):
        try:
            from browser_use.llm import ChatDeepSeek

            return ChatDeepSeek(
                model=model,
                api_key=api_key,
                base_url=base_url or None,
                temperature=0,
                max_tokens=max_tokens,
                timeout=llm_timeout,
            )
        except Exception:
            pass

    if _is_deepseek_endpoint(base_url, model):
        from src.deepseek_browser_use_llm import DeepSeekBrowserUseLLM

        return DeepSeekBrowserUseLLM(
            model=model,
            api_key=api_key,
            base_url=base_url or None,
            temperature=0,
            max_tokens=max_tokens,
            timeout=llm_timeout,
            max_retries=0 if fast_mode else 1,
        )

    if _is_glm_endpoint(base_url, model):
        from src.deepseek_browser_use_llm import OpenAICompatibleBrowserUseLLM

        thinking_type = _env_text("BROWSER_USE_GLM_THINKING", "disabled")
        extra_body = {"thinking": {"type": thinking_type}} if thinking_type else None
        return OpenAICompatibleBrowserUseLLM(
            model=model,
            api_key=api_key,
            base_url=base_url or None,
            temperature=0,
            max_tokens=max_tokens,
            timeout=llm_timeout,
            max_retries=0 if fast_mode else 1,
            provider_name="glm",
            extra_body=extra_body,
        )

    from browser_use import ChatOpenAI

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url or None,
        temperature=0,
        max_completion_tokens=max_tokens,
        max_retries=0 if fast_mode else 1,
        timeout=llm_timeout,
        reasoning_effort=None,
        reasoning_models=[],
        add_schema_to_system_prompt=True,
        dont_force_structured_output=True,
        remove_min_items_from_schema=True,
        remove_defaults_from_schema=True,
    )


def scheduler_run_browser_use_live_workflow(
    payload: dict,
    progress_callback: Callable[[str], None] | None = None,
    should_stop_callback: Callable[[], bool] | None = None,
) -> list[str]:
    status = load_config_status()
    if not status.is_ready or status.config is None:
        raise ValueError(f"Configuration is incomplete: {status.missing_fields}")

    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    cdp_url = str(payload.get("cdp_url") or DEFAULT_CDP_URL).strip()
    start_url = _resolve_browser_use_start_url(plan)
    downloads_path = Path(str(payload.get("downloads_path") or "artifacts/downloads"))
    _ensure_browser_use_cdp_available(cdp_url)

    logs: list[str] = []

    def report(message: str) -> None:
        _task_progress_report(logs, progress_callback, message)

    fast_mode = _env_bool("BROWSER_USE_FAST_MODE", BROWSER_USE_FAST_MODE_DEFAULT)
    wants_file_delivery = _task_requests_file_delivery(payload, plan)
    max_steps = _env_int("BROWSER_USE_MAX_STEPS", 14 if fast_mode else 24, minimum=4, maximum=48)
    max_actions = _env_int("BROWSER_USE_MAX_ACTIONS_PER_STEP", 8 if fast_mode else 4, minimum=1, maximum=12)
    llm_timeout = _env_int("BROWSER_USE_LLM_TIMEOUT", 45 if fast_mode else 60, minimum=10, maximum=180)
    step_timeout = _env_int("BROWSER_USE_STEP_TIMEOUT", 60 if fast_mode else 90, minimum=15, maximum=240)
    browser_use_llm = _resolve_browser_use_llm_settings(status.config)
    browser_use_llm_message = f"Browser Use LLM: {browser_use_llm['model']}"
    if browser_use_llm.get("switched_model"):
        browser_use_llm_message += f" (auto-switched from {status.config.deepseek_model} because thinking models reject tool_choice)"
    elif (
        _is_deepseek_endpoint(str(browser_use_llm["base_url"]), str(browser_use_llm["model"]))
        and not _env_bool("BROWSER_USE_DEEPSEEK_NATIVE_TOOLS", BROWSER_USE_DEEPSEEK_NATIVE_TOOLS_DEFAULT)
    ):
        browser_use_llm_message += " (compat JSON mode, no tool_choice)"
    elif _is_glm_endpoint(str(browser_use_llm["base_url"]), str(browser_use_llm["model"])):
        browser_use_llm_message += " (OpenAI-compatible JSON mode)"

    for message in [
        f"Browser Use start_url: {start_url}",
        f"Browser Use cdp_url: {cdp_url}",
        browser_use_llm_message,
        "Execution strategy: fast recorded-skill preflight first, Browser Use fallback only when needed.",
        "Browser tab policy: keep existing pages open; create a dedicated execution page for each task start URL.",
        (
            "Browser Use speed profile: "
            f"{'fast' if fast_mode else 'standard'}, "
            f"max_steps={max_steps}, max_actions_per_step={max_actions}, "
            f"llm_timeout={llm_timeout}s, step_timeout={step_timeout}s."
        ),
    ]:
        report(message)

    skip_fast_preflight = bool(payload.get("skip_fast_preflight"))
    if skip_fast_preflight:
        preflight_logs = ["Fast preflight skipped because Playwright hybrid replay already attempted it."]
        preflight_completed = False
        for message in preflight_logs:
            report(message)
    else:
        preflight_logs, preflight_completed = scheduler_run_fast_skill_preflight(
            payload,
            plan=plan,
            downloads_path=downloads_path,
            progress_callback=progress_callback,
        )
    for message in preflight_logs:
        if message not in logs:
            logs.append(message)
    if preflight_completed:
        return logs

    task_text = _compose_browser_use_task(
        str(payload.get("user_request") or ""),
        plan,
        skill_names=[str(name) for name in payload.get("skill_names") or []],
        objective=str(payload.get("objective") or ""),
        preflight_attempted=bool(preflight_logs),
    )
    for message in [
        "Browser Use is the fallback execution engine for this task.",
        "Browser Use task:",
        task_text,
    ]:
        report(message)

    attempt_plan = _browser_use_attempt_plan(payload, plan)
    last_attempt_error = ""
    for attempt_index, attempt in enumerate(attempt_plan, start=1):
        if should_stop_callback is not None and should_stop_callback():
            raise ManualTaskRequired("Task stopped before Browser Use attempt started.", logs)
        use_vision = attempt["vision_mode"]
        prefer_vision_llm = bool(attempt["prefer_vision_llm"])
        attempt_llm = _resolve_browser_use_llm_settings(status.config, prefer_vision=prefer_vision_llm)
        report(
            f"Browser Use attempt {attempt_index}/{len(attempt_plan)}: "
            f"vision={_browser_use_vision_mode_label(use_vision)}, "
            f"llm={attempt_llm['model']}, reason={attempt['reason']}"
        )
        if prefer_vision_llm and _is_deepseek_endpoint(str(attempt_llm["base_url"]), str(attempt_llm["model"])):
            report("Browser Use visual attempt warning: selected model is DeepSeek, so Browser Use may disable screenshots.")

        result = asyncio.run(
            _run_browser_use_agent(
                task_text=task_text,
                cdp_url=cdp_url,
                config=status.config,
                plan=plan,
                start_url=start_url,
                downloads_path=downloads_path,
                progress_callback=report,
                should_stop_callback=should_stop_callback,
                fast_mode=fast_mode,
                max_steps=max_steps,
                max_actions_per_step=max_actions,
                llm_timeout=llm_timeout,
                step_timeout=step_timeout,
                wants_file_delivery=wants_file_delivery,
                filename_hint=_task_pdf_filename_hint(payload),
                use_vision=use_vision,
                prefer_vision_llm=prefer_vision_llm,
            )
        )
        for message in result:
            report(message)
        if _browser_use_result_needs_manual_checkpoint(result):
            raise ManualTaskRequired("Browser Use reported a manual checkpoint.", logs)

        attempt_error = _browser_use_result_missing_required_completion(result, plan)
        if not attempt_error and _browser_use_result_indicates_incomplete(result):
            if _browser_use_has_file_deliverable(result):
                report("Browser Use produced a final file deliverable; overriding its failed self-check.")
            else:
                attempt_error = _browser_use_failure_message(result)
        if not attempt_error:
            return logs

        last_attempt_error = attempt_error
        if attempt_index < len(attempt_plan):
            report(f"Browser Use attempt {attempt_index} did not pass final validation; escalating to visual retry: {attempt_error}")
            continue
        raise RuntimeError(attempt_error)

    raise RuntimeError(last_attempt_error or "Browser Use did not finish successfully.")


def scheduler_run_playwright_browser_use_live_workflow(
    payload: dict,
    progress_callback: Callable[[str], None] | None = None,
    should_stop_callback: Callable[[], bool] | None = None,
) -> list[str]:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    downloads_path = Path(str(payload.get("downloads_path") or "artifacts/downloads"))
    logs: list[str] = []

    def report(message: str) -> None:
        _task_progress_report(logs, progress_callback, message)

    report("Execution strategy: Playwright fast replay first; Browser Use fallback only if replay cannot complete.")
    if should_stop_callback is not None and should_stop_callback():
        raise ManualTaskRequired("Task stopped before Playwright replay started.", logs)

    replay_logs, replay_completed = scheduler_run_fast_skill_preflight(
        payload,
        plan=plan,
        downloads_path=downloads_path,
        progress_callback=progress_callback,
    )
    for message in replay_logs:
        if message not in logs:
            logs.append(message)
    if replay_completed:
        report("Playwright fast replay completed; Browser Use fallback skipped.")
        return logs

    if should_stop_callback is not None and should_stop_callback():
        raise ManualTaskRequired("Task stopped before Browser Use fallback started.", logs)

    report("Playwright fast replay did not complete the task; escalating to Browser Use fallback.")
    fallback_payload = dict(payload)
    fallback_payload["skip_fast_preflight"] = True
    fallback_payload["task_type"] = PRIMARY_EXECUTION_TASK_TYPE
    fallback_logs = scheduler_run_browser_use_live_workflow(
        fallback_payload,
        progress_callback=progress_callback,
        should_stop_callback=should_stop_callback,
    )
    for message in fallback_logs:
        if message not in logs:
            logs.append(message)
    return logs


def _downloaded_files_for_attempt(
    *,
    downloads_path: Path,
    before: dict[str, tuple[int, int]],
    logs: list[str],
) -> list[str]:
    files = _new_preflight_download_files(downloads_path, before)
    for raw_path in _downloaded_files_from_logs(logs):
        try:
            path = Path(raw_path).resolve()
            stat = path.stat()
        except OSError:
            continue
        if before.get(str(path)) == (stat.st_mtime_ns, stat.st_size):
            continue
        files.append(str(path))
    return dedupe_preserving_order(files)


def _url_matches_expected_target(candidate: str, expected: str) -> bool:
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


def _routed_final_validation_error(
    *,
    payload: dict[str, Any],
    logs: list[str],
    task_type: str,
    downloaded_files: list[str],
) -> str:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    criteria = _payload_acceptance_criteria(payload, plan)
    requires_file = bool(criteria.get("requires_file"))
    expected_targets = [
        str(item)
        for item in criteria.get("expected_url_targets") or []
        if _is_http_url(str(item)) and not is_eyeclaw_console_url(str(item))
    ]

    if requires_file and not downloaded_files:
        return (
            "Final validation failed: the task requests a file, but this executor "
            "did not create a new downloaded_file artifact."
        )

    if _browser_use_result_indicates_incomplete(logs) and not downloaded_files:
        return _browser_use_failure_message(logs)

    if not requires_file:
        preview_error = _browser_use_result_missing_required_completion(logs, plan)
        if preview_error:
            return f"Final validation failed: {preview_error}"

        if expected_targets:
            logged_urls = _browser_use_logged_urls(logs)
            if not any(
                _url_matches_expected_target(candidate, expected)
                for expected in expected_targets
                for candidate in logged_urls
            ):
                return (
                    "Final validation failed: no logged final URL/page state matched "
                    f"the recorded target {expected_targets[-1]}."
                )

    if task_type == PRIMARY_EXECUTION_TASK_TYPE and not logs:
        return "Final validation failed: Browser Use returned no execution history."
    return ""


def scheduler_run_smart_router_live_workflow(
    payload: dict,
    progress_callback: Callable[[str], None] | None = None,
    should_stop_callback: Callable[[], bool] | None = None,
) -> list[str]:
    logs: list[str] = []
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    downloads_path = Path(str(payload.get("downloads_path") or "artifacts/downloads"))
    acceptance = _payload_acceptance_criteria(payload, plan)

    def report(message: str) -> None:
        _task_progress_report(logs, progress_callback, message)

    order, route_reason = resolve_smart_router_order(payload)
    if not order:
        raise RuntimeError(route_reason)
    report(
        "Smart router stage 1/4 - recorded context: "
        f"skills={len(payload.get('skill_names') or [])}, "
        f"steps={acceptance.get('recorded_step_count')}, "
        f"goal={acceptance.get('goal') or '(empty)'}"
    )
    report(
        "Smart router acceptance policy: "
        + json.dumps(
            {
                "criteria": acceptance.get("criteria") or [],
                "requires_file": bool(acceptance.get("requires_file")),
                "requires_preview": bool(acceptance.get("requires_preview")),
                "expected_url_targets": acceptance.get("expected_url_targets") or [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    report("Smart router stage 2/4 - deterministic executors first; Browser Use is reserved for fallback.")
    report(f"Smart router selected execution order: {' -> '.join(order)}")
    report(route_reason)

    errors: list[str] = []
    for index, task_type in enumerate(order, start=1):
        if should_stop_callback is not None and should_stop_callback():
            raise ManualTaskRequired("Task stopped before the next routed executor started.", logs)
        handler = TASK_HANDLERS.get(task_type)
        if handler is None or task_type == SMART_ROUTER_TASK_TYPE:
            message = f"Smart router skipped unavailable executor: {task_type}"
            errors.append(message)
            report(message)
            continue
        routed_payload = dict(payload)
        routed_payload["task_type"] = task_type
        report(f"Smart router attempt {index}/{len(order)}: {task_type}")
        try:
            if task_type == PRIMARY_EXECUTION_TASK_TYPE:
                routed_payload.setdefault("skip_fast_preflight", True)
                report("Smart router stage 3/4 - Browser Use fallback is taking over from deterministic executors.")
            attempt_download_snapshot = _snapshot_preflight_download_files(downloads_path)
            routed_logs = _call_execution_handler(
                handler,
                routed_payload,
                progress_callback=progress_callback,
                should_stop_callback=should_stop_callback,
            )
            for message in routed_logs:
                if message not in logs:
                    logs.append(message)
            downloaded_files = _downloaded_files_for_attempt(
                downloads_path=downloads_path,
                before=attempt_download_snapshot,
                logs=routed_logs,
            )
            validation_error = _routed_final_validation_error(
                payload=routed_payload,
                logs=routed_logs,
                task_type=task_type,
                downloaded_files=downloaded_files,
            )
            if validation_error:
                message = f"Smart router final validation failed: {task_type}: {validation_error}"
                errors.append(message)
                report(message)
                continue
            report("Smart router stage 4/4 - final validation passed by rules, not model self-report.")
            report(f"Smart router executor succeeded: {task_type}")
            return logs
        except ManualTaskRequired:
            raise
        except Exception as exc:
            message = f"Smart router executor failed: {task_type}: {_compact_error_text(exc)}"
            errors.append(message)
            report(message)

    raise RuntimeError("All routed execution adapters failed. " + " | ".join(errors[-4:]))


def scheduler_run_execution_adapter_benchmark(
    payload: dict,
    progress_callback: Callable[[str], None] | None = None,
    should_stop_callback: Callable[[], bool] | None = None,
) -> list[str]:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    base_downloads_path = Path(str(payload.get("downloads_path") or "artifacts/downloads"))
    candidates = _benchmark_candidate_types(payload)
    runs_per_adapter = _benchmark_run_count(payload)
    if not candidates:
        raise RuntimeError("No executable adapters are available for benchmark testing.")

    wants_file_delivery = _task_requests_file_delivery(payload, plan)
    logs: list[str] = []
    attempts: list[dict[str, Any]] = []
    total_attempts = len(candidates) * runs_per_adapter
    completed_attempts = 0

    def report(message: str) -> None:
        _task_progress_report(logs, progress_callback, message)

    report(f"Benchmark total attempts: {total_attempts}")
    report(f"Benchmark candidates: {' -> '.join(candidates)}")
    report(
        "Benchmark success policy: "
        + ("requires a real downloaded file." if wants_file_delivery else "requires executor completion and records final URL when available.")
    )

    for task_type in candidates:
        handler = TASK_HANDLERS.get(task_type)
        if handler is None:
            attempts.append(_benchmark_attempt_record(task_type, 0, False, 0.0, 0, [], "", "handler unavailable"))
            continue
        for run_index in range(1, runs_per_adapter + 1):
            if should_stop_callback is not None and should_stop_callback():
                raise ManualTaskRequired("Benchmark stopped before the next adapter attempt started.", logs)

            adapter_downloads_path = base_downloads_path / "benchmark" / _safe_adapter_dir_name(task_type) / f"run-{run_index}"
            adapter_downloads_path.mkdir(parents=True, exist_ok=True)
            download_snapshot = _snapshot_preflight_download_files(adapter_downloads_path)
            adapter_progress_events: list[str] = []
            adapter_logs: list[str] = []
            error = ""
            execution_success = False
            started_at = time.monotonic()

            report(f"Benchmark attempt {completed_attempts + 1}/{total_attempts}: {task_type} run {run_index}")

            def adapter_progress(message: str) -> None:
                adapter_progress_events.append(str(message))
                report(
                    f"Benchmark adapter progress [{task_type} run {run_index}]: "
                    f"{_compact_benchmark_log_line(str(message))}"
                )

            try:
                routed_payload = dict(payload)
                routed_payload["task_type"] = task_type
                routed_payload["downloads_path"] = str(adapter_downloads_path.resolve())
                if task_type == PRIMARY_EXECUTION_TASK_TYPE:
                    routed_payload["skip_fast_preflight"] = True
                adapter_logs = _call_execution_handler(
                    handler,
                    routed_payload,
                    progress_callback=adapter_progress,
                    should_stop_callback=should_stop_callback,
                )
                execution_success = True
            except ManualTaskRequired as exc:
                adapter_logs = list(exc.logs or [])
                error = str(exc)
            except Exception as exc:
                error = _compact_error_text(exc)

            duration_seconds = max(0.0, time.monotonic() - started_at)
            files = _new_preflight_download_files(adapter_downloads_path, download_snapshot)
            files = dedupe_preserving_order(files + _downloaded_files_from_logs(adapter_logs))
            final_url = _final_url_from_logs(adapter_logs)
            deliverable_success = bool(files) if wants_file_delivery else bool(final_url or execution_success)
            final_success = bool(execution_success and deliverable_success)
            completed_attempts += 1

            attempts.append(
                _benchmark_attempt_record(
                    task_type,
                    run_index,
                    final_success,
                    duration_seconds,
                    len(adapter_progress_events),
                    files,
                    final_url,
                    error,
                )
            )
            report(
                "Benchmark result "
                f"{completed_attempts}/{total_attempts}: {task_type} run {run_index} "
                f"success={str(final_success).lower()} duration={duration_seconds:.2f}s "
                f"progress_events={len(adapter_progress_events)} files={len(files)}"
                + (f" final_url={final_url}" if final_url else "")
                + (f" error={error}" if error else "")
            )
            for line in adapter_logs[-6:]:
                compacted = _compact_benchmark_log_line(str(line))
                if compacted:
                    logs.append(f"Benchmark log [{task_type} run {run_index}]: {compacted}")

    result = _summarize_benchmark_attempts(attempts, runs_per_adapter, wants_file_delivery=wants_file_delivery)
    report("Benchmark summary ready.")
    logs.append("benchmark_result: " + json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return logs


def _benchmark_candidate_types(payload: dict[str, Any]) -> list[str]:
    raw_candidates = payload.get("benchmark_task_types")
    candidates: list[str] = []
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            task_type = _normalize_execution_task_type(str(item))
            if task_type in {SMART_ROUTER_TASK_TYPE, BENCHMARK_TASK_TYPE}:
                continue
            if task_type in TASK_HANDLERS:
                candidates.append(task_type)
    if not candidates:
        candidates = _enabled_router_candidate_types()
    return dedupe_preserving_order(
        [
            task_type
            for task_type in candidates
            if task_type not in {SMART_ROUTER_TASK_TYPE, BENCHMARK_TASK_TYPE}
            and task_type in TASK_HANDLERS
        ]
    )


def _benchmark_run_count(payload: dict[str, Any]) -> int:
    raw_count = payload.get("benchmark_runs") or os.getenv("EXECUTION_BENCHMARK_RUNS") or 1
    try:
        count = int(str(raw_count).strip())
    except (TypeError, ValueError):
        count = 1
    return max(1, min(5, count))


def _safe_adapter_dir_name(task_type: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", task_type).strip("-") or "adapter"


def _benchmark_attempt_record(
    task_type: str,
    run_index: int,
    success: bool,
    duration_seconds: float,
    progress_events: int,
    files: list[str],
    final_url: str,
    error: str,
) -> dict[str, Any]:
    return {
        "task_type": task_type,
        "label": _adapter_label_for_task_type(task_type),
        "run_index": run_index,
        "success": success,
        "duration_seconds": round(duration_seconds, 3),
        "progress_events": progress_events,
        "files": files,
        "final_url": final_url,
        "error": error,
    }


def _summarize_benchmark_attempts(
    attempts: list[dict[str, Any]],
    runs_per_adapter: int,
    *,
    wants_file_delivery: bool,
) -> dict[str, Any]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        by_type.setdefault(str(attempt.get("task_type") or ""), []).append(attempt)

    adapters: list[dict[str, Any]] = []
    for task_type, adapter_attempts in by_type.items():
        successes = len([attempt for attempt in adapter_attempts if attempt.get("success")])
        durations = [float(attempt.get("duration_seconds") or 0) for attempt in adapter_attempts]
        progress_counts = [int(attempt.get("progress_events") or 0) for attempt in adapter_attempts]
        files = dedupe_preserving_order(
            [
                file_path
                for attempt in adapter_attempts
                for file_path in (attempt.get("files") or [])
                if isinstance(file_path, str) and file_path
            ]
        )
        final_urls = dedupe_preserving_order(
            [
                str(attempt.get("final_url") or "")
                for attempt in adapter_attempts
                if str(attempt.get("final_url") or "")
            ]
        )
        adapters.append(
            {
                "task_type": task_type,
                "label": _adapter_label_for_task_type(task_type),
                "runs": len(adapter_attempts),
                "successes": successes,
                "success_rate": round(successes / max(1, len(adapter_attempts)), 3),
                "avg_duration_seconds": round(sum(durations) / max(1, len(durations)), 3),
                "avg_progress_events": round(sum(progress_counts) / max(1, len(progress_counts)), 2),
                "final_files": files,
                "final_urls": final_urls,
                "last_error": next((str(attempt.get("error") or "") for attempt in reversed(adapter_attempts) if attempt.get("error")), ""),
            }
        )
    adapters.sort(key=lambda item: (-float(item["success_rate"]), float(item["avg_duration_seconds"]), item["task_type"]))
    return {
        "runs_per_adapter": runs_per_adapter,
        "required_deliverable": "file" if wants_file_delivery else "executor_success_or_final_url",
        "adapters": adapters,
        "attempts": attempts,
    }


def _adapter_label_for_task_type(task_type: str) -> str:
    for adapter in _execution_adapter_payload().get("adapters") or []:
        if not isinstance(adapter, dict):
            continue
        if _normalize_execution_task_type(str(adapter.get("task_type") or adapter.get("id") or "")) == task_type:
            return str(adapter.get("label") or task_type)
    return task_type


def _downloaded_files_from_logs(logs: list[str]) -> list[str]:
    files: list[str] = []
    for line in logs:
        text = str(line).strip()
        if not text.lower().startswith("downloaded_file:"):
            continue
        raw_path = text.split(":", 1)[1].strip().strip("'\"")
        if raw_path and Path(raw_path).is_file():
            files.append(str(Path(raw_path).resolve()))
    return dedupe_preserving_order(files)


def _final_url_from_logs(logs: list[str]) -> str:
    for line in reversed(logs):
        text = str(line).strip()
        if not text.lower().startswith("final_url:"):
            continue
        match = re.search(r"https?://[^\s<>)\]\"']+", text)
        if match:
            return match.group(0).rstrip(".,;:")
    return ""


def _compact_benchmark_log_line(message: str, *, limit: int = 180) -> str:
    text = str(message or "").replace("\n", " ").strip()
    if text.startswith("# EyeClaw Browser Use Task"):
        return "Browser Use task prompt prepared."
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _extract_benchmark_result(logs: list[str]) -> dict[str, Any] | None:
    for line in reversed(logs):
        text = str(line).strip()
        if not text.startswith("benchmark_result:"):
            continue
        try:
            payload = json.loads(text.split(":", 1)[1].strip())
        except (json.JSONDecodeError, IndexError):
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _extract_benchmark_progress(logs: list[str]) -> tuple[int | None, int, str]:
    total_attempts: int | None = None
    completed_attempts = 0
    latest_stage = ""
    for line in logs:
        text = str(line).strip()
        if text.startswith("Benchmark total attempts:"):
            try:
                total_attempts = int(text.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                total_attempts = None
        if text.startswith("Benchmark result "):
            completed_attempts += 1
            latest_stage = text
        elif text.startswith("Benchmark attempt ") or text.startswith("Benchmark adapter progress"):
            latest_stage = text
    return total_attempts, completed_attempts, latest_stage


def scheduler_run_selenium_live_workflow(
    payload: dict,
    progress_callback: Callable[[str], None] | None = None,
    should_stop_callback: Callable[[], bool] | None = None,
) -> list[str]:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    if not plan:
        raise RuntimeError("Selenium execution requires a recorded EyeClaw plan.")
    replay_plan = ReplayPlan.model_validate(_compact_plan_for_fast_preflight(plan))
    if not replay_plan.steps:
        raise RuntimeError("Selenium execution requires at least one executable step.")

    downloads_path = Path(str(payload.get("downloads_path") or "artifacts/downloads")).resolve()
    downloads_path.mkdir(parents=True, exist_ok=True)
    download_snapshot = _snapshot_preflight_download_files(downloads_path)
    wants_file_delivery = _task_requests_file_delivery(payload, plan)
    cdp_url = str(payload.get("cdp_url") or DEFAULT_CDP_URL).strip()
    logs: list[str] = []

    def report(message: str) -> None:
        _task_progress_report(logs, progress_callback, message)

    report(f"Selenium start_url: {replay_plan.site_url}")
    report(f"Selenium cdp_url: {cdp_url}")
    report(f"Selenium replaying {len(replay_plan.steps)} recorded steps.")
    driver = _start_selenium_driver(cdp_url, downloads_path)
    try:
        _selenium_prepare_execution_tab(driver, replay_plan.site_url, report)
        for index, step in enumerate(replay_plan.steps, start=1):
            if should_stop_callback is not None and should_stop_callback():
                raise ManualTaskRequired("Task stopped during Selenium replay.", logs)
            report(f"Selenium step {index}/{len(replay_plan.steps)}: {step.action} -> {step.target or step.value or '(empty)'}")
            _selenium_execute_step(driver, replay_plan.site_url, step)
            report(f"Selenium step {index}: success")

        final_url = str(getattr(driver, "current_url", "") or "")
        if final_url and _is_http_url(final_url) and not is_eyeclaw_console_url(final_url):
            report(f"final_url: {final_url}")
        if wants_file_delivery:
            downloaded_files = _new_preflight_download_files(downloads_path, download_snapshot)
            if not downloaded_files:
                raise RuntimeError("Selenium replay finished but did not produce a real downloaded file.")
            for downloaded_file in downloaded_files:
                report(f"downloaded_file: {downloaded_file}")
        report("Selenium replay completed.")
        return logs
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _start_selenium_driver(cdp_url: str, downloads_path: Path) -> Any:
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options as EdgeOptions

    options = EdgeOptions()
    parsed = urlparse(cdp_url)
    debugger_address = parsed.netloc if parsed.scheme and parsed.netloc else ""
    if debugger_address:
        options.add_experimental_option("debuggerAddress", debugger_address)
    prefs = {
        "download.default_directory": str(downloads_path),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)
    driver = webdriver.Edge(options=options)
    try:
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(downloads_path)})
    except Exception:
        pass
    return driver


def _selenium_prepare_execution_tab(driver: Any, site_url: str, report: Callable[[str], None]) -> None:
    if site_url:
        try:
            driver.switch_to.new_window("tab")
            report(f"Selenium created a dedicated execution tab: {site_url}")
        except Exception:
            report("Selenium reused the current browser tab because a new tab could not be created.")
        driver.get(site_url)


def _selenium_execute_step(driver: Any, site_url: str, step: Any) -> None:
    action = str(getattr(step, "action", "") or "")
    target = str(getattr(step, "target", "") or "")
    value = getattr(step, "value", None)
    if action == "open":
        driver.get(str(value or site_url))
        return
    if action == "wait":
        _selenium_wait(driver, target)
        return
    if action == "scroll":
        amount = 900
        try:
            amount = int(re.search(r"-?\d+", str(value or "")).group(0))  # type: ignore[union-attr]
        except Exception:
            pass
        driver.execute_script("window.scrollBy(0, arguments[0]);", amount)
        return
    if action == "click":
        element = _selenium_find_element(driver, step, action="click")
        _selenium_click(driver, element)
        return
    if action == "type":
        element = _selenium_find_element(driver, step, action="type")
        _selenium_click(driver, element)
        try:
            element.clear()
        except Exception:
            pass
        element.send_keys(str(value or ""))
        return
    if action == "select":
        element = _selenium_find_element(driver, step, action="select")
        _selenium_select(driver, element, str(value or target))
        return
    raise ValueError(f"Unsupported Selenium action: {action}")


def _selenium_wait(driver: Any, target: str) -> None:
    from selenium.webdriver.support.ui import WebDriverWait

    if target and _is_http_url(target):
        WebDriverWait(driver, 8).until(lambda current: target in str(current.current_url))
        return
    if target:
        WebDriverWait(driver, 8).until(lambda current: target in str(current.execute_script("return document.body ? document.body.innerText : ''")))
        return
    time.sleep(1.0)


def _selenium_find_element(driver: Any, step: Any, *, action: str) -> Any:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    selector_hint = str(getattr(step, "selector_hint", "") or "").strip()
    if selector_hint:
        candidates: list[tuple[str, str]] = []
        if selector_hint.startswith("css="):
            candidates.append((By.CSS_SELECTOR, selector_hint[4:]))
        elif selector_hint.startswith("xpath="):
            candidates.append((By.XPATH, selector_hint[6:]))
        else:
            candidates.append((By.CSS_SELECTOR, selector_hint))
        for by, selector in candidates:
            try:
                return WebDriverWait(driver, 4).until(lambda current: current.find_element(by, selector))
            except Exception:
                continue

    target = str(getattr(step, "target", "") or "")
    element = driver.execute_script(
        """
        const target = (arguments[0] || '').normalize('NFKC').toLowerCase().replace(/[\\s\\W_]+/g, '');
        const action = arguments[1];
        const selectors = {
          click: 'a,button,[role="button"],[role="link"],[role="option"],[role="menuitem"],li,[onclick],[tabindex]:not([tabindex="-1"])',
          type: 'input:not([type="hidden"]),textarea,[contenteditable="true"],[role="textbox"],[placeholder]',
          select: 'select,[role="combobox"],[aria-haspopup="listbox"],[aria-expanded],button,input'
        };
        const selector = selectors[action] || selectors.click;
        const isVisible = (node) => {
          const rect = node.getBoundingClientRect();
          const style = window.getComputedStyle(node);
          return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        };
        const normalize = (value) => (value || '').normalize('NFKC').toLowerCase().replace(/[\\s\\W_]+/g, '');
        const nodes = Array.from(document.querySelectorAll(selector)).filter(isVisible);
        const scored = nodes.map((node) => {
          const text = [
            node.innerText,
            node.textContent,
            node.getAttribute('aria-label'),
            node.getAttribute('title'),
            node.getAttribute('placeholder'),
            node.getAttribute('name'),
            node.getAttribute('id')
          ].filter(Boolean).join(' ');
          const normalized = normalize(text);
          let score = 0;
          if (target && normalized === target) score = 120;
          else if (target && normalized.includes(target)) score = 90;
          else if (target && target.includes(normalized) && normalized.length > 1) score = 55;
          if (action === 'type' && ['input', 'textarea'].includes(node.tagName.toLowerCase())) score += 20;
          if (action === 'select' && node.tagName.toLowerCase() === 'select') score += 30;
          return { node, score, y: node.getBoundingClientRect().y };
        }).filter((item) => item.score > 0);
        scored.sort((a, b) => (b.score - a.score) || (a.y - b.y));
        const best = scored[0];
        if (!best) return null;
        best.node.scrollIntoView({ block: 'center', inline: 'nearest' });
        return best.node;
        """,
        target,
        action,
    )
    if element is None:
        raise ValueError(f"Selenium could not find target for {action}: {target}")
    return element


def _selenium_click(driver: Any, element: Any) -> None:
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def _selenium_select(driver: Any, element: Any, desired: str) -> None:
    from selenium.webdriver.support.ui import Select

    tag_name = str(getattr(element, "tag_name", "") or "").lower()
    if tag_name == "select":
        select = Select(element)
        try:
            select.select_by_visible_text(desired)
            return
        except Exception:
            select.select_by_value(desired)
            return

    _selenium_click(driver, element)
    option = driver.execute_script(
        """
        const desired = (arguments[0] || '').normalize('NFKC').toLowerCase().replace(/[\\s\\W_]+/g, '');
        const selector = [
          '[role="option"]',
          '[role="menuitem"]',
          '.el-select-dropdown__item',
          '.el-cascader-node',
          '.ant-select-item-option',
          '.ant-cascader-menu-item',
          'li',
          'option'
        ].join(',');
        const normalize = (value) => (value || '').normalize('NFKC').toLowerCase().replace(/[\\s\\W_]+/g, '');
        const isVisible = (node) => {
          const rect = node.getBoundingClientRect();
          const style = window.getComputedStyle(node);
          return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        };
        const candidates = Array.from(document.querySelectorAll(selector)).filter(isVisible).map((node) => {
          const text = node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('title') || '';
          const normalized = normalize(text);
          let score = 0;
          if (normalized === desired) score = 120;
          else if (normalized.includes(desired) || desired.includes(normalized)) score = 80;
          return { node, score };
        }).filter((item) => item.score > 0);
        candidates.sort((a, b) => b.score - a.score);
        const best = candidates[0];
        if (!best) return null;
        best.node.scrollIntoView({ block: 'center', inline: 'nearest' });
        return best.node;
        """,
        desired,
    )
    if option is None:
        raise ValueError(f"Selenium could not find dropdown option: {desired}")
    _selenium_click(driver, option)


def _browser_use_vision_mode_from_env() -> bool | str:
    raw = os.getenv("BROWSER_USE_VISION_MODE")
    value = (raw if raw is not None else BROWSER_USE_VISION_MODE_DEFAULT).strip().lower()
    if value in {"0", "false", "no", "off", "none", "disabled"}:
        return False
    if value in {"1", "true", "yes", "on", "always", "force"}:
        return True
    return "auto"


def _browser_use_visual_retry_enabled() -> bool:
    return _env_bool("BROWSER_USE_VISUAL_RETRY", BROWSER_USE_VISUAL_RETRY_DEFAULT)


def _browser_use_vision_mode_label(mode: bool | str) -> str:
    if mode is True:
        return "always-on screenshots"
    if mode == "auto":
        return "screenshot-on-demand"
    return "off"


def _browser_use_should_prefer_vision_llm(mode: bool | str) -> bool:
    return mode is True or mode == "auto"


def _browser_use_attempt_plan(payload: dict[str, Any], plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    initial_mode = _browser_use_vision_mode_from_env()
    attempts = [
        {
            "name": "dom_text_with_visual_tool",
            "vision_mode": initial_mode,
            "prefer_vision_llm": _browser_use_should_prefer_vision_llm(initial_mode),
            "reason": "DOM/text execution with screenshot available on demand.",
        }
    ]
    if _browser_use_visual_retry_enabled() and initial_mode is not True:
        attempts.append(
            {
                "name": "forced_visual_retry",
                "vision_mode": True,
                "prefer_vision_llm": True,
                "reason": "Previous Browser Use pass did not satisfy rule validation; retry with screenshots every step.",
            }
        )
    return attempts


async def _run_browser_use_agent(
    *,
    task_text: str,
    cdp_url: str,
    config: Any,
    plan: dict[str, Any] | None = None,
    start_url: str = "about:blank",
    downloads_path: Path | str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    should_stop_callback: Callable[[], bool] | None = None,
    fast_mode: bool = True,
    max_steps: int = 14,
    max_actions_per_step: int = 8,
    llm_timeout: int = 45,
    step_timeout: int = 60,
    wants_file_delivery: bool = False,
    filename_hint: str = "eyeclaw-task-result.pdf",
    use_vision: bool | str = "auto",
    prefer_vision_llm: bool = False,
) -> list[str]:
    _ensure_browser_use_local_config_dirs()
    screenshot_timeout = _env_float("BROWSER_USE_SCREENSHOT_TIMEOUT", 2.0 if fast_mode else 8.0, minimum=0.5, maximum=30.0)
    os.environ["TIMEOUT_ScreenshotEvent"] = str(screenshot_timeout)
    from browser_use import Agent, Browser

    resolved_downloads_path = Path(downloads_path or "artifacts/downloads").resolve()
    resolved_downloads_path.mkdir(parents=True, exist_ok=True)
    llm = _build_browser_use_llm(config, fast_mode=fast_mode, llm_timeout=llm_timeout, prefer_vision=prefer_vision_llm)
    wait_between_actions = _env_float("BROWSER_USE_WAIT_BETWEEN_ACTIONS", 0.03 if fast_mode else 0.1, minimum=0.0, maximum=2.0)
    max_iframes = _env_int("BROWSER_USE_MAX_IFRAMES", 5 if fast_mode else 10, minimum=1, maximum=100)
    max_iframe_depth = _env_int("BROWSER_USE_MAX_IFRAME_DEPTH", 1 if fast_mode else 2, minimum=1, maximum=5)
    paint_order_filtering = _env_bool("BROWSER_USE_PAINT_ORDER_FILTERING", False if fast_mode else True)
    browser_session = Browser(
        cdp_url=cdp_url or None,
        channel="msedge",
        downloads_path=str(resolved_downloads_path),
        accept_downloads=True,
        traces_dir=str(Path("artifacts/browser_use_traces").resolve()),
        prohibited_domains=["127.0.0.1:8018", "localhost:8018", "127.0.0.1:8021", "localhost:8021"],
        keep_alive=True,
        enable_default_extensions=False,
        captcha_solver=False,
        chromium_sandbox=False,
        no_viewport=True,
        minimum_wait_page_load_time=0.05 if fast_mode else 0.25,
        wait_for_network_idle_page_load_time=0.15 if fast_mode else 0.5,
        wait_between_actions=wait_between_actions,
        highlight_elements=not fast_mode,
        dom_highlight_elements=False,
        paint_order_filtering=paint_order_filtering,
        cross_origin_iframes=False if fast_mode else True,
        max_iframes=max_iframes,
        max_iframe_depth=max_iframe_depth,
        auto_download_pdfs=True,
    )
    await _prepare_browser_use_execution_page(browser_session, start_url, progress_callback=progress_callback)

    step_started_at = time.monotonic()

    def on_new_step(browser_state_summary: Any, agent_output: Any, step_number: int) -> None:
        nonlocal step_started_at
        if progress_callback is None:
            return
        elapsed = max(0.0, time.monotonic() - step_started_at)
        progress_callback(f"Browser Use agent step {step_number} completed in {elapsed:.1f}s.")
        step_started_at = time.monotonic()

    async def should_stop() -> bool:
        return bool(should_stop_callback and should_stop_callback())

    agent = Agent(
        task=task_text,
        llm=llm,
        browser=browser_session,
        register_new_step_callback=on_new_step,
        register_should_stop_callback=should_stop if should_stop_callback is not None else None,
        use_vision=use_vision,
        max_actions_per_step=max_actions_per_step,
        max_failures=2 if fast_mode else 3,
        llm_timeout=llm_timeout,
        step_timeout=step_timeout,
        use_judge=False,
        flash_mode=True,
        use_thinking=False if fast_mode else True,
        enable_planning=False,
        planning_exploration_limit=0,
        enable_signal_handler=False,
        max_history_items=6 if fast_mode else 10,
        max_clickable_elements_length=12000 if fast_mode else 25000,
        include_attributes=[
            "id",
            "name",
            "class",
            "title",
            "aria-label",
            "aria-controls",
            "aria-expanded",
            "aria-haspopup",
            "placeholder",
            "href",
            "role",
            "type",
            "value",
            "data-value",
        ],
        directly_open_url=False,
        vision_detail_level="low",
        llm_screenshot_size=(1024, 640) if use_vision is not False else None,
        available_file_paths=[],
        file_system_path=str(resolved_downloads_path),
        display_files_in_done_text=True,
        source="eyeclaw",
    )

    if progress_callback is not None:
        progress_callback("Browser Use agent started.")
    try:
        history = await agent.run(max_steps=max_steps)
        logs = _summarize_browser_use_history(history)
        for attachment_path in _copy_browser_use_attachments(history, resolved_downloads_path):
            logs.append(f"downloaded_file: {attachment_path}")
        try:
            final_url = await browser_session.get_current_page_url()
        except Exception:
            final_url = ""
        if final_url and _is_http_url(final_url) and not is_eyeclaw_console_url(final_url):
            logs.append(f"final_url: {final_url}")
        for downloaded_file in getattr(browser_session, "downloaded_files", []) or []:
            logs.append(f"downloaded_file: {downloaded_file}")
        logs.extend(
            await _maybe_save_browser_use_preview_pdf(
                browser_session=browser_session,
                logs=logs,
                final_url=final_url,
                downloads_path=resolved_downloads_path,
                filename_hint=filename_hint,
                wants_file_delivery=wants_file_delivery,
            )
        )
        return logs
    finally:
        try:
            await browser_session.close()
        except Exception:
            pass


async def _maybe_save_browser_use_preview_pdf(
    *,
    browser_session: Any,
    logs: list[str],
    final_url: str,
    downloads_path: Path,
    filename_hint: str,
    wants_file_delivery: bool,
) -> list[str]:
    if not wants_file_delivery or _browser_use_has_file_deliverable(logs):
        return []
    if not _browser_use_reached_document_preview(logs, final_url):
        return []
    try:
        saved_path = await _save_browser_session_page_as_pdf(
            browser_session,
            downloads_path=downloads_path,
            filename_hint=filename_hint,
        )
    except Exception as exc:
        return [f"Browser Use PDF fallback failed: {_compact_error_text(exc)}"]
    return [
        f"Browser Use PDF fallback saved current preview page: {saved_path}",
        f"downloaded_file: {saved_path}",
    ]


def _browser_use_reached_document_preview(logs: list[str], final_url: str) -> bool:
    return _looks_like_preview_completion(final_url)


async def _save_browser_session_page_as_pdf(
    browser_session: Any,
    *,
    downloads_path: Path,
    filename_hint: str,
) -> str:
    downloads_path.mkdir(parents=True, exist_ok=True)
    target_path = _unique_path(downloads_path / _safe_artifact_filename(filename_hint, suffix=".pdf"))
    cdp_session = await browser_session.get_or_create_cdp_session(focus=True)
    result = await asyncio.wait_for(
        cdp_session.cdp_client.send.Page.printToPDF(
            params={
                "printBackground": True,
                "landscape": False,
                "scale": 1.0,
                "paperWidth": 8.27,
                "paperHeight": 11.69,
                "preferCSSPageSize": True,
            },
            session_id=cdp_session.session_id,
        ),
        timeout=30.0,
    )
    pdf_data = result.get("data") if isinstance(result, dict) else None
    if not pdf_data:
        raise RuntimeError("CDP Page.printToPDF returned no PDF data.")
    target_path.write_bytes(base64.b64decode(pdf_data))
    return str(target_path.resolve())


async def _prepare_browser_use_execution_page(
    browser_session: Any,
    start_url: str,
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    await browser_session.start()
    execution_url = start_url if start_url and start_url != "about:blank" else "about:blank"
    page = await browser_session.get_current_page()
    created_page = False
    dedicated_start_page = execution_url != "about:blank"
    current_url = await _browser_use_page_url(page) if page is not None else ""
    if dedicated_start_page:
        page = await browser_session.new_page(execution_url)
        created_page = True
    elif page is None or is_eyeclaw_console_url(current_url):
        page = await browser_session.new_page(execution_url)
        created_page = True

    target_info = await page.get_target_info()
    target_id = target_info.get("targetId") or getattr(page, "_target_id", None)
    if not target_id:
        raise RuntimeError("Browser Use could not determine the execution page target id.")

    try:
        from browser_use.browser.events import SwitchTabEvent

        switch_event = browser_session.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
        await switch_event
        await switch_event.event_result(raise_if_any=True, raise_if_none=False)
    except Exception:
        cdp_session = await browser_session.get_or_create_cdp_session(target_id=target_id, focus=True)
        try:
            await cdp_session.cdp_client.send.Target.activateTarget(params={"targetId": target_id})
        except Exception:
            pass

    if progress_callback is not None:
        if dedicated_start_page:
            progress_callback(f"Browser Use created a dedicated execution page: {execution_url}")
        elif created_page and is_eyeclaw_console_url(current_url):
            progress_callback(f"Browser Use created a separate execution page to keep EyeClaw console open: {execution_url}")
        elif created_page:
            progress_callback(f"Browser Use created an execution page because no reusable page was available: {execution_url}")
        else:
            progress_callback("Browser Use reused current page.")
    return target_id


async def _browser_use_page_url(page: Any) -> str:
    if page is None:
        return ""
    get_url = getattr(page, "get_url", None)
    if callable(get_url):
        try:
            return str(await get_url())
        except Exception:
            pass
    get_target_info = getattr(page, "get_target_info", None)
    if callable(get_target_info):
        try:
            target_info = await get_target_info()
            return str(target_info.get("url") or "")
        except Exception:
            return ""
    return ""


def _ensure_browser_use_local_config_dirs() -> None:
    config_dir = Path(".browser/browser-use-config").resolve()
    profiles_dir = Path(".browser/browser-use-profiles").resolve()
    config_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    os.environ["BROWSER_USE_CONFIG_DIR"] = str(config_dir)
    os.environ["BROWSER_USE_PROFILES_DIR"] = str(profiles_dir)
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    os.environ.setdefault("BROWSER_USE_VERSION_CHECK", "false")
    os.environ.setdefault("BROWSER_USE_CDP_TIMEOUT_S", "12")
    os.environ.setdefault("BROWSER_USE_ACTION_TIMEOUT_S", "30")
    os.environ.setdefault("TIMEOUT_BrowserStateRequestEvent", "18")
    os.environ.setdefault("TIMEOUT_ScreenshotEvent", "8")


def _summarize_browser_use_history(history: Any) -> list[str]:
    logs = ["Browser Use result summary:"]
    for label, method_name in [
        ("final_result", "final_result"),
        ("is_done", "is_done"),
        ("is_successful", "is_successful"),
        ("urls", "urls"),
        ("action_names", "action_names"),
        ("errors", "errors"),
    ]:
        method = getattr(history, method_name, None)
        if not callable(method):
            continue
        try:
            value = method()
        except Exception as exc:
            value = f"<unavailable: {exc}>"
        logs.append(f"{label}: {value}")
    if len(logs) == 1:
        logs.append(str(history))
    token_usage = _browser_use_token_usage_payload(history)
    if token_usage is not None:
        logs.append("token_usage: " + json.dumps(token_usage, ensure_ascii=False, separators=(",", ":")))
    return logs


def _browser_use_token_usage_payload(history: Any) -> dict[str, Any] | None:
    usage = getattr(history, "usage", None)
    if usage is None:
        return None
    dump = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    by_model: dict[str, dict[str, Any]] = {}
    raw_by_model = dump.get("by_model") if isinstance(dump, dict) else None
    if isinstance(raw_by_model, dict):
        for model, stats in raw_by_model.items():
            if hasattr(stats, "model_dump"):
                stats = stats.model_dump()
            if not isinstance(stats, dict):
                continue
            by_model[str(model)] = {
                "model": str(stats.get("model") or model),
                "prompt_tokens": int(stats.get("prompt_tokens") or 0),
                "completion_tokens": int(stats.get("completion_tokens") or 0),
                "total_tokens": int(stats.get("total_tokens") or 0),
                "cost": float(stats.get("cost") or 0.0),
                "invocations": int(stats.get("invocations") or 0),
            }
    return {
        "source": "browser_use",
        "prompt_tokens": int(dump.get("total_prompt_tokens") or 0),
        "completion_tokens": int(dump.get("total_completion_tokens") or 0),
        "total_tokens": int(dump.get("total_tokens") or 0),
        "prompt_cached_tokens": int(dump.get("total_prompt_cached_tokens") or 0),
        "entry_count": int(dump.get("entry_count") or 0),
        "cost": float(dump.get("total_cost") or 0.0),
        "by_model": by_model,
    }


def _copy_browser_use_attachments(history: Any, downloads_path: Path) -> list[str]:
    downloads_path.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    seen: set[str] = set()
    for source_path in _browser_use_attachment_paths(history):
        source = Path(source_path)
        try:
            resolved_source = source.resolve()
        except OSError:
            continue
        if str(resolved_source) in seen or not resolved_source.exists() or not resolved_source.is_file():
            continue
        seen.add(str(resolved_source))
        try:
            resolved_source.relative_to(downloads_path.resolve())
            copied.append(str(resolved_source))
            continue
        except ValueError:
            pass

        target = _unique_path(downloads_path / _safe_artifact_filename(resolved_source.name))
        try:
            if resolved_source != target.resolve():
                shutil.copy2(resolved_source, target)
            copied.append(str(target.resolve()))
        except OSError:
            continue
    return copied


def _browser_use_attachment_paths(history: Any) -> list[str]:
    paths: list[str] = []
    action_results = getattr(history, "action_results", None)
    if not callable(action_results):
        return paths
    try:
        results = action_results()
    except Exception:
        return paths
    for result in results or []:
        attachments = getattr(result, "attachments", None) or []
        for item in attachments:
            text = str(item or "").strip()
            if text:
                paths.append(text)
    return paths


def _browser_use_result_needs_manual_checkpoint(logs: list[str]) -> bool:
    text = "\n".join(logs).lower()
    return any(
        marker in text
        for marker in [
            "manual checkpoint",
            "captcha",
            "qr code",
            "scan",
            "login required",
            "验证码",
            "扫码",
            "登录",
            "人工",
        ]
    )


def _browser_use_result_value(logs: list[str], key: str) -> str:
    prefix = f"{key}:"
    for line in logs:
        text = str(line).strip()
        if text.lower().startswith(prefix.lower()):
            return text[len(prefix):].strip()
    return ""


def _browser_use_result_indicates_incomplete(logs: list[str]) -> bool:
    if _browser_use_has_file_deliverable(logs):
        return False

    done = _browser_use_result_value(logs, "is_done").lower()
    successful = _browser_use_result_value(logs, "is_successful").lower()
    final_result = _browser_use_result_value(logs, "final_result").lower()
    errors = _browser_use_result_value(logs, "errors").lower()

    if done in {"false", "none", "null"}:
        return True
    if successful in {"false"}:
        return True
    has_errors = errors and errors not in {"[]", "none", "null"}
    if final_result in {"none", "null", ""} and has_errors:
        return True
    if final_result in {"none", "null", ""} and any(
        marker in errors
        for marker in [
            "llm call timed out",
            "validation error",
            "json_invalid",
            "max steps",
        ]
    ):
        return True
    return False


def _browser_use_result_missing_required_completion(logs: list[str], plan: dict[str, Any] | None) -> str:
    if not _plan_requires_preview_completion(plan):
        return ""
    if _browser_use_has_file_deliverable(logs):
        return ""

    logged_urls = _browser_use_logged_urls(logs)
    if any(_looks_like_preview_completion(url) for url in logged_urls):
        return ""

    final_result = _browser_use_result_value(logs, "final_result").lower()
    modal_preview_markers = [
        "preview page opened",
        "opened preview",
        "pdf viewer",
        "预览页",
        "预览页面",
        "pdf预览",
    ]
    if any(marker in final_result for marker in modal_preview_markers):
        return ""

    return (
        "Browser Use stopped before completing the required preview step. "
        "The saved skill includes a `预览` action, but the final browser URLs did not reach a preview/PDF page."
    )


def _plan_requires_preview_completion(plan: dict[str, Any] | None) -> bool:
    if not isinstance(plan, dict):
        return False
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return False
    for step in steps:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action") or "").strip().lower()
        if action not in {"click", "open", "wait", "operate"}:
            continue
        step_text = " ".join(
            str(step.get(key) or "")
            for key in ("target", "value", "notes", "selector_hint")
        ).lower()
        if any(marker in step_text for marker in ("预览", "preview", "previewpdf")):
            return True
    return False


def _browser_use_logged_urls(logs: list[str]) -> list[str]:
    urls: list[str] = []
    for line in logs:
        urls.extend(re.findall(r"https?://[^\s<>)\]\"']+", str(line)))
    return dedupe_preserving_order([url.rstrip(".,;:") for url in urls])


def _looks_like_preview_completion(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in ("preview", "previewpdf", "pdf"))


def _browser_use_has_file_deliverable(logs: list[str]) -> bool:
    for line in logs:
        text = str(line).strip()
        if not text.lower().startswith("downloaded_file:"):
            continue
        raw_path = text.split(":", 1)[1].strip()
        if raw_path and Path(raw_path).is_file():
            return True
    return False


def _browser_use_failure_message(logs: list[str]) -> str:
    final_result = _browser_use_result_value(logs, "final_result")
    errors = _browser_use_result_value(logs, "errors")
    combined = f"{final_result}\n{errors}".lower()
    if "thinking mode does not support this tool_choice" in combined:
        return (
            "Browser Use model is incompatible with DeepSeek thinking mode: "
            "thinking mode rejects tool_choice. EyeClaw disables DeepSeek native "
            "tool forcing by default now; restart the service or set "
            "BROWSER_USE_LLM to a GLM/OpenAI-compatible execution model."
        )
    if final_result and final_result.lower() not in {"none", "null"}:
        return f"Browser Use 未完成任务：{final_result[:260]}"
    if errors and errors.lower() not in {"[]", "none", "null"}:
        return f"Browser Use 执行失败：{errors[:260]}"
    return "Browser Use did not finish successfully. Check the task card events for timeout, JSON output, or page-state errors."


def scheduler_run_autoglm_live_workflow(payload: dict) -> list[str]:
    server = _load_mcporter_server_config()
    mcporter_path = Path(server["command"]).resolve().parent.parent / "dependency" / "mcporter.exe"
    if not mcporter_path.exists():
        raise RuntimeError(f"mcporter.exe not found: {mcporter_path}")

    plan = payload.get("plan") if isinstance(payload, dict) else None
    task_text = _sanitize_autoglm_task_text(
        _compose_autoglm_task(str(payload.get("user_request") or ""), plan if isinstance(plan, dict) else None)
    )
    start_url = _resolve_autoglm_start_url(plan if isinstance(plan, dict) else None)

    command = [
        str(mcporter_path),
        "call",
        AUTOGLM_BROWSER_AGENT_NAME,
        "browser_subagent",
        f'task={task_text}',
        f"start_url={start_url}",
        "--timeout",
        "7200000",
    ]
    completed = subprocess.run(
        command,
        cwd=str(Path.cwd()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=7200,
        check=False,
    )

    logs: list[str] = [
        f"AutoGLM task start_url: {start_url}",
        f"AutoGLM task: {task_text}",
    ]
    stdout_text = (completed.stdout or "").strip()
    stderr_text = (completed.stderr or "").strip()
    if stdout_text:
        logs.append(stdout_text)
    if stderr_text:
        logs.append(f"stderr: {stderr_text}")
    if completed.returncode != 0:
        raise RuntimeError(stderr_text or stdout_text or f"AutoGLM execution failed with exit code {completed.returncode}")
    return logs


register_task_handler("eia_live_workflow", scheduler_run_eia_live_workflow)
register_task_handler(SMART_ROUTER_TASK_TYPE, scheduler_run_smart_router_live_workflow)
register_task_handler(BENCHMARK_TASK_TYPE, scheduler_run_execution_adapter_benchmark)
register_task_handler(LEGACY_REPLAY_TASK_TYPE, scheduler_run_generic_live_workflow)
register_task_handler(HYBRID_REPLAY_TASK_TYPE, scheduler_run_playwright_browser_use_live_workflow)
register_task_handler(SELENIUM_TASK_TYPE, scheduler_run_selenium_live_workflow)
register_task_handler(PRIMARY_EXECUTION_TASK_TYPE, scheduler_run_browser_use_live_workflow)
register_task_handler("autoglm_live_workflow", scheduler_run_autoglm_live_workflow)


async def homepage(request: Request) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


async def app_homepage(request: Request) -> HTMLResponse:
    return HTMLResponse(APP_HTML.read_text(encoding="utf-8"))


def _model_batch_count(frame_count: int) -> int:
    normalized_frame_count = max(0, int(frame_count))
    if normalized_frame_count == 0:
        return 0
    return (normalized_frame_count + DEFAULT_FRAME_BATCH_SIZE - 1) // DEFAULT_FRAME_BATCH_SIZE


def _model_input_summary(frame_count: int, label: str) -> str:
    if frame_count <= 0:
        return f"实际送入模型：0 张{label}。"
    return f"实际送入模型：{frame_count} 张{label}，分 {_model_batch_count(frame_count)} 批。"


async def upload_video(request: Request) -> JSONResponse:
    form = await request.form()
    uploaded = form.get("video")
    if not isinstance(uploaded, UploadFile):
        return JSONResponse({"error": "Missing uploaded video file."}, status_code=400)

    target_path = save_uploaded_video(uploaded.filename, uploaded.file)
    metadata = get_video_metadata(target_path)
    response = UploadResponse(
        video_path=str(target_path),
        duration_seconds=metadata.duration_seconds,
        fps=metadata.fps,
        width=metadata.width,
        height=metadata.height,
    )
    return JSONResponse(asdict(response))


def _build_video_analysis_result(
    payload: dict,
    *,
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    status = load_config_status()
    if not status.is_ready or status.config is None:
        raise ValueError("Configuration is incomplete.")

    def report(percent: int, stage: str) -> None:
        if progress_callback is not None:
            progress_callback(percent, stage)

    report(6, "正在检查配置...")
    video_path = payload.get("video_path") or ""
    user_request = payload.get("user_request") or ""
    start_second = float(payload.get("start_second", 0.0))
    end_second = payload.get("end_second")
    max_frames = int(payload.get("max_frames", 12))
    listener_session_id = payload.get("listener_session_id") or BROWSER_EVENT_STORE.latest_session_id()

    if not listener_session_id:
        raise ValueError("Listener-guided analysis requires a listener session. Start the browser listener first.")

    report(18, "正在确认录屏和监听会话...")
    recording = BROWSER_EVENT_STORE.get_session_recording(listener_session_id)
    source = Path(video_path) if video_path else (Path(recording.recording_path) if recording else None)
    if source is None:
        raise ValueError("No video path was provided and the listener session has no recording yet.")
    if not source.exists():
        raise FileNotFoundError(f"Video file not found: {source}")

    report(26, "正在读取视频元数据...")
    metadata = get_video_metadata(source)
    actual_end = float(end_second) if end_second is not None else metadata.duration_seconds

    report(34, "正在用监听时间轴规划关键帧...")
    listener_events = BROWSER_EVENT_STORE.session_events(listener_session_id)
    if not listener_events:
        raise ValueError(
            "这个录制视频缺少可恢复的监听时间轴，通常是因为它来自服务重启前的旧录屏，"
            "或者对应监听记录已经被清空。请重新开始一次演示录制，或选择事件数大于 0 的录屏。"
        )
    guided_frames = plan_listener_guided_frames(
        listener_events,
        start_second=start_second,
        end_second=actual_end,
        max_frames=max_frames,
        recording=recording,
    )
    if not guided_frames:
        raise ValueError(
            f"会话 {listener_session_id} 没有生成可用的关键时间点。请重新录制一次，或先用“分析演示过程”确认监听事件已成功采集。"
        )

    report(48, "已根据监听时间轴锁定关键时间点...")
    timestamps = [frame.timestamp_second for frame in guided_frames]
    report(56, "正在抽取关键帧...")
    frame_paths = extract_frames(source, timestamps, job_id=uuid4().hex[:8])
    frame_hints = [frame.hint for frame in guided_frames[: len(frame_paths)]]
    site_url = choose_site_url(listener_events, fallback_site_url=status.config.target_site_url)
    frame_count = len(frame_paths)
    model_input_summary = _model_input_summary(frame_count, "关键帧")

    report(66, f"关键帧已准备，{model_input_summary}")

    def report_model_progress(phase: str, current: int, total: int) -> None:
        if phase == "vision_started":
            report(70, f"正在调用多模态模型分析关键帧，{model_input_summary}")
        elif phase == "vision_batch":
            batch_total = max(total, 1)
            percent = 70 + int(14 * min(current, batch_total) / batch_total)
            report(percent, f"多模态模型分析中：第 {current}/{batch_total} 批")
        elif phase == "vision_completed":
            report(86, "多模态识别完成，正在准备步骤整理...")
        elif phase == "normalization_started":
            report(90, "正在把识别结果整理成操作步骤...")
        elif phase == "normalization_completed":
            report(94, "正在合并监听线索与操作计划...")

    result = build_replay_plan(
        frame_paths=frame_paths,
        config=status.config,
        user_request=user_request,
        site_url=site_url,
        frame_hints=frame_hints,
        progress_callback=report_model_progress,
    )
    plan_payload = _enrich_plan_from_listener_events(
        result.replay_bundle.plan.model_dump(),
        listener_events,
    )
    suggested_skill_name = _build_skill_title(
        steps=plan_payload.get("steps") or [],
        site_url=site_url,
        user_request=user_request,
        listener_events=listener_events,
    )
    suggested_skill_description = _build_skill_description(
        steps=plan_payload.get("steps") or [],
        source_type="video_analysis",
        user_request=user_request,
        listener_events=listener_events,
    )

    report(100, "已完成")
    return {
        "video_path": str(source),
        "listener_guided": True,
        "listener_session_id": listener_session_id,
        "frame_count": len(frame_paths),
        "frame_paths": [str(path) for path in frame_paths],
        "timestamps": timestamps,
        "frame_hints": frame_hints,
        "sop": result.sop,
        "plan": plan_payload,
        "suggested_skill_name": suggested_skill_name,
        "suggested_skill_description": suggested_skill_description,
        "assumptions": result.replay_bundle.assumptions,
        "raw_notes": result.raw_glm_output.get("uncertainties", []),
    }


async def analyze_video(request: Request) -> JSONResponse:
    payload = await request.json()
    try:
        result = await asyncio.to_thread(_build_video_analysis_result, payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result)


def _build_listener_analysis_result(
    payload: ListenerAnalysisRequest,
    *,
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    status = load_config_status()
    if not status.is_ready or status.config is None:
        raise ValueError("Configuration is incomplete.")

    def report(percent: int, stage: str) -> None:
        if progress_callback is not None:
            progress_callback(percent, stage)

    report(10, "正在读取监听会话...")
    session_id, candidate_events = BROWSER_EVENT_STORE.select_analysis_candidates(
        session_id=payload.session_id,
        limit=payload.max_events,
    )
    if not session_id or not candidate_events:
        raise LookupError("No listener screenshots are available for analysis yet.")
    session_events = BROWSER_EVENT_STORE.session_events(session_id)

    report(38, "正在整理截图候选...")
    frame_paths = [Path(event.screenshot_path) for event in candidate_events if event.screenshot_path]
    frame_hints = [summarize_browser_event(event) for event in candidate_events]
    site_url = choose_site_url(candidate_events, fallback_site_url=status.config.target_site_url)
    frame_count = len(frame_paths)
    model_input_summary = _model_input_summary(frame_count, "候选截图")

    report(62, f"候选截图已准备，{model_input_summary}")

    def report_model_progress(phase: str, current: int, total: int) -> None:
        if phase == "vision_started":
            report(68, f"正在调用多模态模型分析候选截图，{model_input_summary}")
        elif phase == "vision_batch":
            batch_total = max(total, 1)
            percent = 68 + int(18 * min(current, batch_total) / batch_total)
            report(percent, f"多模态模型分析中：第 {current}/{batch_total} 批")
        elif phase == "vision_completed":
            report(88, "多模态识别完成，正在整理步骤...")
        elif phase == "normalization_started":
            report(92, "正在把识别结果整理成操作步骤...")
        elif phase == "normalization_completed":
            report(96, "正在合并监听线索与操作计划...")

    result = build_replay_plan(
        frame_paths=frame_paths,
        config=status.config,
        user_request=payload.user_request,
        site_url=site_url,
        frame_hints=frame_hints,
        progress_callback=report_model_progress,
    )
    plan_payload = _enrich_plan_from_listener_events(result.replay_bundle.plan.model_dump(), session_events)
    suggested_skill_name = _build_skill_title(
        steps=plan_payload.get("steps") or [],
        site_url=site_url,
        user_request=payload.user_request,
        listener_events=session_events,
    )
    suggested_skill_description = _build_skill_description(
        steps=plan_payload.get("steps") or [],
        source_type="listener_analysis",
        user_request=payload.user_request,
        listener_events=session_events,
    )

    report(100, "已完成")
    return {
        "session_id": session_id,
        "site_url": site_url,
        "frame_count": len(frame_paths),
        "frame_paths": [str(path) for path in frame_paths],
        "candidate_events": [
            {
                **event.model_dump(),
                "frame_hint": summarize_browser_event(event),
            }
            for event in candidate_events
        ],
        "sop": result.sop,
        "plan": plan_payload,
        "suggested_skill_name": suggested_skill_name,
        "suggested_skill_description": suggested_skill_description,
        "assumptions": result.replay_bundle.assumptions,
        "raw_notes": result.raw_glm_output.get("uncertainties", []),
    }


async def start_video_analysis_job(request: Request) -> JSONResponse:
    payload = await request.json()
    job = ANALYSIS_JOBS.create_job("video_analysis", stage="正在排队...")

    async def runner() -> None:
        def report(percent: int, stage: str) -> None:
            ANALYSIS_JOBS.update(job.id, progress_percent=percent, stage=stage)

        try:
            result = await asyncio.to_thread(_build_video_analysis_result, payload, progress_callback=report)
        except ValueError as exc:
            ANALYSIS_JOBS.update(job.id, status="failed", stage="分析失败", error=str(exc))
        except FileNotFoundError as exc:
            ANALYSIS_JOBS.update(job.id, status="failed", stage="分析失败", error=str(exc))
        except Exception as exc:
            ANALYSIS_JOBS.update(job.id, status="failed", stage="分析失败", error=str(exc))
        else:
            ANALYSIS_JOBS.update(job.id, status="completed", progress_percent=100, stage="已完成", result=result)

    asyncio.create_task(runner())
    return JSONResponse({"job_id": job.id, "status": job.status})


async def start_listener_analysis_job(request: Request) -> JSONResponse:
    try:
        payload = ListenerAnalysisRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    job = ANALYSIS_JOBS.create_job("listener_analysis", stage="正在排队...")

    async def runner() -> None:
        def report(percent: int, stage: str) -> None:
            ANALYSIS_JOBS.update(job.id, progress_percent=percent, stage=stage)

        try:
            result = await asyncio.to_thread(_build_listener_analysis_result, payload, progress_callback=report)
        except ValueError as exc:
            ANALYSIS_JOBS.update(job.id, status="failed", stage="分析失败", error=str(exc))
        except LookupError as exc:
            ANALYSIS_JOBS.update(job.id, status="failed", stage="分析失败", error=str(exc))
        except Exception as exc:
            ANALYSIS_JOBS.update(job.id, status="failed", stage="分析失败", error=str(exc))
        else:
            ANALYSIS_JOBS.update(job.id, status="completed", progress_percent=100, stage="已完成", result=result)

    asyncio.create_task(runner())
    return JSONResponse({"job_id": job.id, "status": job.status})


async def analysis_job_status(request: Request) -> JSONResponse:
    job_id = str(request.query_params.get("job_id") or "").strip()
    if not job_id:
        return JSONResponse({"error": "job_id is required."}, status_code=400)

    payload = ANALYSIS_JOBS.as_dict(job_id)
    if payload is None:
        return JSONResponse({"error": f"Analysis job not found: {job_id}"}, status_code=404)
    return JSONResponse(payload)


async def connect_live_browser(request: Request) -> JSONResponse:
    payload = await request.json()
    cdp_url = payload.get("cdp_url") or DEFAULT_CDP_URL
    data = _run_in_dedicated_thread(_connect_live_browser_sync, cdp_url)
    return JSONResponse(data)


async def run_live_workflow(request: Request) -> JSONResponse:
    try:
        payload = RunLiveWorkflowRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    data, status_code = _run_in_dedicated_thread(
        _run_live_workflow_sync,
        payload.cdp_url,
        payload.user_request,
        payload.plan,
        payload.task_type,
    )
    return JSONResponse(data, status_code=status_code)


async def start_live_workflow_job(request: Request) -> JSONResponse:
    try:
        payload = RunLiveWorkflowRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    job = ANALYSIS_JOBS.create_job("live_workflow", stage="正在连接浏览器")

    async def runner() -> None:
        def report(message: str) -> None:
            snapshot = ANALYSIS_JOBS.get(job.id)
            existing_logs = list(snapshot.result.get("logs", [])) if snapshot and snapshot.result else []
            existing_logs.append(message)
            percent, stage = infer_live_workflow_progress(existing_logs, message)
            ANALYSIS_JOBS.update(
                job.id,
                progress_percent=percent,
                stage=stage,
                result={"logs": existing_logs},
            )

        ANALYSIS_JOBS.update(job.id, progress_percent=5, stage="正在连接浏览器", result={"logs": []})
        try:
            data, status_code = _run_in_dedicated_thread(
                _run_live_workflow_sync,
                payload.cdp_url,
                payload.user_request,
                payload.plan,
                payload.task_type,
                report,
            )
        except Exception as exc:
            current = ANALYSIS_JOBS.get(job.id)
            existing_result = current.result if current and current.result else {"logs": []}
            ANALYSIS_JOBS.update(
                job.id,
                status="failed",
                stage="执行失败",
                error=str(exc),
                result=existing_result,
            )
            return

        logs = list(data.get("logs", []))
        percent, stage = infer_live_workflow_progress(logs, "正在执行流程")
        if status_code == 409:
            ANALYSIS_JOBS.update(
                job.id,
                status="manual",
                progress_percent=max(percent, 92),
                stage="等待人工处理",
                error=data.get("message"),
                result=data,
            )
            return

        ANALYSIS_JOBS.update(
            job.id,
            status="completed",
            progress_percent=100,
            stage="执行完成",
            result=data,
        )

    asyncio.create_task(runner())
    return JSONResponse({"job_id": job.id, "status": job.status})


def _compose_user_request(objective: str, skills: list[Any]) -> str:
    cleaned_objective = objective.strip()
    if not skills:
        return cleaned_objective

    preview_lines = []
    for skill in skills[:3]:
        step_preview = "；".join(
            str(step.get("target") or step.get("action") or f"步骤{index + 1}")
            for index, step in enumerate(skill.steps[:4])
        )
        preview_lines.append(f"- {skill.name}: {step_preview}")

    parts = [cleaned_objective] if cleaned_objective else []
    parts.append("请参考以下技能步骤执行任务：")
    parts.extend(preview_lines)
    return "\n".join(parts)


def _load_mcporter_server_config(server_name: str = AUTOGLM_BROWSER_AGENT_NAME) -> dict[str, Any]:
    if not MCPORTER_CONFIG_PATH.exists():
        raise RuntimeError(f"mcporter config not found: {MCPORTER_CONFIG_PATH}")
    payload = json.loads(MCPORTER_CONFIG_PATH.read_text(encoding="utf-8"))
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    if not isinstance(servers, dict) or server_name not in servers:
        raise RuntimeError(f"mcporter server not configured: {server_name}")
    server = servers[server_name]
    if not isinstance(server, dict):
        raise RuntimeError(f"Invalid mcporter server config for: {server_name}")
    return server


def _resolve_autoglm_start_url(plan: dict[str, Any] | None) -> str:
    if isinstance(plan, dict):
        site_url = str(plan.get("site_url") or "").strip()
        if site_url:
            return site_url
    return "https://www.bing.com"


def _compose_autoglm_task(user_request: str, plan: dict[str, Any] | None) -> str:
    objective = (user_request or "").strip()
    if not isinstance(plan, dict):
        return objective or "请根据当前任务目标完成网页操作。"

    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return objective or "请根据当前任务目标完成网页操作。"

    lines: list[str] = []
    if objective:
        lines.append(f"任务目标：{objective}")
    lines.append("请严格按照以下中文步骤在浏览器中执行：")
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            continue
        action = str(raw_step.get("action") or "").strip() or "操作"
        target = str(raw_step.get("target") or "").strip() or "当前页面目标"
        value = str(raw_step.get("value") or "").strip()
        notes = str(raw_step.get("notes") or "").strip()
        selector_hint = str(raw_step.get("selector_hint") or "").strip()
        step_line = f"{index}. {action}：{target}"
        if value:
            step_line += f"；值：{value}"
        if notes:
            step_line += f"；说明：{notes}"
        if selector_hint:
            step_line += f"；定位提示：{selector_hint}"
        lines.append(step_line)
    lines.append("如果页面与步骤不完全一致，请优先依据页面上最接近的真实中文文案完成操作。")
    return "\n".join(lines)


def _compose_browser_use_task(
    user_request: str,
    plan: dict[str, Any] | None,
    *,
    skill_names: list[str] | None = None,
    objective: str = "",
    preflight_attempted: bool = False,
) -> str:
    start_url = _resolve_browser_use_start_url(plan)
    raw_steps = plan.get("steps") if isinstance(plan, dict) else None
    steps = [step for step in raw_steps if isinstance(step, dict)] if isinstance(raw_steps, list) else []
    steps = (
        _compact_browser_use_steps(
            _normalize_dropdown_replay_steps(
                _repair_misordered_location_child_steps(_filter_internal_replay_steps(steps))
            )
        )
        if steps
        else []
    )
    site_host = urlparse(start_url).netloc if start_url else ""
    cleaned_objective = (objective or "").strip()
    cleaned_user_request = (user_request or "").strip()
    generated_skill_request = cleaned_user_request.startswith("请参考以下技能步骤执行任务：")
    goal = cleaned_objective or ("Complete the selected saved skill workflow." if generated_skill_request else cleaned_user_request)

    lines: list[str] = [
        "# EyeClaw Browser Use Task",
        "",
        "You are the primary browser automation executor for EyeClaw.",
        "Complete the business goal by reasoning from the current page, visible text, labels, menus, URLs, and page state.",
        "Do not mechanically replay CSS nth-of-type selectors. Treat recorded selectors only as weak historical hints.",
        "Batch safe consecutive actions in one step when the next control is already visible.",
        "",
        "## Goal",
        goal or "Complete the selected saved skill workflow.",
    ]
    if skill_names:
        lines.extend(["", "## Selected Skills", *[f"- {name}" for name in skill_names if name]])
    if cleaned_user_request and not generated_skill_request and cleaned_user_request != cleaned_objective:
        lines.extend(["", "## User Request", cleaned_user_request])

    lines.extend(
        [
            "",
            "## Start",
            f"- Start URL: {start_url}",
            f"- Target host: {site_host or '(infer from start URL)'}",
            "- EyeClaw opens a dedicated execution page for this task when a Start URL is available.",
            "- Keep the EyeClaw console and existing user pages open; operate only on the task execution page.",
            "- Do not navigate, close, or operate on an EyeClaw console page such as 127.0.0.1:8018.",
            "- Do not click EyeClaw console buttons, saved skill cards, task logs, or tutorial text.",
            "",
            "## Semantic Workflow",
        ]
    )

    if steps:
        for index, step in enumerate(steps, start=1):
            instruction = _browser_use_step_instruction(index, step)
            if instruction:
                lines.append(instruction)
    else:
        lines.append("1. Inspect the page and complete the user goal using visible page controls.")

    lines.extend(
        [
            "",
            "## Recovery Rules",
            "- If a click target is not visible, use find_text, scroll, reopen the relevant menu/dropdown, then click the closest matching visible control.",
        "- For native select/listbox/combobox controls, prefer Browser Use's dropdown_options action first, then select_dropdown with the exact returned option text/value.",
        "- If a custom dropdown or cascader option is missing, reopen the dropdown and scroll the option list itself; for province/city cascaders, choose the parent area before the child area.",
            "- If a normal click fails, try keyboard navigation with Tab, ArrowDown, Enter, or a nearby visible label/control.",
            "- If the recorded workflow includes a keyboard shortcut such as Ctrl+S, press that shortcut on the task execution page and verify the resulting page state or real downloaded/saved file.",
            "- If the current page does not match the workflow, navigate back to the Start URL and continue from the closest valid state.",
            "- Avoid repeating steps that are already visibly complete.",
            "- If login, QR code, SMS code, captcha, or human confirmation appears, stop and report that a manual checkpoint is required.",
            "- Prefer task success over exact step order when the page state already satisfies an earlier step.",
            "- If the workflow includes `预览` / preview / detail opening, click that control and verify the preview/detail page, modal, or PDF viewer is actually open; merely showing a filtered result list is incomplete.",
            "- If the goal says download, save, export, PDF, or file, click the actual download/save/export/PDF control and wait for a real browser download or saved PDF file path.",
            "- If only a file preview/detail page opens, do not treat that as a completed download; click its download button, use save_as_pdf only for an actual PDF/document preview, or report success=false with the blocking state.",
            "",
            "## Success Criteria",
            "- The requested business workflow is completed on the target website, not inside the EyeClaw console.",
            "- Important selected filters, opened items, downloads, saves, or final page state match the goal.",
            "- Required terminal actions from the semantic workflow, such as `预览`, must be completed before calling done.",
            "- When a file is requested, success requires a real downloaded/saved file path, not just reaching a preview or detail page.",
            "- If the workflow cannot be completed, clearly explain the blocking page state and whether manual action is needed.",
            "",
            "## Final Answer",
            "When finished, call done with a concise summary containing: success, final_url, selected_filters, opened_item_title, downloaded_or_saved, manual_checkpoint_required, and errors.",
        ]
    )
    if preflight_attempted:
        lines.insert(
            lines.index("## Recovery Rules"),
            "The fast preflight attempted the recorded steps but did not produce a final deliverable; continue from the current page state instead of starting over unless the page is clearly wrong.\n",
        )
    return "\n".join(lines)


def _resolve_browser_use_start_url(plan: dict[str, Any] | None) -> str:
    if isinstance(plan, dict):
        urls: list[str] = []
        site_url = str(plan.get("site_url") or "").strip()
        if site_url:
            urls.append(site_url)
        for step in plan.get("steps") or []:
            if isinstance(step, dict):
                urls.extend(_extract_http_urls_from_step(step))
        usable_urls = [url for url in urls if _is_http_url(url) and not is_eyeclaw_console_url(url)]
        business_urls = [url for url in usable_urls if not _is_search_engine_url(url)]
        if business_urls:
            return business_urls[0]
        if usable_urls:
            return usable_urls[0]
    return "https://www.bing.com"


def _compact_browser_use_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    previous_action = ""
    previous_target = ""
    for step in steps:
        action = str(step.get("action") or "").strip().lower()
        target = str(step.get("target") or "").strip()
        if action == "wait" and _is_low_value_wait_target(target) and previous_action not in {"click", "open"}:
            continue
        if action == "click" and previous_action == "click" and target and target == previous_target:
            continue
        compacted.append(step)
        previous_action = action
        previous_target = target
    return _renumber_plan_steps(compacted) if compacted else steps


def _browser_use_step_instruction(index: int, step: dict[str, Any]) -> str:
    action = str(step.get("action") or "").strip().lower() or "operate"
    target = str(step.get("target") or "").strip()
    value = str(step.get("value") or "").strip()
    notes = str(step.get("notes") or "").strip()
    semantic_target = _browser_use_semantic_target(target, notes)

    if action == "open":
        destination = value or target
        return f"{index}. Navigate to {destination}."
    if action == "wait":
        return f"{index}. Wait until the page, URL, or visible content indicates: {semantic_target or target}."
    if action == "type":
        return f"{index}. Enter `{value}` into the field related to `{semantic_target or target}`; if the field is already filled correctly, continue."
    if action == "select":
        desired = value or semantic_target or target
        return f"{index}. Select `{desired}` from the relevant dropdown/list; reopen and scroll the list if needed."
    if action == "scroll":
        direction = value or "as needed"
        return f"{index}. Scroll {direction} until the next relevant target is visible."
    if action == "press":
        shortcut = value or semantic_target or target
        return f"{index}. Press the keyboard shortcut `{shortcut}` on the current task page; if it is a save/download shortcut such as Ctrl+S, wait for a real downloaded/saved file path."
    if action == "click":
        if _looks_like_low_level_selector(target):
            context = _browser_use_context_from_notes(notes)
            return (
                f"{index}. A recorded low-level click happened here. Infer the intended visible control from context"
                f"{(': ' + context) if context else ''}; do not click by CSS selector alone."
            )
        return f"{index}. Click the visible control/text related to `{semantic_target or target}`."
    return f"{index}. Perform `{action}` on `{semantic_target or target}` using visible page semantics."


def _browser_use_semantic_target(target: str, notes: str) -> str:
    if not _looks_like_low_level_selector(target):
        return target
    quoted = re.findall(r"[“\"']([^”\"']+)[”\"']", notes)
    for item in quoted:
        cleaned = item.strip()
        if cleaned and not _looks_like_low_level_selector(cleaned):
            return cleaned
    return ""


def _browser_use_context_from_notes(notes: str) -> str:
    return _clean_title_part(notes, limit=140)


def _looks_like_low_level_selector(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    selector_markers = [">", "nth-of-type", ".el-", ".ant-", "[", "]", "#", " > ", "css=", "xpath=", ":nth"]
    return any(marker in text for marker in selector_markers)


def _sanitize_autoglm_task_text(task_text: str) -> str:
    sanitized = (task_text or "").replace('"', "'").strip()
    return sanitized or "请根据当前任务目标完成网页操作。"


def _compose_plan_from_skills(skills: list[Any]) -> dict[str, Any] | None:
    if not skills:
        return None

    merged_steps: list[dict[str, Any]] = []
    for skill in skills:
        skill_steps = _execution_steps_for_skill(skill)
        for step in skill_steps:
            normalized = dict(step)
            normalized["step_number"] = len(merged_steps) + 1
            merged_steps.append(normalized)

    if not merged_steps:
        return None

    execution_steps = _repair_misordered_location_child_steps(_filter_internal_replay_steps(merged_steps))
    return {
        "site_url": _infer_execution_site_url_from_skills(skills, merged_steps) or "about:blank",
        "steps": _renumber_plan_steps(execution_steps),
    }


def _execution_steps_for_skill(skill: Any) -> list[dict[str, Any]]:
    listener_session_id = str(getattr(skill, "listener_session_id", "") or "").strip()
    if listener_session_id:
        listener_steps = _replay_steps_from_listener_events(BROWSER_EVENT_STORE.session_events(listener_session_id))
        if len(listener_steps) >= 2:
            return listener_steps
    return [dict(step) for step in (getattr(skill, "steps", []) or []) if isinstance(step, dict)]


def _replay_steps_from_listener_events(events: list[Any]) -> list[dict[str, Any]]:
    if not events:
        return []
    ordered_events = sorted(events, key=lambda event: int(getattr(event, "client_timestamp_ms", 0) or 0))
    steps: list[dict[str, Any]] = []
    previous_signature: tuple[str, str, str, str] | None = None

    for event in ordered_events:
        event_type = str(getattr(event, "event_type", "") or "").strip().lower()
        if event_type not in {"click", "input", "change", "keyboard_shortcut"}:
            continue
        page_url = str(getattr(event, "page_url", "") or "")
        if is_eyeclaw_console_url(page_url):
            continue
        target_selector = str(getattr(event, "target_selector", "") or "").strip()
        target_text = str(getattr(event, "target_text", "") or "").strip()
        input_value = str(getattr(event, "input_value", "") or "").strip()
        target_type = str(getattr(event, "target_type", "") or "").strip().lower()
        target_tag = str(getattr(event, "target_tag", "") or "").strip().lower()
        details = getattr(event, "details", {}) or {}
        shortcut = str(details.get("shortcut") or input_value or target_text or "").strip()
        target = shortcut if event_type == "keyboard_shortcut" else target_text or target_selector
        if not target:
            continue
        if event_type in {"input", "change"} and target_type in {"checkbox", "radio"}:
            continue

        action = "click"
        value: str | None = None
        if event_type == "keyboard_shortcut":
            action = "press"
            value = shortcut
        elif event_type in {"input", "change"} and input_value:
            action = "select" if target_tag == "select" or target_type == "select" else "type"
            value = input_value
        elif event_type != "click":
            continue

        signature = (action, target, value or "", target_selector)
        if previous_signature == signature:
            continue
        previous_signature = signature

        step: dict[str, Any] = {
            "step_number": len(steps) + 1,
            "action": action,
            "target": target,
        }
        if value:
            step["value"] = value
        if target_selector:
            step["selector_hint"] = target_selector
        steps.append(step)

        wait_step = _listener_transition_wait_step(event, ordered_events)
        if wait_step is not None and not _step_list_contains_wait(steps, wait_step):
            wait_step["step_number"] = len(steps) + 1
            steps.append(wait_step)

    return _normalize_dropdown_replay_steps(steps)


def _listener_transition_wait_step(event: Any, events: list[Any]) -> dict[str, Any] | None:
    if str(getattr(event, "event_type", "") or "").strip().lower() != "click":
        return None
    transition = _find_followup_state_event(event, events)
    if transition is None:
        return None
    transition_url = str(getattr(transition, "page_url", "") or "").strip()
    transition_title = str(getattr(transition, "page_title", "") or "").strip()
    target = transition_url or transition_title
    if not target:
        return None
    return {
        "step_number": 0,
        "action": "wait",
        "target": target,
        "notes": "Wait for the browser state observed after the recorded click.",
    }


def _infer_execution_site_url_from_skills(skills: list[Any], steps: list[dict[str, Any]]) -> str | None:
    urls: list[str] = []
    for skill in skills:
        site_url = str(getattr(skill, "site_url", "") or "").strip()
        if site_url:
            urls.append(site_url)
    for step in steps:
        urls.extend(_extract_http_urls_from_step(step))

    usable_urls = [url for url in urls if _is_http_url(url) and not is_eyeclaw_console_url(url)]
    business_urls = [url for url in usable_urls if not _is_search_engine_url(url)]
    if business_urls:
        return business_urls[0]
    if usable_urls:
        return usable_urls[0]
    return urls[0] if urls else None


def _extract_http_urls_from_step(step: dict[str, Any]) -> list[str]:
    values = [
        str(step.get("target") or ""),
        str(step.get("value") or ""),
        str(step.get("notes") or ""),
    ]
    urls: list[str] = []
    for value in values:
        for match in re.findall(r"https?://[^\s'\"<>]+", value):
            cleaned = match.rstrip(".,;)]}，。；）】")
            if cleaned:
                urls.append(cleaned)
    return urls


def _is_http_url(raw_url: str) -> bool:
    return raw_url.lower().startswith(("http://", "https://"))


def _filter_internal_replay_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered = [step for step in steps if not _is_internal_replay_step(step) and not _is_noisy_replay_step(step)]
    return filtered or steps


def _is_internal_replay_step(step: dict[str, Any]) -> bool:
    target = str(step.get("target") or "")
    value = str(step.get("value") or "")
    notes = str(step.get("notes") or "")
    selector_hint = str(step.get("selector_hint") or "")
    combined = " ".join([target, value, notes, selector_hint])
    if "Eyeclaw" in combined or any(host in combined for host in ("127.0.0.1:8018", "localhost:8018")):
        return True
    internal_target_markers = (
        "\u6d4f\u89c8\u5668\u6807\u7b7e\u680f",
        "\u6807\u7b7e\u9875",
    )
    return any(marker in target for marker in internal_target_markers)


def _is_noisy_replay_step(step: dict[str, Any]) -> bool:
    if str(step.get("action") or "").strip().lower() != "click":
        return False
    target = str(step.get("target") or "").strip()
    if len(target) < 120:
        return False
    location_markers = (
        "\u7701",
        "\u5e02",
        "\u81ea\u6cbb\u533a",
        "\u7279\u522b\u884c\u653f\u533a",
    )
    marker_count = sum(target.count(marker) for marker in location_markers)
    return marker_count >= 8


def _repair_misordered_location_child_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repaired: list[dict[str, Any]] = []
    deferred_children: list[dict[str, Any]] = []

    for index, step in enumerate(steps):
        if _should_defer_location_child_step(step, steps[index + 1 :]):
            deferred_children.append(step)
            continue

        repaired.append(step)
        if deferred_children and _is_location_parent_option_step(step):
            repaired.extend(deferred_children)
            deferred_children = []

    if deferred_children:
        repaired.extend(deferred_children)
    return repaired


def _should_defer_location_child_step(step: dict[str, Any], later_steps: list[dict[str, Any]]) -> bool:
    if str(step.get("action") or "").strip().lower() != "click":
        return False
    target = str(step.get("target") or "").strip()
    notes = str(step.get("notes") or "")
    if not _is_city_like_target(target):
        return False
    if "\u5148\u5c55\u5f00\u4e0a\u7ea7\u83dc\u5355" not in notes:
        return False
    return any(_is_location_parent_option_step(later_step) for later_step in later_steps)


def _is_city_like_target(target: str) -> bool:
    return target.endswith("\u5e02") and len(target) <= 12


def _is_location_parent_option_step(step: dict[str, Any]) -> bool:
    if str(step.get("action") or "").strip().lower() != "click":
        return False
    target = str(step.get("target") or "").strip()
    parent_suffixes = (
        "\u7701",
        "\u81ea\u6cbb\u533a",
        "\u7279\u522b\u884c\u653f\u533a",
        "\u5175\u56e2",
    )
    return len(target) <= 16 and target.endswith(parent_suffixes)


def _enrich_plan_from_listener_events(plan: dict[str, Any], events: list[Any]) -> dict[str, Any]:
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return plan

    enriched_steps: list[dict[str, Any]] = []
    event_candidates = [event for event in events if getattr(event, "target_text", None) or getattr(event, "target_selector", None)]
    ordered_events = sorted(event_candidates, key=lambda event: int(getattr(event, "client_timestamp_ms", 0) or 0))

    for step in raw_steps:
        if not isinstance(step, dict):
            enriched_steps.append(step)
            continue

        updated = dict(step)
        matched_event = _match_listener_event_for_step(updated, ordered_events)
        if matched_event is not None:
            if not updated.get("selector_hint") and getattr(matched_event, "target_selector", None):
                updated["selector_hint"] = matched_event.target_selector
            if _looks_abstract_target(str(updated.get("target") or "")) and getattr(matched_event, "target_text", None):
                updated["target"] = matched_event.target_text
            after_wait_step = _build_wait_step_after_event(matched_event, events)
        else:
            after_wait_step = None
        enriched_steps.append(updated)
        if after_wait_step is not None and not _step_list_contains_wait(enriched_steps, after_wait_step):
            enriched_steps.append(after_wait_step)

    enriched_steps = _restore_missing_parent_click_steps(enriched_steps, ordered_events)
    enriched_steps = _normalize_plan_sequence(enriched_steps)

    enriched = dict(plan)
    enriched["steps"] = _renumber_plan_steps(enriched_steps)
    return enriched


def _match_listener_event_for_step(step: dict[str, Any], events: list[Any]) -> Any | None:
    action = str(step.get("action") or "").strip().lower()
    target = str(step.get("target") or "").strip().lower()
    if not action or not events:
        return None

    action_aliases = {
        "click": {"click"},
        "type": {"input", "change"},
        "select": {"change", "input", "click"},
        "press": {"keyboard_shortcut"},
        "wait": {"page_loaded", "navigation", "history", "tab_updated"},
        "open": {"navigation", "history", "tab_updated", "page_loaded"},
    }
    accepted_actions = action_aliases.get(action, {action})

    def score(event: Any) -> tuple[int, int]:
        event_action = str(getattr(event, "event_type", "") or "").strip().lower()
        event_target = str(getattr(event, "target_text", "") or "").strip().lower()
        target_related = _target_text_related(target, event_target)
        score_value = 0
        if event_action in accepted_actions:
            score_value += 3
        if target and event_target:
            if target == event_target:
                score_value += 5
            elif target in event_target or event_target in target:
                score_value += 3
            elif target_related:
                score_value += 3
            else:
                score_value -= 4
        if getattr(event, "target_selector", None):
            score_value += 1
        transition = _find_followup_state_event(event, events)
        if transition is not None and target:
            transition_url = str(getattr(transition, "page_url", "") or "")
            transition_title = str(getattr(transition, "page_title", "") or "")
            transition_text = f"{transition_url} {transition_title}".lower()
            decoded_url = unquote(transition_url).lower()
            encoded_target = quote(target).lower()
            if target in transition_text or target in decoded_url or encoded_target in transition_text:
                score_value += 4
        if target and event_target and _looks_abstract_target(target) and not target_related:
            score_value -= 4
        timestamp = int(getattr(event, "client_timestamp_ms", 0) or 0)
        return (score_value, -timestamp)

    ranked = sorted(events, key=score, reverse=True)
    best = ranked[0] if ranked else None
    if best is None:
        return None
    if score(best)[0] <= 0:
        return None
    return best


def _looks_abstract_target(target: str) -> bool:
    lowered = target.strip().lower()
    if not lowered:
        return False
    abstract_markers = [
        "first ",
        "second ",
        "third ",
        "announcement",
        "link",
        "button",
        "item",
        "result",
        "entry",
        "tab",
        "第一条",
        "第二条",
        "第三条",
        "首条",
        "结果列表",
        "搜索结果",
        "标题",
        "链接",
        "按钮",
        "入口",
        "条目",
        "结果",
        "列表",
    ]
    return any(marker in lowered for marker in abstract_markers)


def _target_text_related(target: str, event_target: str) -> bool:
    if not target or not event_target:
        return False
    normalized_target = target.strip().lower()
    normalized_event = event_target.strip().lower()
    if normalized_target == normalized_event:
        return True
    if normalized_target in normalized_event or normalized_event in normalized_target:
        return True

    marker_groups = [
        {"first", "第一", "第1", "首条", "第一个"},
        {"second", "第二", "第2"},
        {"third", "第三", "第3"},
        {"announcement", "公告"},
        {"result", "结果"},
        {"link", "链接"},
        {"button", "按钮"},
        {"title", "标题"},
        {"search", "搜索"},
    ]
    for markers in marker_groups:
        if any(marker in normalized_target for marker in markers) and any(marker in normalized_event for marker in markers):
            return True
    return False


def _build_wait_step_after_event(event: Any, events: list[Any]) -> dict[str, Any] | None:
    page_url = str(getattr(event, "page_url", "") or "").strip()
    page_title = str(getattr(event, "page_title", "") or "").strip()
    target_selector = str(getattr(event, "target_selector", "") or "").strip()
    target_text = str(getattr(event, "target_text", "") or "").strip()
    event_type = str(getattr(event, "event_type", "") or "").strip().lower()

    transition = _find_followup_state_event(event, events)
    if transition is not None:
        transition_url = str(getattr(transition, "page_url", "") or "").strip()
        transition_title = str(getattr(transition, "page_title", "") or "").strip()
        return {
            "step_number": 0,
            "action": "wait",
            "target": transition_url or transition_title,
            "notes": "等待页面 URL、路由或标题切换到点击后的目标页面。",
        }
    if event_type == "click" and page_url and ("#/" in page_url or page_url.startswith("http://") or page_url.startswith("https://")):
        return {
            "step_number": 0,
            "action": "wait",
            "target": page_url,
            "notes": "等待页面保持在目标列表页或详情页地址。",
        }
    if event_type == "click" and target_selector:
        return {
            "step_number": 0,
            "action": "wait",
            "target": target_text or target_selector,
            "selector_hint": target_selector,
            "notes": "等待目标控件或内容区稳定出现。",
        }
    if page_title:
        return {
            "step_number": 0,
            "action": "wait",
            "target": page_title,
            "notes": "等待目标页面标题出现。",
        }
    return None


def _find_followup_state_event(current_event: Any, events: list[Any]) -> Any | None:
    current_timestamp = int(getattr(current_event, "client_timestamp_ms", 0) or 0)
    current_url = str(getattr(current_event, "page_url", "") or "").strip()
    current_title = str(getattr(current_event, "page_title", "") or "").strip()
    seen_current = False

    for event in sorted(events, key=lambda item: int(getattr(item, "client_timestamp_ms", 0) or 0)):
        if event is current_event:
            seen_current = True
            continue
        if not seen_current:
            continue

        event_timestamp = int(getattr(event, "client_timestamp_ms", 0) or 0)
        if current_timestamp and event_timestamp and event_timestamp - current_timestamp > 5000:
            break

        event_type = str(getattr(event, "event_type", "") or "").strip().lower()
        if event_type in {"click", "input", "change"}:
            break
        if event_type not in {"navigation", "history", "tab_updated", "page_loaded"}:
            continue

        next_url = str(getattr(event, "page_url", "") or "").strip()
        next_title = str(getattr(event, "page_title", "") or "").strip()
        if next_url and next_url != current_url:
            return event
        if next_title and next_title != current_title:
            return event
    return None


def _restore_missing_parent_click_steps(steps: list[dict[str, Any]], events: list[Any]) -> list[dict[str, Any]]:
    if len(steps) < 1 or not events:
        return steps

    restored: list[dict[str, Any]] = []
    click_events = [event for event in events if str(getattr(event, "event_type", "") or "").strip().lower() == "click"]

    for step in steps:
        if str(step.get("action") or "").strip().lower() == "click":
            matched_event = _match_listener_event_for_step(step, click_events)
        else:
            matched_event = None
        if matched_event is not None:
            parent_event = _find_parent_click_event(matched_event, click_events)
            if parent_event is not None and not _plan_already_contains_target(restored + steps, parent_event):
                restored.append(
                    {
                        "step_number": 0,
                        "action": "click",
                        "target": getattr(parent_event, "target_text", None) or getattr(parent_event, "target_selector", "") or "",
                        "selector_hint": getattr(parent_event, "target_selector", None),
                        "notes": "先展开上级菜单，再进入目标子项。",
                    }
                )
        restored.append(step)

    return restored


def _find_parent_click_event(current_event: Any, events: list[Any]) -> Any | None:
    current_target = str(getattr(current_event, "target_text", "") or getattr(current_event, "target_selector", "") or "").strip()
    if not current_target:
        return None

    current_timestamp = int(getattr(current_event, "client_timestamp_ms", 0) or 0)
    current_url = str(getattr(current_event, "page_url", "") or "").strip()

    prior_events: list[Any] = []
    for event in events:
        event_target = str(getattr(event, "target_text", "") or getattr(event, "target_selector", "") or "").strip()
        if not event_target or event_target == current_target:
            continue
        event_timestamp = int(getattr(event, "client_timestamp_ms", 0) or 0)
        if current_timestamp and event_timestamp and not (0 <= current_timestamp - event_timestamp <= 2000):
            continue
        event_url = str(getattr(event, "page_url", "") or "").strip()
        if current_url and event_url and event_url != current_url:
            continue
        prior_events.append(event)

    if not prior_events:
        return None
    return sorted(prior_events, key=lambda item: int(getattr(item, "client_timestamp_ms", 0) or 0), reverse=True)[0]


def _plan_already_contains_target(steps: list[dict[str, Any]], event: Any) -> bool:
    event_target = str(getattr(event, "target_text", "") or getattr(event, "target_selector", "") or "").strip().lower()
    if not event_target:
        return False
    for step in steps:
        target = str(step.get("target") or "").strip().lower()
        if target == event_target:
            return True
    return False


def _renumber_plan_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    renumbered: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        updated = dict(step)
        updated["step_number"] = index
        renumbered.append(updated)
    return renumbered


def _step_list_contains_wait(steps: list[dict[str, Any]], candidate: dict[str, Any]) -> bool:
    for step in steps:
        if str(step.get("action") or "").strip().lower() != "wait":
            continue
        if str(step.get("target") or "").strip() == str(candidate.get("target") or "").strip():
            if str(step.get("selector_hint") or "").strip() == str(candidate.get("selector_hint") or "").strip():
                return True
    return False


def _normalize_plan_sequence(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    previous_click_target = ""
    for step in steps:
        action = str(step.get("action") or "").strip().lower()
        target = str(step.get("target") or "").strip()

        if action == "wait" and not target:
            continue
        if action == "click" and target and previous_click_target == target:
            continue
        normalized.append(step)
        if action == "click":
            previous_click_target = target
        elif action != "wait":
            previous_click_target = ""
    return normalized


async def create_skill(request: Request) -> JSONResponse:
    try:
        payload = CreateSkillRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not payload.steps:
        return JSONResponse({"error": "steps is required."}, status_code=400)

    listener_events = BROWSER_EVENT_STORE.session_events(payload.listener_session_id) if payload.listener_session_id else []
    skill_name = payload.name.strip()
    if _is_placeholder_skill_name(skill_name):
        skill_name = _build_skill_title(
            steps=payload.steps,
            site_url=payload.site_url,
            user_request=payload.user_request,
            listener_events=listener_events,
        )
    skill_description = payload.description.strip() or _build_skill_description(
        steps=payload.steps,
        source_type=payload.source_type,
        user_request=payload.user_request,
        listener_events=listener_events,
    )

    skill = SKILL_LIBRARY.create_skill(
        name=skill_name,
        description=skill_description,
        source_type=payload.source_type,
        steps=payload.steps,
        site_url=payload.site_url,
        user_request=payload.user_request,
        video_path=payload.video_path,
        listener_session_id=payload.listener_session_id,
    )
    return JSONResponse(asdict(skill), status_code=201)


async def list_skills(request: Request) -> JSONResponse:
    return JSONResponse({"skills": [asdict(skill) for skill in SKILL_LIBRARY.list_skills()]})


async def list_execution_adapters(request: Request) -> JSONResponse:
    return JSONResponse(_execution_adapter_payload())


async def update_skill(request: Request) -> JSONResponse:
    skill_id = str(request.path_params.get("skill_id") or "").strip()
    if not skill_id:
        return JSONResponse({"error": "skill_id is required."}, status_code=400)
    try:
        payload = UpdateSkillRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    update_data = payload.model_dump(exclude_unset=True)
    if "steps" in update_data and not update_data["steps"]:
        return JSONResponse({"error": "steps cannot be empty."}, status_code=400)
    skill = SKILL_LIBRARY.update_skill(skill_id, **update_data)
    if skill is None:
        return JSONResponse({"error": f"Skill not found: {skill_id}"}, status_code=404)
    return JSONResponse(asdict(skill))


async def delete_skill(request: Request) -> JSONResponse:
    skill_id = str(request.path_params.get("skill_id") or "").strip()
    if not skill_id:
        return JSONResponse({"error": "skill_id is required."}, status_code=400)
    skill = SKILL_LIBRARY.delete_skill(skill_id)
    if skill is None:
        return JSONResponse({"error": f"Skill not found: {skill_id}"}, status_code=404)
    return JSONResponse({"deleted": True, "skill": asdict(skill)})


async def run_user_task_now(request: Request) -> JSONResponse:
    try:
        payload = RunUserTaskRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    selected_skills = SKILL_LIBRARY.get_many(payload.skill_ids)
    user_request = _compose_user_request(payload.objective, selected_skills)
    task_name = payload.name or payload.objective or "立即执行任务"
    task_plan = _compose_plan_from_skills(selected_skills)
    task_type = _normalize_execution_task_type(payload.task_type)
    acceptance_criteria = _build_task_acceptance_criteria(
        objective=payload.objective,
        plan=task_plan,
        skill_names=[skill.name for skill in selected_skills],
    )

    task = _run_in_dedicated_thread(
        SCHEDULER.create_and_run_task,
        name=task_name,
        task_type=task_type,
        payload={
            "cdp_url": payload.cdp_url or DEFAULT_CDP_URL,
            "user_request": user_request,
            "plan": task_plan,
            "skill_ids": payload.skill_ids,
            "skill_names": [skill.name for skill in selected_skills],
            "objective": payload.objective,
            "acceptance_criteria": acceptance_criteria,
            "task_type": task_type,
            "benchmark_task_types": [_normalize_execution_task_type(item) for item in payload.benchmark_task_types],
            "benchmark_runs": payload.benchmark_runs,
        },
    )
    if task.status == "manual":
        status_code = 409
    elif task.status == "completed":
        status_code = 200
    else:
        status_code = 500
    return JSONResponse(
        {
            "id": task.id,
            "name": task.name,
            "status": task.status,
            "logs": task.logs,
            "last_error": task.last_error,
            "task_type": task.task_type,
            "skill_ids": payload.skill_ids,
            "skill_names": [skill.name for skill in selected_skills],
            "objective": payload.objective,
            "token_usage": _task_token_usage_for_api(task),
        },
        status_code=status_code,
    )


def _serialize_task(task: Any) -> dict[str, Any]:
    if (
        getattr(task, "status", None) in {"completed", "failed", "cancelled", "manual", "scheduled"}
        and not getattr(task, "artifact_dir", None)
        and (getattr(task, "run_count", 0) > 0 or getattr(task, "status", None) == "cancelled")
    ):
        refreshed = SCHEDULER.ensure_task_artifacts(task.id)
        if refreshed is not None:
            task = refreshed
    token_usage = _task_token_usage_for_api(task)
    payload = {
        "id": task.id,
        "name": task.name,
        "run_at_iso": task.run_at_iso,
        "task_type": task.task_type,
        "frequency": task.frequency,
        "status": task.status,
        "logs": task.logs,
        "last_error": task.last_error,
        "created_at_iso": task.created_at_iso,
        "run_count": task.run_count,
        "last_run_at_iso": task.last_run_at_iso,
        "payload": task.payload,
        "cancel_requested": bool(getattr(task, "cancel_requested", False)),
        "cancelled_at_iso": getattr(task, "cancelled_at_iso", None),
        "progress_events": list(getattr(task, "progress_events", []) or []),
        "deliverables": list(getattr(task, "deliverables", []) or []),
        "artifact_dir": getattr(task, "artifact_dir", None),
        "benchmark_result": _extract_benchmark_result(list(getattr(task, "logs", []) or [])),
        "token_usage": token_usage,
    }
    payload.update(infer_task_execution_snapshot(task))
    return payload


def _task_token_usage_for_api(task: Any) -> dict[str, Any]:
    log_usage = extract_token_usage_from_logs(list(getattr(task, "logs", []) or []))
    if token_usage_has_activity(log_usage):
        return log_usage
    stored_usage = getattr(task, "token_usage", None)
    if isinstance(stored_usage, dict) and token_usage_has_activity(stored_usage):
        return stored_usage
    return empty_token_usage()


async def start_user_task_now(request: Request) -> JSONResponse:
    try:
        payload = RunUserTaskRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    selected_skills = SKILL_LIBRARY.get_many(payload.skill_ids)
    user_request = _compose_user_request(payload.objective, selected_skills)
    task_name = payload.name or payload.objective or "立即执行任务"
    task_plan = _compose_plan_from_skills(selected_skills)
    task_type = _normalize_execution_task_type(payload.task_type)
    acceptance_criteria = _build_task_acceptance_criteria(
        objective=payload.objective,
        plan=task_plan,
        skill_names=[skill.name for skill in selected_skills],
    )

    task = SCHEDULER.create_and_start_task(
        name=task_name,
        task_type=task_type,
        payload={
            "cdp_url": payload.cdp_url or DEFAULT_CDP_URL,
            "user_request": user_request,
            "plan": task_plan,
            "skill_ids": payload.skill_ids,
            "skill_names": [skill.name for skill in selected_skills],
            "objective": payload.objective,
            "acceptance_criteria": acceptance_criteria,
            "task_type": task_type,
            "benchmark_task_types": [_normalize_execution_task_type(item) for item in payload.benchmark_task_types],
            "benchmark_runs": payload.benchmark_runs,
        },
    )
    return JSONResponse(_serialize_task(task), status_code=202)


async def get_task_status(request: Request) -> JSONResponse:
    task_id = str(request.path_params.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"error": "task_id is required."}, status_code=400)
    task = SCHEDULER.get_task(task_id)
    if task is None:
        return JSONResponse({"error": f"Task not found: {task_id}"}, status_code=404)
    return JSONResponse(_serialize_task(task))


async def cancel_task(request: Request) -> JSONResponse:
    task_id = str(request.path_params.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"error": "task_id is required."}, status_code=400)
    if SCHEDULER.get_task(task_id) is None:
        return JSONResponse({"error": f"Task not found: {task_id}"}, status_code=404)
    task = SCHEDULER.request_cancel(task_id)
    status_code = 202 if task.status == "cancelling" else 200
    return JSONResponse(_serialize_task(task), status_code=status_code)


async def task_browser_preview(request: Request) -> Response | JSONResponse:
    task_id = str(request.path_params.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"error": "task_id is required."}, status_code=400)
    task = SCHEDULER.get_task(task_id)
    if task is None:
        return JSONResponse({"error": f"Task not found: {task_id}"}, status_code=404)
    payload = getattr(task, "payload", {}) if isinstance(getattr(task, "payload", None), dict) else {}
    cdp_url = str(payload.get("cdp_url") or DEFAULT_CDP_URL).strip()
    try:
        cache_key = cdp_url
        now = time.monotonic()
        with PREVIEW_CACHE_LOCK:
            cached = PREVIEW_CACHE.get(cache_key)
            if cached is not None and now - cached[0] <= PREVIEW_CACHE_TTL_SECONDS:
                image_bytes = cached[1]
            else:
                image_bytes = b""
        if not image_bytes:
            image_bytes = _run_in_dedicated_thread(_capture_browser_preview_sync, cdp_url)
            with PREVIEW_CACHE_LOCK:
                PREVIEW_CACHE[cache_key] = (time.monotonic(), image_bytes, "image/png")
    except Exception as exc:
        return JSONResponse({"error": f"Browser preview unavailable: {exc}"}, status_code=404)
    return Response(
        image_bytes,
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


async def download_task_artifact(request: Request) -> FileResponse | JSONResponse:
    task_id = str(request.path_params.get("task_id") or "").strip()
    filename = unquote(str(request.path_params.get("filename") or "").strip()).replace("\\", "/")
    if not task_id or not filename:
        return JSONResponse({"error": "task_id and filename are required."}, status_code=400)
    if filename.startswith("/") or any(part == ".." for part in filename.split("/")):
        return JSONResponse({"error": "Invalid artifact path."}, status_code=400)

    task = SCHEDULER.get_task(task_id)
    if task is None:
        return JSONResponse({"error": f"Task not found: {task_id}"}, status_code=404)
    allowed_files = {
        str(item.get("filename") or "").replace("\\", "/")
        for item in getattr(task, "deliverables", []) or []
        if item.get("kind") == "file"
    }
    if filename not in allowed_files:
        return JSONResponse({"error": f"Unsupported task artifact: {filename}"}, status_code=400)
    artifact_dir = getattr(task, "artifact_dir", None)
    if not artifact_dir:
        return JSONResponse({"error": f"No artifacts are available for task: {task_id}"}, status_code=404)

    root = Path(artifact_dir).resolve()
    path = (root / filename).resolve()
    if root not in path.parents and path != root:
        return JSONResponse({"error": "Invalid artifact path."}, status_code=400)
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": f"Artifact not found: {filename}"}, status_code=404)

    media_type = "application/json" if filename.endswith(".json") else None
    return FileResponse(path, media_type=media_type, filename=Path(filename).name)


async def create_schedule(request: Request) -> JSONResponse:
    payload = await request.json()
    frequency = str(payload.get("frequency") or "once").strip().lower()
    run_at_iso = payload.get("run_at_iso")
    if frequency != "manual" and not run_at_iso:
        return JSONResponse({"error": "run_at_iso is required."}, status_code=400)
    if frequency not in {"once", "daily", "weekly", "manual"}:
        return JSONResponse({"error": "frequency must be one of once, daily, weekly, manual."}, status_code=400)

    skill_ids = [str(skill_id) for skill_id in payload.get("skill_ids") or []]
    selected_skills = SKILL_LIBRARY.get_many(skill_ids)
    objective = str(payload.get("objective") or payload.get("user_request") or "")
    composed_request = _compose_user_request(objective, selected_skills)
    task_plan = _compose_plan_from_skills(selected_skills)
    if task_plan is None and isinstance(payload.get("plan"), dict):
        task_plan = payload.get("plan")
    task_type = _normalize_execution_task_type(payload.get("task_type"))
    acceptance_criteria = _build_task_acceptance_criteria(
        objective=objective,
        plan=task_plan,
        skill_names=[skill.name for skill in selected_skills],
    )

    task = SCHEDULER.add_task(
        name=payload.get("name") or "Scheduled browser workflow",
        run_at_iso=run_at_iso or datetime.now(timezone.utc).isoformat(),
        task_type=task_type,
        payload={
            "cdp_url": payload.get("cdp_url") or DEFAULT_CDP_URL,
            "user_request": composed_request,
            "plan": task_plan,
            "objective": objective,
            "skill_ids": skill_ids,
            "skill_names": [skill.name for skill in selected_skills],
            "acceptance_criteria": acceptance_criteria,
            "task_type": task_type,
            "benchmark_task_types": [
                _normalize_execution_task_type(str(item))
                for item in payload.get("benchmark_task_types") or []
            ],
            "benchmark_runs": int(payload.get("benchmark_runs") or 1),
        },
        frequency=frequency,
    )
    return JSONResponse(
        {
            "id": task.id,
            "name": task.name,
            "run_at_iso": task.run_at_iso,
            "task_type": task.task_type,
            "frequency": task.frequency,
            "status": task.status,
        }
    )


async def list_schedules(request: Request) -> JSONResponse:
    tasks = [task for task in SCHEDULER.list_tasks() if task.task_type in USER_VISIBLE_TASK_TYPES]
    return JSONResponse(
        {
            "tasks": [_serialize_task(task) for task in tasks]
        }
    )


async def app_status(request: Request) -> JSONResponse:
    status = load_config_status()
    browser_use_llm = _resolve_browser_use_llm_settings(status.config) if status.config else None
    browser_use_vision_llm = _resolve_browser_use_llm_settings(status.config, prefer_vision=True) if status.config else None
    return JSONResponse(
        {
            "config_ready": status.is_ready,
            "missing_fields": status.missing_fields,
            "target_site_url": status.config.target_site_url if status.config else None,
            "glm_model": status.config.glm_model if status.config else None,
            "deepseek_model": status.config.deepseek_model if status.config else None,
            "browser_use_llm": browser_use_llm["model"] if browser_use_llm else None,
            "browser_use_llm_base_url": browser_use_llm["base_url"] if browser_use_llm else None,
            "browser_use_vision_llm": browser_use_vision_llm["model"] if browser_use_vision_llm else None,
            "browser_use_vision_mode": _browser_use_vision_mode_label(_browser_use_vision_mode_from_env()),
            "listener_buffered_events": BROWSER_EVENT_STORE.status()["buffered_events"],
        }
    )


async def browser_listener_status(request: Request) -> JSONResponse:
    return JSONResponse(BROWSER_EVENT_STORE.status())


async def browser_listener_events(request: Request) -> JSONResponse:
    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        return JSONResponse({"error": "limit must be an integer."}, status_code=400)
    session_id = request.query_params.get("session_id") or None
    screenshot_only = request.query_params.get("screenshot_only", "").lower() in {"1", "true", "yes"}
    events = [
        event.model_dump()
        for event in BROWSER_EVENT_STORE.list_events(
            limit=limit,
            session_id=session_id,
            only_with_screenshots=screenshot_only,
        )
    ]
    return JSONResponse(
        {
            "events": events,
            "total_events": BROWSER_EVENT_STORE.status()["buffered_events"],
            "limit": max(1, min(limit, 200)),
            "session_id": session_id,
            "screenshot_only": screenshot_only,
        }
    )


async def ingest_browser_listener_events(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        batch = BrowserEventBatchIn.model_validate(payload)
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    accepted = BROWSER_EVENT_STORE.ingest(batch)
    return JSONResponse(
        {
            "accepted_count": len(accepted),
            "last_event_id": accepted[-1].event_id if accepted else None,
            "buffered_events": BROWSER_EVENT_STORE.status()["buffered_events"],
        }
    )


async def clear_browser_listener_events(request: Request) -> JSONResponse:
    cleared = BROWSER_EVENT_STORE.clear()
    return JSONResponse({"cleared_count": cleared})


async def upload_browser_listener_recording(request: Request) -> JSONResponse:
    form = await request.form()
    session_id = str(form.get("session_id") or "").strip()
    started_at_ms_raw = str(form.get("started_at_ms") or "").strip()
    ended_at_ms_raw = str(form.get("ended_at_ms") or "").strip()
    tab_id_raw = str(form.get("tab_id") or "").strip()
    mime_type = str(form.get("mime_type") or "video/webm").strip() or "video/webm"
    uploaded = form.get("video")

    if not session_id:
        return JSONResponse({"error": "session_id is required."}, status_code=400)
    if not isinstance(uploaded, UploadFile):
        return JSONResponse({"error": "Missing recorded video file."}, status_code=400)

    recording_path = save_session_recording(session_id, uploaded.filename or "session.webm", uploaded.file)
    recording = BROWSER_EVENT_STORE.set_session_recording(
        session_id,
        recording_path=str(recording_path),
        mime_type=mime_type,
        tab_id=int(tab_id_raw) if tab_id_raw.isdigit() else None,
        started_at_ms=int(started_at_ms_raw) if started_at_ms_raw.isdigit() else None,
        ended_at_ms=int(ended_at_ms_raw) if ended_at_ms_raw.isdigit() else None,
    )
    return JSONResponse(
        {
            "session_id": recording.session_id,
            "recording_path": recording.recording_path,
            "mime_type": recording.mime_type,
        }
    )


def _recording_payload(recording: Any) -> dict[str, Any]:
    summary = BROWSER_EVENT_STORE.session_summary(recording.session_id)
    events = BROWSER_EVENT_STORE.session_events(recording.session_id)
    path = Path(recording.recording_path)
    return {
        "session_id": recording.session_id,
        "has_recording": True,
        "listener_analysis_ready": bool(summary.get("listener_analysis_ready")),
        "session_recording_ready": True,
        "title": _build_recording_title(events),
        "title_source": "listener_event_summary",
        "video_url": f"/api/browser-listener/recording-video?session_id={quote(recording.session_id)}",
        "recording_path": recording.recording_path,
        "mime_type": recording.mime_type,
        "file_size_bytes": path.stat().st_size if path.exists() else 0,
        "saved_at_iso": recording.saved_at_iso,
        "started_at_ms": recording.started_at_ms,
        "ended_at_ms": recording.ended_at_ms,
        "duration_ms": (
            recording.ended_at_ms - recording.started_at_ms
            if recording.started_at_ms is not None and recording.ended_at_ms is not None
            else None
        ),
        "event_count": summary.get("event_count", 0),
        "key_event_count": summary.get("key_event_count", 0),
        "screenshot_count": summary.get("screenshot_count", 0),
    }


def _disk_recording_payload(session_id: str, path: Path) -> dict[str, Any]:
    events = BROWSER_EVENT_STORE.session_events(session_id)
    stat = path.stat()
    saved_at_iso = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    title = _build_recording_title(events)
    if not events:
        title = f"Recording {session_id[:8]}"
    return {
        "session_id": session_id,
        "has_recording": True,
        "listener_analysis_ready": bool(events),
        "session_recording_ready": True,
        "title": title,
        "title_source": "disk_fallback" if not events else "listener_event_summary",
        "video_url": f"/api/browser-listener/recording-video?session_id={quote(session_id)}",
        "recording_path": str(path),
        "mime_type": _guess_recording_mime_type(path),
        "file_size_bytes": stat.st_size,
        "saved_at_iso": saved_at_iso,
        "started_at_ms": None,
        "ended_at_ms": None,
        "duration_ms": None,
        "event_count": len(events),
        "key_event_count": sum(1 for event in events if getattr(event, "is_key_candidate", False)),
        "screenshot_count": sum(1 for event in events if getattr(event, "screenshot_path", None)),
    }


def _listener_session_payload(summary: dict[str, Any]) -> dict[str, Any]:
    session_id = str(summary.get("session_id") or "")
    events = BROWSER_EVENT_STORE.session_events(session_id)
    title = _build_recording_title(events) if events else f"Listener session {session_id[:8]}"
    saved_at_iso = str(summary.get("last_event_at_iso") or summary.get("recording_saved_at_iso") or "")
    return {
        "session_id": session_id,
        "has_recording": bool(summary.get("has_recording")),
        "listener_analysis_ready": bool(summary.get("listener_analysis_ready")),
        "session_recording_ready": bool(summary.get("session_recording_ready")),
        "title": title,
        "title_source": "listener_event_summary" if events else "listener_session",
        "video_url": None,
        "recording_path": summary.get("recording_path"),
        "mime_type": summary.get("recording_mime_type"),
        "file_size_bytes": 0,
        "saved_at_iso": saved_at_iso,
        "started_at_ms": summary.get("recording_started_at_ms"),
        "ended_at_ms": summary.get("recording_ended_at_ms"),
        "duration_ms": None,
        "event_count": int(summary.get("event_count") or 0),
        "key_event_count": int(summary.get("key_event_count") or 0),
        "screenshot_count": int(summary.get("screenshot_count") or 0),
    }


def _list_disk_recording_files() -> list[tuple[str, Path]]:
    root = Path("artifacts/session_recordings")
    if not root.exists():
        return []
    recordings: list[tuple[str, Path]] = []
    for session_dir in root.iterdir():
        if not session_dir.is_dir():
            continue
        candidates = sorted(session_dir.glob("session_recording.*"), key=lambda item: item.stat().st_mtime, reverse=True)
        if candidates:
            recordings.append((session_dir.name, candidates[0]))
    recordings.sort(key=lambda item: item[1].stat().st_mtime, reverse=True)
    return recordings


def _find_recording_file(session_id: str) -> Path | None:
    if not session_id or any(part in session_id for part in {"/", "\\", ".."}):
        return None
    recording = BROWSER_EVENT_STORE.get_session_recording(session_id)
    if recording is not None:
        path = Path(recording.recording_path)
        if path.exists():
            return path
    session_dir = Path("artifacts/session_recordings") / session_id
    if not session_dir.exists() or not session_dir.is_dir():
        return None
    candidates = sorted(session_dir.glob("session_recording.*"), key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _guess_recording_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp4":
        return "video/mp4"
    return "video/webm"


def _build_recording_title(events: list[Any]) -> str:
    ordered_events = sorted(events, key=lambda event: int(getattr(event, "client_timestamp_ms", 0) or 0))
    base = _infer_site_label_from_events(ordered_events)
    operation = _summarize_operation_from_events(ordered_events)
    return _compose_semantic_title(
        base=base,
        operation=operation,
        suffix="操作录屏",
        fallback="网页操作录屏",
        limit=56,
    )


def _infer_site_label_from_events(events: list[Any]) -> str:
    page_titles: list[str] = []
    site_targets: list[str] = []
    hosts: list[str] = []
    for event in events:
        raw_url = str(getattr(event, "page_url", "") or "").strip()
        title = _normalize_page_title(str(getattr(event, "page_title", "") or ""))
        if title and not _is_internal_title(title) and not _is_search_title(title, raw_url):
            page_titles.append(title)

        target = _clean_action_target(str(getattr(event, "target_text", "") or ""), limit=32)
        if target and _looks_like_site_name(target):
            site_targets.append(target)

        if raw_url:
            host = urlparse(raw_url).netloc.lower()
            if host and not _is_internal_host(host) and not _is_search_engine_url(raw_url):
                hosts.append(host)
    if page_titles:
        return Counter(page_titles).most_common(1)[0][0]
    if site_targets:
        return Counter(site_targets).most_common(1)[0][0]
    if hosts:
        return _host_to_site_label(Counter(hosts).most_common(1)[0][0])
    return "网页操作"


def _summarize_operation_from_events(events: list[Any]) -> str:
    targets: list[str] = []
    urls: list[str] = []
    search_terms: list[str] = []
    form_values: list[str] = []
    has_scroll = False
    for event in events:
        event_type = str(getattr(event, "event_type", "") or "").strip()
        if event_type not in {"click", "keyboard_shortcut", "change", "input", "navigation", "history", "tab_updated", "page_loaded", "scroll"}:
            continue
        target = _clean_action_target(str(getattr(event, "target_text", "") or ""))
        selector_hint = str(getattr(event, "target_selector", "") or "")
        value = _clean_action_target(str(getattr(event, "input_value", "") or ""), limit=32)
        if target and target not in targets:
            targets.append(target)
        raw_url = str(getattr(event, "page_url", "") or "").strip()
        if raw_url:
            urls.append(raw_url)
        if event_type == "scroll":
            has_scroll = True
        if value and not _looks_sensitive(target, value):
            if _looks_like_search_context(target, selector_hint, raw_url):
                _append_unique(search_terms, value)
            else:
                _append_unique(form_values, value)

    phrases: list[str] = []
    for term in search_terms[:2]:
        _append_unique(phrases, f"搜索{term}")
    site_targets = [target for target in targets if _looks_like_site_name(target)]
    if site_targets and not search_terms:
        _append_unique(phrases, f"打开{site_targets[0]}")
    location_targets = [target for target in targets + form_values if _looks_like_location(target)]
    if location_targets:
        phrases.append(f"筛选{'、'.join(location_targets[:3])}")
    if not search_terms and any(_contains_any(target, {"搜索", "查询", "筛选", "检索"}) for target in targets):
        _append_unique(phrases, "执行搜索查询")
    if form_values and not location_targets:
        _append_unique(phrases, "填写条件")
    if any(_contains_any(target, {"公告保存", "保存", "下载", "PDF", "导出"}) for target in targets):
        _append_unique(phrases, "保存或下载结果")
    if any("detail" in url.lower() or "详情" in url for url in urls) or any("详情" in target for target in targets):
        _append_unique(phrases, "打开详情页")
    if not phrases:
        meaningful_targets = [target for target in targets if not _looks_like_generic_target(target)]
        if meaningful_targets:
            phrases.append(f"操作{'、'.join(meaningful_targets[:3])}")
    if not phrases and has_scroll:
        phrases.append("浏览页面内容")
    return "，".join(phrases[:3])


def _build_skill_title(
    *,
    steps: list[dict[str, Any]],
    site_url: str | None,
    user_request: str | None,
    listener_events: list[Any] | None = None,
) -> str:
    events = listener_events or []
    base = _infer_site_label_from_events(events) if events else _infer_site_label_from_url(site_url)
    operation = _summarize_operation_from_events(events) if events else _summarize_operation_from_steps(steps)
    request_title = _summarize_user_request_title(user_request)
    if request_title and (base in {"网页", "网页操作"} or not operation):
        return _ensure_title_suffix(request_title, "技能", limit=48) or "网页自动化技能"

    return _compose_semantic_title(
        base=base,
        operation=operation,
        suffix="技能",
        fallback="网页自动化技能",
        limit=48,
    )


def _build_skill_description(
    *,
    steps: list[dict[str, Any]],
    source_type: str,
    user_request: str | None,
    listener_events: list[Any] | None = None,
) -> str:
    request = _clean_title_part(user_request or "", limit=72)
    if request and not _looks_like_generic_request(request):
        return f"目标：{request}"
    events = listener_events or []
    operation = _summarize_operation_from_events(events) if events else _summarize_operation_from_steps(steps)
    source = "视频分析" if source_type == "video_analysis" else "监听分析"
    if operation:
        return f"基于{source}生成，可复用来{operation}。"
    return f"基于{source}生成，共 {len(steps)} 个步骤。"


def _summarize_operation_from_steps(steps: list[dict[str, Any]]) -> str:
    targets = [_clean_action_target(str(step.get("target") or "")) for step in steps]
    values = [_clean_action_target(str(step.get("value") or ""), limit=32) for step in steps]
    targets = [target for target in targets if target]
    values = [value for value in values if value and not _looks_sensitive("", value)]
    actions = [str(step.get("action") or "").strip().lower() for step in steps]
    phrases: list[str] = []
    site_targets = [target for target in targets if _looks_like_site_name(target)]
    if site_targets:
        _append_unique(phrases, f"打开{site_targets[0]}")
    location_targets = [target for target in targets + values if _looks_like_location(target)]
    if location_targets:
        _append_unique(phrases, f"筛选{'、'.join(location_targets[:3])}")
    if any(_contains_any(target, {"搜索", "查询", "筛选", "检索"}) for target in targets):
        _append_unique(phrases, "执行搜索查询")
    if any(action in {"type", "input", "change"} for action in actions):
        _append_unique(phrases, "填写条件")
    if any(_contains_any(target, {"保存", "下载", "PDF", "导出"}) for target in targets):
        _append_unique(phrases, "保存或下载结果")
    if any("详情" in target for target in targets):
        _append_unique(phrases, "打开详情页")
    if not phrases:
        meaningful_targets = [target for target in targets if not _looks_like_generic_target(target)]
        if meaningful_targets:
            phrases.append(f"操作{'、'.join(meaningful_targets[:3])}")
    return "，".join(phrases[:3])


def _infer_site_label_from_url(site_url: str | None) -> str:
    if not site_url:
        return "网页"
    host = urlparse(site_url).netloc.lower()
    if not host or _is_search_engine_host(host) or _is_internal_host(host):
        return "网页"
    return _host_to_site_label(host)


def _host_to_site_label(host: str) -> str:
    normalized = host.lower().removeprefix("www.")
    if "landchina" in normalized:
        return "中国土地市场网"
    if "51dzhp" in normalized:
        return "大众环评藏经阁"
    return normalized or "网页"


def _compose_semantic_title(*, base: str, operation: str, suffix: str, fallback: str, limit: int) -> str:
    clean_base = _clean_title_part(base, limit=28)
    clean_operation = _compact_operation_for_base(_clean_title_part(operation, limit=36), clean_base)
    if clean_operation:
        if clean_base and clean_base not in {"网页", "网页操作"}:
            title = f"{clean_base}：{clean_operation}"
        else:
            title = f"{clean_operation}{suffix}"
    elif clean_base and clean_base not in {"网页", "网页操作"}:
        title = f"{clean_base}{suffix}"
    else:
        title = fallback
    return _clean_title_part(title, limit=limit) or fallback


def _compact_operation_for_base(operation: str, base: str) -> str:
    if not operation or not base:
        return operation
    exact_replacements = {
        f"搜索{base}": "搜索并打开站点",
        f"打开{base}": "打开目标页面",
        f"进入{base}": "打开目标页面",
        f"查看{base}": "查看目标页面",
    }
    if operation in exact_replacements:
        return exact_replacements[operation]
    duplicated = f"搜索{base}，打开{base}"
    if operation.startswith(duplicated):
        return operation.replace(duplicated, "搜索并打开站点", 1)
    return operation


def _summarize_user_request_title(user_request: str | None) -> str:
    request = _clean_title_part(user_request or "", limit=72)
    if not request or _looks_like_generic_request(request):
        return ""
    request = re.sub(r"^(请|请你|帮我|麻烦|我要|我想|需要|使用已保存的?)", "", request).strip()
    pieces = [piece.strip() for piece in re.split(r"[。；;，,\n\r]+", request) if piece.strip()]
    action_pieces = [
        piece
        for piece in pieces
        if _contains_any(piece, {"打开", "搜索", "查询", "筛选", "下载", "导出", "保存", "生成", "查看"})
    ]
    selected = action_pieces[:2] or pieces[:1]
    return _clean_title_part("，".join(selected), limit=30)


def _ensure_title_suffix(title: str, suffix: str, *, limit: int) -> str:
    cleaned = _clean_title_part(title, limit=max(1, limit - len(suffix)))
    if not cleaned:
        return ""
    if cleaned.endswith(suffix):
        return _clean_title_part(cleaned, limit=limit)
    return _clean_title_part(f"{cleaned}{suffix}", limit=limit)


def _clean_title_part(value: str, *, limit: int = 32) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    blocked_fragments = ["data:image", "javascript:", "chrome-extension://"]
    if any(fragment in text.lower() for fragment in blocked_fragments):
        return ""
    text = text.strip(" -_|·：:")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _clean_action_target(value: str, *, limit: int = 28) -> str:
    text = _clean_title_part(value, limit=limit)
    if not text or _is_internal_title(text):
        return ""
    if _looks_like_generic_target(text):
        return ""
    return text


def _normalize_page_title(value: str) -> str:
    title = _clean_title_part(value, limit=40)
    if not title:
        return ""
    for separator in (" - ", " | ", "_"):
        if separator in title:
            first = _clean_title_part(title.split(separator, 1)[0], limit=32)
            if first and not _is_search_title(first, ""):
                return first
    return title


def _is_internal_title(value: str) -> bool:
    return value in {"Eyeclaw 用户端", "Eyeclaw Smoke Target"} or "127.0.0.1" in value or "localhost" in value


def _is_internal_host(host: str) -> bool:
    normalized = host.lower()
    return normalized.startswith("127.0.0.1") or normalized in {"localhost", "::1"}


def _is_search_title(title: str, raw_url: str) -> bool:
    lowered = title.lower()
    if _is_search_engine_url(raw_url):
        return True
    return any(keyword in lowered for keyword in {"bing", "google", "百度", "必应", "搜索结果", "search"})


def _is_search_engine_url(raw_url: str) -> bool:
    host = urlparse(raw_url).netloc.lower() if raw_url else ""
    return _is_search_engine_host(host)


def _is_search_engine_host(host: str) -> bool:
    normalized = host.lower()
    return any(
        keyword in normalized
        for keyword in {"bing.", "baidu.", "google.", "sogou.", "so.com", "sm.cn", "yahoo."}
    )


def _looks_like_search_context(target: str, selector_hint: str, raw_url: str) -> bool:
    combined = f"{target} {selector_hint} {raw_url}".lower()
    return any(keyword in combined for keyword in {"搜索", "查询", "检索", "search", "query", "keyword", "wd=", "q="})


def _looks_sensitive(target: str, value: str) -> bool:
    combined = f"{target} {value}".lower()
    return any(
        keyword in combined
        for keyword in {"密码", "验证码", "手机号", "电话", "身份证", "账号", "token", "secret", "password", "code"}
    )


def _looks_like_generic_request(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {"", "未命名技能", "untitled skill", "new skill"} or _contains_any(
        normalized,
        {"使用已保存", "已保存的技能", "立即执行一遍"},
    )


def _looks_like_site_name(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) > 28 or _looks_like_location(stripped):
        return False
    if "网" in stripped and not _contains_any(stripped, {"网页", "下一页", "上一页"}):
        return True
    return bool(re.search(r"\b[a-z0-9-]+\.(com|cn|org|net|gov)\b", stripped, flags=re.IGNORECASE))


def _looks_like_generic_target(value: str) -> bool:
    stripped = value.strip()
    generic_values = {
        "button",
        "label",
        "input",
        "div",
        "span",
        "不限省市区",
        "请选择",
        "全部",
        "下一页",
        "上一页",
        "确定",
        "取消",
    }
    return stripped in generic_values or stripped.startswith("div.") or stripped.startswith("ul.")


def _looks_like_location(value: str) -> bool:
    return _contains_any(value, {"省", "市", "区", "县", "自治州"}) and len(value) <= 16


def _contains_any(value: str, needles: set[str]) -> bool:
    return any(needle in value for needle in needles)


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _is_placeholder_skill_name(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {"", "未命名技能", "untitled skill", "new skill"}


async def list_browser_listener_recordings(request: Request) -> JSONResponse:
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        return JSONResponse({"error": "limit must be an integer."}, status_code=400)
    recordings = {}
    for recording in BROWSER_EVENT_STORE.list_session_recordings(limit=limit):
        if Path(recording.recording_path).exists():
            recordings[recording.session_id] = _recording_payload(recording)

    for session_id, path in _list_disk_recording_files():
        if session_id not in recordings:
            recordings[session_id] = _disk_recording_payload(session_id, path)

    for summary in BROWSER_EVENT_STORE.list_session_summaries(limit=limit):
        session_id = str(summary.get("session_id") or "")
        if not summary.get("has_recording") and not summary.get("has_screenshots"):
            continue
        if session_id and session_id not in recordings:
            recordings[session_id] = _listener_session_payload(summary)

    ordered = sorted(recordings.values(), key=lambda item: str(item.get("saved_at_iso") or ""), reverse=True)[: max(1, min(limit, 100))]
    return JSONResponse({"recordings": ordered, "count": len(ordered)})


async def browser_listener_recording_video(request: Request) -> FileResponse | JSONResponse:
    session_id = str(request.query_params.get("session_id") or "").strip()
    if not session_id:
        return JSONResponse({"error": "session_id is required."}, status_code=400)

    path = _find_recording_file(session_id)
    if path is None:
        return JSONResponse({"error": f"Recording not found for session: {session_id}"}, status_code=404)
    if not path.exists():
        return JSONResponse({"error": f"Recording file not found: {path}"}, status_code=404)
    recording = BROWSER_EVENT_STORE.get_session_recording(session_id)
    media_type = recording.mime_type if recording else _guess_recording_mime_type(path)
    return FileResponse(path, media_type=media_type or "video/webm", filename=path.name)


async def delete_browser_listener_recording(request: Request) -> JSONResponse:
    session_id = str(request.query_params.get("session_id") or "").strip()
    if not session_id:
        return JSONResponse({"error": "session_id is required."}, status_code=400)

    path = _find_recording_file(session_id)
    if path is None:
        return JSONResponse({"error": f"Recording not found for session: {session_id}"}, status_code=404)
    if not path.name.startswith("session_recording."):
        return JSONResponse({"error": f"Refusing to delete unexpected recording file: {path.name}"}, status_code=400)

    BROWSER_EVENT_STORE.remove_session_recording(session_id)
    deleted = False
    if path.exists():
        path.unlink()
        deleted = True
    try:
        path.parent.rmdir()
    except OSError:
        pass

    return JSONResponse(
        {
            "session_id": session_id,
            "deleted": deleted,
            "recording_path": str(path),
        }
    )


async def browser_listener_session_summary(request: Request) -> JSONResponse:
    session_id = str(request.query_params.get("session_id") or "").strip()
    if not session_id:
        return JSONResponse({"error": "session_id is required."}, status_code=400)
    return JSONResponse(BROWSER_EVENT_STORE.session_summary(session_id))


async def browser_listener_latest_session_summary(request: Request) -> JSONResponse:
    summary = BROWSER_EVENT_STORE.latest_session_summary()
    if summary is None:
        return JSONResponse({"error": "No listener session is available yet."}, status_code=404)
    return JSONResponse(summary)


async def analyze_browser_listener_session(request: Request) -> JSONResponse:
    try:
        payload = ListenerAnalysisRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        result = await asyncio.to_thread(_build_listener_analysis_result, payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result)


routes = [
    Route("/", homepage),
    Route("/app", app_homepage),
    Route("/api/status", app_status),
    Route("/api/execution-adapters", list_execution_adapters, methods=["GET"]),
    Route("/api/skills", list_skills, methods=["GET"]),
    Route("/api/skills", create_skill, methods=["POST"]),
    Route("/api/skills/{skill_id}", update_skill, methods=["PATCH"]),
    Route("/api/skills/{skill_id}", delete_skill, methods=["DELETE"]),
    Route("/api/tasks/run-now", run_user_task_now, methods=["POST"]),
    Route("/api/tasks/run-now/start", start_user_task_now, methods=["POST"]),
    Route("/api/tasks/{task_id}/cancel", cancel_task, methods=["POST"]),
    Route("/api/tasks/{task_id}/browser-preview.png", task_browser_preview, methods=["GET"]),
    Route("/api/tasks/{task_id}/artifacts/{filename:path}", download_task_artifact, methods=["GET"]),
    Route("/api/tasks/{task_id}", get_task_status, methods=["GET"]),
    Route("/api/upload", upload_video, methods=["POST"]),
    Route("/api/analyze", analyze_video, methods=["POST"]),
    Route("/api/analyze/start", start_video_analysis_job, methods=["POST"]),
    Route("/api/analysis-jobs/status", analysis_job_status, methods=["GET"]),
    Route("/api/browser/connect", connect_live_browser, methods=["POST"]),
    Route("/api/browser/live-run", run_live_workflow, methods=["POST"]),
    Route("/api/browser/live-run/start", start_live_workflow_job, methods=["POST"]),
    Route("/api/browser-listener/status", browser_listener_status, methods=["GET"]),
    Route("/api/browser-listener/session", browser_listener_session_summary, methods=["GET"]),
    Route("/api/browser-listener/session-latest", browser_listener_latest_session_summary, methods=["GET"]),
    Route("/api/browser-listener/events", browser_listener_events, methods=["GET"]),
    Route("/api/browser-listener/events", ingest_browser_listener_events, methods=["POST"]),
    Route("/api/browser-listener/recordings", list_browser_listener_recordings, methods=["GET"]),
    Route("/api/browser-listener/recording-video", browser_listener_recording_video, methods=["GET"]),
    Route("/api/browser-listener/recording", delete_browser_listener_recording, methods=["DELETE"]),
    Route("/api/browser-listener/session-recording", upload_browser_listener_recording, methods=["POST"]),
    Route("/api/browser-listener/events/clear", clear_browser_listener_events, methods=["POST"]),
    Route("/api/browser-listener/analyze", analyze_browser_listener_session, methods=["POST"]),
    Route("/api/browser-listener/analyze/start", start_listener_analysis_job, methods=["POST"]),
    Route("/api/schedules", create_schedule, methods=["POST"]),
    Route("/api/schedules", list_schedules, methods=["GET"]),
]

async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail or str(exc)}, status_code=exc.status_code)
    return JSONResponse({"error": exc.detail or str(exc)}, status_code=exc.status_code)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"error": str(exc)}, status_code=500)


app = Starlette(
    debug=False,
    routes=routes,
    exception_handlers={
        HTTPException: http_exception_handler,
        Exception: unhandled_exception_handler,
    },
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _run_in_dedicated_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result_queue: Queue[tuple[bool, Any]] = Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put((True, func(*args, **kwargs)))
        except Exception as exc:
            result_queue.put((False, exc))

    thread = Thread(target=worker, daemon=True)
    thread.start()
    thread.join()
    ok, value = result_queue.get()
    if ok:
        return value
    raise value


def _connect_live_browser_sync(cdp_url: str) -> dict:
    session = connect_over_cdp(cdp_url)
    try:
        state = detect_eia_state(session)
        return {
            "cdp_url": cdp_url,
            "page_role": state.page_role,
            "title": state.title,
            "url": state.url,
            "summary": state.summary,
        }
    finally:
        close_replay_session(session)


def _capture_browser_preview_sync(cdp_url: str) -> bytes:
    session = connect_over_cdp(cdp_url)
    try:
        page = session.page
        return page.screenshot(type="png", full_page=False, timeout=1000)
    finally:
        close_replay_session(session)


def _run_live_workflow_sync(
    cdp_url: str,
    user_request: str,
    raw_plan: dict[str, Any] | None = None,
    task_type: str = PRIMARY_EXECUTION_TASK_TYPE,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[dict, int]:
    logs: list[str] = []

    def report(message: str) -> None:
        logs.append(message)
        if progress_callback is not None:
            progress_callback(message)

    normalized_task_type = _normalize_execution_task_type(task_type)
    payload = {
        "user_request": user_request,
        "plan": raw_plan,
        "cdp_url": cdp_url,
        "task_type": normalized_task_type,
    }

    try:
        if normalized_task_type == PRIMARY_EXECUTION_TASK_TYPE:
            report("Executing Browser Use live workflow.")
            browser_use_logs = scheduler_run_browser_use_live_workflow(payload)
            logs.extend(browser_use_logs)
            return {"status": "completed", "mode": "browser_use", "task_type": normalized_task_type, "logs": logs, "plan": raw_plan}, 200

        if normalized_task_type == SMART_ROUTER_TASK_TYPE:
            report("Executing smart routed browser workflow.")
            router_logs = scheduler_run_smart_router_live_workflow(payload)
            logs.extend(router_logs)
            return {"status": "completed", "mode": "smart_router", "task_type": normalized_task_type, "logs": logs, "plan": raw_plan}, 200

        if normalized_task_type == HYBRID_REPLAY_TASK_TYPE:
            report("Executing Playwright fast replay with Browser Use fallback.")
            hybrid_logs = scheduler_run_playwright_browser_use_live_workflow(payload)
            logs.extend(hybrid_logs)
            return {"status": "completed", "mode": "playwright_browser_use", "task_type": normalized_task_type, "logs": logs, "plan": raw_plan}, 200

        if normalized_task_type == LEGACY_REPLAY_TASK_TYPE and isinstance(raw_plan, dict):
            report("Executing generic replay plan.")
            generic_logs = scheduler_run_generic_live_workflow(payload)
            logs.extend(generic_logs)
            return {"status": "completed", "mode": "generic", "task_type": normalized_task_type, "logs": logs, "plan": raw_plan}, 200

        handler = {
            "eia_live_workflow": scheduler_run_eia_live_workflow,
            "autoglm_live_workflow": scheduler_run_autoglm_live_workflow,
            SMART_ROUTER_TASK_TYPE: scheduler_run_smart_router_live_workflow,
            BENCHMARK_TASK_TYPE: scheduler_run_execution_adapter_benchmark,
            HYBRID_REPLAY_TASK_TYPE: scheduler_run_playwright_browser_use_live_workflow,
            SELENIUM_TASK_TYPE: scheduler_run_selenium_live_workflow,
            LEGACY_REPLAY_TASK_TYPE: scheduler_run_generic_live_workflow,
        }.get(normalized_task_type)
        if handler is None:
            raise ValueError(f"Unknown live workflow task type: {normalized_task_type}")

        report(f"Executing {normalized_task_type} live workflow.")
        handler_logs = handler(payload)
        logs.extend(handler_logs)
        return {"status": "completed", "mode": normalized_task_type, "task_type": normalized_task_type, "logs": logs, "plan": raw_plan}, 200
    except ManualTaskRequired as exc:
        combined_logs = logs + list(exc.logs or [])
        return {"status": "manual_checkpoint", "task_type": normalized_task_type, "logs": combined_logs, "message": str(exc)}, 409
