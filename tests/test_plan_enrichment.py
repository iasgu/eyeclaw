from types import SimpleNamespace

import src.webapp as webapp_module


def test_enrich_plan_uses_listener_selector_and_real_target_text() -> None:
    plan = {
        "site_url": "https://example.com/list",
        "steps": [
            {
                "step_number": 1,
                "action": "click",
                "target": "first announcement",
            }
        ],
    }
    events = [
        SimpleNamespace(
            event_type="click",
            target_text="第一条公告",
            target_selector=".result-list > li:nth-of-type(1) a",
            client_timestamp_ms=1000,
        )
    ]

    enriched = webapp_module._enrich_plan_from_listener_events(plan, events)
    first_step = enriched["steps"][0]
    second_step = enriched["steps"][1]

    assert first_step["target"] == "第一条公告"
    assert first_step["selector_hint"] == ".result-list > li:nth-of-type(1) a"
    assert second_step["action"] == "wait"
    assert second_step["selector_hint"] == ".result-list > li:nth-of-type(1) a"


def test_enrich_plan_ignores_unrelated_listener_selector() -> None:
    plan = {
        "site_url": "https://example.com/list",
        "steps": [
            {
                "step_number": 1,
                "action": "click",
                "target": "第一条公告",
            }
        ],
    }
    events = [
        SimpleNamespace(
            event_type="click",
            target_text="查看更多",
            target_selector=".more-link",
            client_timestamp_ms=1000,
        )
    ]

    enriched = webapp_module._enrich_plan_from_listener_events(plan, events)
    step = enriched["steps"][0]
    assert step["target"] == "第一条公告"
    assert "selector_hint" not in step


def test_enrich_plan_restores_missing_parent_menu_click() -> None:
    plan = {
        "site_url": "https://example.com/list",
        "steps": [
            {
                "step_number": 1,
                "action": "click",
                "target": "出让公告",
            }
        ],
    }
    events = [
        SimpleNamespace(
            event_type="click",
            page_url="https://example.com/list",
            target_text="土地供应",
            target_selector=".menu-supply",
            client_timestamp_ms=1000,
        ),
        SimpleNamespace(
            event_type="click",
            page_url="https://example.com/list",
            target_text="出让公告",
            target_selector=".submenu-notice",
            client_timestamp_ms=1600,
        ),
    ]

    enriched = webapp_module._enrich_plan_from_listener_events(plan, events)

    assert [step["target"] for step in enriched["steps"][:3]] == ["土地供应", "出让公告", "https://example.com/list"]
    assert [step["step_number"] for step in enriched["steps"]] == [1, 2, 3]
    assert enriched["steps"][0]["selector_hint"] == ".menu-supply"
    assert enriched["steps"][2]["action"] == "wait"


def test_enrich_plan_inserts_url_wait_after_navigation_style_click() -> None:
    plan = {
        "site_url": "https://example.com/list",
        "steps": [
            {
                "step_number": 1,
                "action": "click",
                "target": "出让公告",
            }
        ],
    }
    events = [
        SimpleNamespace(
            event_type="click",
            page_url="https://example.com/#/givingNotice",
            target_text="出让公告",
            target_selector="a.notice-link",
            client_timestamp_ms=1000,
        )
    ]

    enriched = webapp_module._enrich_plan_from_listener_events(plan, events)

    assert enriched["steps"][1]["action"] == "wait"
    assert enriched["steps"][1]["target"] == "https://example.com/#/givingNotice"


def test_enrich_plan_does_not_restore_parent_from_unrelated_click() -> None:
    plan = {
        "site_url": "https://example.com/list",
        "steps": [
            {
                "step_number": 1,
                "action": "click",
                "target": "Export report",
            }
        ],
    }
    events = [
        SimpleNamespace(
            event_type="click",
            page_url="https://example.com/list",
            target_text="City picker",
            target_selector=".city-picker",
            client_timestamp_ms=1000,
        ),
        SimpleNamespace(
            event_type="click",
            page_url="https://example.com/list",
            target_text="Hangzhou",
            target_selector=".city-hangzhou",
            client_timestamp_ms=1600,
        ),
    ]

    enriched = webapp_module._enrich_plan_from_listener_events(plan, events)

    assert [step["target"] for step in enriched["steps"]] == ["Export report"]
