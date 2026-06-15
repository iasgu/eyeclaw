from pydantic import ValidationError

from src.dsl import ObservedAction


def test_observed_action_accepts_supported_action() -> None:
    action = ObservedAction(
        step_number=1,
        action="click",
        target="Search",
        evidence="Button is highlighted in the next frame.",
        confidence=0.9,
    )

    assert action.action == "click"


def test_observed_action_accepts_keyboard_shortcut_alias() -> None:
    action = ObservedAction(
        step_number=1,
        action="keyboard-shortcut",
        target="Ctrl+S",
        evidence="The recording shows a save shortcut.",
        confidence=0.9,
    )

    assert action.action == "press"


def test_observed_action_rejects_unsupported_action() -> None:
    try:
        ObservedAction(step_number=1, action="drag", target="Card")
    except ValidationError as exc:
        assert "action" in str(exc)
    else:
        raise AssertionError("ObservedAction should reject unsupported actions")
