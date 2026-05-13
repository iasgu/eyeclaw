from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from src.config import AppConfig
from src.deepseek_client import DeepSeekClient
from src.dsl import ObservedAction, ReplayBundle
from src.glm_client import GLMClient


@dataclass
class AnalysisResult:
    sop: list[str]
    replay_bundle: ReplayBundle
    raw_glm_output: dict[str, Any]
    raw_deepseek_output: dict[str, Any]


def build_replay_plan(
    frame_paths: list[Path],
    config: AppConfig,
    user_request: str | None = None,
    site_url: str | None = None,
    frame_hints: Sequence[str] | None = None,
) -> AnalysisResult:
    glm_client = GLMClient(config)
    deepseek_client = DeepSeekClient(config)
    effective_site_url = site_url or config.target_site_url

    raw_glm_output = glm_client.analyze_frames(
        frame_paths=frame_paths,
        site_url=effective_site_url,
        user_request=user_request,
        frame_hints=frame_hints,
    )
    observed_actions = [ObservedAction.model_validate(item) for item in raw_glm_output.get("observed_actions", [])]
    raw_glm_output["observed_actions"] = [action.model_dump() for action in observed_actions]

    raw_deepseek_output = deepseek_client.normalize_plan(
        raw_analysis=raw_glm_output,
        site_url=effective_site_url,
        user_request=user_request,
    )
    replay_bundle = ReplayBundle.model_validate(raw_deepseek_output)

    return AnalysisResult(
        sop=replay_bundle.sop,
        replay_bundle=replay_bundle,
        raw_glm_output=raw_glm_output,
        raw_deepseek_output=raw_deepseek_output,
    )
