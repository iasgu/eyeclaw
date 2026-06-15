from types import SimpleNamespace

from src.dsl import ReplayPlan
from src.replay import ReplaySession, choose_plan_page, normalized_host, should_create_execution_tab, should_open_plan_site


def make_page(url: str):
    return SimpleNamespace(url=url)


def test_should_create_execution_tab_for_console_page() -> None:
    page = make_page("http://127.0.0.1:8018/")
    assert should_create_execution_tab(page, "https://example.com/workflow") is True


def test_should_create_execution_tab_when_already_on_target_host() -> None:
    page = make_page("https://example.com/dashboard")
    assert should_create_execution_tab(page, "https://example.com/workflow") is True


def test_should_reuse_blank_execution_page() -> None:
    page = make_page("about:blank")
    assert should_create_execution_tab(page, "https://example.com/workflow") is False


def test_choose_plan_page_creates_dedicated_execution_page() -> None:
    new_page = make_page("about:blank")
    new_page.brought_to_front = False

    def bring_to_front() -> None:
        new_page.brought_to_front = True

    new_page.bring_to_front = bring_to_front
    context = SimpleNamespace(new_page=lambda: new_page)
    current_page = make_page("https://example.com/dashboard")
    session = ReplaySession(
        playwright=SimpleNamespace(stop=lambda: None),
        context=context,
        page=current_page,
        browser=None,
        owns_browser=False,
    )
    plan = ReplayPlan.model_validate(
        {
            "site_url": "https://example.com/workflow",
            "steps": [{"step_number": 1, "action": "click", "target": "进入"}],
        }
    )

    assert choose_plan_page(session, plan) is new_page


def test_should_open_plan_site_when_current_host_differs() -> None:
    page = make_page("http://127.0.0.1:8018/")
    plan = ReplayPlan.model_validate(
        {
            "site_url": "https://example.com/workflow",
            "steps": [{"step_number": 1, "action": "click", "target": "进入"}],
        }
    )
    assert should_open_plan_site(page, plan) is True


def test_should_not_open_plan_site_when_first_step_is_open() -> None:
    page = make_page("http://127.0.0.1:8018/")
    plan = ReplayPlan.model_validate(
        {
            "site_url": "https://example.com/workflow",
            "steps": [{"step_number": 1, "action": "open", "target": "", "value": "https://example.com/workflow"}],
        }
    )
    assert should_open_plan_site(page, plan) is False


def test_normalized_host_handles_empty() -> None:
    assert normalized_host("") == ""
