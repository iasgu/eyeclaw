from pathlib import Path

from src.config import AppConfig


def test_app_config_accepts_llm_and_vlm_aliases(tmp_path: Path) -> None:
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


def test_app_config_keeps_legacy_env_names_compatible(tmp_path: Path) -> None:
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
