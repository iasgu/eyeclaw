from src.eia_workflow import (
    TEXT_CANGJINGGE,
    VALUE_CITY,
    VALUE_MAJOR_INDUSTRY,
    VALUE_PROVINCE,
    VALUE_SUB_INDUSTRY,
)


def test_eia_constants_match_expected_demo_values() -> None:
    assert TEXT_CANGJINGGE == "藏经阁"
    assert VALUE_MAJOR_INDUSTRY == "畜牧业"
    assert VALUE_SUB_INDUSTRY == "牲畜饲养 031"
    assert VALUE_PROVINCE == "浙江省"
    assert VALUE_CITY == "杭州市"
