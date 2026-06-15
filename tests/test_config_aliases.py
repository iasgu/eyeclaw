from pathlib import Path

from src.config import AppConfig, load_config_status


CONFIG_ENV_NAMES = [
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_API_KEY",
    "GLM_MODEL",
    "GLM_BASE_URL",
    "GLM_API_KEY",
    "LLM",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "VLM",
    "VLM_BASE_URL",
    "VLM_API_KEY",
    "VLMAPI_KEY",
    "EDGE_USER_DATA_DIR",
    "EDGE_CHANNEL",
    "TARGET_SITE_URL",
]


def clear_config_env(monkeypatch) -> None:
    for name in CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_app_config_accepts_llm_and_vlm_aliases(tmp_path: Path, monkeypatch) -> None:
    clear_config_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "LLM=GLM-5.1",
                "LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4",
                "LLM_API_KEY=test-llm-key",
                "VLM=doubao-seed-2-0-pro-260215",
                "VLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3",
                "VLMAPI_KEY=test-vlm-key",
                "EDGE_USER_DATA_DIR=C:\\\\edge-profile",
                "EDGE_CHANNEL=msedge",
                "TARGET_SITE_URL=https://example.com/#/",
            ]
        ),
        encoding="utf-8",
    )

    config = AppConfig(_env_file=env_file)

    assert config.deepseek_model == "GLM-5.1"
    assert config.deepseek_base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert config.deepseek_api_key == "test-llm-key"
    assert config.glm_model == "doubao-seed-2-0-pro-260215"
    assert config.glm_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert config.glm_api_key == "test-vlm-key"


def test_app_config_keeps_legacy_env_names_compatible(tmp_path: Path, monkeypatch) -> None:
    clear_config_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DEEPSEEK_MODEL=deepseek-v4-pro",
                "DEEPSEEK_BASE_URL=https://api.deepseek.com/v1",
                "DEEPSEEK_API_KEY=test-deepseek-key",
                "GLM_MODEL=glm-4.5v",
                "GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4",
                "GLM_API_KEY=test-glm-key",
                "EDGE_USER_DATA_DIR=C:\\\\edge-profile",
                "EDGE_CHANNEL=msedge",
                "TARGET_SITE_URL=https://example.com",
            ]
        ),
        encoding="utf-8",
    )

    config = AppConfig(_env_file=env_file)

    assert config.deepseek_model == "deepseek-v4-pro"
    assert config.glm_model == "glm-4.5v"


def test_load_config_status_reloads_env_file_edits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    clear_config_env(monkeypatch)

    env_file = tmp_path / ".env"
    common_lines = [
        "LLM_BASE_URL=https://api.deepseek.com",
        "LLM_API_KEY=test-llm-key",
        "VLM=doubao-seed-2-0-pro-260215",
        "VLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3",
        "VLM_API_KEY=test-vlm-key",
        "EDGE_USER_DATA_DIR=C:\\\\edge-profile",
        "EDGE_CHANNEL=msedge",
        "TARGET_SITE_URL=https://example.com",
    ]
    env_file.write_text("\n".join(["LLM=deepseek-chat", *common_lines]), encoding="utf-8")

    first = load_config_status()
    assert first.is_ready is True
    assert first.config is not None
    assert first.config.deepseek_model == "deepseek-chat"

    env_file.write_text("\n".join(["LLM=GLM-5.1", *common_lines]), encoding="utf-8")

    second = load_config_status()
    assert second.is_ready is True
    assert second.config is not None
    assert second.config.deepseek_model == "GLM-5.1"


def test_load_config_status_supports_unified_glm_vlm_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    clear_config_env(monkeypatch)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "LLM=glm-5v-turbo",
                "LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4",
                "LLM_API_KEY=test-glm-key",
                "VLM=glm-5v-turbo",
                "VLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4",
                "VLM_API_KEY=${LLM_API_KEY}",
                "EDGE_USER_DATA_DIR=C:\\\\edge-profile",
                "EDGE_CHANNEL=msedge",
                "TARGET_SITE_URL=https://example.com",
            ]
        ),
        encoding="utf-8",
    )

    status = load_config_status()

    assert status.is_ready is True
    assert status.config is not None
    assert status.config.deepseek_model == "glm-5v-turbo"
    assert status.config.deepseek_base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert status.config.glm_model == "glm-5v-turbo"
    assert status.config.glm_base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert status.config.glm_api_key == "test-glm-key"
