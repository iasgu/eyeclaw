from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import AliasChoices, BaseModel, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    deepseek_model: str = Field(
        alias="DEEPSEEK_MODEL",
        validation_alias=AliasChoices("DEEPSEEK_MODEL", "LLM"),
    )
    deepseek_base_url: str = Field(
        alias="DEEPSEEK_BASE_URL",
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "LLM_BASE_URL"),
    )
    deepseek_api_key: str = Field(
        alias="DEEPSEEK_API_KEY",
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "LLM_API_KEY"),
    )

    glm_model: str = Field(
        alias="GLM_MODEL",
        validation_alias=AliasChoices("GLM_MODEL", "VLM"),
    )
    glm_base_url: str = Field(
        alias="GLM_BASE_URL",
        validation_alias=AliasChoices("GLM_BASE_URL", "VLM_BASE_URL"),
    )
    glm_api_key: str = Field(
        alias="GLM_API_KEY",
        validation_alias=AliasChoices("GLM_API_KEY", "VLM_API_KEY", "VLMAPI_KEY"),
    )

    edge_user_data_dir: str = Field(alias="EDGE_USER_DATA_DIR")
    edge_channel: str = Field(alias="EDGE_CHANNEL")
    target_site_url: str = Field(alias="TARGET_SITE_URL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="ignore",
    )

    @field_validator("deepseek_model", mode="before")
    @classmethod
    def normalize_deepseek_model(cls, value: str) -> str:
        original = str(value).strip()
        normalized = original.lower().replace("_", "-").replace(" ", "-")
        aliases = {
            "deepseek-v4-pro": "deepseek-v4-pro",
            "deepseek-v4-flash": "deepseek-v4-flash",
        }
        return aliases.get(normalized, original)


class ConfigStatus(BaseModel):
    is_ready: bool
    config: Optional[AppConfig] = None
    missing_fields: List[str] = []
    error_message: Optional[str] = None


def load_config_status() -> ConfigStatus:
    load_dotenv(dotenv_path=Path(".env"), override=False)
    try:
        return ConfigStatus(is_ready=True, config=AppConfig())
    except ValidationError as exc:
        missing_fields = sorted(
            {
                ".".join(str(part) for part in error["loc"])
                for error in exc.errors()
                if error.get("type") == "missing"
            }
        )
        return ConfigStatus(
            is_ready=False,
            missing_fields=missing_fields,
            error_message=str(exc),
        )
