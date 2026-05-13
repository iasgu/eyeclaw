from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence


GLM_SYSTEM_PROMPT = """You analyze a short sequence of browser screenshots from a single workflow.

Return strict JSON with this shape:
{
  "session_summary": "short summary",
  "observed_actions": [
    {
      "step_number": 1,
      "action": "click|type|select|wait|scroll|open",
      "target": "visible UI target or page area",
      "value": "text entered if any",
      "evidence": "what in the frames supports this inference",
      "confidence": 0.0
    }
  ],
  "uncertainties": ["optional notes"]
}

Rules:
- Infer only what is visible from the screenshot sequence.
- Prefer concise targets using visible labels or placeholders.
- If a typed value is unclear, say so in evidence.
- Do not wrap JSON in markdown fences.
"""


DEEPSEEK_SYSTEM_PROMPT = """You normalize inferred browser actions into a compact replay plan.

Return strict JSON with this shape:
{
  "sop": [
    "short human-readable step"
  ],
  "plan": {
    "site_url": "full url",
    "steps": [
      {
        "step_number": 1,
        "action": "click|type|select|wait|scroll|open",
        "target": "target label",
        "value": "optional value",
        "selector_hint": "optional CSS/text hint",
        "notes": "optional replay note"
      }
    ]
  },
  "assumptions": [
    "optional assumption"
  ]
}

Rules:
- Keep the plan short and executable.
- Use only the supported actions.
- Preserve ordering.
- Do not wrap JSON in markdown fences.
"""


def build_glm_user_prompt(
    site_url: str,
    frame_paths: Iterable[Path],
    user_request: str | None = None,
    frame_hints: Sequence[str] | None = None,
) -> str:
    frame_path_list = list(frame_paths)
    frame_names = ", ".join(path.name for path in frame_path_list)
    prompt = (
        f"Target site: {site_url}\n"
        f"Frames in chronological order: {frame_names}\n"
        "Infer the most likely browser actions between adjacent frames."
    )
    if frame_hints:
        prompt += "\nFrame hints in chronological order:"
        for index, frame_path in enumerate(frame_path_list):
            hint = frame_hints[index] if index < len(frame_hints) else ""
            if hint:
                prompt += f"\n- {frame_path.name}: {hint}"
        prompt += "\nTreat the hints as soft cues. Prefer what is visible in the screenshots if they disagree."
    if user_request:
        prompt += f"\nUser request or desired outcome: {user_request}"
    return prompt


def build_deepseek_user_prompt(site_url: str, raw_analysis_json: str, user_request: str | None = None) -> str:
    prompt = (
        f"Target site: {site_url}\n"
        "Normalize the following raw browser workflow analysis into SOP bullets and a replay plan.\n"
        f"{raw_analysis_json}"
    )
    if user_request:
        prompt += f"\nPrioritize this user request when summarizing the workflow: {user_request}"
    return prompt
