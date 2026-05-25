import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time

import src.webapp as webapp_module
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
