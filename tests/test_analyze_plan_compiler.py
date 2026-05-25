from src.analyze import compile_replay_plan_from_observed_actions
from src.dsl import ObservedAction


def test_compile_replay_plan_from_observed_actions_preserves_atomic_steps() -> None:
    actions = [
        ObservedAction(step_number=1, action="click", target="土地供应", evidence="点击顶部菜单"),
        ObservedAction(step_number=2, action="click", target="出让公告", evidence="点击下拉子项"),
        ObservedAction(step_number=3, action="click", target="第一条公告", evidence="点击列表首条结果"),
    ]

    plan = compile_replay_plan_from_observed_actions(actions, "https://www.landchina.com")

    assert plan["site_url"] == "https://www.landchina.com"
    assert [step["target"] for step in plan["steps"]] == ["土地供应", "出让公告", "第一条公告"]
    assert [step["step_number"] for step in plan["steps"]] == [1, 2, 3]


def test_compile_replay_plan_from_observed_actions_deduplicates_identical_neighbors() -> None:
    actions = [
        ObservedAction(step_number=1, action="click", target="查询", evidence="第一次点击"),
        ObservedAction(step_number=2, action="click", target="查询", evidence="第二次点击"),
    ]

    plan = compile_replay_plan_from_observed_actions(actions, "https://example.com")

    assert len(plan["steps"]) == 1
    assert plan["steps"][0]["target"] == "查询"
