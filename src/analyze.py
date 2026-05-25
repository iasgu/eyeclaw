from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

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


PlanProgressCallback = Callable[[str, int, int], None]


def build_replay_plan(
    frame_paths: list[Path],
    config: AppConfig,
    user_request: str | None = None,
    site_url: str | None = None,
    frame_hints: Sequence[str] | None = None,
    progress_callback: PlanProgressCallback | None = None,
) -> AnalysisResult:
    glm_client = GLMClient(config)
    deepseek_client = DeepSeekClient(config)
    effective_site_url = site_url or config.target_site_url

    def report(phase: str, current: int = 0, total: int = 0) -> None:
        if progress_callback is not None:
            progress_callback(phase, current, total)

    report("vision_started", 0, len(frame_paths))
    raw_glm_output = glm_client.analyze_frames(
        frame_paths=frame_paths,
        site_url=effective_site_url,
        user_request=user_request,
        frame_hints=frame_hints,
        batch_progress_callback=lambda current, total: report("vision_batch", current, total),
    )
    report("vision_completed", 1, 1)
    observed_actions = [ObservedAction.model_validate(item) for item in raw_glm_output.get("observed_actions", [])]
    raw_glm_output["observed_actions"] = [action.model_dump() for action in observed_actions]

    report("normalization_started", 0, 1)
    raw_deepseek_output = deepseek_client.normalize_plan(
        raw_analysis=raw_glm_output,
        site_url=effective_site_url,
        user_request=user_request,
    )
    report("normalization_completed", 1, 1)
    replay_bundle = ReplayBundle.model_validate(
        {
            "sop": _coerce_sop(raw_deepseek_output, observed_actions),
            "plan": compile_replay_plan_from_observed_actions(observed_actions, effective_site_url),
            "assumptions": _coerce_assumptions(raw_deepseek_output),
        }
    )

    return AnalysisResult(
        sop=replay_bundle.sop,
        replay_bundle=replay_bundle,
        raw_glm_output=raw_glm_output,
        raw_deepseek_output=raw_deepseek_output,
    )


def compile_replay_plan_from_observed_actions(
    observed_actions: Sequence[ObservedAction],
    site_url: str,
) -> dict[str, Any]:
    compiled_steps: list[dict[str, Any]] = []
    last_signature: tuple[str, str, str] | None = None

    for action in observed_actions:
        target = (action.target or "").strip()
        value = (action.value or "").strip() or None
        notes = _build_step_notes(action)
        step_payload = {
            "step_number": len(compiled_steps) + 1,
            "action": action.action,
            "target": target,
            "value": value,
            "notes": notes,
        }

        signature = (action.action, target, value or "")
        if signature == last_signature:
            continue
        compiled_steps.append(step_payload)
        last_signature = signature

    return {
        "site_url": site_url,
        "steps": compiled_steps,
    }


def _build_step_notes(action: ObservedAction) -> str | None:
    notes: list[str] = []
    evidence = str(action.evidence or "").strip()
    if evidence:
        notes.append(f"证据：{evidence}")
    if action.confidence < 0.55:
        notes.append("该步骤置信度偏低，执行时需要优先依赖页面状态校验。")
    if not notes:
        return None
    return " ".join(notes)


def _coerce_sop(raw_deepseek_output: dict[str, Any], observed_actions: Sequence[ObservedAction]) -> list[str]:
    raw_sop = raw_deepseek_output.get("sop")
    if isinstance(raw_sop, list):
        normalized = [str(item).strip() for item in raw_sop if str(item).strip()]
        if normalized:
            return normalized

    fallback: list[str] = []
    for index, action in enumerate(observed_actions, start=1):
        target = (action.target or "").strip()
        value = (action.value or "").strip()
        line = f"{index}. {action.action}"
        if target:
            line += f" {target}"
        if value:
            line += f"：{value}"
        fallback.append(line)
    return fallback


def _coerce_assumptions(raw_deepseek_output: dict[str, Any]) -> list[str]:
    raw_assumptions = raw_deepseek_output.get("assumptions")
    if not isinstance(raw_assumptions, list):
        return []
    return [str(item).strip() for item in raw_assumptions if str(item).strip()]
