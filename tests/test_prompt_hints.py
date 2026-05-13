from pathlib import Path

from src.prompts import build_glm_user_prompt


def test_build_glm_user_prompt_includes_frame_hints() -> None:
    prompt = build_glm_user_prompt(
        site_url="https://example.com",
        frame_paths=[Path("frame_01.png"), Path("frame_02.png")],
        user_request="Find the key workflow",
        frame_hints=[
            "click | target=Search",
            "page_loaded | title=Results",
        ],
    )

    assert "frame_01.png: click | target=Search" in prompt
    assert "frame_02.png: page_loaded | title=Results" in prompt
    assert "Find the key workflow" in prompt
