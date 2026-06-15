from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


SupportedAction = Literal["open", "click", "type", "select", "wait", "scroll", "press"]


def normalize_action_name(value: str) -> str:
    normalized = str(value).strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "navigate": "open",
        "goto": "open",
        "go-to": "open",
        "input": "type",
        "enter-text": "type",
        "fill": "type",
        "choose": "select",
        "pick": "select",
        "pause": "wait",
        "key": "press",
        "keyboard": "press",
        "keypress": "press",
        "press-key": "press",
        "shortcut": "press",
        "hotkey": "press",
        "keyboard-shortcut": "press",
    }
    return aliases.get(normalized, normalized)


class ObservedAction(BaseModel):
    step_number: int = Field(ge=1)
    action: SupportedAction
    target: str = ""
    value: Optional[str] = None
    evidence: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, value: str) -> str:
        return normalize_action_name(value)


class ReplayStep(BaseModel):
    step_number: int = Field(ge=1)
    action: SupportedAction
    target: str = ""
    value: Optional[str] = None
    selector_hint: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, value: str) -> str:
        return normalize_action_name(value)

    @field_validator("target")
    @classmethod
    def ensure_required_target(cls, value: str, info) -> str:
        action = info.data.get("action")
        if action in {"click", "type", "select", "press"} and not value.strip():
            raise ValueError("target is required for click, type, select, and press actions")
        return value.strip()


class ReplayPlan(BaseModel):
    site_url: str
    steps: list[ReplayStep]

    @model_validator(mode="after")
    def validate_order(self) -> "ReplayPlan":
        step_numbers = [step.step_number for step in self.steps]
        if step_numbers != sorted(step_numbers):
            raise ValueError("step numbers must be in ascending order")
        if len(step_numbers) != len(set(step_numbers)):
            raise ValueError("step numbers must be unique")
        return self

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


class ReplayBundle(BaseModel):
    sop: list[str]
    plan: ReplayPlan
    assumptions: list[str] = Field(default_factory=list)
