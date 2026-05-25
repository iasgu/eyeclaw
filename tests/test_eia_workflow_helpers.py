from src.eia_workflow import (
    EIAWorkflowState,
    TEXT_CANGJINGGE,
    VALUE_CITY,
    VALUE_MAJOR_INDUSTRY,
    VALUE_PROVINCE,
    VALUE_SUB_INDUSTRY,
    is_supported_eia_state,
    unsupported_eia_site_message,
)


def test_eia_constants_match_expected_demo_values() -> None:
    assert TEXT_CANGJINGGE == "藏经阁"
    assert VALUE_MAJOR_INDUSTRY == "畜牧业"
    assert VALUE_SUB_INDUSTRY == "牲畜饲养 031"
    assert VALUE_PROVINCE == "浙江省"
    assert VALUE_CITY == "杭州市"


def test_unknown_non_eia_page_is_not_supported() -> None:
    state = EIAWorkflowState(
        page_role="unknown",
        page_index=0,
        url="https://example.com/dashboard",
        title="Example Dashboard",
        summary="Unable to classify the current page.",
    )
    assert is_supported_eia_state(state) is False
    message = unsupported_eia_site_message(state)
    assert "仅支持环评藏经阁 / 大众环评站点" in message
    assert "Example Dashboard" in message
    assert "https://example.com/dashboard" in message


def test_known_eia_page_is_supported() -> None:
    state = EIAWorkflowState(
        page_role="report_list",
        page_index=0,
        url="https://eia.51dzhp.com/#/eia/environmentalReport",
        title="大众环评 - 藏经阁",
        summary="Filtered report list page is open.",
    )
    assert is_supported_eia_state(state) is True
