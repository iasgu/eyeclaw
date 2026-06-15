from __future__ import annotations

import asyncio
from types import SimpleNamespace

from browser_use.llm.messages import SystemMessage, UserMessage
from pydantic import BaseModel

from src.deepseek_browser_use_llm import DeepSeekBrowserUseLLM, OpenAICompatibleBrowserUseLLM, _extract_json_object


class BrowserAction(BaseModel):
    action: str
    target: str


class FakeCompletions:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.response_text),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )


class FakeClient:
    def __init__(self, response_text: str):
        self.completions = FakeCompletions(response_text)
        self.chat = SimpleNamespace(completions=self.completions)


def test_deepseek_browser_use_llm_structured_call_never_sends_tool_choice() -> None:
    client = FakeClient('{"action":"click","target":"Start Test"}')
    llm = DeepSeekBrowserUseLLM(
        model="deepseek-chat",
        api_key="stub-key",
        base_url="https://api.deepseek.com",
        max_tokens=128,
        client=client,
    )

    result = asyncio.run(
        llm.ainvoke(
            [
                SystemMessage(content="You are a browser agent."),
                UserMessage(content="Click the start button."),
            ],
            output_format=BrowserAction,
        )
    )

    call = client.completions.calls[0]
    assert "tool_choice" not in call
    assert "tools" not in call
    assert "max_completion_tokens" not in call
    assert call["max_tokens"] == 128
    assert call["response_format"] == {"type": "json_object"}
    assert result.completion.action == "click"
    assert result.completion.target == "Start Test"


def test_deepseek_browser_use_llm_accepts_fenced_json() -> None:
    client = FakeClient('```json\n{"action":"click","target":"Start Test"}\n```')
    llm = DeepSeekBrowserUseLLM(model="deepseek-chat", client=client)

    result = asyncio.run(
        llm.ainvoke(
            [UserMessage(content="Return JSON.")],
            output_format=BrowserAction,
        )
    )

    assert result.completion.action == "click"


def test_openai_compatible_browser_use_llm_sends_extra_body() -> None:
    client = FakeClient('{"action":"click","target":"Start Test"}')
    llm = OpenAICompatibleBrowserUseLLM(
        model="glm-5.1",
        provider_name="glm",
        extra_body={"thinking": {"type": "disabled"}},
        client=client,
    )

    result = asyncio.run(
        llm.ainvoke(
            [UserMessage(content="Return JSON.")],
            output_format=BrowserAction,
        )
    )

    call = client.completions.calls[0]
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert llm.provider == "glm"
    assert result.completion.action == "click"


def test_extract_json_object_from_surrounding_text() -> None:
    assert _extract_json_object('result: {"action":"click","target":"A"} done') == '{"action":"click","target":"A"}'


def test_deepseek_browser_use_llm_exposes_browser_use_model_name() -> None:
    llm = DeepSeekBrowserUseLLM(model="deepseek-chat")

    assert llm.name == "deepseek-chat"
    assert llm.model_name == "deepseek-chat"
