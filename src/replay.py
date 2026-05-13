from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, TimeoutError, sync_playwright

from src.config import AppConfig
from src.dsl import ReplayPlan, ReplayStep


ProgressCallback = Callable[[str], None]


@dataclass
class ReplaySession:
    playwright: Playwright
    context: BrowserContext | None
    page: Page
    browser: Browser | None = None
    owns_browser: bool = True


def build_selector_candidates(target: str, selector_hint: str | None = None) -> List[str]:
    candidates: List[str] = []
    cleaned_target = target.strip()
    if selector_hint:
        candidates.append(f"css={selector_hint}")
    if cleaned_target:
        candidates.extend(
            [
                f"text={cleaned_target}",
                f"placeholder={cleaned_target}",
                f"label={cleaned_target}",
            ]
        )
    return candidates


def start_replay_session(config: AppConfig) -> ReplaySession:
    user_data_dir = Path(config.edge_user_data_dir)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        channel=config.edge_channel,
        headless=False,
        viewport={"width": 1440, "height": 900},
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(config.target_site_url, wait_until="domcontentloaded")
    return ReplaySession(playwright=playwright, context=context, page=page, owns_browser=True)


def connect_over_cdp(cdp_url: str) -> ReplaySession:
    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.pages[0] if context.pages else context.new_page()
    return ReplaySession(
        playwright=playwright,
        context=context,
        page=page,
        browser=browser,
        owns_browser=False,
    )


def close_replay_session(session: ReplaySession | None) -> None:
    if session is None:
        return
    if session.browser is not None and session.owns_browser:
        session.browser.close()
    elif session.context is not None and session.owns_browser:
        session.context.close()
    session.playwright.stop()


def run_replay_plan(
    session: ReplaySession,
    replay_plan: ReplayPlan,
    progress_callback: ProgressCallback | None = None,
) -> List[str]:
    logs: List[str] = []

    def emit(message: str) -> None:
        logs.append(message)
        if progress_callback is not None:
            progress_callback(message)

    page = session.page
    for step in replay_plan.steps:
        emit(f"Step {step.step_number}: {step.action} -> {step.target or '(no target)'}")
        execute_step(page=page, site_url=replay_plan.site_url, step=step)
        emit(f"Step {step.step_number}: success")

    return logs


def execute_step(page: Page, site_url: str, step: ReplayStep) -> None:
    if step.action == "open":
        page.goto(step.value or site_url, wait_until="domcontentloaded")
        return

    if step.action == "wait":
        handle_wait(page, step)
        return

    if step.action == "scroll":
        amount = int(step.value) if step.value and step.value.isdigit() else 900
        page.mouse.wheel(0, amount)
        return

    locator = resolve_locator(page, step)
    if step.action == "click":
        locator.click(timeout=5_000)
        return
    if step.action == "type":
        locator.click(timeout=5_000)
        locator.fill(step.value or "")
        return
    if step.action == "select":
        if step.value:
            locator.select_option(label=step.value)
        else:
            locator.select_option(index=0)
        return

    raise ValueError(f"Unsupported replay action: {step.action}")


def handle_wait(page: Page, step: ReplayStep) -> None:
    if step.target.strip():
        candidates = build_selector_candidates(step.target, step.selector_hint)
        for candidate in candidates:
            try:
                wait_for_candidate(page, candidate, timeout_ms=3_000)
                return
            except TimeoutError:
                continue
    page.wait_for_timeout(1_500)


def resolve_locator(page: Page, step: ReplayStep):
    candidates = build_selector_candidates(step.target, step.selector_hint)
    for candidate in candidates:
        locator = locator_for_candidate(page, candidate)
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    raise ValueError(f"Unable to resolve locator for target: {step.target}")


def wait_for_candidate(page: Page, candidate: str, timeout_ms: int) -> None:
    locator = locator_for_candidate(page, candidate)
    locator.first.wait_for(timeout=timeout_ms)


def locator_for_candidate(page: Page, candidate: str):
    if candidate.startswith("css="):
        return page.locator(candidate[4:])
    if candidate.startswith("text="):
        return page.get_by_text(candidate[5:], exact=False)
    if candidate.startswith("placeholder="):
        return page.get_by_placeholder(candidate[12:], exact=False)
    if candidate.startswith("label="):
        return page.get_by_label(candidate[6:], exact=False)
    return page.locator(candidate)
