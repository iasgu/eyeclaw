from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, TypeVar, overload

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError
from openai.types.chat import ChatCompletionContentPartTextParam
from openai.types.chat.chat_completion import ChatCompletion
from pydantic import BaseModel, ValidationError

from browser_use.llm.exceptions import ModelProviderError, ModelRateLimitError
from browser_use.llm.messages import BaseMessage
from browser_use.llm.openai.serializer import OpenAIMessageSerializer
from browser_use.llm.schema import SchemaOptimizer
from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

T = TypeVar("T", bound=BaseModel)


@dataclass
class DeepSeekBrowserUseLLM:
    """DeepSeek adapter for Browser Use that never sends tool_choice."""

    model: str = "deepseek-chat"
    api_key: str | None = None
    base_url: str | httpx.URL | None = "https://api.deepseek.com"
    temperature: float | None = 0
    max_tokens: int | None = 2048
    timeout: float | httpx.Timeout | None = None
    max_retries: int = 0
    client: Any | None = None

    @property
    def provider(self) -> str:
        return "deepseek"

    @property
    def name(self) -> str:
        return str(self.model)

    @property
    def model_name(self) -> str:
        return str(self.model)

    def get_client(self) -> Any:
        if self.client is not None:
            return self.client
        return AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    @overload
    async def ainvoke(
        self,
        messages: list[BaseMessage],
        output_format: None = None,
        **kwargs: Any,
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self,
        messages: list[BaseMessage],
        output_format: type[T],
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T]: ...

    async def ainvoke(
        self,
        messages: list[BaseMessage],
        output_format: type[T] | None = None,
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        openai_messages = OpenAIMessageSerializer.serialize_messages(messages)
        model_params: dict[str, Any] = {}
        if self.temperature is not None:
            model_params["temperature"] = self.temperature
        if self.max_tokens is not None:
            model_params["max_tokens"] = self.max_tokens
        extra_body = getattr(self, "extra_body", None)
        if extra_body:
            model_params["extra_body"] = extra_body

        if output_format is not None:
            _append_json_schema_instruction(openai_messages, output_format)
            model_params["response_format"] = {"type": "json_object"}

        try:
            response = await self.get_client().chat.completions.create(
                model=self.model,
                messages=openai_messages,
                **model_params,
            )
        except RateLimitError as exc:
            raise ModelRateLimitError(str(exc), model=self.name) from exc
        except (APIStatusError, APIConnectionError, APITimeoutError) as exc:
            raise ModelProviderError(str(exc), model=self.name) from exc
        except Exception as exc:
            raise ModelProviderError(str(exc), model=self.name) from exc

        choice = response.choices[0] if response.choices else None
        if choice is None:
            raise ModelProviderError("DeepSeek returned no choices.", model=self.name)

        content = choice.message.content or ""
        usage = _get_usage(response)
        if output_format is None:
            return ChatInvokeCompletion(
                completion=content,
                usage=usage,
                stop_reason=choice.finish_reason,
            )

        try:
            parsed = output_format.model_validate_json(_extract_json_object(content))
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise ModelProviderError(
                f"Failed to parse DeepSeek structured JSON response: {exc}",
                model=self.name,
            ) from exc

        return ChatInvokeCompletion(
            completion=parsed,
            usage=usage,
            stop_reason=choice.finish_reason,
        )


@dataclass
class OpenAICompatibleBrowserUseLLM(DeepSeekBrowserUseLLM):
    """OpenAI-compatible adapter for providers that need custom request body fields."""

    provider_name: str = "openai-compatible"
    extra_body: dict[str, Any] | None = None

    @property
    def provider(self) -> str:
        return self.provider_name


def _append_json_schema_instruction(messages: list[dict[str, Any]], output_format: type[BaseModel]) -> None:
    schema = SchemaOptimizer.create_optimized_json_schema(
        output_format,
        remove_min_items=True,
        remove_defaults=True,
    )
    instruction = (
        "\n\nReturn only one valid JSON object, with no markdown fences or commentary. "
        "The JSON object must match this schema exactly:\n"
        f"{json.dumps({'name': output_format.__name__, 'schema': schema}, ensure_ascii=False)}"
    )
    if messages and messages[0].get("role") == "system":
        content = messages[0].get("content")
        if isinstance(content, str):
            messages[0]["content"] = content + instruction
            return
        if isinstance(content, Iterable):
            messages[0]["content"] = list(content) + [
                ChatCompletionContentPartTextParam(text=instruction, type="text")
            ]
            return
    messages.insert(0, {"role": "system", "content": "Return valid JSON." + instruction})


def _extract_json_object(content: str) -> str:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    raise ValueError("No JSON object found in DeepSeek response.")


def _get_usage(response: ChatCompletion) -> ChatInvokeUsage | None:
    if response.usage is None:
        return None
    prompt_details = getattr(response.usage, "prompt_tokens_details", None)
    return ChatInvokeUsage(
        prompt_tokens=response.usage.prompt_tokens,
        prompt_cached_tokens=getattr(prompt_details, "cached_tokens", None),
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=response.usage.completion_tokens,
        total_tokens=response.usage.total_tokens,
    )
