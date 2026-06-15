from __future__ import annotations

import json
import os
from typing import Any

import requests
from requests import Response
from requests.exceptions import RequestException

from src.config import AppConfig
from src.prompts import DEEPSEEK_SYSTEM_PROMPT, build_deepseek_user_prompt


class DeepSeekClient:
    def __init__(self, config: AppConfig, timeout_seconds: int = 90) -> None:
        self._config = config
        self._timeout_seconds = timeout_seconds

    @property
    def endpoint(self) -> str:
        base_url = self._config.deepseek_base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        if base_url.endswith("/v1"):
            return f"{base_url}/chat/completions"
        return f"{base_url}/chat/completions"

    def normalize_plan(
        self,
        raw_analysis: dict[str, Any],
        site_url: str,
        user_request: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": self._config.deepseek_model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": DEEPSEEK_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_deepseek_user_prompt(
                        site_url=site_url,
                        raw_analysis_json=json.dumps(raw_analysis, ensure_ascii=False, indent=2),
                        user_request=user_request,
                    ),
                },
            ],
        }
        if _is_glm_model_or_endpoint(self._config.deepseek_model, self._config.deepseek_base_url):
            payload["thinking"] = {"type": os.getenv("LLM_GLM_THINKING", "disabled")}
        try:
            response = requests.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self._config.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout_seconds,
            )
        except RequestException as exc:
            raise RuntimeError(f"DeepSeek request could not be completed: {exc}") from exc
        raise_for_status_with_detail(response, provider="DeepSeek")
        response_json = response.json()
        content_text = response_json["choices"][0]["message"]["content"]
        return json.loads(content_text)


def _is_glm_model_or_endpoint(model: str, base_url: str) -> bool:
    normalized_model = str(model or "").strip().lower()
    normalized_base_url = str(base_url or "").strip().lower()
    return normalized_model.startswith("glm") or "bigmodel.cn" in normalized_base_url or "zhipu" in normalized_base_url


def raise_for_status_with_detail(response: Response, provider: str) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = response.text[:500]
        raise RuntimeError(f"{provider} request failed with status {response.status_code}: {detail}") from exc
