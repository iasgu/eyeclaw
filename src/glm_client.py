from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Sequence

import requests
from requests import Response
from requests.exceptions import RequestException

from src.config import AppConfig
from src.prompts import GLM_SYSTEM_PROMPT, build_glm_user_prompt


class GLMClient:
    def __init__(self, config: AppConfig, timeout_seconds: int = 240) -> None:
        self._config = config
        self._timeout_seconds = timeout_seconds

    @property
    def endpoint(self) -> str:
        base_url = self._config.glm_base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        if base_url.endswith("/v4"):
            return f"{base_url}/chat/completions"
        return f"{base_url}/chat/completions"

    def analyze_frames(
        self,
        frame_paths: list[Path],
        site_url: str,
        user_request: str | None = None,
        frame_hints: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": build_glm_user_prompt(
                    site_url,
                    frame_paths,
                    user_request=user_request,
                    frame_hints=frame_hints,
                ),
            }
        ]
        for frame_path in frame_paths:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_file_to_data_url(frame_path)},
                }
            )

        payload = {
            "model": self._config.glm_model,
            "messages": [
                {"role": "system", "content": GLM_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        }
        response = post_chat_completion(
            endpoint=self.endpoint,
            api_key=self._config.glm_api_key,
            payload={**payload, "response_format": {"type": "json_object"}},
            timeout_seconds=self._timeout_seconds,
            provider="GLM",
        )
        response_json = response.json()
        content_text = response_json["choices"][0]["message"]["content"]
        return json.loads(extract_json_text(content_text))


def image_file_to_data_url(image_path: Path) -> str:
    suffix = image_path.suffix.lower().lstrip(".") or "png"
    mime_type = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
    encoded = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return f"data:image/{mime_type};base64,{encoded}"


def raise_for_status_with_detail(response: Response, provider: str) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = response.text[:500]
        raise RuntimeError(f"{provider} request failed with status {response.status_code}: {detail}") from exc


def post_chat_completion(
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    provider: str,
) -> Response:
    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout_seconds,
        )
    except RequestException as exc:
        raise RuntimeError(f"{provider} request could not be completed: {exc}") from exc

    if (
        response.status_code == 400
        and "response_format.type" in response.text
        and "not supported" in response.text.lower()
        and "response_format" in payload
    ):
        retry_payload = dict(payload)
        retry_payload.pop("response_format", None)
        try:
            response = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=retry_payload,
                timeout=timeout_seconds,
            )
        except RequestException as exc:
            raise RuntimeError(f"{provider} fallback request could not be completed: {exc}") from exc

    raise_for_status_with_detail(response, provider=provider)
    return response


def extract_json_text(content_text: str) -> str:
    stripped = content_text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped
