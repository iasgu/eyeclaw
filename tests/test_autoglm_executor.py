from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import src.webapp as webapp_module


def test_compose_autoglm_task_uses_chinese_steps() -> None:
    task = webapp_module._compose_autoglm_task(
        "请打开公告页并保存",
        {
            "site_url": "https://example.com",
            "steps": [
                {"step_number": 1, "action": "click", "target": "公告列表"},
                {"step_number": 2, "action": "click", "target": "第一条公告", "selector_hint": ".notice-list a"},
            ],
        },
    )
    assert "任务目标：请打开公告页并保存" in task
    assert "1. click：公告列表" in task
    assert "2. click：第一条公告" in task
    assert "定位提示：.notice-list a" in task


def test_scheduler_run_autoglm_live_workflow_invokes_mcporter(monkeypatch) -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        dependency_dir = root / "dependency"
        dependency_dir.mkdir(parents=True, exist_ok=True)
        mcporter_path = dependency_dir / "mcporter.exe"
        mcporter_path.write_text("", encoding="utf-8")

        monkeypatch.setattr(
            webapp_module,
            "_load_mcporter_server_config",
            lambda server_name=webapp_module.AUTOGLM_BROWSER_AGENT_NAME: {
                "command": str(root / "dist" / "mcp_server.exe"),
                "args": [],
            },
        )

        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return SimpleNamespace(returncode=0, stdout="AUTOGML_OK", stderr="")

        monkeypatch.setattr(webapp_module.subprocess, "run", fake_run)

        logs = webapp_module.scheduler_run_autoglm_live_workflow(
            {
                "user_request": "请执行任务",
                "plan": {
                    "site_url": "https://example.com/workflow",
                    "steps": [{"step_number": 1, "action": "click", "target": "开始"}],
                },
            }
        )

    assert any("AutoGLM task start_url: https://example.com/workflow" in line for line in logs)
    assert any("AUTOGML_OK" in line for line in logs)
    assert captured["command"][0].endswith("mcporter.exe")
    assert captured["command"][1:4] == ["call", "autoglm-browser-agent", "browser_subagent"]
