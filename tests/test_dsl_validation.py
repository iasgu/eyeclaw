from pydantic import ValidationError

from src.dsl import ReplayPlan


def test_replay_plan_requires_ascending_step_numbers() -> None:
    try:
        ReplayPlan.model_validate(
            {
                "site_url": "https://example.com",
                "steps": [
                    {"step_number": 2, "action": "click", "target": "Next"},
                    {"step_number": 1, "action": "wait", "target": ""},
                ],
            }
        )
    except ValidationError as exc:
        assert "ascending" in str(exc)
    else:
        raise AssertionError("ReplayPlan should reject descending step numbers")


def test_replay_plan_requires_targets_for_click_type_select() -> None:
    try:
        ReplayPlan.model_validate(
            {
                "site_url": "https://example.com",
                "steps": [
                    {"step_number": 1, "action": "click", "target": ""},
                ],
            }
        )
    except ValidationError as exc:
        assert "target is required" in str(exc)
    else:
        raise AssertionError("ReplayPlan should reject missing click targets")
