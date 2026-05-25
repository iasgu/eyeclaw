from types import SimpleNamespace

import requests

import src.webapp as webapp_module


def test_run_live_workflow_sync_prefers_generic_plan(monkeypatch) -> None:
    captured = {"replay_called": False, "eia_called": False}

    def fake_connect_over_cdp(cdp_url: str):
        return SimpleNamespace(page=object(), context=None, browser=None, playwright=SimpleNamespace(stop=lambda: None), owns_browser=False)

    def fake_close_replay_session(session) -> None:
        return None

    def fake_run_replay_plan(session, replay_plan, progress_callback=None):
        captured["replay_called"] = True
        if progress_callback is not None:
            progress_callback("Step 1: click -> Search")
            progress_callback("Step 1: success")
        return ["Step 1: click -> Search", "Step 1: success"]

    def fake_run_eia_live_workflow(session, progress_callback=None, filter_spec=None):
        captured["eia_called"] = True
        raise AssertionError("generic plan execution should not call EIA workflow")

    monkeypatch.setattr(webapp_module, "connect_over_cdp", fake_connect_over_cdp)
    monkeypatch.setattr(webapp_module, "close_replay_session", fake_close_replay_session)
    monkeypatch.setattr(webapp_module, "run_replay_plan", fake_run_replay_plan)
    monkeypatch.setattr(webapp_module, "run_eia_live_workflow", fake_run_eia_live_workflow)

    data, status_code = webapp_module._run_live_workflow_sync(
        "http://127.0.0.1:9222",
        "执行搜索",
        {
            "site_url": "https://example.com",
            "steps": [
                {"step_number": 1, "action": "click", "target": "Search"},
            ],
        },
        "generic_live_workflow",
    )

    assert status_code == 200
    assert data["mode"] == "generic"
    assert data["task_type"] == "generic_live_workflow"
    assert captured["replay_called"] is True
    assert captured["eia_called"] is False


def test_run_live_workflow_sync_defaults_to_browser_use(monkeypatch) -> None:
    captured = {"browser_use_called": False}

    def fake_browser_use_handler(payload: dict):
        captured["browser_use_called"] = True
        assert payload["task_type"] == "browser_use_live_workflow"
        assert payload["plan"]["site_url"] == "https://example.com"
        return ["Browser Use result summary:", "final_result: ok"]

    monkeypatch.setattr(webapp_module, "scheduler_run_browser_use_live_workflow", fake_browser_use_handler)

    data, status_code = webapp_module._run_live_workflow_sync(
        "http://127.0.0.1:9222",
        "执行搜索",
        {
            "site_url": "https://example.com",
            "steps": [
                {"step_number": 1, "action": "click", "target": "Search"},
            ],
        },
    )

    assert status_code == 200
    assert data["mode"] == "browser_use"
    assert data["task_type"] == "browser_use_live_workflow"
    assert captured["browser_use_called"] is True


def test_browser_use_scheduler_reports_unreachable_cdp(monkeypatch) -> None:
    def fake_load_config_status():
        return SimpleNamespace(
            is_ready=True,
            config=SimpleNamespace(
                deepseek_model="stub-model",
                deepseek_api_key="stub-key",
                deepseek_base_url="https://example.com/v1",
            ),
            missing_fields=[],
        )

    def fake_requests_get(*args, **kwargs):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(webapp_module, "load_config_status", fake_load_config_status)
    monkeypatch.setattr(webapp_module.requests, "get", fake_requests_get)

    try:
        webapp_module.scheduler_run_browser_use_live_workflow(
            {
                "cdp_url": "http://127.0.0.1:9222",
                "user_request": "执行任务",
                "plan": {"site_url": "https://example.com", "steps": []},
            }
        )
    except RuntimeError as exc:
        assert "浏览器调试地址不可用" in str(exc)
        assert "127.0.0.1:9222" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError for unreachable CDP")
