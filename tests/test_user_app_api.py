import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace

import pytest

import src.webapp as webapp_module
import src.task_scheduler as task_scheduler_module
from src.skill_library import SkillLibrary
from src.task_scheduler import ManualTaskRequired, TaskScheduler, register_task_handler


class DummyRequest:
    def __init__(
        self,
        payload: dict | None = None,
        query_params: dict | None = None,
        path_params: dict | None = None,
    ):
        self._payload = payload or {}
        self.query_params = query_params or {}
        self.path_params = path_params or {}

    async def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def isolate_task_run_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(task_scheduler_module, "TASK_RUN_ARTIFACT_ROOT", tmp_path / "task_runs")


def test_create_and_list_skills_route() -> None:
    original_library = webapp_module.SKILL_LIBRARY

    with TemporaryDirectory() as temp_dir:
        webapp_module.SKILL_LIBRARY = SkillLibrary(Path(temp_dir) / "skills.json")
        try:
            create_response = asyncio.run(
                webapp_module.create_skill(
                    DummyRequest(
                        {
                            "name": "报告筛选技能",
                            "description": "用于筛选并查看报告",
                            "source_type": "listener_analysis",
                            "site_url": "https://example.com/report",
                            "steps": [
                                {"step_number": 1, "action": "click", "target": "进入模块"},
                                {"step_number": 2, "action": "change", "target": "选择条件"},
                            ],
                        }
                    )
                )
            )
            list_response = asyncio.run(webapp_module.list_skills(DummyRequest()))
        finally:
            webapp_module.SKILL_LIBRARY = original_library

    assert create_response.status_code == 201
    create_payload = json.loads(create_response.body.decode("utf-8"))
    assert create_payload["name"] == "报告筛选技能"
    assert create_payload["site_url"] == "https://example.com/report"
    assert create_payload["steps"][0]["target"] == "进入模块"

    list_payload = json.loads(list_response.body.decode("utf-8"))
    assert len(list_payload["skills"]) == 1
    assert list_payload["skills"][0]["description"] == "用于筛选并查看报告"


def test_list_execution_adapters_route_exposes_enabled_and_pending_adapters() -> None:
    response = asyncio.run(webapp_module.list_execution_adapters(DummyRequest()))

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    adapters = {adapter["id"]: adapter for adapter in payload["adapters"]}
    assert payload["default_task_type"] == webapp_module.SMART_ROUTER_TASK_TYPE
    assert adapters["browser_use"]["enabled"] is True
    assert adapters["browser_use"]["task_type"] == webapp_module.PRIMARY_EXECUTION_TASK_TYPE
    assert adapters["smart_router"]["enabled"] is True
    assert adapters["smart_router"]["task_type"] == webapp_module.SMART_ROUTER_TASK_TYPE
    assert adapters["playwright"]["enabled"] is True
    assert adapters["playwright"]["task_type"] == webapp_module.HYBRID_REPLAY_TASK_TYPE
    assert adapters["skyvern"]["enabled"] is False
    assert adapters["skyvern"]["disabled_reason"]


def test_execution_adapter_aliases_normalize_to_existing_task_types() -> None:
    assert webapp_module._normalize_execution_task_type("smart") == webapp_module.SMART_ROUTER_TASK_TYPE
    assert webapp_module._normalize_execution_task_type("benchmark") == webapp_module.BENCHMARK_TASK_TYPE
    assert webapp_module._normalize_execution_task_type("playwright") == webapp_module.HYBRID_REPLAY_TASK_TYPE
    assert webapp_module._normalize_execution_task_type("selenium") == webapp_module.SELENIUM_TASK_TYPE
    assert webapp_module._normalize_execution_task_type("cdp_replay") == webapp_module.LEGACY_REPLAY_TASK_TYPE
    assert webapp_module._normalize_execution_task_type("browser_use") == webapp_module.PRIMARY_EXECUTION_TASK_TYPE
    assert webapp_module._normalize_execution_task_type("autoglm_browser_agent") == "autoglm_live_workflow"


def test_update_and_delete_skill_route() -> None:
    original_library = webapp_module.SKILL_LIBRARY

    with TemporaryDirectory() as temp_dir:
        webapp_module.SKILL_LIBRARY = SkillLibrary(Path(temp_dir) / "skills.json")
        saved_skill = webapp_module.SKILL_LIBRARY.create_skill(
            name="旧技能名",
            description="旧说明",
            source_type="analysis",
            site_url="https://example.com/old",
            steps=[{"step_number": 1, "action": "click", "target": "旧按钮"}],
        )
        try:
            update_response = asyncio.run(
                webapp_module.update_skill(
                    DummyRequest(
                        {"name": "新技能名", "description": "新说明", "site_url": "https://example.com/new"},
                        path_params={"skill_id": saved_skill.id},
                    )
                )
            )
            delete_response = asyncio.run(
                webapp_module.delete_skill(DummyRequest(path_params={"skill_id": saved_skill.id}))
            )
            list_response = asyncio.run(webapp_module.list_skills(DummyRequest()))
        finally:
            webapp_module.SKILL_LIBRARY = original_library

    assert update_response.status_code == 200
    update_payload = json.loads(update_response.body.decode("utf-8"))
    assert update_payload["name"] == "新技能名"
    assert update_payload["description"] == "新说明"
    assert update_payload["site_url"] == "https://example.com/new"

    assert delete_response.status_code == 200
    delete_payload = json.loads(delete_response.body.decode("utf-8"))
    assert delete_payload["deleted"] is True
    assert delete_payload["skill"]["id"] == saved_skill.id

    list_payload = json.loads(list_response.body.decode("utf-8"))
    assert list_payload["skills"] == []


def test_run_user_task_now_uses_selected_skills() -> None:
    original_library = webapp_module.SKILL_LIBRARY
    original_scheduler = webapp_module.SCHEDULER
    captured_payloads: list[dict] = []

    def fake_handler(payload: dict) -> list[str]:
        captured_payloads.append(payload)
        return ["ok", payload["user_request"]]

    register_task_handler("fake_task_test", fake_handler)

    with TemporaryDirectory() as temp_dir:
        webapp_module.SKILL_LIBRARY = SkillLibrary(Path(temp_dir) / "skills.json")
        webapp_module.SCHEDULER = TaskScheduler()
        saved_skill = webapp_module.SKILL_LIBRARY.create_skill(
            name="导出技能",
            description="导出筛选结果",
            source_type="video_analysis",
            site_url="https://example.com/export",
            steps=[
                {"step_number": 1, "action": "click", "target": "筛选"},
                {"step_number": 2, "action": "click", "target": "导出"},
            ],
        )
        try:
            response = asyncio.run(
                webapp_module.run_user_task_now(
                    DummyRequest(
                        {
                            "name": "立刻执行导出",
                            "objective": "请导出今天的数据",
                            "cdp_url": "http://127.0.0.1:9222",
                            "task_type": "fake_task_test",
                            "skill_ids": [saved_skill.id],
                        }
                    )
                )
            )
        finally:
            webapp_module.SKILL_LIBRARY = original_library
            webapp_module.SCHEDULER = original_scheduler

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["status"] == "completed"
    assert payload["skill_names"] == ["导出技能"]
    assert captured_payloads
    assert "请导出今天的数据" in captured_payloads[0]["user_request"]
    assert "导出技能" in captured_payloads[0]["user_request"]
    assert captured_payloads[0]["plan"]["site_url"] == "https://example.com/export"
    assert [step["step_number"] for step in captured_payloads[0]["plan"]["steps"]] == [1, 2]
    assert captured_payloads[0]["acceptance_criteria"]["recorded_step_count"] == 2
    assert captured_payloads[0]["acceptance_criteria"]["goal"]


def test_start_user_task_now_returns_progress_and_task_status() -> None:
    original_library = webapp_module.SKILL_LIBRARY
    original_scheduler = webapp_module.SCHEDULER

    def progress_handler(payload: dict, progress_callback=None) -> list[str]:
        if progress_callback is not None:
            progress_callback("Browser Use start_url: https://example.com")
            progress_callback("Browser Use agent step 1 completed.")
            progress_callback("Browser Use agent step 2 completed.")
        return [
            "Browser Use start_url: https://example.com",
            "Browser Use agent step 1 completed.",
            "Browser Use agent step 2 completed.",
            "final_result: ok",
        ]

    register_task_handler("progress_task_test", progress_handler)

    with TemporaryDirectory() as temp_dir:
        webapp_module.SKILL_LIBRARY = SkillLibrary(Path(temp_dir) / "skills.json")
        webapp_module.SCHEDULER = TaskScheduler()
        saved_skill = webapp_module.SKILL_LIBRARY.create_skill(
            name="进度技能",
            description="",
            source_type="analysis",
            site_url="https://example.com/progress",
            steps=[
                {"step_number": 1, "action": "click", "target": "第一步"},
                {"step_number": 2, "action": "click", "target": "第二步"},
            ],
        )
        try:
            start_response = asyncio.run(
                webapp_module.start_user_task_now(
                    DummyRequest(
                        {
                            "name": "后台执行进度任务",
                            "objective": "执行两步流程",
                            "cdp_url": "http://127.0.0.1:9222",
                            "task_type": "progress_task_test",
                            "skill_ids": [saved_skill.id],
                        }
                    )
                )
            )
            start_payload = json.loads(start_response.body.decode("utf-8"))

            task_payload = {}
            for _ in range(20):
                status_response = asyncio.run(
                    webapp_module.get_task_status(DummyRequest(path_params={"task_id": start_payload["id"]}))
                )
                task_payload = json.loads(status_response.body.decode("utf-8"))
                if task_payload["status"] != "running":
                    break
                time.sleep(0.02)
        finally:
            webapp_module.SKILL_LIBRARY = original_library
            webapp_module.SCHEDULER = original_scheduler

    assert start_response.status_code == 202
    assert start_payload["status"] in {"running", "completed"}
    assert task_payload["status"] == "completed"
    assert task_payload["current_step"] == 2
    assert task_payload["total_steps"] == 2
    assert task_payload["progress_percent"] == 100
    assert "Browser Use agent step 2 completed." in task_payload["logs"]


def test_create_schedule_supports_manual_frequency_without_run_at() -> None:
    original_library = webapp_module.SKILL_LIBRARY
    original_scheduler = webapp_module.SCHEDULER

    with TemporaryDirectory() as temp_dir:
        webapp_module.SKILL_LIBRARY = SkillLibrary(Path(temp_dir) / "skills.json")
        webapp_module.SCHEDULER = TaskScheduler()
        try:
            response = asyncio.run(
                webapp_module.create_schedule(
                    DummyRequest(
                        {
                            "name": "手动任务",
                            "objective": "需要时再执行",
                            "frequency": "manual",
                            "skill_ids": [],
                        }
                    )
                )
            )
            list_response = asyncio.run(webapp_module.list_schedules(DummyRequest()))
        finally:
            webapp_module.SKILL_LIBRARY = original_library
            webapp_module.SCHEDULER = original_scheduler

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["frequency"] == "manual"
    assert payload["status"] == "manual"
    assert payload["task_type"] == "browser_use_live_workflow"

    list_payload = json.loads(list_response.body.decode("utf-8"))
    assert list_payload["tasks"][0]["frequency"] == "manual"
    assert list_payload["tasks"][0]["task_type"] == "browser_use_live_workflow"


def test_create_schedule_preserves_console_plan_for_browser_use() -> None:
    original_library = webapp_module.SKILL_LIBRARY
    original_scheduler = webapp_module.SCHEDULER

    plan = {
        "site_url": "https://example.com/start",
        "steps": [
            {"step_number": 1, "action": "click", "target": "开始"},
        ],
    }

    with TemporaryDirectory() as temp_dir:
        webapp_module.SKILL_LIBRARY = SkillLibrary(Path(temp_dir) / "skills.json")
        webapp_module.SCHEDULER = TaskScheduler()
        try:
            response = asyncio.run(
                webapp_module.create_schedule(
                    DummyRequest(
                        {
                            "name": "Browser Use 定时任务",
                            "objective": "执行控制台分析出的步骤",
                            "frequency": "manual",
                            "task_type": "browser_use",
                            "plan": plan,
                            "skill_ids": [],
                        }
                    )
                )
            )
            list_response = asyncio.run(webapp_module.list_schedules(DummyRequest()))
        finally:
            webapp_module.SKILL_LIBRARY = original_library
            webapp_module.SCHEDULER = original_scheduler

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["task_type"] == "browser_use_live_workflow"

    list_payload = json.loads(list_response.body.decode("utf-8"))
    task_payload = list_payload["tasks"][0]["payload"]
    assert task_payload["task_type"] == "browser_use_live_workflow"
    assert task_payload["plan"] == plan


def test_run_user_task_now_returns_409_for_manual_checkpoint() -> None:
    original_library = webapp_module.SKILL_LIBRARY
    original_scheduler = webapp_module.SCHEDULER

    def manual_handler(payload: dict) -> list[str]:
        raise ManualTaskRequired("请先扫码登录", ["Detected page state: login_modal", "Manual checkpoint required: 请先扫码登录"])

    register_task_handler("manual_task_test", manual_handler)

    with TemporaryDirectory() as temp_dir:
        webapp_module.SKILL_LIBRARY = SkillLibrary(Path(temp_dir) / "skills.json")
        webapp_module.SCHEDULER = TaskScheduler()
        try:
            response = asyncio.run(
                webapp_module.run_user_task_now(
                    DummyRequest(
                        {
                            "name": "立即执行需要人工处理的任务",
                            "objective": "触发扫码登录",
                            "cdp_url": "http://127.0.0.1:9222",
                            "task_type": "manual_task_test",
                            "skill_ids": [],
                        }
                    )
                )
            )
        finally:
            webapp_module.SKILL_LIBRARY = original_library
            webapp_module.SCHEDULER = original_scheduler

    assert response.status_code == 409
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["status"] == "manual"
    assert payload["last_error"] == "请先扫码登录"
    assert "Manual checkpoint required" in payload["logs"][-1]


def test_start_user_task_now_returns_progress_updates() -> None:
    original_library = webapp_module.SKILL_LIBRARY
    original_scheduler = webapp_module.SCHEDULER

    def progress_handler(payload: dict, progress_callback=None) -> list[str]:
        if progress_callback is not None:
            progress_callback("Browser Use agent step 1 completed.")
        time.sleep(0.05)
        if progress_callback is not None:
            progress_callback("Browser Use result summary:")
        return ["Browser Use agent step 1 completed.", "Browser Use result summary:", "final_result: ok"]

    register_task_handler("progress_task_test", progress_handler)

    with TemporaryDirectory() as temp_dir:
        webapp_module.SKILL_LIBRARY = SkillLibrary(Path(temp_dir) / "skills.json")
        webapp_module.SCHEDULER = TaskScheduler()
        saved_skill = webapp_module.SKILL_LIBRARY.create_skill(
            name="进度技能",
            description="",
            source_type="analysis",
            site_url="https://example.com/progress",
            steps=[
                {"step_number": 1, "action": "click", "target": "Open"},
                {"step_number": 2, "action": "click", "target": "Save"},
            ],
        )
        try:
            start_response = asyncio.run(
                webapp_module.start_user_task_now(
                    DummyRequest(
                        {
                            "name": "后台执行任务",
                            "objective": "执行进度测试",
                            "cdp_url": "http://127.0.0.1:9222",
                            "task_type": "progress_task_test",
                            "skill_ids": [saved_skill.id],
                        }
                    )
                )
            )
            start_payload = json.loads(start_response.body.decode("utf-8"))

            final_payload = None
            for _ in range(20):
                status_response = asyncio.run(
                    webapp_module.get_task_status(DummyRequest(path_params={"task_id": start_payload["id"]}))
                )
                final_payload = json.loads(status_response.body.decode("utf-8"))
                if final_payload["status"] != "running":
                    break
                time.sleep(0.02)
        finally:
            webapp_module.SKILL_LIBRARY = original_library
            webapp_module.SCHEDULER = original_scheduler

    assert start_response.status_code == 202
    assert start_payload["status"] in {"running", "completed"}
    assert final_payload is not None
    assert final_payload["status"] == "completed"
    assert final_payload["progress_percent"] == 100
    assert final_payload["current_step"] == 2
    assert final_payload["total_steps"] == 2
    assert "Browser Use agent step 1 completed." in final_payload["logs"]


def test_browser_use_running_progress_is_reported_as_live_activity() -> None:
    now = datetime.now(timezone.utc)
    task = SimpleNamespace(
        status="running",
        task_type=webapp_module.PRIMARY_EXECUTION_TASK_TYPE,
        logs=["Browser Use agent step 4 completed."],
        payload={"plan": {"steps": [{"step_number": 1}, {"step_number": 2}]}},
        last_error=None,
        created_at_iso=(now - timedelta(seconds=14)).isoformat(),
        last_run_at_iso=(now - timedelta(seconds=12)).isoformat(),
        progress_events=[
            {
                "message": "Browser Use agent step 4 completed.",
                "timestamp_iso": (now - timedelta(seconds=3)).isoformat(),
            }
        ],
    )

    snapshot = webapp_module.infer_task_execution_snapshot(task)

    assert snapshot["progress_mode"] == "activity"
    assert snapshot["progress_percent"] == 0
    assert snapshot["browser_round"] == 4
    assert snapshot["planned_step_count"] == 2
    assert snapshot["elapsed_seconds"] >= 12
    assert snapshot["last_event_age_seconds"] >= 3
    assert "4 轮" in snapshot["progress_stage"]


def test_fast_preflight_progress_is_not_reported_as_browser_use_round() -> None:
    now = datetime.now(timezone.utc)
    task = SimpleNamespace(
        status="running",
        task_type=webapp_module.PRIMARY_EXECUTION_TASK_TYPE,
        logs=[
            "Fast preflight: replaying 6 recorded skill steps with local CDP.",
            "Step 3: click -> 公告信息",
        ],
        payload={"plan": {"steps": [{"step_number": 1}, {"step_number": 2}, {"step_number": 3}]}},
        last_error=None,
        created_at_iso=(now - timedelta(seconds=9)).isoformat(),
        last_run_at_iso=(now - timedelta(seconds=8)).isoformat(),
        progress_events=[
            {
                "message": "Step 3: click -> 公告信息",
                "timestamp_iso": (now - timedelta(seconds=2)).isoformat(),
            }
        ],
    )

    snapshot = webapp_module.infer_task_execution_snapshot(task)

    assert snapshot["progress_mode"] == "activity"
    assert snapshot["progress_percent"] == 0
    assert snapshot["browser_round"] is None
    assert snapshot["current_step"] == 3
    assert "快速执行已完成 3 步" in snapshot["progress_stage"]


def test_cancel_running_user_task_marks_it_cancelled() -> None:
    original_library = webapp_module.SKILL_LIBRARY
    original_scheduler = webapp_module.SCHEDULER

    def cancellable_handler(payload: dict, progress_callback=None) -> list[str]:
        time.sleep(0.1)
        if progress_callback is not None:
            progress_callback("Browser Use agent step 1 completed.")
        return ["should not complete"]

    register_task_handler("cancellable_task_test", cancellable_handler)

    with TemporaryDirectory() as temp_dir:
        webapp_module.SKILL_LIBRARY = SkillLibrary(Path(temp_dir) / "skills.json")
        webapp_module.SCHEDULER = TaskScheduler()
        try:
            start_response = asyncio.run(
                webapp_module.start_user_task_now(
                    DummyRequest(
                        {
                            "name": "可停止任务",
                            "objective": "测试停止",
                            "cdp_url": "http://127.0.0.1:9222",
                            "task_type": "cancellable_task_test",
                            "skill_ids": [],
                        }
                    )
                )
            )
            start_payload = json.loads(start_response.body.decode("utf-8"))
            cancel_response = asyncio.run(
                webapp_module.cancel_task(DummyRequest(path_params={"task_id": start_payload["id"]}))
            )
            cancel_payload = json.loads(cancel_response.body.decode("utf-8"))

            final_payload = cancel_payload
            for _ in range(30):
                status_response = asyncio.run(
                    webapp_module.get_task_status(DummyRequest(path_params={"task_id": start_payload["id"]}))
                )
                final_payload = json.loads(status_response.body.decode("utf-8"))
                if final_payload["status"] not in {"running", "cancelling"}:
                    break
                time.sleep(0.02)
        finally:
            webapp_module.SKILL_LIBRARY = original_library
            webapp_module.SCHEDULER = original_scheduler

    assert start_response.status_code == 202
    assert cancel_response.status_code in {200, 202}
    assert cancel_payload["status"] in {"cancelling", "cancelled"}
    assert final_payload["status"] == "cancelled"
    assert final_payload["cancel_requested"] is True
    assert "Task cancelled by user." in final_payload["logs"][-1]


def test_scheduler_limits_concurrent_tasks_to_three() -> None:
    active_lock = Lock()
    release = Event()
    active_count = 0
    max_seen = 0

    def limited_handler(payload: dict, progress_callback=None) -> list[str]:
        nonlocal active_count, max_seen
        with active_lock:
            active_count += 1
            max_seen = max(max_seen, active_count)
        try:
            if progress_callback is not None:
                progress_callback(f"started task {payload['index']}")
            release.wait(timeout=2.0)
            return [f"completed task {payload['index']}"]
        finally:
            with active_lock:
                active_count -= 1

    register_task_handler("limited_concurrency_task_test", limited_handler)
    scheduler = TaskScheduler(max_concurrent_tasks=3)
    tasks = [
        scheduler.create_and_start_task(
            name=f"任务 {index}",
            task_type="limited_concurrency_task_test",
            payload={"index": index},
        )
        for index in range(5)
    ]

    try:
        queued_count = 0
        for _ in range(50):
            statuses = [scheduler.get_task(task.id).status for task in tasks if scheduler.get_task(task.id)]
            queued_count = statuses.count("queued")
            if max_seen == 3 and queued_count == 2:
                break
            time.sleep(0.02)

        assert max_seen == 3
        assert queued_count == 2
    finally:
        release.set()

    final_statuses = []
    for _ in range(80):
        final_statuses = [scheduler.get_task(task.id).status for task in tasks if scheduler.get_task(task.id)]
        if all(status == "completed" for status in final_statuses):
            break
        time.sleep(0.02)

    assert all(status == "completed" for status in final_statuses)
    assert max_seen <= 3


def test_completed_task_exposes_only_final_deliverables(tmp_path) -> None:
    original_library = webapp_module.SKILL_LIBRARY
    original_scheduler = webapp_module.SCHEDULER
    original_artifact_root = task_scheduler_module.TASK_RUN_ARTIFACT_ROOT

    def report_handler(payload: dict, progress_callback=None) -> list[str]:
        if progress_callback is not None:
            progress_callback("Browser Use agent step 1 completed.")
        downloads_dir = Path(payload["downloads_path"])
        downloads_dir.mkdir(parents=True, exist_ok=True)
        (downloads_dir / "result.txt").write_text("downloaded result", encoding="utf-8")
        return [
            "final_result: visited https://example.com/list then final_url: https://example.com/result",
            "urls: ['https://example.com/list', 'https://example.com/result']",
            "final_url: https://example.com/result",
        ]

    register_task_handler("report_task_test", report_handler)
    task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = tmp_path / "task_runs"

    with TemporaryDirectory() as temp_dir:
        webapp_module.SKILL_LIBRARY = SkillLibrary(Path(temp_dir) / "skills.json")
        webapp_module.SCHEDULER = TaskScheduler()
        try:
            start_response = asyncio.run(
                webapp_module.start_user_task_now(
                    DummyRequest(
                        {
                            "name": "交付物任务",
                            "objective": "生成报告",
                            "cdp_url": "http://127.0.0.1:9222",
                            "task_type": "report_task_test",
                            "skill_ids": [],
                        }
                    )
                )
            )
            start_payload = json.loads(start_response.body.decode("utf-8"))

            final_payload = None
            for _ in range(60):
                status_response = asyncio.run(
                    webapp_module.get_task_status(DummyRequest(path_params={"task_id": start_payload["id"]}))
                )
                final_payload = json.loads(status_response.body.decode("utf-8"))
                if final_payload["status"] == "completed" and final_payload["deliverables"]:
                    break
                time.sleep(0.02)

            report_response = asyncio.run(
                webapp_module.download_task_artifact(
                    DummyRequest(path_params={"task_id": start_payload["id"], "filename": "report.md"})
                )
            )
            download_response = asyncio.run(
                webapp_module.download_task_artifact(
                    DummyRequest(path_params={"task_id": start_payload["id"], "filename": "downloads/result.txt"})
                )
            )
        finally:
            webapp_module.SKILL_LIBRARY = original_library
            webapp_module.SCHEDULER = original_scheduler
            task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = original_artifact_root

    assert start_response.status_code == 202
    assert final_payload is not None
    assert final_payload["status"] == "completed"
    assert len(final_payload["progress_events"]) >= 3
    file_deliverables = [item for item in final_payload["deliverables"] if item.get("kind") == "file"]
    link_deliverables = [item for item in final_payload["deliverables"] if item.get("kind") == "link"]
    assert {item["filename"] for item in file_deliverables} == {"downloads/result.txt"}
    assert {item["label"] for item in file_deliverables} == {"result.txt"}
    assert [item["url"] for item in link_deliverables] == ["https://example.com/result"]
    assert "交付物任务" in Path(final_payload["artifact_dir"], "report.md").read_text(encoding="utf-8")
    assert report_response.status_code == 400
    assert download_response.status_code == 200


def test_task_deliverables_ignore_browser_history_urls_without_final_url(tmp_path) -> None:
    original_scheduler = webapp_module.SCHEDULER
    original_artifact_root = task_scheduler_module.TASK_RUN_ARTIFACT_ROOT

    def report_handler(payload: dict, progress_callback=None) -> list[str]:
        return [
            "Browser Use result summary:",
            "final_result: None",
            "is_done: False",
            "urls: ['https://example.com/list', 'https://example.com/preview']",
        ]

    register_task_handler("history_url_only_task_test", report_handler)
    task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = tmp_path / "task_runs"

    webapp_module.SCHEDULER = TaskScheduler()
    try:
        task = webapp_module.SCHEDULER.create_and_run_task(
            name="history url only",
            task_type="history_url_only_task_test",
            payload={},
        )
    finally:
        webapp_module.SCHEDULER = original_scheduler
        task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = original_artifact_root

    assert task.status == "completed"
    assert task.deliverables == []


def test_task_deliverables_prefer_verified_plan_final_url() -> None:
    scheduler = TaskScheduler()
    snapshot = {
        "status": "completed",
        "logs": [
            "Browser Use result summary:",
            "final_result: Workflow completed. Opened https://example.com/#/previewPdf?id=7",
            "is_done: True",
            "is_successful: True",
            "urls: ['https://example.com/#/', 'https://example.com/#/list', 'https://example.com/#/previewPdf?id=7']",
            "final_url: https://example.com/#/list",
        ],
        "payload": {
            "plan": {
                "site_url": "https://example.com/#/",
                "steps": [
                    {"step_number": 1, "action": "wait", "target": "https://example.com/#/list"},
                    {"step_number": 2, "action": "wait", "target": "https://example.com/#/previewPdf"},
                ],
            }
        },
    }

    deliverables = scheduler._extract_web_link_deliverables(snapshot)

    assert [item["url"] for item in deliverables] == ["https://example.com/#/previewPdf?id=7"]


def test_task_deliverables_suppress_unverified_final_url_for_planned_preview() -> None:
    scheduler = TaskScheduler()
    snapshot = {
        "status": "completed",
        "logs": [
            "final_result: 已筛选结果，但尚未点击预览。",
            "is_done: True",
            "is_successful: False",
            "final_url: https://example.com/#/list",
        ],
        "payload": {
            "plan": {
                "steps": [
                    {"step_number": 1, "action": "wait", "target": "https://example.com/#/previewPdf"},
                ],
            }
        },
    }

    assert scheduler._extract_web_link_deliverables(snapshot) == []


def test_task_deliverables_do_not_expose_start_url_as_final_link() -> None:
    scheduler = TaskScheduler()
    snapshot = {
        "status": "completed",
        "logs": [
            "Browser Use start_url: https://example.com/#/",
            "Browser Use result summary:",
            "is_done: True",
            "is_successful: True",
            "urls: ['https://example.com/#/', 'https://example.com/#/']",
            "final_url: https://example.com/#/",
        ],
        "payload": {
            "plan": {
                "site_url": "https://example.com/#/",
            }
        },
    }

    assert scheduler._extract_web_link_deliverables(snapshot) == []


def test_task_deliverables_use_last_non_start_history_url_when_current_page_returns_start() -> None:
    scheduler = TaskScheduler()
    snapshot = {
        "status": "completed",
        "logs": [
            "Browser Use start_url: https://example.com/#/",
            "Browser Use result summary:",
            "is_done: True",
            "is_successful: True",
            "urls: ['https://example.com/#/', 'https://example.com/#/result?id=1']",
            "final_url: https://example.com/#/",
        ],
        "payload": {
            "plan": {
                "site_url": "https://example.com/#/",
            }
        },
    }

    deliverables = scheduler._extract_web_link_deliverables(snapshot)

    assert [item["url"] for item in deliverables] == ["https://example.com/#/result?id=1"]


def test_completed_task_exposes_token_usage(tmp_path) -> None:
    def token_handler(payload: dict, progress_callback=None) -> list[str]:
        return [
            'token_usage: {"prompt_tokens":100,"completion_tokens":25,"total_tokens":125,'
            '"prompt_cached_tokens":10,"entry_count":2,'
            '"by_model":{"glm-5v-turbo":{"prompt_tokens":100,"completion_tokens":25,'
            '"total_tokens":125,"prompt_cached_tokens":10,"invocations":2}}}',
        ]

    register_task_handler("token_usage_task_test", token_handler)
    scheduler = TaskScheduler()
    task = scheduler.create_and_run_task(
        name="Token usage task",
        task_type="token_usage_task_test",
        payload={},
    )

    payload = webapp_module._serialize_task(task)
    run_json = json.loads((tmp_path / "task_runs" / task.id / "run.json").read_text(encoding="utf-8"))

    assert payload["token_usage"]["total_tokens"] == 125
    assert payload["token_usage"]["prompt_tokens"] == 100
    assert payload["token_usage"]["completion_tokens"] == 25
    assert payload["token_usage"]["entry_count"] == 2
    assert run_json["token_usage"]["total_tokens"] == 125


def test_serialize_task_prefers_token_usage_recovered_from_logs() -> None:
    task = SimpleNamespace(
        id="token-recovery",
        name="Token recovery",
        run_at_iso="2026-05-29T00:00:00+00:00",
        task_type="browser_use_live_workflow",
        frequency="once",
        status="completed",
        logs=[
            'token_usage: {"prompt_tokens":12,"completion_tokens":3,"total_tokens":15,"entry_count":1}'
        ],
        last_error=None,
        created_at_iso="2026-05-29T00:00:00+00:00",
        run_count=1,
        last_run_at_iso="2026-05-29T00:01:00+00:00",
        payload={},
        cancel_requested=False,
        cancelled_at_iso=None,
        progress_events=[],
        deliverables=[],
        artifact_dir="D:\\Codex\\liangzhu\\artifacts\\task_runs\\token-recovery",
        benchmark_result=None,
        token_usage={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "prompt_cached_tokens": 0,
            "entry_count": 0,
            "cost": 0.0,
            "by_model": {},
        },
    )

    payload = webapp_module._serialize_task(task)

    assert payload["token_usage"]["total_tokens"] == 15
    assert payload["token_usage"]["entry_count"] == 1


def test_browser_use_history_summary_logs_token_usage() -> None:
    usage = SimpleNamespace(
        model_dump=lambda: {
            "total_prompt_tokens": 80,
            "total_prompt_cached_tokens": 5,
            "total_completion_tokens": 20,
            "total_tokens": 100,
            "total_cost": 0.001,
            "entry_count": 1,
            "by_model": {
                "glm-5v-turbo": {
                    "model": "glm-5v-turbo",
                    "prompt_tokens": 80,
                    "completion_tokens": 20,
                    "total_tokens": 100,
                    "cost": 0.001,
                    "invocations": 1,
                }
            },
        }
    )
    history = SimpleNamespace(usage=usage)

    logs = webapp_module._summarize_browser_use_history(history)
    token_line = next(line for line in logs if line.startswith("token_usage:"))
    token_usage = json.loads(token_line.split(":", 1)[1].strip())

    assert token_usage["total_tokens"] == 100
    assert token_usage["prompt_tokens"] == 80
    assert token_usage["completion_tokens"] == 20
    assert token_usage["entry_count"] == 1


def test_scheduler_restores_completed_task_history_from_artifacts(tmp_path) -> None:
    original_artifact_root = task_scheduler_module.TASK_RUN_ARTIFACT_ROOT
    task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = tmp_path / "task_runs"
    task_dir = task_scheduler_module.TASK_RUN_ARTIFACT_ROOT / "history123"
    task_dir.mkdir(parents=True)
    (task_dir / "run.json").write_text(
        json.dumps(
            {
                "id": "history123",
                "name": "历史下载任务",
                "run_at_iso": "2026-05-27T08:00:00+00:00",
                "task_type": "browser_use_live_workflow",
                "frequency": "once",
                "status": "completed",
                "logs": ["final_url: https://example.com/result"],
                "last_error": None,
                "created_at_iso": "2026-05-27T07:59:00+00:00",
                "run_count": 1,
                "last_run_at_iso": "2026-05-27T08:00:00+00:00",
                "payload": {"objective": "下载结果"},
                "progress_events": [],
                "deliverables": [
                    {
                        "kind": "link",
                        "label": "最终网页: example.com",
                        "filename": "",
                        "url": "https://example.com/result",
                        "download_url": "https://example.com/result",
                    }
                ],
                "downloaded_files": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    try:
        scheduler = TaskScheduler()
        restored = scheduler.get_task("history123")
    finally:
        task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = original_artifact_root

    assert restored is not None
    assert restored.status == "completed"
    assert restored.name == "历史下载任务"
    assert restored.artifact_dir == str(task_dir.resolve())
    assert restored.deliverables[0]["url"] == "https://example.com/result"


def test_scheduler_persists_manual_task_before_any_execution(tmp_path) -> None:
    original_artifact_root = task_scheduler_module.TASK_RUN_ARTIFACT_ROOT
    task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = tmp_path / "task_runs"

    try:
        scheduler = TaskScheduler()
        task = scheduler.add_task(
            name="Saved manual workflow",
            run_at_iso=datetime.now(timezone.utc).isoformat(),
            task_type="browser_use_live_workflow",
            payload={"objective": "Run later"},
            frequency="manual",
        )
        restored = TaskScheduler().get_task(task.id)
    finally:
        task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = original_artifact_root

    assert (tmp_path / "task_runs" / task.id / "run.json").exists()
    assert restored is not None
    assert restored.status == "manual"
    assert restored.name == "Saved manual workflow"


def test_scheduler_persists_running_task_for_reload_history(tmp_path) -> None:
    original_artifact_root = task_scheduler_module.TASK_RUN_ARTIFACT_ROOT
    task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = tmp_path / "task_runs"
    started = Event()
    release = Event()

    def blocking_handler(payload: dict, progress_callback=None) -> list[str]:
        if progress_callback is not None:
            progress_callback("Task reached browser execution.")
        started.set()
        release.wait(timeout=2)
        return ["finished"]

    register_task_handler("persistent_running_task_test", blocking_handler)

    try:
        scheduler = TaskScheduler()
        task = scheduler.create_and_start_task(
            name="Reloadable execution",
            task_type="persistent_running_task_test",
            payload={"objective": "Observe progress"},
        )
        assert started.wait(timeout=1)

        snapshot = json.loads((tmp_path / "task_runs" / task.id / "run.json").read_text(encoding="utf-8"))
        restored = TaskScheduler().get_task(task.id)

        release.set()
        for _ in range(40):
            if scheduler.get_task(task.id).status == "completed":
                break
            time.sleep(0.02)
    finally:
        release.set()
        task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = original_artifact_root

    assert snapshot["status"] == "running"
    assert snapshot["progress_events"][-1]["message"] == "Task reached browser execution."
    assert restored is not None
    assert restored.status == "failed"
    assert "interrupted" in (restored.last_error or "")


def test_serialize_task_exposes_benchmark_result(tmp_path) -> None:
    original_scheduler = webapp_module.SCHEDULER
    original_artifact_root = task_scheduler_module.TASK_RUN_ARTIFACT_ROOT

    def benchmark_handler(payload: dict, progress_callback=None) -> list[str]:
        if progress_callback is not None:
            progress_callback("Benchmark total attempts: 1")
        return [
            "Benchmark total attempts: 1",
            "Benchmark result 1/1: fake success=true duration=0.10s progress_events=1 files=0",
            'benchmark_result: {"runs_per_adapter":1,"required_deliverable":"executor_success_or_final_url","adapters":[{"task_type":"fake","label":"Fake","runs":1,"successes":1,"success_rate":1.0,"avg_duration_seconds":0.1,"avg_progress_events":1,"final_files":[],"final_urls":["https://example.com"],"last_error":""}],"attempts":[]}',
        ]

    register_task_handler("benchmark_status_test", benchmark_handler)
    task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = tmp_path / "task_runs"
    webapp_module.SCHEDULER = TaskScheduler()
    try:
        task = webapp_module.SCHEDULER.create_and_run_task(
            name="Benchmark status",
            task_type="benchmark_status_test",
            payload={},
        )
        payload = webapp_module._serialize_task(task)
    finally:
        webapp_module.SCHEDULER = original_scheduler
        task_scheduler_module.TASK_RUN_ARTIFACT_ROOT = original_artifact_root

    assert payload["benchmark_result"]["adapters"][0]["task_type"] == "fake"


def test_browser_use_false_self_check_is_not_incomplete_when_file_exists(tmp_path) -> None:
    delivered = tmp_path / "result.pdf"
    delivered.write_bytes(b"%PDF-1.4\n")

    logs = [
        "Browser Use result summary:",
        "final_result: saved as PDF but model marked success false",
        "is_done: True",
        "is_successful: False",
        f"downloaded_file: {delivered}",
    ]

    assert webapp_module._browser_use_has_file_deliverable(logs) is True
    assert webapp_module._browser_use_result_indicates_incomplete(logs) is False


def test_browser_use_failure_message_uses_final_result() -> None:
    logs = [
        "Browser Use result summary:",
        "final_result: 未找到下载入口",
        "is_done: True",
        "is_successful: False",
        "errors: []",
    ]

    assert "未找到下载入口" in webapp_module._browser_use_failure_message(logs)


def test_task_browser_preview_route_returns_current_browser_image(monkeypatch) -> None:
    original_scheduler = webapp_module.SCHEDULER
    webapp_module.SCHEDULER = TaskScheduler()
    try:
        task = webapp_module.SCHEDULER.add_task(
            name="预览任务",
            run_at_iso=datetime.now(timezone.utc).isoformat(),
            task_type="preview_test",
            payload={"cdp_url": "http://127.0.0.1:9222"},
            frequency="manual",
        )
        monkeypatch.setattr(webapp_module, "_capture_browser_preview_sync", lambda cdp_url: b"png-bytes")

        response = asyncio.run(
            webapp_module.task_browser_preview(DummyRequest(path_params={"task_id": task.id}))
        )
    finally:
        webapp_module.SCHEDULER = original_scheduler

    assert response.status_code == 200
    assert response.media_type == "image/png"
    assert response.body == b"png-bytes"


def test_compose_plan_from_multiple_skills_reindexes_steps(tmp_path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")
    skills = [
        library.create_skill(
            name="技能A",
            description="",
            source_type="analysis",
            site_url="https://example.com/a",
            steps=[
                {"step_number": 3, "action": "click", "target": "A"},
            ],
        ),
        library.create_skill(
            name="技能B",
            description="",
            source_type="analysis",
            site_url="https://example.com/b",
            steps=[
                {"step_number": 8, "action": "type", "target": "B", "value": "demo"},
            ],
        ),
    ]
    plan = webapp_module._compose_plan_from_skills(skills)
    assert plan is not None
    assert plan["site_url"] == "https://example.com/a"
    assert [step["step_number"] for step in plan["steps"]] == [1, 2]


def test_compose_plan_recovers_external_site_from_console_saved_skill(tmp_path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")
    skill = library.create_skill(
        name="Saved from console",
        description="",
        source_type="video_analysis",
        site_url="http://127.0.0.1:8018/app",
        steps=[
            {"step_number": 1, "action": "scroll", "target": "Eyeclaw console"},
            {"step_number": 2, "action": "wait", "target": "https://example.com/workflow"},
            {"step_number": 3, "action": "click", "target": "Export"},
        ],
    )

    plan = webapp_module._compose_plan_from_skills([skill])

    assert plan is not None
    assert plan["site_url"] == "https://example.com/workflow"
    assert [step["target"] for step in plan["steps"]] == ["https://example.com/workflow", "Export"]
    assert [step["step_number"] for step in plan["steps"]] == [1, 2]


def test_compose_plan_moves_cascader_child_after_parent_option(tmp_path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")
    city = "\u676d\u5dde\u5e02"
    province = "\u6d59\u6c5f\u7701"
    note = "\u5148\u5c55\u5f00\u4e0a\u7ea7\u83dc\u5355\uff0c\u518d\u8fdb\u5165\u76ee\u6807\u5b50\u9879\u3002"
    skill = library.create_skill(
        name="Cascader skill",
        description="",
        source_type="video_analysis",
        site_url="http://127.0.0.1:8018/app",
        steps=[
            {"step_number": 1, "action": "click", "target": city, "notes": note},
            {"step_number": 2, "action": "wait", "target": "https://example.com/workflow"},
            {"step_number": 3, "action": "click", "target": "Open region cascader"},
            {"step_number": 4, "action": "click", "target": province},
        ],
    )

    plan = webapp_module._compose_plan_from_skills([skill])

    assert plan is not None
    assert [step["target"] for step in plan["steps"]] == [
        "https://example.com/workflow",
        "Open region cascader",
        province,
        city,
    ]


def test_compose_plan_drops_noisy_location_container_text(tmp_path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")
    noisy_target = (
        "\u5317\u4eac\u5e02\u5929\u6d25\u5e02\u6cb3\u5317\u7701\u5c71\u897f\u7701"
        "\u5185\u8499\u53e4\u81ea\u6cbb\u533a\u8fbd\u5b81\u7701\u5409\u6797\u7701"
        "\u9ed1\u9f99\u6c5f\u7701\u4e0a\u6d77\u5e02\u6c5f\u82cf\u7701\u6d59\u6c5f\u7701"
        "\u5b89\u5fbd\u7701\u798f\u5efa\u7701\u6c5f\u897f\u7701\u5c71\u4e1c\u7701"
    ) * 3
    skill = library.create_skill(
        name="Noisy cascader skill",
        description="",
        source_type="video_analysis",
        site_url="https://example.com/workflow",
        steps=[
            {"step_number": 1, "action": "click", "target": noisy_target},
            {"step_number": 2, "action": "click", "target": "Open region cascader"},
        ],
    )

    plan = webapp_module._compose_plan_from_skills([skill])

    assert plan is not None
    assert [step["target"] for step in plan["steps"]] == ["Open region cascader"]


def test_fast_preflight_compacts_dropdown_trigger_and_option_steps() -> None:
    plan = {
        "site_url": "https://example.com/workflow",
        "steps": [
            {
                "step_number": 1,
                "action": "click",
                "target": "\u884c\u4e1a\u5206\u7c7b\u53f3\u4fa7\u7684\u4e0b\u62c9\u9009\u62e9\u6846",
                "selector_hint": ".stale-tool-icon",
            },
            {
                "step_number": 2,
                "action": "click",
                "target": "\u8bf7\u9009\u62e9",
                "selector_hint": "div.row > div.el-select:nth-of-type(2) input.el-input__inner",
            },
            {
                "step_number": 3,
                "action": "click",
                "target": "\u98df\u54c1\u5236\u9020\u4e1a",
                "selector_hint": "li.el-select-dropdown__item:nth-of-type(12)",
            },
            {
                "step_number": 4,
                "action": "click",
                "target": "\u9884\u89c8",
                "selector_hint": ".viewReport",
            },
        ],
    }

    compacted = webapp_module._compact_plan_for_fast_preflight(plan)

    assert [step["action"] for step in compacted["steps"]] == ["select", "click"]
    assert compacted["steps"][0]["target"] == "\u8bf7\u9009\u62e9"
    assert compacted["steps"][0]["value"] == "\u98df\u54c1\u5236\u9020\u4e1a"
    assert compacted["steps"][0]["selector_hint"] == "div.row > div.el-select:nth-of-type(2) input.el-input__inner"


def test_browser_use_prompt_describes_dropdown_pairs_as_select_steps() -> None:
    plan = {
        "site_url": "https://example.com/workflow",
        "steps": [
            {
                "step_number": 1,
                "action": "click",
                "target": "\u8bf7\u9009\u62e9",
                "selector_hint": "div.el-select input.el-input__inner",
            },
            {
                "step_number": 2,
                "action": "click",
                "target": "\u6d59\u6c5f\u7701",
                "selector_hint": "li.el-select-dropdown__item:nth-of-type(12)",
            },
        ],
    }

    prompt = webapp_module._compose_browser_use_task("", plan)

    assert "Select `\u6d59\u6c5f\u7701` from the relevant dropdown/list" in prompt
    assert "Click the visible control/text related to `\u8bf7\u9009\u62e9`" not in prompt


def test_listener_events_recover_distinct_dropdown_triggers() -> None:
    events = [
        SimpleNamespace(
            event_type="click",
            page_url="https://example.com/workflow",
            target_text="\u8bf7\u9009\u62e9",
            target_selector="div.row > div.el-select:nth-of-type(2) input.el-input__inner",
            target_tag="input",
            target_type="text",
            input_value=None,
            client_timestamp_ms=1,
        ),
        SimpleNamespace(
            event_type="click",
            page_url="https://example.com/workflow",
            target_text="\u98df\u54c1\u5236\u9020\u4e1a",
            target_selector="li.el-select-dropdown__item:nth-of-type(12)",
            target_tag="li",
            target_type=None,
            input_value=None,
            client_timestamp_ms=2,
        ),
        SimpleNamespace(
            event_type="click",
            page_url="https://example.com/workflow",
            target_text="\u8bf7\u9009\u62e9",
            target_selector="div.row > div.el-select:nth-of-type(3) input.el-input__inner",
            target_tag="input",
            target_type="text",
            input_value=None,
            client_timestamp_ms=3,
        ),
        SimpleNamespace(
            event_type="click",
            page_url="https://example.com/workflow",
            target_text="\u4e73\u5236\u54c1\u5236\u9020 144",
            target_selector="li.el-select-dropdown__item:nth-of-type(4) > span",
            target_tag="span",
            target_type=None,
            input_value=None,
            client_timestamp_ms=4,
        ),
    ]

    steps = webapp_module._replay_steps_from_listener_events(events)

    assert [step["action"] for step in steps] == ["select", "select"]
    assert [step["value"] for step in steps] == ["\u98df\u54c1\u5236\u9020\u4e1a", "\u4e73\u5236\u54c1\u5236\u9020 144"]
    assert [step["selector_hint"] for step in steps] == [
        "div.row > div.el-select:nth-of-type(2) input.el-input__inner",
        "div.row > div.el-select:nth-of-type(3) input.el-input__inner",
    ]


def test_listener_events_preserve_keyboard_shortcut_steps() -> None:
    events = [
        SimpleNamespace(
            event_type="keyboard_shortcut",
            page_url="https://example.com/report.pdf",
            target_text="Ctrl+S",
            target_selector="body",
            target_tag="body",
            target_type="keyboard-shortcut",
            input_value="Ctrl+S",
            details={"shortcut": "Ctrl+S"},
            client_timestamp_ms=1,
        )
    ]

    steps = webapp_module._replay_steps_from_listener_events(events)

    assert steps == [
        {
            "step_number": 1,
            "action": "press",
            "target": "Ctrl+S",
            "value": "Ctrl+S",
            "selector_hint": "body",
        }
    ]
