from __future__ import annotations

import asyncio
import json
import os
import re
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
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
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
from src.replay import close_replay_session, connect_over_cdp, run_replay_plan
from src.skill_library import SkillLibrary
from src.task_scheduler import ManualTaskRequired, TaskScheduler, register_task_handler
from src.video import extract_frames, get_video_metadata, save_uploaded_video


INDEX_HTML = Path("web/index.html")
APP_HTML = Path("web/app.html")
MCPORTER_CONFIG_PATH = Path("config/mcporter.json")
AUTOGLM_BROWSER_AGENT_NAME = "autoglm-browser-agent"
PRIMARY_EXECUTION_TASK_TYPE = "browser_use_live_workflow"
LEGACY_REPLAY_TASK_TYPE = "generic_live_workflow"
SCHEDULER = TaskScheduler()
BROWSER_EVENT_STORE = BrowserEventStore()
SKILL_LIBRARY = SkillLibrary()


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
        phase = _analysis_progress_phase(job.job_type, job.progress_percent)
        is_estimated = job.status == "running" and phase is not None and (
            display_progress != job.progress_percent or phase[2] >= 4.0
        )
        return {
            "id": job.id,
            "job_type": job.job_type,
            "status": job.status,
            "progress_percent": display_progress,
            "reported_progress_percent": job.progress_percent,
            "progress_estimated": is_estimated,
            "stage": job.stage,
            "created_at_iso": job.created_at_iso,
            "updated_at_iso": job.updated_at_iso,
            "result": job.result,
            "error": job.error,
        }


ANALYSIS_JOBS = AnalysisJobStore()


ANALYSIS_PROGRESS_PHASES: dict[str, tuple[tuple[int, int, float], ...]] = {
    "video_analysis": (
        (82, 96, 75.0),
        (66, 88, 45.0),
        (56, 72, 10.0),
        (34, 52, 6.0),
        (18, 32, 4.0),
        (6, 16, 2.0),
        (0, 5, 1.0),
    ),
    "listener_analysis": (
        (78, 96, 60.0),
        (62, 86, 35.0),
        (38, 66, 10.0),
        (10, 28, 4.0),
        (0, 6, 1.0),
    ),
}


def infer_display_progress(job: AnalysisJob) -> int:
    if job.status == "completed":
        return 100
    if job.status == "failed":
        return job.progress_percent

    phase = _analysis_progress_phase(job.job_type, job.progress_percent)
    if phase is None:
        return job.progress_percent

    base_percent, cap_percent, duration_seconds = phase
    if job.progress_percent < base_percent or cap_percent <= job.progress_percent:
        return job.progress_percent

    elapsed_seconds = max(0.0, time.monotonic() - job.stage_started_at_monotonic)
    ratio = min(1.0, elapsed_seconds / max(duration_seconds, 0.1))
    eased_ratio = 1 - (1 - ratio) ** 2
    smoothed = job.progress_percent + (cap_percent - job.progress_percent) * eased_ratio
    return max(job.progress_percent, min(cap_percent, int(round(smoothed))))


def _analysis_progress_phase(job_type: str, progress_percent: int) -> tuple[int, int, float] | None:
    phases = ANALYSIS_PROGRESS_PHASES.get(job_type)
    if not phases:
        return None
    for phase in phases:
        base_percent, _cap_percent, _duration_seconds = phase
        if progress_percent >= base_percent:
            return phase
    return None


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
    patterns = [
        re.compile(r"\bStep\s+(\d+)\b", flags=re.IGNORECASE),
        re.compile(r"\bBrowser Use agent step\s+(\d+)\b", flags=re.IGNORECASE),
    ]
    latest: int | None = None
    for log in logs:
        for pattern in patterns:
            match = pattern.search(log)
            if match:
                step_number = int(match.group(1))
                latest = step_number if latest is None else max(latest, step_number)
    return latest


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

    current_step = _extract_logged_step_number(logs)
    percent, stage = infer_live_workflow_progress(logs, fallback_stage)
    estimated = False

    status = str(getattr(task, "status", "") or "")
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
    elif status == "running":
        if total_steps and current_step is not None:
            estimated = str(getattr(task, "task_type", "") or "") == PRIMARY_EXECUTION_TASK_TYPE
            current_step = min(current_step, total_steps)
            percent = max(percent, min(95, 10 + round((current_step / max(total_steps, 1)) * 80)))
            stage = f"正在执行第 {current_step}/{total_steps} 步"
        elif current_step is not None:
            estimated = True
            percent = max(percent, min(92, 8 + current_step * 6))
            stage = f"智能执行中：第 {current_step} 轮"
        elif total_steps:
            percent = max(percent, 8)
            stage = f"正在准备 {total_steps} 步计划"

    return {
        "progress_percent": percent,
        "progress_stage": stage,
        "progress_estimated": estimated,
        "current_step": current_step,
        "total_steps": total_steps or None,
    }


def _normalize_execution_task_type(raw_task_type: str | None) -> str:
    normalized = (raw_task_type or "").strip()
    if not normalized:
        return PRIMARY_EXECUTION_TASK_TYPE
    aliases = {
        "browser_use": PRIMARY_EXECUTION_TASK_TYPE,
        "browser-use": PRIMARY_EXECUTION_TASK_TYPE,
        "browser_use_live_workflow": PRIMARY_EXECUTION_TASK_TYPE,
        "generic": LEGACY_REPLAY_TASK_TYPE,
        "replay": LEGACY_REPLAY_TASK_TYPE,
        "cdp": LEGACY_REPLAY_TASK_TYPE,
        "generic_live_workflow": LEGACY_REPLAY_TASK_TYPE,
        "autoglm": "autoglm_live_workflow",
        "autoglm_live_workflow": "autoglm_live_workflow",
        "eia": "eia_live_workflow",
        "eia_live_workflow": "eia_live_workflow",
    }
    return aliases.get(normalized.lower(), normalized)


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


def scheduler_run_browser_use_live_workflow(payload: dict, progress_callback: Callable[[str], None] | None = None) -> list[str]:
    status = load_config_status()
    if not status.is_ready or status.config is None:
        raise ValueError(f"Configuration is incomplete: {status.missing_fields}")

    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    task_text = _compose_browser_use_task(
        str(payload.get("user_request") or ""),
        plan,
        skill_names=[str(name) for name in payload.get("skill_names") or []],
        objective=str(payload.get("objective") or ""),
    )
    cdp_url = str(payload.get("cdp_url") or DEFAULT_CDP_URL).strip()
    start_url = _resolve_browser_use_start_url(plan)
    _ensure_browser_use_cdp_available(cdp_url)

    logs: list[str] = []

    def report(message: str) -> None:
        _task_progress_report(logs, progress_callback, message)

    for message in [
        f"Browser Use start_url: {start_url}",
        f"Browser Use cdp_url: {cdp_url}",
        "Browser Use is the primary execution engine for this task.",
        "Browser Use task:",
        task_text,
    ]:
        report(message)

    result = asyncio.run(
        _run_browser_use_agent(
            task_text=task_text,
            cdp_url=cdp_url,
            config=status.config,
            plan=plan,
            progress_callback=report,
        )
    )
    for message in result:
        if message not in logs:
            report(message)
    if _browser_use_result_needs_manual_checkpoint(result):
        raise ManualTaskRequired("Browser Use reported a manual checkpoint.", logs)
    return logs


async def _run_browser_use_agent(
    *,
    task_text: str,
    cdp_url: str,
    config: Any,
    plan: dict[str, Any] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> list[str]:
    _ensure_browser_use_local_config_dirs()
    from browser_use import Agent, BrowserSession, ChatOpenAI

    llm = ChatOpenAI(
        model=config.deepseek_model,
        api_key=config.deepseek_api_key,
        base_url=config.deepseek_base_url,
        temperature=0,
        max_completion_tokens=4096,
    )
    browser_session = BrowserSession(
        cdp_url=cdp_url or None,
        channel="msedge",
        downloads_path=str(Path("artifacts/downloads").resolve()),
        traces_dir=str(Path("artifacts/browser_use_traces").resolve()),
        prohibited_domains=["127.0.0.1:8018", "localhost:8018", "127.0.0.1:8021", "localhost:8021"],
        keep_alive=False,
        enable_default_extensions=False,
        captcha_solver=False,
        chromium_sandbox=False,
        no_viewport=True,
    )
    def on_new_step(browser_state_summary: Any, agent_output: Any, step_number: int) -> None:
        if progress_callback is None:
            return
        progress_callback(f"Browser Use agent step {step_number} completed.")

    agent = Agent(
        task=task_text,
        llm=llm,
        browser_session=browser_session,
        register_new_step_callback=on_new_step,
        use_vision="auto",
        max_actions_per_step=5,
        max_failures=5,
        llm_timeout=120,
        step_timeout=180,
        use_judge=False,
        enable_planning=True,
        planning_exploration_limit=3,
        enable_signal_handler=False,
        max_history_items=30,
        source="eyeclaw",
    )
    history = await agent.run(max_steps=45)
    return _summarize_browser_use_history(history)


def _ensure_browser_use_local_config_dirs() -> None:
    config_dir = Path(".browser/browser-use-config").resolve()
    profiles_dir = Path(".browser/browser-use-profiles").resolve()
    config_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    os.environ["BROWSER_USE_CONFIG_DIR"] = str(config_dir)
    os.environ["BROWSER_USE_PROFILES_DIR"] = str(profiles_dir)


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
    return logs


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
register_task_handler(LEGACY_REPLAY_TASK_TYPE, scheduler_run_generic_live_workflow)
register_task_handler(PRIMARY_EXECUTION_TASK_TYPE, scheduler_run_browser_use_live_workflow)
register_task_handler("autoglm_live_workflow", scheduler_run_autoglm_live_workflow)


async def homepage(request: Request) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


async def app_homepage(request: Request) -> HTMLResponse:
    return HTMLResponse(APP_HTML.read_text(encoding="utf-8"))


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

    report(48, f"已锁定 {len(guided_frames)} 个监听关键点...")
    timestamps = [frame.timestamp_second for frame in guided_frames]
    report(56, "正在抽取关键帧...")
    frame_paths = extract_frames(source, timestamps, job_id=uuid4().hex[:8])
    frame_hints = [frame.hint for frame in guided_frames[: len(frame_paths)]]
    site_url = choose_site_url(listener_events, fallback_site_url=status.config.target_site_url)

    report(66, f"已抽取 {len(frame_paths)} 张关键帧，准备多模态分析...")

    def report_model_progress(phase: str, current: int, total: int) -> None:
        if phase == "vision_started":
            report(70, f"正在调用多模态模型分析 {len(frame_paths)} 张截图...")
        elif phase == "vision_batch":
            batch_total = max(total, 1)
            percent = 70 + int(14 * min(current, batch_total) / batch_total)
            report(percent, f"多模态模型分析中：{current}/{batch_total} 批")
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

    report(62, f"已整理 {len(frame_paths)} 张候选截图，准备多模态分析...")

    def report_model_progress(phase: str, current: int, total: int) -> None:
        if phase == "vision_started":
            report(68, f"正在调用多模态模型分析 {len(frame_paths)} 张截图...")
        elif phase == "vision_batch":
            batch_total = max(total, 1)
            percent = 68 + int(18 * min(current, batch_total) / batch_total)
            report(percent, f"多模态模型分析中：{current}/{batch_total} 批")
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
) -> str:
    start_url = _resolve_browser_use_start_url(plan)
    raw_steps = plan.get("steps") if isinstance(plan, dict) else None
    steps = [step for step in raw_steps if isinstance(step, dict)] if isinstance(raw_steps, list) else []
    steps = _repair_misordered_location_child_steps(_filter_internal_replay_steps(steps)) if steps else []
    site_host = urlparse(start_url).netloc if start_url else ""

    lines: list[str] = [
        "# EyeClaw Browser Use Task",
        "",
        "You are the primary browser automation executor for EyeClaw.",
        "Complete the business goal by reasoning from the current page, visible text, labels, menus, URLs, and page state.",
        "Do not mechanically replay CSS nth-of-type selectors. Treat recorded selectors only as weak historical hints.",
        "",
        "## Goal",
        (objective or user_request or "Complete the selected saved skill workflow.").strip(),
    ]
    if skill_names:
        lines.extend(["", "## Selected Skills", *[f"- {name}" for name in skill_names if name]])
    if user_request and user_request.strip() and user_request.strip() != objective.strip():
        lines.extend(["", "## User Request", user_request.strip()])

    lines.extend(
        [
            "",
            "## Start",
            f"- Start URL: {start_url}",
            f"- Target host: {site_host or '(infer from start URL)'}",
            "- If the browser is currently on an EyeClaw console page such as 127.0.0.1:8018, navigate away to the Start URL before doing business actions.",
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
            "- If a dropdown or cascader option is missing, reopen the dropdown and scroll the option list; for province/city cascaders, choose the parent area before the child area.",
            "- If a normal click fails, try keyboard navigation with Tab, ArrowDown, Enter, or a nearby visible label/control.",
            "- If the current page does not match the workflow, navigate back to the Start URL and continue from the closest valid state.",
            "- If login, QR code, SMS code, captcha, or human confirmation appears, stop and report that a manual checkpoint is required.",
            "- Prefer task success over exact step order when the page state already satisfies an earlier step.",
            "",
            "## Success Criteria",
            "- The requested business workflow is completed on the target website, not inside the EyeClaw console.",
            "- Important selected filters, opened items, downloads, saves, or final page state match the goal.",
            "- If the workflow cannot be completed, clearly explain the blocking page state and whether manual action is needed.",
            "",
            "## Final Answer",
            "When finished, call done with a concise summary containing: success, final_url, selected_filters, opened_item_title, downloaded_or_saved, manual_checkpoint_required, and errors.",
        ]
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
        for step in getattr(skill, "steps", []) or []:
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
            "task_type": task_type,
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
        },
        status_code=status_code,
    )


def _serialize_task(task: Any) -> dict[str, Any]:
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
    }
    payload.update(infer_task_execution_snapshot(task))
    return payload


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
            "task_type": task_type,
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
    tasks = SCHEDULER.list_tasks()
    return JSONResponse(
        {
            "tasks": [_serialize_task(task) for task in tasks]
        }
    )


async def app_status(request: Request) -> JSONResponse:
    status = load_config_status()
    return JSONResponse(
        {
            "config_ready": status.is_ready,
            "missing_fields": status.missing_fields,
            "target_site_url": status.config.target_site_url if status.config else None,
            "glm_model": status.config.glm_model if status.config else None,
            "deepseek_model": status.config.deepseek_model if status.config else None,
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
        if event_type not in {"click", "change", "input", "navigation", "history", "tab_updated", "page_loaded", "scroll"}:
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
    Route("/api/skills", list_skills, methods=["GET"]),
    Route("/api/skills", create_skill, methods=["POST"]),
    Route("/api/skills/{skill_id}", update_skill, methods=["PATCH"]),
    Route("/api/skills/{skill_id}", delete_skill, methods=["DELETE"]),
    Route("/api/tasks/run-now", run_user_task_now, methods=["POST"]),
    Route("/api/tasks/run-now/start", start_user_task_now, methods=["POST"]),
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

        if normalized_task_type == LEGACY_REPLAY_TASK_TYPE and isinstance(raw_plan, dict):
            report("Executing generic replay plan.")
            generic_logs = scheduler_run_generic_live_workflow(payload)
            logs.extend(generic_logs)
            return {"status": "completed", "mode": "generic", "task_type": normalized_task_type, "logs": logs, "plan": raw_plan}, 200

        handler = {
            "eia_live_workflow": scheduler_run_eia_live_workflow,
            "autoglm_live_workflow": scheduler_run_autoglm_live_workflow,
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
