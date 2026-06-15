import asyncio
from pathlib import Path
from types import SimpleNamespace

import requests

import src.webapp as webapp_module


class CompletedEvent:
    def __await__(self):
        if False:
            yield None
        return None

    async def event_result(self, **kwargs):
        return None


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


def test_browser_use_llm_auto_switches_deepseek_thinking_model(monkeypatch) -> None:
    monkeypatch.delenv("BROWSER_USE_LLM", raising=False)
    monkeypatch.delenv("BROWSER_USE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("BROWSER_USE_LLM_API_KEY", raising=False)

    settings = webapp_module._resolve_browser_use_llm_settings(
        SimpleNamespace(
            deepseek_model="deepseek-v4-pro",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_api_key="stub-key",
        )
    )

    assert settings["model"] == "deepseek-chat"
    assert settings["switched_model"] is True


def test_browser_use_llm_respects_explicit_execution_override(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_USE_LLM", "deepseek-chat")
    monkeypatch.setenv("BROWSER_USE_LLM_BASE_URL", "https://api.deepseek.com")

    settings = webapp_module._resolve_browser_use_llm_settings(
        SimpleNamespace(
            deepseek_model="deepseek-v4-pro",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_api_key="stub-key",
        )
    )

    assert settings["model"] == "deepseek-chat"
    assert settings["switched_model"] is False


def test_browser_use_llm_uses_glm_credentials_for_glm_execution_model(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_USE_LLM", "GLM-4.6")
    monkeypatch.delenv("BROWSER_USE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("BROWSER_USE_LLM_API_KEY", raising=False)

    settings = webapp_module._resolve_browser_use_llm_settings(
        SimpleNamespace(
            deepseek_model="deepseek-v4-pro",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_api_key="deepseek-key",
            glm_base_url="https://open.bigmodel.cn/api/paas/v4",
            glm_api_key="glm-key",
        )
    )

    assert settings["model"] == "GLM-4.6"
    assert settings["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert settings["api_key"] == "glm-key"


def test_browser_use_vision_llm_uses_vlm_credentials(monkeypatch) -> None:
    monkeypatch.delenv("BROWSER_USE_VISION_LLM", raising=False)
    monkeypatch.delenv("BROWSER_USE_VISION_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("BROWSER_USE_VISION_LLM_API_KEY", raising=False)

    settings = webapp_module._resolve_browser_use_llm_settings(
        SimpleNamespace(
            deepseek_model="deepseek-chat",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_api_key="deepseek-key",
            glm_model="glm-5v-turbo",
            glm_base_url="https://open.bigmodel.cn/api/paas/v4",
            glm_api_key="glm-key",
        ),
        prefer_vision=True,
    )

    assert settings["model"] == "glm-5v-turbo"
    assert settings["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert settings["api_key"] == "glm-key"
    assert settings["prefer_vision"] is True


def test_browser_use_llm_explicit_glm_endpoint_keeps_primary_llm_key(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_USE_LLM", "glm-5.1")
    monkeypatch.setenv("BROWSER_USE_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.delenv("BROWSER_USE_LLM_API_KEY", raising=False)

    settings = webapp_module._resolve_browser_use_llm_settings(
        SimpleNamespace(
            deepseek_model="glm-5.1",
            deepseek_base_url="https://open.bigmodel.cn/api/paas/v4",
            deepseek_api_key="llm-key",
            glm_base_url="https://ark.cn-beijing.volces.com/api/v3",
            glm_api_key="vlm-key",
        )
    )

    assert settings["model"] == "glm-5.1"
    assert settings["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert settings["api_key"] == "llm-key"


def test_browser_use_llm_uses_deepseek_json_adapter_by_default(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_USE_LLM", "deepseek-chat")
    monkeypatch.setenv("BROWSER_USE_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.delenv("BROWSER_USE_DEEPSEEK_NATIVE_TOOLS", raising=False)

    llm = webapp_module._build_browser_use_llm(
        SimpleNamespace(
            deepseek_model="deepseek-v4-pro",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_api_key="deepseek-key",
        ),
        fast_mode=True,
        llm_timeout=10,
    )

    assert llm.__class__.__name__ == "DeepSeekBrowserUseLLM"
    assert llm.model == "deepseek-chat"
    assert llm.max_tokens == 1536


def test_browser_use_llm_uses_glm_adapter_with_thinking_disabled(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_USE_LLM", "glm-5.1")
    monkeypatch.setenv("BROWSER_USE_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.delenv("BROWSER_USE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("BROWSER_USE_GLM_THINKING", raising=False)

    llm = webapp_module._build_browser_use_llm(
        SimpleNamespace(
            deepseek_model="glm-5.1",
            deepseek_base_url="https://open.bigmodel.cn/api/paas/v4",
            deepseek_api_key="llm-key",
            glm_base_url="https://ark.cn-beijing.volces.com/api/v3",
            glm_api_key="vlm-key",
        ),
        fast_mode=True,
        llm_timeout=10,
    )

    assert llm.__class__.__name__ == "OpenAICompatibleBrowserUseLLM"
    assert llm.provider == "glm"
    assert llm.extra_body == {"thinking": {"type": "disabled"}}
    assert llm.api_key == "llm-key"


def test_browser_use_llm_can_opt_into_deepseek_native_tools(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_USE_LLM", "deepseek-chat")
    monkeypatch.setenv("BROWSER_USE_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("BROWSER_USE_DEEPSEEK_NATIVE_TOOLS", "true")

    llm = webapp_module._build_browser_use_llm(
        SimpleNamespace(
            deepseek_model="deepseek-v4-pro",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_api_key="deepseek-key",
        ),
        fast_mode=True,
        llm_timeout=10,
    )

    assert llm.__class__.__name__ == "ChatDeepSeek"
    assert llm.model == "deepseek-chat"


def test_browser_use_scheduler_passes_start_url_to_agent(monkeypatch) -> None:
    captured: dict[str, object] = {"logs": []}

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

    def fake_ensure_browser_use_cdp_available(cdp_url: str) -> None:
        captured["cdp_url"] = cdp_url

    async def fake_run_browser_use_agent(**kwargs):
        captured["start_url"] = kwargs["start_url"]
        captured["task_text"] = kwargs["task_text"]
        return ["Browser Use result summary:", "final_result: ok"]

    monkeypatch.setattr(webapp_module, "load_config_status", fake_load_config_status)
    monkeypatch.setattr(webapp_module, "_ensure_browser_use_cdp_available", fake_ensure_browser_use_cdp_available)
    monkeypatch.setattr(webapp_module, "_run_browser_use_agent", fake_run_browser_use_agent)

    logs = webapp_module.scheduler_run_browser_use_live_workflow(
        {
            "cdp_url": "http://127.0.0.1:9222",
            "user_request": "执行任务",
            "plan": {"site_url": "https://example.com/workflow", "steps": []},
        },
        progress_callback=lambda message: captured["logs"].append(message),
    )

    assert captured["cdp_url"] == "http://127.0.0.1:9222"
    assert captured["start_url"] == "https://example.com/workflow"
    assert "Start URL: https://example.com/workflow" in str(captured["task_text"])
    assert "Browser Use start_url: https://example.com/workflow" in logs


def test_browser_use_scheduler_uses_visual_auto_for_first_fallback(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_load_config_status():
        return SimpleNamespace(
            is_ready=True,
            config=SimpleNamespace(
                deepseek_model="deepseek-chat",
                deepseek_api_key="deepseek-key",
                deepseek_base_url="https://api.deepseek.com",
                glm_model="glm-5v-turbo",
                glm_api_key="glm-key",
                glm_base_url="https://open.bigmodel.cn/api/paas/v4",
            ),
            missing_fields=[],
        )

    async def fake_run_browser_use_agent(**kwargs):
        captured["use_vision"] = kwargs["use_vision"]
        captured["prefer_vision_llm"] = kwargs["prefer_vision_llm"]
        return ["Browser Use result summary:", "final_result: ok", "is_done: True", "is_successful: True"]

    monkeypatch.setenv("BROWSER_USE_VISION_MODE", "auto")
    monkeypatch.setattr(webapp_module, "load_config_status", fake_load_config_status)
    monkeypatch.setattr(webapp_module, "_ensure_browser_use_cdp_available", lambda cdp_url: None)
    monkeypatch.setattr(webapp_module, "scheduler_run_fast_skill_preflight", lambda *args, **kwargs: (["preflight failed"], False))
    monkeypatch.setattr(webapp_module, "_run_browser_use_agent", fake_run_browser_use_agent)

    logs = webapp_module.scheduler_run_browser_use_live_workflow(
        {
            "cdp_url": "http://127.0.0.1:9222",
            "user_request": "run task",
            "plan": {"site_url": "https://example.com/workflow", "steps": []},
        },
    )

    assert captured["use_vision"] == "auto"
    assert captured["prefer_vision_llm"] is True
    assert any("vision=screenshot-on-demand" in item for item in logs)


def test_browser_use_scheduler_retries_with_forced_vision_after_failed_validation(monkeypatch) -> None:
    attempts: list[tuple[object, bool]] = []

    def fake_load_config_status():
        return SimpleNamespace(
            is_ready=True,
            config=SimpleNamespace(
                deepseek_model="deepseek-chat",
                deepseek_api_key="deepseek-key",
                deepseek_base_url="https://api.deepseek.com",
                glm_model="glm-5v-turbo",
                glm_api_key="glm-key",
                glm_base_url="https://open.bigmodel.cn/api/paas/v4",
            ),
            missing_fields=[],
        )

    async def fake_run_browser_use_agent(**kwargs):
        attempts.append((kwargs["use_vision"], kwargs["prefer_vision_llm"]))
        if len(attempts) == 1:
            return [
                "Browser Use result summary:",
                "final_result: None",
                "is_done: False",
                "is_successful: None",
                "errors: ['LLM call timed out after 60 seconds']",
            ]
        return ["Browser Use result summary:", "final_result: ok", "is_done: True", "is_successful: True"]

    monkeypatch.setenv("BROWSER_USE_VISION_MODE", "auto")
    monkeypatch.setenv("BROWSER_USE_VISUAL_RETRY", "true")
    monkeypatch.setattr(webapp_module, "load_config_status", fake_load_config_status)
    monkeypatch.setattr(webapp_module, "_ensure_browser_use_cdp_available", lambda cdp_url: None)
    monkeypatch.setattr(webapp_module, "scheduler_run_fast_skill_preflight", lambda *args, **kwargs: (["preflight failed"], False))
    monkeypatch.setattr(webapp_module, "_run_browser_use_agent", fake_run_browser_use_agent)

    logs = webapp_module.scheduler_run_browser_use_live_workflow(
        {
            "cdp_url": "http://127.0.0.1:9222",
            "user_request": "run task",
            "plan": {"site_url": "https://example.com/workflow", "steps": []},
        },
    )

    assert attempts == [("auto", True), (True, True)]
    assert any("escalating to visual retry" in item for item in logs)
    assert any("vision=always-on screenshots" in item for item in logs)


def test_browser_use_scheduler_fails_when_agent_is_incomplete(monkeypatch) -> None:
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

    async def fake_run_browser_use_agent(**kwargs):
        return [
            "Browser Use result summary:",
            "final_result: None",
            "is_done: False",
            "is_successful: None",
            "errors: ['LLM call timed out after 60 seconds', 'json_invalid']",
        ]

    monkeypatch.setattr(webapp_module, "load_config_status", fake_load_config_status)
    monkeypatch.setattr(webapp_module, "_ensure_browser_use_cdp_available", lambda cdp_url: None)
    monkeypatch.setattr(webapp_module, "_run_browser_use_agent", fake_run_browser_use_agent)

    try:
        webapp_module.scheduler_run_browser_use_live_workflow(
            {
                "cdp_url": "http://127.0.0.1:9222",
                "user_request": "run task",
                "plan": {"site_url": "https://example.com/workflow", "steps": []},
            },
        )
    except RuntimeError as exc:
        assert "LLM call timed out after 60 seconds" in str(exc)
    else:
        raise AssertionError("expected incomplete Browser Use result to fail the task")


def test_browser_use_scheduler_rejects_early_success_before_required_preview(monkeypatch) -> None:
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

    async def fake_run_browser_use_agent(**kwargs):
        return [
            "Browser Use result summary:",
            "final_result: Workflow completed successfully. Filters applied and results displayed.",
            "is_done: True",
            "is_successful: True",
            "urls: ['https://example.com/workflow']",
            "errors: [None]",
            "final_url: https://example.com/workflow",
        ]

    monkeypatch.setattr(webapp_module, "load_config_status", fake_load_config_status)
    monkeypatch.setattr(webapp_module, "_ensure_browser_use_cdp_available", lambda cdp_url: None)
    monkeypatch.setattr(webapp_module, "scheduler_run_fast_skill_preflight", lambda *args, **kwargs: (["preflight failed"], False))
    monkeypatch.setattr(webapp_module, "_run_browser_use_agent", fake_run_browser_use_agent)

    try:
        webapp_module.scheduler_run_browser_use_live_workflow(
            {
                "cdp_url": "http://127.0.0.1:9222",
                "user_request": "run task",
                "plan": {
                    "site_url": "https://example.com/workflow",
                    "steps": [
                        {"step_number": 1, "action": "select", "target": "Region", "value": "Zhejiang"},
                        {"step_number": 2, "action": "click", "target": "预览"},
                        {"step_number": 3, "action": "wait", "target": "https://example.com/previewPdf"},
                    ],
                },
            },
        )
    except RuntimeError as exc:
        assert "required preview step" in str(exc)
    else:
        raise AssertionError("expected early Browser Use success to fail required preview validation")


def test_browser_use_scheduler_accepts_preview_url_completion(monkeypatch) -> None:
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

    async def fake_run_browser_use_agent(**kwargs):
        return [
            "Browser Use result summary:",
            "final_result: Preview page opened.",
            "is_done: True",
            "is_successful: True",
            "urls: ['https://example.com/workflow', 'https://example.com/previewPdf?id=1']",
            "errors: [None]",
            "final_url: https://example.com/previewPdf?id=1",
        ]

    monkeypatch.setattr(webapp_module, "load_config_status", fake_load_config_status)
    monkeypatch.setattr(webapp_module, "_ensure_browser_use_cdp_available", lambda cdp_url: None)
    monkeypatch.setattr(webapp_module, "scheduler_run_fast_skill_preflight", lambda *args, **kwargs: (["preflight failed"], False))
    monkeypatch.setattr(webapp_module, "_run_browser_use_agent", fake_run_browser_use_agent)

    logs = webapp_module.scheduler_run_browser_use_live_workflow(
        {
            "cdp_url": "http://127.0.0.1:9222",
            "user_request": "run task",
            "plan": {
                "site_url": "https://example.com/workflow",
                "steps": [
                    {"step_number": 1, "action": "click", "target": "预览"},
                    {"step_number": 2, "action": "wait", "target": "https://example.com/previewPdf"},
                ],
            },
        },
    )

    assert "final_url: https://example.com/previewPdf?id=1" in logs


def test_browser_use_task_guides_dropdown_and_real_downloads() -> None:
    task_text = webapp_module._compose_browser_use_task(
        "Download the selected PDF file.",
        {
            "site_url": "https://example.com/workflow",
            "steps": [
                {"step_number": 1, "action": "select", "target": "Region", "value": "Zhejiang"},
                {"step_number": 2, "action": "click", "target": "Download"},
            ],
        },
    )

    assert "dropdown_options" in task_text
    assert "select_dropdown" in task_text
    assert "real downloaded/saved file path" in task_text
    assert "available_file_paths" not in task_text


def test_browser_use_task_preserves_keyboard_save_shortcut() -> None:
    task_text = webapp_module._compose_browser_use_task(
        "Save the selected PDF file.",
        {
            "site_url": "https://example.com/workflow",
            "steps": [
                {"step_number": 1, "action": "press", "target": "Ctrl+S", "value": "Ctrl+S"},
            ],
        },
    )

    assert "Press the keyboard shortcut `Ctrl+S`" in task_text
    assert "real downloaded/saved file path" in task_text


def test_browser_use_task_keeps_required_preview_wait() -> None:
    task_text = webapp_module._compose_browser_use_task(
        "",
        {
            "site_url": "https://example.com/workflow",
            "steps": [
                {"step_number": 1, "action": "select", "target": "Region", "value": "Zhejiang"},
                {"step_number": 2, "action": "click", "target": "预览"},
                {"step_number": 3, "action": "wait", "target": "https://example.com/previewPdf?id=1"},
            ],
        },
    )

    assert "Click the visible control/text related to `预览`" in task_text
    assert "Wait until the page, URL, or visible content indicates: https://example.com/previewPdf?id=1" in task_text
    assert "merely showing a filtered result list is incomplete" in task_text


def test_fast_preflight_file_task_falls_back_without_real_download(monkeypatch, tmp_path) -> None:
    logs: list[str] = []
    session = SimpleNamespace(page=SimpleNamespace(url="https://example.com/preview"))

    monkeypatch.setattr(webapp_module, "connect_over_cdp", lambda cdp_url: session)
    monkeypatch.setattr(webapp_module, "close_replay_session", lambda session: None)
    monkeypatch.setattr(webapp_module, "run_replay_plan", lambda *args, **kwargs: [])
    monkeypatch.setattr(webapp_module, "_try_trigger_fast_preflight_download", lambda *args, **kwargs: [])

    preflight_logs, completed = webapp_module.scheduler_run_fast_skill_preflight(
        {"cdp_url": "http://127.0.0.1:9222", "objective": "Download PDF"},
        plan={
            "site_url": "https://example.com/workflow",
            "steps": [{"step_number": 1, "action": "click", "target": "Open preview"}],
        },
        downloads_path=tmp_path,
        progress_callback=logs.append,
    )

    assert completed is False
    assert any("did not produce a real downloaded file" in item for item in preflight_logs)


def test_fast_preflight_file_task_completes_when_download_appears(monkeypatch, tmp_path) -> None:
    session = SimpleNamespace(page=SimpleNamespace(url="https://example.com/download"))

    def fake_run_replay_plan(*args, **kwargs):
        (tmp_path / "result.pdf").write_bytes(b"%PDF-1.4\n")
        return []

    monkeypatch.setattr(webapp_module, "connect_over_cdp", lambda cdp_url: session)
    monkeypatch.setattr(webapp_module, "close_replay_session", lambda session: None)
    monkeypatch.setattr(webapp_module, "run_replay_plan", fake_run_replay_plan)

    preflight_logs, completed = webapp_module.scheduler_run_fast_skill_preflight(
        {"cdp_url": "http://127.0.0.1:9222", "objective": "Download PDF"},
        plan={
            "site_url": "https://example.com/workflow",
            "steps": [{"step_number": 1, "action": "click", "target": "Download"}],
        },
        downloads_path=tmp_path,
    )

    assert completed is True
    assert any("downloaded_file:" in item and "result.pdf" in item for item in preflight_logs)


def test_hybrid_playwright_task_skips_browser_use_when_replay_completes(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {"browser_use_called": False}

    def fake_preflight(payload, *, plan, downloads_path, progress_callback=None):
        assert plan["site_url"] == "https://example.com/workflow"
        assert downloads_path == tmp_path
        return ["Fast preflight completed; Browser Use fallback skipped."], True

    def fake_browser_use(*args, **kwargs):
        captured["browser_use_called"] = True
        raise AssertionError("Browser Use fallback should not run after successful replay")

    monkeypatch.setattr(webapp_module, "scheduler_run_fast_skill_preflight", fake_preflight)
    monkeypatch.setattr(webapp_module, "scheduler_run_browser_use_live_workflow", fake_browser_use)

    logs = webapp_module.scheduler_run_playwright_browser_use_live_workflow(
        {
            "cdp_url": "http://127.0.0.1:9222",
            "downloads_path": str(tmp_path),
            "plan": {"site_url": "https://example.com/workflow", "steps": [{"step_number": 1, "action": "click", "target": "Search"}]},
        }
    )

    assert captured["browser_use_called"] is False
    assert any("Playwright fast replay completed" in item for item in logs)


def test_hybrid_playwright_task_falls_back_to_browser_use(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_preflight(payload, *, plan, downloads_path, progress_callback=None):
        return ["Fast preflight failed; falling back to Browser Use: missing selector"], False

    def fake_browser_use(payload, progress_callback=None, should_stop_callback=None):
        captured["skip_fast_preflight"] = payload.get("skip_fast_preflight")
        captured["task_type"] = payload.get("task_type")
        return ["Browser Use result summary:", "final_result: ok"]

    monkeypatch.setattr(webapp_module, "scheduler_run_fast_skill_preflight", fake_preflight)
    monkeypatch.setattr(webapp_module, "scheduler_run_browser_use_live_workflow", fake_browser_use)

    logs = webapp_module.scheduler_run_playwright_browser_use_live_workflow(
        {
            "cdp_url": "http://127.0.0.1:9222",
            "downloads_path": str(tmp_path),
            "plan": {"site_url": "https://example.com/workflow", "steps": [{"step_number": 1, "action": "click", "target": "Search"}]},
        }
    )

    assert captured["skip_fast_preflight"] is True
    assert captured["task_type"] == webapp_module.PRIMARY_EXECUTION_TASK_TYPE
    assert any("escalating to Browser Use fallback" in item for item in logs)
    assert "Browser Use result summary:" in logs


def test_smart_router_heuristic_prefers_hybrid_for_recorded_plan(monkeypatch) -> None:
    monkeypatch.setattr(
        webapp_module,
        "_enabled_router_candidate_types",
        lambda: [webapp_module.HYBRID_REPLAY_TASK_TYPE, webapp_module.PRIMARY_EXECUTION_TASK_TYPE, "autoglm_live_workflow"],
    )
    monkeypatch.setenv("EXECUTION_ROUTER_USE_LLM", "false")

    order, reason = webapp_module.resolve_smart_router_order(
        {
            "objective": "Download PDF",
            "plan": {"site_url": "https://example.com", "steps": [{"step_number": 1, "action": "click", "target": "Download"}]},
        }
    )

    assert order[0] == webapp_module.HYBRID_REPLAY_TASK_TYPE
    assert webapp_module.PRIMARY_EXECUTION_TASK_TYPE in order
    assert reason.startswith("Heuristic route:")


def test_smart_router_stage_order_keeps_browser_use_after_deterministic_executors(monkeypatch) -> None:
    monkeypatch.setattr(
        webapp_module,
        "_enabled_router_candidate_types",
        lambda: [
            webapp_module.PRIMARY_EXECUTION_TASK_TYPE,
            "autoglm_live_workflow",
            webapp_module.SELENIUM_TASK_TYPE,
            webapp_module.HYBRID_REPLAY_TASK_TYPE,
        ],
    )
    monkeypatch.setenv("EXECUTION_ROUTER_USE_LLM", "false")

    order, _ = webapp_module.resolve_smart_router_order(
        {
            "objective": "Download PDF after selecting a dropdown.",
            "plan": {
                "site_url": "https://example.com",
                "steps": [{"step_number": 1, "action": "select", "target": "Region", "value": "Zhejiang"}],
            },
        }
    )

    assert order[:3] == [
        webapp_module.HYBRID_REPLAY_TASK_TYPE,
        webapp_module.SELENIUM_TASK_TYPE,
        webapp_module.PRIMARY_EXECUTION_TASK_TYPE,
    ]


def test_smart_router_enforces_stage_order_on_llm_route(monkeypatch) -> None:
    monkeypatch.setattr(
        webapp_module,
        "_enabled_router_candidate_types",
        lambda: [
            webapp_module.PRIMARY_EXECUTION_TASK_TYPE,
            webapp_module.SELENIUM_TASK_TYPE,
            webapp_module.HYBRID_REPLAY_TASK_TYPE,
        ],
    )
    monkeypatch.setenv("EXECUTION_ROUTER_USE_LLM", "true")
    monkeypatch.setattr(
        webapp_module,
        "_llm_router_order",
        lambda payload, candidates: ([webapp_module.PRIMARY_EXECUTION_TASK_TYPE, webapp_module.SELENIUM_TASK_TYPE], "model order"),
    )

    order, reason = webapp_module.resolve_smart_router_order(
        {"plan": {"site_url": "https://example.com", "steps": [{"action": "click", "target": "Search"}]}}
    )

    assert order == [
        webapp_module.HYBRID_REPLAY_TASK_TYPE,
        webapp_module.SELENIUM_TASK_TYPE,
        webapp_module.PRIMARY_EXECUTION_TASK_TYPE,
    ]
    assert reason.startswith("LLM route:")


def test_smart_router_uses_heuristic_by_default_without_llm_delay(monkeypatch) -> None:
    monkeypatch.delenv("EXECUTION_ROUTER_USE_LLM", raising=False)
    monkeypatch.setattr(
        webapp_module,
        "_enabled_router_candidate_types",
        lambda: [webapp_module.HYBRID_REPLAY_TASK_TYPE, webapp_module.PRIMARY_EXECUTION_TASK_TYPE],
    )

    def fail_llm_router(*args, **kwargs):
        raise AssertionError("LLM router should not run by default")

    monkeypatch.setattr(webapp_module, "_llm_router_order", fail_llm_router)

    order, reason = webapp_module.resolve_smart_router_order(
        {"plan": {"site_url": "https://example.com", "steps": [{"action": "click", "target": "Search"}]}}
    )

    assert order[0] == webapp_module.HYBRID_REPLAY_TASK_TYPE
    assert reason == "Heuristic route: LLM router disabled by EXECUTION_ROUTER_USE_LLM."


def test_smart_router_parses_llm_order_and_filters_candidates() -> None:
    order = webapp_module._router_order_from_text(
        '{"order":["autoglm_live_workflow","unknown","browser_use_live_workflow"],"reason":"test"}',
        [webapp_module.PRIMARY_EXECUTION_TASK_TYPE, "autoglm_live_workflow"],
    )

    assert order == ["autoglm_live_workflow", webapp_module.PRIMARY_EXECUTION_TASK_TYPE]


def test_smart_router_falls_back_after_executor_failure(monkeypatch) -> None:
    calls: list[str] = []

    def first_handler(payload, progress_callback=None, should_stop_callback=None):
        calls.append(payload["task_type"])
        raise RuntimeError("first failed")

    def second_handler(payload, progress_callback=None, should_stop_callback=None):
        calls.append(payload["task_type"])
        return ["second ok"]

    monkeypatch.setattr(
        webapp_module,
        "resolve_smart_router_order",
        lambda payload: ([webapp_module.HYBRID_REPLAY_TASK_TYPE, webapp_module.PRIMARY_EXECUTION_TASK_TYPE], "test route"),
    )
    monkeypatch.setitem(webapp_module.TASK_HANDLERS, webapp_module.HYBRID_REPLAY_TASK_TYPE, first_handler)
    monkeypatch.setitem(webapp_module.TASK_HANDLERS, webapp_module.PRIMARY_EXECUTION_TASK_TYPE, second_handler)

    logs = webapp_module.scheduler_run_smart_router_live_workflow({"task_type": webapp_module.SMART_ROUTER_TASK_TYPE})

    assert calls == [webapp_module.HYBRID_REPLAY_TASK_TYPE, webapp_module.PRIMARY_EXECUTION_TASK_TYPE]
    assert any("executor failed" in item for item in logs)
    assert any("executor succeeded" in item for item in logs)
    assert "second ok" in logs


def test_smart_router_rejects_file_task_without_download_and_falls_back(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    def first_handler(payload, progress_callback=None, should_stop_callback=None):
        calls.append(payload["task_type"])
        return [
            "final_url: https://example.com/previewPdf?id=1",
            "Browser Use result summary:",
            "is_done: True",
            "is_successful: True",
        ]

    def second_handler(payload, progress_callback=None, should_stop_callback=None):
        calls.append(payload["task_type"])
        downloads = Path(payload["downloads_path"])
        downloads.mkdir(parents=True, exist_ok=True)
        result_file = downloads / "result.pdf"
        result_file.write_bytes(b"%PDF-1.4\n")
        return [f"downloaded_file: {result_file}", "final_url: https://example.com/previewPdf?id=1"]

    monkeypatch.setattr(
        webapp_module,
        "resolve_smart_router_order",
        lambda payload: ([webapp_module.HYBRID_REPLAY_TASK_TYPE, webapp_module.PRIMARY_EXECUTION_TASK_TYPE], "test route"),
    )
    monkeypatch.setitem(webapp_module.TASK_HANDLERS, webapp_module.HYBRID_REPLAY_TASK_TYPE, first_handler)
    monkeypatch.setitem(webapp_module.TASK_HANDLERS, webapp_module.PRIMARY_EXECUTION_TASK_TYPE, second_handler)

    logs = webapp_module.scheduler_run_smart_router_live_workflow(
        {
            "objective": "Download PDF",
            "downloads_path": str(tmp_path),
            "task_type": webapp_module.SMART_ROUTER_TASK_TYPE,
            "plan": {
                "site_url": "https://example.com/workflow",
                "steps": [{"step_number": 1, "action": "click", "target": "Download"}],
            },
        }
    )

    assert calls == [webapp_module.HYBRID_REPLAY_TASK_TYPE, webapp_module.PRIMARY_EXECUTION_TASK_TYPE]
    assert any("final validation failed" in item for item in logs)
    assert any("final validation passed" in item for item in logs)
    assert any("downloaded_file:" in item and "result.pdf" in item for item in logs)


def test_execution_adapter_benchmark_records_speed_progress_and_success(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    def first_handler(payload, progress_callback=None, should_stop_callback=None):
        calls.append(payload["task_type"])
        if progress_callback is not None:
            progress_callback("first adapter action 1")
        downloads = Path(payload["downloads_path"])
        downloads.mkdir(parents=True, exist_ok=True)
        (downloads / "result.pdf").write_bytes(b"%PDF-1.4\n")
        return [f"downloaded_file: {downloads / 'result.pdf'}", "final_url: https://example.com/result"]

    def second_handler(payload, progress_callback=None, should_stop_callback=None):
        calls.append(payload["task_type"])
        if progress_callback is not None:
            progress_callback("second adapter started")
        raise RuntimeError("second failed")

    monkeypatch.setitem(webapp_module.TASK_HANDLERS, "benchmark_first", first_handler)
    monkeypatch.setitem(webapp_module.TASK_HANDLERS, "benchmark_second", second_handler)

    logs = webapp_module.scheduler_run_execution_adapter_benchmark(
        {
            "objective": "Download PDF",
            "downloads_path": str(tmp_path),
            "benchmark_task_types": ["benchmark_first", "benchmark_second"],
            "benchmark_runs": 1,
        }
    )

    result = webapp_module._extract_benchmark_result(logs)
    assert calls == ["benchmark_first", "benchmark_second"]
    assert result is not None
    adapters = {item["task_type"]: item for item in result["adapters"]}
    assert adapters["benchmark_first"]["success_rate"] == 1.0
    assert adapters["benchmark_first"]["avg_progress_events"] == 1
    assert adapters["benchmark_first"]["final_files"]
    assert adapters["benchmark_second"]["success_rate"] == 0.0
    assert "second failed" in adapters["benchmark_second"]["last_error"]


def test_browser_use_execution_creates_dedicated_page_for_start_url() -> None:
    captured: dict[str, object] = {"logs": []}

    class FakeEventBus:
        def dispatch(self, event):
            captured["switch_target_id"] = getattr(event, "target_id", None)
            return CompletedEvent()

    class FakePage:
        async def get_target_info(self):
            return {"targetId": "existing-target"}

    class FakeBrowserSession:
        def __init__(self):
            self.event_bus = FakeEventBus()

        async def start(self):
            captured["started"] = True

        async def get_current_page(self):
            return FakePage()

        async def new_page(self, url):
            captured["new_page_url"] = url
            return FakePage()

        async def navigate_to(self, url, new_tab=False):
            raise AssertionError("existing page should not be navigated")

        async def get_or_create_cdp_session(self, **kwargs):
            raise AssertionError("focus fallback should not be needed when switch succeeds")

    target_id = asyncio.run(
        webapp_module._prepare_browser_use_execution_page(
            FakeBrowserSession(),
            "https://example.com/workflow",
            progress_callback=lambda message: captured["logs"].append(message),
        )
    )

    assert target_id == "existing-target"
    assert captured["started"] is True
    assert captured["new_page_url"] == "https://example.com/workflow"
    assert captured["switch_target_id"] == "existing-target"
    assert captured["logs"] == ["Browser Use created a dedicated execution page: https://example.com/workflow"]


def test_browser_use_execution_keeps_console_page_open() -> None:
    captured: dict[str, object] = {"logs": []}

    class FakeEventBus:
        def dispatch(self, event):
            captured["switch_target_id"] = getattr(event, "target_id", None)
            return CompletedEvent()

    class FakePage:
        def __init__(self, target_id: str, url: str):
            self.target_id = target_id
            self.url = url

        async def get_target_info(self):
            return {"targetId": self.target_id, "url": self.url}

        async def get_url(self):
            return self.url

    class FakeBrowserSession:
        def __init__(self):
            self.event_bus = FakeEventBus()

        async def start(self):
            captured["started"] = True

        async def get_current_page(self):
            return FakePage("console-target", "http://127.0.0.1:8018/app")

        async def new_page(self, url):
            captured["new_page_url"] = url
            return FakePage("execution-target", url)

        async def navigate_to(self, url, new_tab=False):
            raise AssertionError("console page should not be navigated")

        async def get_or_create_cdp_session(self, **kwargs):
            raise AssertionError("focus fallback should not be needed when switch succeeds")

    target_id = asyncio.run(
        webapp_module._prepare_browser_use_execution_page(
            FakeBrowserSession(),
            "https://example.com/workflow",
            progress_callback=lambda message: captured["logs"].append(message),
        )
    )

    assert target_id == "execution-target"
    assert captured["started"] is True
    assert captured["new_page_url"] == "https://example.com/workflow"
    assert captured["switch_target_id"] == "execution-target"
    assert captured["logs"] == [
        "Browser Use created a dedicated execution page: https://example.com/workflow"
    ]


def test_browser_use_pdf_preview_fallback_saves_file(tmp_path) -> None:
    class FakePdfApi:
        async def printToPDF(self, **kwargs):
            return {"data": "JVBERi0xLjQK"}

    class FakeSend:
        Page = FakePdfApi()

    class FakeCdpClient:
        send = FakeSend()

    class FakeCdpSession:
        session_id = "session-1"
        cdp_client = FakeCdpClient()

    class FakeBrowserSession:
        async def get_or_create_cdp_session(self, **kwargs):
            assert kwargs == {"focus": True}
            return FakeCdpSession()

    logs = asyncio.run(
        webapp_module._maybe_save_browser_use_preview_pdf(
            browser_session=FakeBrowserSession(),
            logs=[
                "final_result: Preview page opened, but no file was downloaded.",
                "is_successful: False",
            ],
            final_url="https://example.com/previewPdf?id=1",
            downloads_path=tmp_path,
            filename_hint="report.pdf",
            wants_file_delivery=True,
        )
    )

    downloaded = [line for line in logs if line.startswith("downloaded_file:")]
    assert downloaded
    saved_path = Path(downloaded[0].split(":", 1)[1].strip())
    assert saved_path.is_file()
    assert saved_path.read_bytes().startswith(b"%PDF")


def test_browser_use_pdf_preview_fallback_skips_when_file_not_requested(tmp_path) -> None:
    class FakeBrowserSession:
        async def get_or_create_cdp_session(self, **kwargs):
            raise AssertionError("PDF fallback should not run")

    logs = asyncio.run(
        webapp_module._maybe_save_browser_use_preview_pdf(
            browser_session=FakeBrowserSession(),
            logs=["final_result: Preview page opened."],
            final_url="https://example.com/previewPdf?id=1",
            downloads_path=tmp_path,
            filename_hint="report.pdf",
            wants_file_delivery=False,
        )
    )

    assert logs == []


def test_browser_use_pdf_preview_fallback_skips_when_final_url_is_not_preview(tmp_path) -> None:
    class FakeBrowserSession:
        async def get_or_create_cdp_session(self, **kwargs):
            raise AssertionError("PDF fallback should not print a non-preview page")

    logs = asyncio.run(
        webapp_module._maybe_save_browser_use_preview_pdf(
            browser_session=FakeBrowserSession(),
            logs=[
                "final_result: 已筛选结果，但尚未点击预览按钮。",
                "is_successful: False",
            ],
            final_url="https://example.com/#/list",
            downloads_path=tmp_path,
            filename_hint="report.pdf",
            wants_file_delivery=True,
        )
    )

    assert logs == []
