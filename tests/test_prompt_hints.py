from pathlib import Path

from src.prompts import (
    DEEPSEEK_SYSTEM_PROMPT,
    GLM_SYSTEM_PROMPT,
    build_deepseek_user_prompt,
    build_glm_user_prompt,
)


def test_build_glm_user_prompt_includes_frame_hints() -> None:
    prompt = build_glm_user_prompt(
        site_url="https://example.com",
        frame_paths=[Path("frame_01.png"), Path("frame_02.png")],
        user_request="请分析搜索结果页的操作步骤",
        frame_hints=[
            "click | target=Search",
            "page_loaded | title=Results",
        ],
    )

    assert "frame_01.png: click | target=Search" in prompt
    assert "frame_02.png: page_loaded | title=Results" in prompt
    assert "请分析搜索结果页的操作步骤" in prompt
    assert "目标网站" in prompt
    assert "监听提示" in prompt


def test_system_prompts_require_chinese_and_specific_targets() -> None:
    assert "输出必须优先使用中文" in GLM_SYSTEM_PROMPT
    assert "输出必须优先使用中文" in DEEPSEEK_SYSTEM_PROMPT
    assert "first announcement" in GLM_SYSTEM_PROMPT
    assert "公告保存" in DEEPSEEK_SYSTEM_PROMPT
    assert "必须保留为两个独立步骤" in DEEPSEEK_SYSTEM_PROMPT
    assert "你的目标不是写摘要" in GLM_SYSTEM_PROMPT
    assert "进入列表页不是打开详情页" in DEEPSEEK_SYSTEM_PROMPT


def test_build_deepseek_user_prompt_uses_chinese_context() -> None:
    prompt = build_deepseek_user_prompt(
        site_url="https://example.com",
        raw_analysis_json='{"observed_actions":[]}',
        user_request="整理成便于回放的中文步骤",
    )
    assert "目标网站" in prompt
    assert "SOP" in prompt
    assert "整理成便于回放的中文步骤" in prompt
