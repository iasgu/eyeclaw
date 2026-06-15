from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Sequence

import requests
from requests import Response
from requests.exceptions import RequestException

from src.config import AppConfig
from src.prompts import GLM_SYSTEM_PROMPT, build_glm_user_prompt


DEFAULT_FRAME_BATCH_SIZE = 3
BatchProgressCallback = Callable[[int, int], None]


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
        batch_progress_callback: BatchProgressCallback | None = None,
    ) -> dict[str, Any]:
        if not frame_paths:
            return {
                "session_summary": "",
                "observed_actions": [],
                "uncertainties": ["No frames were provided for GLM analysis."],
            }

        batches = list(split_frame_batches(frame_paths, frame_hints, batch_size=DEFAULT_FRAME_BATCH_SIZE))
        batch_outputs = []
        for batch_index, (batch_frame_paths, batch_hints) in enumerate(batches, start=1):
            batch_outputs.append(
                self._analyze_frame_batch(
                    frame_paths=batch_frame_paths,
                    site_url=site_url,
                    user_request=user_request,
                    frame_hints=batch_hints,
                    batch_index=batch_index,
                    batch_count=len(batches),
                )
            )
            if batch_progress_callback is not None:
                batch_progress_callback(batch_index, len(batches))
        if len(batch_outputs) == 1:
            return batch_outputs[0]
        return merge_batch_outputs(batch_outputs)

    def _analyze_frame_batch(
        self,
        frame_paths: list[Path],
        site_url: str,
        user_request: str | None = None,
        frame_hints: Sequence[str] | None = None,
        *,
        batch_index: int,
        batch_count: int,
    ) -> dict[str, Any]:
        prompt_text = build_glm_user_prompt(
            site_url,
            frame_paths,
            user_request=user_request,
            frame_hints=frame_hints,
        )
        if batch_count > 1:
            prompt_text += (
                f"\nThis batch is part {batch_index} of {batch_count} from one chronological workflow."
                "\nKeep the actions strictly in chronological order within this batch."
            )

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": prompt_text,
            }
        ]
        use_glm_format = _is_glm_model_or_endpoint(self._config.glm_model, self._config.glm_base_url)
        for frame_path in frame_paths:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_file_to_base64(frame_path)
                        if use_glm_format
                        else image_file_to_data_url(frame_path)
                    },
                }
            )

        payload = {
            "model": self._config.glm_model,
            "messages": [
                {"role": "system", "content": GLM_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        }
        if use_glm_format:
            payload["thinking"] = {"type": os.getenv("VLM_GLM_THINKING", "disabled")}
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


def split_frame_batches(
    frame_paths: Sequence[Path],
    frame_hints: Sequence[str] | None,
    *,
    batch_size: int,
) -> list[tuple[list[Path], list[str] | None]]:
    normalized_batch_size = max(1, int(batch_size))
    batches: list[tuple[list[Path], list[str] | None]] = []
    for start in range(0, len(frame_paths), normalized_batch_size):
        end = start + normalized_batch_size
        batch_paths = list(frame_paths[start:end])
        batch_hints = list(frame_hints[start:end]) if frame_hints is not None else None
        batches.append((batch_paths, batch_hints))
    return batches


def merge_batch_outputs(batch_outputs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    merged_actions: list[dict[str, Any]] = []
    merged_uncertainties: list[str] = []
    summaries: list[str] = []

    for batch_index, output in enumerate(batch_outputs, start=1):
        summary = str(output.get("session_summary") or "").strip()
        if summary:
            summaries.append(summary)

        for item in output.get("observed_actions", []) or []:
            action = dict(item)
            action["step_number"] = len(merged_actions) + 1
            merged_actions.append(action)

        for note in output.get("uncertainties", []) or []:
            text = str(note).strip()
            if text:
                merged_uncertainties.append(f"batch {batch_index}: {text}")

    return {
        "session_summary": " ".join(summaries).strip(),
        "observed_actions": merged_actions,
        "uncertainties": merged_uncertainties,
    }


def image_file_to_data_url(image_path: Path) -> str:
    suffix = image_path.suffix.lower().lstrip(".") or "png"
    mime_type = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
    return f"data:image/{mime_type};base64,{image_file_to_base64(image_path)}"


def image_file_to_base64(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("utf-8")


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
