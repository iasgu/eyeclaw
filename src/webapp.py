from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from src.analyze import build_replay_plan
from src.browser_listener import (
    BrowserEventBatchIn,
    BrowserEventStore,
    choose_site_url,
    plan_listener_guided_frames,
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
from src.replay import close_replay_session, connect_over_cdp
from src.task_scheduler import TaskScheduler, register_task_handler
from src.video import extract_frames, get_video_metadata, save_uploaded_video


INDEX_HTML = Path("web/index.html")
SCHEDULER = TaskScheduler()
BROWSER_EVENT_STORE = BrowserEventStore()


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


def scheduler_run_eia_live_workflow(payload: dict) -> list[str]:
    cdp_url = payload.get("cdp_url") or DEFAULT_CDP_URL
    filter_spec = parse_eia_request(payload.get("user_request") or "")
    session = connect_over_cdp(cdp_url)
    try:
        logs = run_eia_live_workflow(session, filter_spec=filter_spec)
    finally:
        close_replay_session(session)
    return logs


register_task_handler("eia_live_workflow", scheduler_run_eia_live_workflow)


async def homepage(request: Request) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


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


async def analyze_video(request: Request) -> JSONResponse:
    status = load_config_status()
    if not status.is_ready or status.config is None:
        return JSONResponse({"error": "Configuration is incomplete."}, status_code=400)

    payload = await request.json()
    video_path = payload.get("video_path") or "website.mp4"
    user_request = payload.get("user_request") or ""
    start_second = float(payload.get("start_second", 0.0))
    end_second = payload.get("end_second")
    max_frames = int(payload.get("max_frames", 12))
    listener_session_id = payload.get("listener_session_id") or BROWSER_EVENT_STORE.latest_session_id()

    source = Path(video_path)
    if not source.exists():
        return JSONResponse({"error": f"Video file not found: {source}"}, status_code=404)

    metadata = get_video_metadata(source)
    actual_end = float(end_second) if end_second is not None else metadata.duration_seconds
    if not listener_session_id:
        return JSONResponse(
            {"error": "Listener-guided analysis requires a listener session. Start the browser listener first."},
            status_code=400,
        )

    listener_events = BROWSER_EVENT_STORE.session_events(listener_session_id)
    guided_frames = plan_listener_guided_frames(
        listener_events,
        start_second=start_second,
        end_second=actual_end,
        max_frames=max_frames,
    )
    if not guided_frames:
        return JSONResponse(
            {"error": f"No key listener events were found for session {listener_session_id}."},
            status_code=400,
        )

    timestamps = [frame.timestamp_second for frame in guided_frames]
    frame_paths = extract_frames(source, timestamps, job_id=uuid4().hex[:8])
    frame_hints = [frame.hint for frame in guided_frames[: len(frame_paths)]]
    site_url = choose_site_url(listener_events, fallback_site_url=status.config.target_site_url)
    result = build_replay_plan(
        frame_paths=frame_paths,
        config=status.config,
        user_request=user_request,
        site_url=site_url,
        frame_hints=frame_hints,
    )

    return JSONResponse(
        {
            "video_path": str(source),
            "listener_guided": True,
            "listener_session_id": listener_session_id,
            "frame_count": len(frame_paths),
            "frame_paths": [str(path) for path in frame_paths],
            "timestamps": timestamps,
            "frame_hints": frame_hints,
            "sop": result.sop,
            "plan": result.replay_bundle.plan.model_dump(),
            "assumptions": result.replay_bundle.assumptions,
            "raw_notes": result.raw_glm_output.get("uncertainties", []),
        }
    )


async def connect_live_browser(request: Request) -> JSONResponse:
    payload = await request.json()
    cdp_url = payload.get("cdp_url") or DEFAULT_CDP_URL
    data = await asyncio.to_thread(_connect_live_browser_sync, cdp_url)
    return JSONResponse(data)


async def run_live_workflow(request: Request) -> JSONResponse:
    payload = await request.json()
    cdp_url = payload.get("cdp_url") or DEFAULT_CDP_URL
    user_request = payload.get("user_request") or ""
    data, status_code = await asyncio.to_thread(_run_live_workflow_sync, cdp_url, user_request)
    return JSONResponse(data, status_code=status_code)


async def create_schedule(request: Request) -> JSONResponse:
    payload = await request.json()
    run_at_iso = payload.get("run_at_iso")
    if not run_at_iso:
        return JSONResponse({"error": "run_at_iso is required."}, status_code=400)

    task = SCHEDULER.add_task(
        name=payload.get("name") or "Scheduled browser workflow",
        run_at_iso=run_at_iso,
        task_type=payload.get("task_type") or "eia_live_workflow",
        payload={
            "cdp_url": payload.get("cdp_url") or DEFAULT_CDP_URL,
            "user_request": payload.get("user_request") or "",
        },
    )
    return JSONResponse(
        {
            "id": task.id,
            "name": task.name,
            "run_at_iso": task.run_at_iso,
            "task_type": task.task_type,
            "status": task.status,
        }
    )


async def list_schedules(request: Request) -> JSONResponse:
    tasks = SCHEDULER.list_tasks()
    return JSONResponse(
        {
            "tasks": [
                {
                    "id": task.id,
                    "name": task.name,
                    "run_at_iso": task.run_at_iso,
                    "task_type": task.task_type,
                    "status": task.status,
                    "logs": task.logs,
                    "last_error": task.last_error,
                    "created_at_iso": task.created_at_iso,
                }
                for task in tasks
            ]
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


async def analyze_browser_listener_session(request: Request) -> JSONResponse:
    status = load_config_status()
    if not status.is_ready or status.config is None:
        return JSONResponse({"error": "Configuration is incomplete."}, status_code=400)

    try:
        payload = ListenerAnalysisRequest.model_validate(await request.json())
    except ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    session_id, candidate_events = BROWSER_EVENT_STORE.select_analysis_candidates(
        session_id=payload.session_id,
        limit=payload.max_events,
    )
    if not session_id or not candidate_events:
        return JSONResponse(
            {"error": "No listener screenshots are available for analysis yet."},
            status_code=404,
        )

    frame_paths = [Path(event.screenshot_path) for event in candidate_events if event.screenshot_path]
    frame_hints = [summarize_browser_event(event) for event in candidate_events]
    site_url = choose_site_url(candidate_events, fallback_site_url=status.config.target_site_url)
    result = build_replay_plan(
        frame_paths=frame_paths,
        config=status.config,
        user_request=payload.user_request,
        site_url=site_url,
        frame_hints=frame_hints,
    )

    return JSONResponse(
        {
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
            "plan": result.replay_bundle.plan.model_dump(),
            "assumptions": result.replay_bundle.assumptions,
            "raw_notes": result.raw_glm_output.get("uncertainties", []),
        }
    )


routes = [
    Route("/", homepage),
    Route("/api/status", app_status),
    Route("/api/upload", upload_video, methods=["POST"]),
    Route("/api/analyze", analyze_video, methods=["POST"]),
    Route("/api/browser/connect", connect_live_browser, methods=["POST"]),
    Route("/api/browser/live-run", run_live_workflow, methods=["POST"]),
    Route("/api/browser-listener/status", browser_listener_status, methods=["GET"]),
    Route("/api/browser-listener/events", browser_listener_events, methods=["GET"]),
    Route("/api/browser-listener/events", ingest_browser_listener_events, methods=["POST"]),
    Route("/api/browser-listener/events/clear", clear_browser_listener_events, methods=["POST"]),
    Route("/api/browser-listener/analyze", analyze_browser_listener_session, methods=["POST"]),
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


def _run_live_workflow_sync(cdp_url: str, user_request: str) -> tuple[dict, int]:
    session = connect_over_cdp(cdp_url)
    logs: list[str] = []

    def report(message: str) -> None:
        logs.append(message)

    try:
        spec = parse_eia_request(user_request)
        run_eia_live_workflow(session, progress_callback=report, filter_spec=spec)
    except ManualCheckpointRequired as exc:
        return {"status": "manual_checkpoint", "logs": logs, "message": str(exc)}, 409
    finally:
        close_replay_session(session)

    return {"status": "completed", "logs": logs}, 200
