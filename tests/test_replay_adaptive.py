from types import SimpleNamespace

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.dsl import ReplayPlan
from src.replay import (
    current_page_looks_compatible_with_plan,
    execute_step,
    handle_select,
    handle_wait,
    parse_scroll_amount,
    resolve_locator,
    run_replay_plan,
    score_element_snapshot,
    target_text_variants,
    url_matches_target,
)
from src.webapp import _looks_abstract_target


def test_target_text_variants_extracts_quoted_phrase_and_strips_search_prefix() -> None:
    variants = target_text_variants("必应搜索结果列表第一条的标题“中国土地市场网”")

    assert variants[0] == "中国土地市场网"
    assert "必应搜索结果列表第一条的标题“中国土地市场网”" in variants


def test_score_element_snapshot_prefers_search_result_title_match() -> None:
    target_variants = target_text_variants("必应搜索结果列表第一条的标题“中国土地市场网”")
    strong = score_element_snapshot(
        {
            "index": 0,
            "tag": "a",
            "text": "中国土地市场网 - 官方网站",
            "aria_label": "",
            "title": "",
            "placeholder": "",
            "href": "https://www.landchina.com/",
            "role": "",
            "input_type": "",
            "visible": True,
            "y": 120,
        },
        target_variants=target_variants,
        action="click",
        site_url="https://www.bing.com/search?q=%E4%B8%AD%E5%9B%BD%E5%9C%9F%E5%9C%B0%E5%B8%82%E5%9C%BA%E7%BD%91",
        group_bonus=40,
    )
    weak = score_element_snapshot(
        {
            "index": 12,
            "tag": "a",
            "text": "设置",
            "aria_label": "",
            "title": "",
            "placeholder": "",
            "href": "https://www.bing.com/preferences",
            "role": "",
            "input_type": "",
            "visible": True,
            "y": 1800,
        },
        target_variants=target_variants,
        action="click",
        site_url="https://www.bing.com/search?q=%E4%B8%AD%E5%9B%BD%E5%9C%9F%E5%9C%B0%E5%B8%82%E5%9C%BA%E7%BD%91",
        group_bonus=10,
    )

    assert strong > weak
    assert strong >= 100


def test_looks_abstract_target_supports_chinese_search_phrases() -> None:
    assert _looks_abstract_target("必应搜索结果列表第一条的标题“中国土地市场网”") is True
    assert _looks_abstract_target("中国土地市场网") is False


def test_parse_scroll_amount_supports_direction_words() -> None:
    assert parse_scroll_amount("向上滚动") < 0
    assert parse_scroll_amount("scroll down 1200") == 1200
    assert parse_scroll_amount(None) == 900


def test_current_page_compatible_when_targets_already_visible() -> None:
    class FakeLocator:
        def inner_text(self, timeout=None):
            return "当前位置 浙江省 杭州市 查询按钮"

    page = SimpleNamespace(
        url="https://example.com/current",
        title=lambda: "任务页面",
        locator=lambda selector: FakeLocator(),
    )
    plan = ReplayPlan.model_validate(
        {
            "site_url": "https://different.example.com/start",
            "steps": [
                {"step_number": 1, "action": "select", "target": "浙江省"},
                {"step_number": 2, "action": "select", "target": "杭州市"},
            ],
        }
    )

    assert current_page_looks_compatible_with_plan(page, plan) is True


def test_url_matches_target_supports_decoded_and_hash_routes() -> None:
    assert url_matches_target(
        "https://example.com/#/givingNotice?city=%E6%9D%AD%E5%B7%9E",
        "https://example.com/#/givingNotice",
    )
    assert url_matches_target(
        "https://cn.bing.com/search?q=中国土地市场网",
        "https://cn.bing.com/search?q=%E4%B8%AD%E5%9B%BD",
    )


def test_handle_wait_treats_url_timeout_as_soft_stability_hint(monkeypatch) -> None:
    calls: list[str] = []

    class FakePage:
        url = "https://example.com/current"

        def wait_for_timeout(self, timeout):
            calls.append(f"timeout:{timeout}")

    def fake_wait_for_url_match(page, target, timeout_ms):
        raise PlaywrightTimeoutError("stale listener URL")

    monkeypatch.setattr("src.replay.wait_for_url_match", fake_wait_for_url_match)
    step = SimpleNamespace(step_number=3, target="https://example.com/old", selector_hint=None)

    handle_wait(FakePage(), step, progress_callback=calls.append)

    assert any("URL wait skipped" in item for item in calls)
    assert "timeout:800" in calls


def test_run_replay_plan_switches_to_new_page_matching_next_wait(monkeypatch) -> None:
    calls: list[tuple[int, str]] = []

    class FakePage:
        def __init__(self, url, title=""):
            self.url = url
            self._title = title
            self.context = None

        def title(self):
            return self._title

        def is_closed(self):
            return False

        def wait_for_timeout(self, timeout):
            return None

        def bring_to_front(self):
            return None

    class FakeContext:
        def __init__(self, pages):
            self.pages = pages

    home_page = FakePage("https://eia.51dzhp.com/#/", "大众环评")
    report_page = FakePage("https://eia.51dzhp.com/#/eia/environmentalReport", "列表搜文本")
    context = FakeContext([home_page])
    home_page.context = context
    report_page.context = context
    session = SimpleNamespace(context=context, page=home_page)
    plan = ReplayPlan.model_validate(
        {
            "site_url": "https://eia.51dzhp.com/#/",
            "steps": [
                {"step_number": 1, "action": "click", "target": "藏经阁"},
                {"step_number": 2, "action": "wait", "target": "https://eia.51dzhp.com/#/eia/environmentalReport"},
            ],
        }
    )

    monkeypatch.setattr("src.replay.prepare_execution_page", lambda session, replay_plan, emit=None: home_page)

    def fake_execute_step(page, site_url, step, progress_callback=None):
        calls.append((step.step_number, page.url))
        if step.step_number == 1:
            context.pages.append(report_page)

    monkeypatch.setattr("src.replay.execute_step", fake_execute_step)

    logs = run_replay_plan(session, plan)

    assert calls == [
        (1, "https://eia.51dzhp.com/#/"),
        (2, "https://eia.51dzhp.com/#/eia/environmentalReport"),
    ]
    assert any("Execution moved to browser page" in item for item in logs)


def test_handle_select_skips_native_select_timeout_for_custom_dropdown() -> None:
    calls: list[str] = []

    class FakeLocator:
        def evaluate(self, script):
            calls.append("tag-check")
            return False

        def select_option(self, *args, **kwargs):
            raise AssertionError("custom dropdowns should not call native select_option")

        def click(self, timeout=None):
            calls.append("trigger-click")

    class FakeBody:
        def inner_text(self, timeout=None):
            return "已选择 食品制造业"

    class FakePage:
        url = "https://eia.51dzhp.com/#/eia/environmentalReport"

        def evaluate(self, script, args):
            calls.append("option-click")
            return {"ok": True, "text": "食品制造业", "score": 100}

        def wait_for_timeout(self, timeout):
            calls.append(f"wait:{timeout}")

        def locator(self, selector):
            return FakeBody()

    step = SimpleNamespace(step_number=3, target="请选择", value="食品制造业")

    handle_select(FakePage(), FakeLocator(), step, progress_callback=calls.append)

    assert "tag-check" in calls
    assert "trigger-click" in calls
    assert "option-click" in calls


def test_resolve_locator_skips_hidden_text_matches() -> None:
    class FakeElement:
        def __init__(self, name, visible):
            self.name = name
            self.visible = visible

        def is_visible(self, timeout=None):
            return self.visible

        def is_enabled(self, timeout=None):
            return True

    class FakeLocator:
        def __init__(self, elements):
            self.elements = elements

        def count(self):
            return len(self.elements)

        def nth(self, index):
            return self.elements[index]

    hidden = FakeElement("hidden", False)
    visible = FakeElement("visible", True)
    empty = FakeLocator([])
    page = SimpleNamespace(
        get_by_text=lambda text, exact=False: FakeLocator([hidden, visible]),
        get_by_placeholder=lambda text, exact=False: empty,
        get_by_label=lambda text, exact=False: empty,
    )
    step = SimpleNamespace(step_number=1, action="click", target="Target", selector_hint=None)

    locator = resolve_locator(page, step)

    assert locator.name == "visible"


def test_execute_step_recovers_after_click_failure(monkeypatch) -> None:
    calls: list[str] = []

    class FailingLocator:
        def scroll_into_view_if_needed(self, timeout=None):
            calls.append("scroll")

        def click(self, timeout=None, force=False):
            calls.append("primary-click")
            raise PlaywrightTimeoutError("click blocked")

    page = SimpleNamespace(
        url="https://example.com/page",
        wait_for_timeout=lambda timeout: calls.append(f"wait:{timeout}"),
    )
    step = SimpleNamespace(step_number=1, action="click", target="Export", selector_hint=None)

    monkeypatch.setattr("src.replay.resolve_locator", lambda *args, **kwargs: FailingLocator())
    monkeypatch.setattr(
        "src.replay.recover_click",
        lambda page, step, site_url, progress_callback=None: calls.append(f"recover:{step.target}") or True,
    )

    execute_step(page, "https://example.com/page", step, progress_callback=calls.append)

    assert "primary-click" in calls
    assert "recover:Export" in calls
    assert any("primary click failed" in call for call in calls)
