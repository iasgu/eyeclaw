from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional
from urllib.parse import unquote, urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, TimeoutError, sync_playwright

from src.config import AppConfig
from src.dsl import ReplayPlan, ReplayStep


ProgressCallback = Callable[[str], None]
INTERACTIVE_ELEMENT_SELECTOR = (
    "a,button,[role='button'],[role='link'],input:not([type='hidden']),textarea,select,"
    "[contenteditable='true'],[tabindex]:not([tabindex='-1']),[aria-label],[title],[placeholder]"
)
SEARCH_RESULT_LINK_SELECTOR = (
    "#b_results h2 a,#b_results .b_algo a,.b_algo h2 a,.b_algo a,"
    "[data-testid='result'] a,main article a,main li a,li a[href]"
)
TEXT_INPUT_SELECTOR = (
    "input:not([type='hidden']):not([type='submit']):not([type='button']),"
    "textarea,[contenteditable='true'],[role='textbox'],[placeholder]"
)
SELECT_INPUT_SELECTOR = "select,[role='combobox'],[aria-haspopup='listbox'],[aria-expanded]"
ADAPTIVE_MIN_SCORE = 55
MAX_LOCATOR_CANDIDATES_TO_PROBE = 30
CONSOLE_HOSTS = {"127.0.0.1:8018", "localhost:8018", "127.0.0.1:8021", "localhost:8021"}
TEXT_CLICK_SELECTOR = (
    "a,button,[role='button'],[role='link'],[role='option'],[role='menuitem'],"
    ".el-select-dropdown__item,.el-cascader-node,.el-dropdown-menu__item,"
    ".ant-select-item-option,.ant-cascader-menu-item,li,[onclick],[tabindex]:not([tabindex='-1'])"
)


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
        normalized_hint = selector_hint.strip()
        if normalized_hint.startswith(("css=", "text=", "placeholder=", "label=", "xpath=")):
            candidates.append(normalized_hint)
        else:
            candidates.append(f"css={normalized_hint}")
    for candidate_text in target_text_variants(cleaned_target):
        candidates.extend(
            [
                f"text={candidate_text}",
                f"placeholder={candidate_text}",
                f"label={candidate_text}",
            ]
        )
    return dedupe_preserving_order(candidates)


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
    context = choose_best_browser_context(browser) if browser.contexts else browser.new_context()
    page = choose_best_context_page(context)
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

    page = prepare_execution_page(session, replay_plan, emit)
    for index, step in enumerate(replay_plan.steps):
        emit(f"Step {step.step_number}: {step.action} -> {step.target or '(no target)'}")
        execute_step(page=page, site_url=replay_plan.site_url, step=step, progress_callback=emit)
        emit(f"Step {step.step_number}: success")
        next_step = replay_plan.steps[index + 1] if index + 1 < len(replay_plan.steps) else None
        page = refresh_replay_page_after_step(session, page, next_step, emit)

    return logs


def refresh_replay_page_after_step(
    session: ReplaySession,
    current_page: Page,
    next_step: ReplayStep | None,
    emit: ProgressCallback | None = None,
) -> Page:
    context = session.context or getattr(current_page, "context", None)
    if context is None:
        return current_page

    if next_step is not None and next_step.action == "wait":
        candidate = wait_for_page_matching_step_target(context, next_step, current_page=current_page, timeout_ms=2_000)
        if candidate is not None and candidate != current_page:
            try:
                candidate.bring_to_front()
            except Exception:
                pass
            session.page = candidate
            if emit is not None:
                emit(f"Execution moved to browser page: {candidate.url or safe_page_title(candidate)}")
            return candidate

    try:
        if current_page.is_closed():
            candidate = choose_best_context_page(context)
            session.page = candidate
            return candidate
    except Exception:
        pass
    return current_page


def wait_for_page_matching_step_target(
    context: BrowserContext,
    step: ReplayStep,
    *,
    current_page: Page,
    timeout_ms: int,
) -> Page | None:
    target = step.target.strip()
    if not target:
        return None

    deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
    while True:
        candidate = find_page_matching_step_target(context, step)
        if candidate is not None:
            return candidate
        if time.monotonic() >= deadline:
            return None
        try:
            current_page.wait_for_timeout(100)
        except Exception:
            time.sleep(0.1)


def find_page_matching_step_target(context: BrowserContext, step: ReplayStep) -> Page | None:
    target = step.target.strip()
    if not target:
        return None
    pages = [
        page
        for page in reversed(list(getattr(context, "pages", []) or []))
        if page is not None and not page_is_closed(page)
    ]
    if _looks_like_url_wait_target(target):
        for page in pages:
            if url_matches_target(page.url or "", target):
                return page

    variants = target_text_variants(target)
    normalized_variants = [normalize_for_match(variant) for variant in variants if variant]
    if not normalized_variants:
        return None
    for page in pages:
        haystack = normalize_for_match(" ".join([page.url or "", safe_page_title(page)]))
        if any(variant and variant in haystack for variant in normalized_variants):
            return page
    return None


def page_is_closed(page: Page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return False


def prepare_execution_page(
    session: ReplaySession,
    replay_plan: ReplayPlan,
    emit: ProgressCallback | None = None,
) -> Page:
    page = choose_plan_page(session, replay_plan)
    if should_open_plan_site(page, replay_plan):
        if emit is not None:
            emit(f"Opening target site: {replay_plan.site_url}")
        page.goto(replay_plan.site_url, wait_until="domcontentloaded")
        if emit is not None:
            emit("Target site loaded.")
    return page


def execute_step(
    page: Page,
    site_url: str,
    step: ReplayStep,
    progress_callback: ProgressCallback | None = None,
) -> None:
    try:
        execute_step_once(page=page, site_url=site_url, step=step, progress_callback=progress_callback)
        return
    except Exception as exc:
        if recover_step_execution(
            page=page,
            site_url=site_url,
            step=step,
            original_error=exc,
            progress_callback=progress_callback,
        ):
            return
        raise


def execute_step_once(
    page: Page,
    site_url: str,
    step: ReplayStep,
    progress_callback: ProgressCallback | None = None,
) -> None:
    if step.action == "open":
        page.goto(step.value or site_url, wait_until="domcontentloaded")
        return

    if step.action == "wait":
        handle_wait(page, step, progress_callback=progress_callback)
        return

    if step.action == "scroll":
        handle_scroll(page, step)
        return

    if step.action == "press":
        handle_press(page, step, progress_callback=progress_callback)
        return

    locator = resolve_locator(page, step, site_url=site_url, progress_callback=progress_callback)
    if step.action == "click":
        click_locator(locator, timeout_ms=5_000)
        return
    if step.action == "type":
        fill_locator(locator, step.value or "")
        return
    if step.action == "select":
        handle_select(page, locator, step, progress_callback=progress_callback)
        return

    raise ValueError(f"Unsupported replay action: {step.action}")


def handle_press(page: Page, step: ReplayStep, progress_callback: ProgressCallback | None = None) -> None:
    shortcut = normalize_keyboard_shortcut(step.value or step.target)
    if not shortcut:
        raise ValueError("Keyboard shortcut is required for press action.")
    if progress_callback is not None:
        progress_callback(f"Step {step.step_number}: pressing keyboard shortcut {shortcut}")
    page.keyboard.press(shortcut)
    page.wait_for_timeout(500)


def normalize_keyboard_shortcut(value: str) -> str:
    aliases = {
        "ctrl": "Control",
        "control": "Control",
        "cmd": "Meta",
        "command": "Meta",
        "win": "Meta",
        "meta": "Meta",
        "alt": "Alt",
        "shift": "Shift",
        "esc": "Escape",
        "escape": "Escape",
        "space": "Space",
    }
    parts = [part.strip() for part in str(value or "").replace("-", "+").split("+") if part.strip()]
    normalized: list[str] = []
    for part in parts:
        mapped = aliases.get(part.lower())
        normalized.append(mapped or (part.upper() if len(part) == 1 else part))
    return "+".join(normalized)


def recover_step_execution(
    *,
    page: Page,
    site_url: str,
    step: ReplayStep,
    original_error: Exception,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    if step.action in {"open", "wait"}:
        return False
    if is_eyeclaw_console_page(page) and site_url.lower().startswith(("http://", "https://")) and not is_eyeclaw_console_url(site_url):
        if progress_callback is not None:
            progress_callback(f"Step {step.step_number}: recovery opens target site after console page was detected.")
        try:
            page.goto(site_url, wait_until="domcontentloaded")
        except Exception:
            return False

    if progress_callback is not None:
        progress_callback(
            f"Step {step.step_number}: primary {step.action} failed; trying adaptive recovery "
            f"({trim_reason_text(str(original_error), limit=160)})"
        )
    try:
        page.wait_for_timeout(350)
    except Exception:
        pass

    try:
        if step.action == "click":
            return recover_click(page, step, site_url=site_url, progress_callback=progress_callback)
        if step.action == "type":
            return recover_type(page, step, site_url=site_url, progress_callback=progress_callback)
        if step.action == "select":
            return recover_select(page, step, site_url=site_url, progress_callback=progress_callback)
        if step.action == "scroll":
            page.mouse.wheel(0, parse_scroll_amount(step.value))
            return True
    except Exception as recovery_error:
        if progress_callback is not None:
            progress_callback(
                f"Step {step.step_number}: adaptive recovery failed "
                f"({trim_reason_text(str(recovery_error), limit=160)})"
            )
    return False


def recover_click(
    page: Page,
    step: ReplayStep,
    *,
    site_url: str,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    adaptive_match = resolve_locator_from_page_state(page, step, site_url=site_url)
    if adaptive_match is not None:
        locator, reason = adaptive_match
        if progress_callback is not None:
            progress_callback(f"Step {step.step_number}: adaptive click locator selected: {reason}")
        if click_locator_with_recovery(locator):
            return True

    if click_dropdown_option(page, step.target, progress_callback=progress_callback):
        return True

    result = click_visible_text_match(page, step.target)
    if result.get("ok"):
        if progress_callback is not None:
            progress_callback(
                f"Step {step.step_number}: DOM text click recovered with "
                f"{result.get('text') or step.target} (score={result.get('score')})"
            )
        return True
    return False


def recover_type(
    page: Page,
    step: ReplayStep,
    *,
    site_url: str,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    adaptive_match = resolve_locator_from_page_state(page, step, site_url=site_url)
    if adaptive_match is not None:
        locator, reason = adaptive_match
        if progress_callback is not None:
            progress_callback(f"Step {step.step_number}: adaptive input locator selected: {reason}")
        fill_locator(locator, step.value or "")
        return True

    locator = first_usable_locator(page.locator(TEXT_INPUT_SELECTOR), action="type", matched_count=20)
    if locator is None:
        return False
    fill_locator(locator, step.value or "")
    if progress_callback is not None:
        progress_callback(f"Step {step.step_number}: filled the first usable input as a fallback.")
    return True


def recover_select(
    page: Page,
    step: ReplayStep,
    *,
    site_url: str,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    desired = (step.value or step.target).strip()
    if not desired:
        return False
    if click_dropdown_option(page, desired, progress_callback=progress_callback):
        return True
    adaptive_step = step.model_copy(update={"action": "click", "target": desired, "value": None})
    return recover_click(page, adaptive_step, site_url=site_url, progress_callback=progress_callback)


def click_locator(locator, *, timeout_ms: int = 5_000) -> None:
    try:
        locator.scroll_into_view_if_needed(timeout=1_500)
    except Exception:
        pass
    locator.click(timeout=timeout_ms)


def click_locator_with_recovery(locator) -> bool:
    attempts = [
        lambda: click_locator(locator, timeout_ms=2_000),
        lambda: locator.click(timeout=2_000, force=True),
        lambda: locator.evaluate("(element) => element.click()"),
    ]
    for attempt in attempts:
        try:
            attempt()
            return True
        except Exception:
            continue
    return False


def fill_locator(locator, value: str) -> None:
    try:
        locator.scroll_into_view_if_needed(timeout=1_500)
    except Exception:
        pass
    locator.click(timeout=3_000)
    try:
        locator.fill(value, timeout=3_000)
    except TypeError:
        locator.fill(value)


def click_visible_text_match(page: Page, target: str) -> dict[str, Any]:
    if is_eyeclaw_console_page(page):
        return {"ok": False, "reason": "console-page"}
    variants = target_text_variants(target)
    if not variants:
        return {"ok": False, "reason": "empty-target"}
    return page.evaluate(
        """
        ({ variants, selector }) => {
          const normalize = (value) => (value || '').normalize('NFKC').toLowerCase().replace(/[\\s\\W_]+/g, '');
          const normalizedVariants = variants.map(normalize).filter(Boolean);
          const isVisible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const isConsoleNoise = (node, text) => {
            const combined = [
              text || '',
              node.className || '',
              node.id || '',
              node.getAttribute('aria-label') || '',
              node.getAttribute('title') || ''
            ].join(' ').toLowerCase();
            return combined.includes('eyeclaw') || combined.includes('127.0.0.1:8018') || combined.includes('localhost:8018');
          };
          const nodes = Array.from(document.querySelectorAll(selector)).filter(isVisible);
          const candidates = nodes.map((node, index) => {
            const text = (node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
            if (!text || isConsoleNoise(node, text)) return null;
            const normalizedText = normalize(text);
            let score = 0;
            for (const variant of normalizedVariants) {
              if (!variant || !normalizedText) continue;
              if (variant === normalizedText) score = Math.max(score, 120);
              else if (normalizedText.includes(variant)) score = Math.max(score, 80);
              else if (variant.includes(normalizedText)) score = Math.max(score, 55);
            }
            if (score <= 0) return null;
            const tag = (node.tagName || '').toLowerCase();
            const role = (node.getAttribute('role') || '').toLowerCase();
            if (['a', 'button', 'input'].includes(tag) || ['button', 'link', 'option', 'menuitem'].includes(role)) score += 20;
            if (node.matches('.el-cascader-node,.el-select-dropdown__item,.ant-select-item-option,.ant-cascader-menu-item')) score += 18;
            const normalizedLength = normalizedText.length;
            const shortestVariant = normalizedVariants.reduce((best, item) => Math.min(best, item.length), 9999);
            if (shortestVariant < 9999 && normalizedLength > shortestVariant * 8) score -= 35;
            if (normalizedLength > 180) score -= 45;
            return { node, index, text, score, y: node.getBoundingClientRect().y };
          }).filter(Boolean);
          candidates.sort((a, b) => (b.score - a.score) || (a.y - b.y));
          const best = candidates[0];
          if (!best || best.score < 55) {
            return { ok: false, reason: 'no-visible-text-match', candidateCount: candidates.length };
          }
          best.node.scrollIntoView({ block: 'center', inline: 'nearest' });
          best.node.click();
          return { ok: true, text: best.text, score: best.score, index: best.index };
        }
        """,
        {"variants": variants, "selector": TEXT_CLICK_SELECTOR},
    )


def is_eyeclaw_console_page(page: Page) -> bool:
    return is_eyeclaw_console_url(page.url or "")


def is_eyeclaw_console_url(raw_url: str | None) -> bool:
    if not raw_url:
        return False
    lowered = raw_url.strip().lower()
    if lowered.startswith(("edge://", "chrome://", "devtools://", "chrome-extension://")):
        return True
    return normalized_host(lowered) in CONSOLE_HOSTS


def handle_scroll(page: Page, step: ReplayStep) -> None:
    if step.target.strip():
        target_variants = target_text_variants(step.target)
        for text in target_variants:
            try:
                target_locator = page.get_by_text(text, exact=False).first
                if target_locator.count() > 0:
                    target_locator.scroll_into_view_if_needed(timeout=3_000)
                    return
            except Exception:
                continue

    amount = parse_scroll_amount(step.value)
    page.mouse.wheel(0, amount)
    page.wait_for_timeout(400)


def handle_select(
    page: Page,
    locator,
    step: ReplayStep,
    progress_callback: ProgressCallback | None = None,
) -> None:
    desired = (step.value or step.target).strip()
    if not desired:
        locator.select_option(index=0)
        return

    if locator_is_native_select(locator):
        try:
            locator.select_option(label=desired, timeout=2_500)
            verify_selection_state(page, desired)
            return
        except Exception:
            pass

    try:
        locator.click(timeout=3_000)
    except Exception:
        pass

    if click_dropdown_option(page, desired, progress_callback=progress_callback):
        verify_selection_state(page, desired)
        return

    adaptive_step = step.model_copy(update={"action": "click", "target": desired, "value": None})
    adaptive_match = resolve_locator_from_page_state(page, adaptive_step, site_url=page.url)
    if adaptive_match is not None:
        option_locator, reason = adaptive_match
        if progress_callback is not None:
            progress_callback(f"Adaptive select option recovered for step {step.step_number}: {reason}")
        option_locator.scroll_into_view_if_needed(timeout=3_000)
        option_locator.click(timeout=5_000)
        verify_selection_state(page, desired)
        return

    raise ValueError(f"Unable to select option: {desired}")


def locator_is_native_select(locator) -> bool:
    try:
        return bool(locator.evaluate("(element) => (element.tagName || '').toLowerCase() === 'select'"))
    except Exception:
        return False


def handle_wait(
    page: Page,
    step: ReplayStep,
    progress_callback: ProgressCallback | None = None,
) -> None:
    target = step.target.strip()
    if target:
        if _looks_like_url_wait_target(target):
            try:
                wait_for_url_match(page, target, timeout_ms=5_000)
            except TimeoutError as exc:
                if progress_callback is not None:
                    progress_callback(f"URL wait skipped for step {step.step_number}: {exc}")
                page.wait_for_timeout(800)
            return

        candidates = build_selector_candidates(target, step.selector_hint)
        for candidate in candidates:
            try:
                wait_for_candidate(page, candidate, timeout_ms=3_000)
                return
            except TimeoutError:
                continue
    page.wait_for_timeout(1_500)


def resolve_locator(
    page: Page,
    step: ReplayStep,
    *,
    site_url: str = "",
    progress_callback: ProgressCallback | None = None,
):
    candidates = build_selector_candidates(step.target, step.selector_hint)
    for candidate in candidates:
        locator = locator_for_candidate(page, candidate)
        try:
            matched_count = locator.count()
            if matched_count <= 0:
                continue
            usable_locator = first_usable_locator(locator, action=step.action, matched_count=matched_count)
            if usable_locator is not None:
                return usable_locator
            if progress_callback is not None:
                progress_callback(
                    f"Locator candidate skipped for step {step.step_number}: "
                    f"matched {matched_count} hidden or disabled elements ({trim_reason_text(candidate)})"
                )
        except Exception:
            continue
    adaptive_match = resolve_locator_from_page_state(page, step, site_url=site_url)
    if adaptive_match is not None:
        locator, reason = adaptive_match
        if progress_callback is not None:
            progress_callback(f"Adaptive locator recovered for step {step.step_number}: {reason}")
        return locator

    page_title = safe_page_title(page)
    raise ValueError(
        "Unable to resolve locator for target: "
        f"{step.target}; page_url={page.url}; page_title={page_title}; "
        f"target_variants={target_text_variants(step.target)}"
    )


def first_usable_locator(locator, *, action: str, matched_count: int):
    probe_count = min(max(matched_count, 0), MAX_LOCATOR_CANDIDATES_TO_PROBE)
    for index in range(probe_count):
        candidate = locator.nth(index)
        if locator_is_usable(candidate, action=action):
            return candidate
    return None


def locator_is_usable(locator, *, action: str) -> bool:
    try:
        if not locator.is_visible(timeout=250):
            return False
    except Exception:
        return False

    if action in {"click", "type", "select"}:
        try:
            if not locator.is_enabled(timeout=250):
                return False
        except Exception:
            pass
    return True


def resolve_locator_from_page_state(page: Page, step: ReplayStep, *, site_url: str = "") -> tuple[Any, str] | None:
    target_variants = target_text_variants(step.target)
    selector_groups = adaptive_selector_groups(page, step)
    best: tuple[int, Any, str] | None = None

    for group_name, selector, group_bonus in selector_groups:
        try:
            locator = page.locator(selector)
            snapshots = collect_element_snapshots(locator)
        except Exception:
            continue

        for snapshot in snapshots:
            score = score_element_snapshot(
                snapshot,
                target_variants=target_variants,
                action=step.action,
                site_url=site_url,
                group_bonus=group_bonus,
            )
            if score <= 0:
                continue
            index = int(snapshot.get("index") or 0)
            text = trim_reason_text(snapshot.get("text") or snapshot.get("aria_label") or snapshot.get("placeholder") or "")
            href = trim_reason_text(snapshot.get("href") or "")
            reason = f"{group_name}, score={score}, text={text or '(empty)'}, href={href or '(none)'}"
            candidate = (score, locator.nth(index), reason)
            if best is None or candidate[0] > best[0]:
                best = candidate

    if best is None or best[0] < adaptive_score_threshold(step, target_variants):
        return None
    return best[1], best[2]


def wait_for_candidate(page: Page, candidate: str, timeout_ms: int) -> None:
    locator = locator_for_candidate(page, candidate)
    locator.first.wait_for(timeout=timeout_ms)


def wait_for_url_match(page: Page, target: str, timeout_ms: int) -> None:
    expected = target.strip()
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0

    while True:
        current_url = page.url or ""
        if url_matches_target(current_url, expected):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=min(max(timeout_ms, 1), 1_500))
            except TimeoutError:
                pass
            return

        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for URL like {expected!r}; current_url={current_url!r}")

        page.wait_for_timeout(100)


def url_matches_target(current_url: str, target: str) -> bool:
    current = (current_url or "").strip()
    expected = (target or "").strip()
    if not current or not expected:
        return False
    if current == expected or expected in current:
        return True

    decoded_current = unquote_safe(current)
    decoded_expected = unquote_safe(expected)
    if decoded_current == decoded_expected or decoded_expected in decoded_current:
        return True

    current_parts = urlparse(current)
    expected_parts = urlparse(expected)
    if expected_parts.netloc and current_parts.netloc != expected_parts.netloc:
        return False
    if expected_parts.path and current_parts.path != expected_parts.path:
        return False
    if expected_parts.fragment and expected_parts.fragment not in current_parts.fragment:
        return False
    if expected_parts.query and expected_parts.query not in current_parts.query:
        return False
    return bool(expected_parts.netloc or expected_parts.path or expected_parts.fragment or expected_parts.query)


def unquote_safe(value: str) -> str:
    try:
        return unquote(value)
    except Exception:
        return value


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


def _looks_like_url_wait_target(target: str) -> bool:
    lowered = target.lower()
    return lowered.startswith("http://") or lowered.startswith("https://") or "#/" in lowered or "/" in lowered


def choose_plan_page(session: ReplaySession, replay_plan: ReplayPlan) -> Page:
    if session.context is not None:
        if should_create_execution_tab(session.page, replay_plan.site_url):
            page = session.context.new_page()
            session.page = page
            return page
    return session.page


def find_page_for_site(context: BrowserContext, site_url: str) -> Page | None:
    target_host = normalized_host(site_url)
    if not target_host:
        return None

    raw_pages = getattr(context, "pages", [])
    candidates = [page for page in raw_pages if page and not getattr(page, "is_closed", lambda: False)()]
    exact_matches = [page for page in candidates if normalized_host(page.url) == target_host]
    if exact_matches:
        return exact_matches[-1]
    partial_matches = [page for page in candidates if target_host in normalized_host(page.url)]
    if partial_matches:
        return partial_matches[-1]
    return None


def should_create_execution_tab(page: Page, site_url: str) -> bool:
    target_host = normalized_host(site_url)
    if not target_host:
        return False
    current_url = str(getattr(page, "url", "") or "").strip()
    if not current_url or current_url.startswith("about:"):
        return False
    return True


def should_open_plan_site(page: Page, replay_plan: ReplayPlan) -> bool:
    if not replay_plan.site_url:
        return False
    if replay_plan.steps and replay_plan.steps[0].action == "open":
        return False
    if current_page_looks_compatible_with_plan(page, replay_plan):
        return False
    target_host = normalized_host(replay_plan.site_url)
    if not target_host:
        return False
    return normalized_host(page.url) != target_host


def current_page_looks_compatible_with_plan(page: Page, replay_plan: ReplayPlan) -> bool:
    url = page.url or ""
    if url.startswith("edge://") or url.startswith("chrome://") or url.startswith("devtools://"):
        return False
    if normalized_host(url) in {"127.0.0.1:8018", "localhost:8018", "127.0.0.1:8021", "localhost:8021"}:
        return False
    try:
        page_text = page.locator("body").inner_text(timeout=1_200)
    except Exception:
        page_text = ""
    haystack = normalize_for_match(" ".join([url, safe_page_title(page), page_text[:4000]]))
    if not haystack:
        return False

    meaningful_targets = []
    for step in replay_plan.steps[:5]:
        if step.action in {"click", "type", "select", "wait"}:
            meaningful_targets.extend(target_text_variants(step.target))
            if step.value:
                meaningful_targets.extend(target_text_variants(step.value))

    matches = 0
    for target in meaningful_targets:
        normalized_target = normalize_for_match(target)
        if normalized_target and normalized_target in haystack:
            matches += 1
    return matches >= 2 or (matches >= 1 and normalized_host(url) and normalized_host(url) != normalized_host(replay_plan.site_url))


def normalized_host(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def choose_best_context_page(context: BrowserContext) -> Page:
    pages = [page for page in context.pages if page]
    if not pages:
        return context.new_page()

    ranked_pages = sorted(pages, key=page_priority, reverse=True)
    return ranked_pages[0]


def choose_best_browser_context(browser: Browser) -> BrowserContext:
    contexts = [context for context in browser.contexts if context]
    if not contexts:
        return browser.new_context()
    ranked_contexts = sorted(contexts, key=context_priority, reverse=True)
    return ranked_contexts[0]


def context_priority(context: BrowserContext) -> tuple[int, int]:
    pages = [page for page in context.pages if page and not page.is_closed()]
    if not pages:
        return (-100, 0)
    page_scores = sorted((page_priority(page) for page in pages), reverse=True)
    best_page_score = page_scores[0][0] if page_scores else 0
    return (best_page_score, len(pages))


def page_priority(page: Page) -> tuple[int, int, int]:
    url = page.url or ""
    title = safe_page_title(page).lower()
    host = urlparse(url).netloc.lower()

    score = 0
    if page == page.context.pages[-1]:
        score += 5
    if page.is_closed():
        score -= 100
    if url.startswith("devtools://") or url.startswith("chrome://") or url.startswith("edge://"):
        score -= 50
    if host:
        score += 10
    if "51dzhp.com" in host:
        score += 80
    if "environmentalreport" in url.lower() or "previewpdf" in url.lower():
        score += 60
    if "login" in url.lower():
        score += 30
    if "大众环评" in title or "环评" in title:
        score += 40

    return (score, len(title), len(url))


def safe_page_title(page: Page) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def adaptive_selector_groups(page: Page, step: ReplayStep) -> list[tuple[str, str, int]]:
    selectors: list[tuple[str, str, int]] = []
    if is_search_result_like(page, step):
        selectors.append(("search-result", SEARCH_RESULT_LINK_SELECTOR, 40))
    if step.action == "type":
        selectors.append(("text-input", TEXT_INPUT_SELECTOR, 30))
    elif step.action == "select":
        selectors.append(("select-input", SELECT_INPUT_SELECTOR, 30))
    selectors.append(("interactive", INTERACTIVE_ELEMENT_SELECTOR, 10))
    return selectors


def collect_element_snapshots(locator) -> list[dict[str, Any]]:
    return locator.evaluate_all(
        """
        (elements) => elements.map((element, index) => {
          const rect = element.getBoundingClientRect();
          const style = window.getComputedStyle(element);
          const text = (element.innerText || element.textContent || element.getAttribute('aria-label') || element.getAttribute('title') || element.getAttribute('placeholder') || '').replace(/\\s+/g, ' ').trim();
          return {
            index,
            tag: (element.tagName || '').toLowerCase(),
            text,
            aria_label: element.getAttribute('aria-label') || '',
            title: element.getAttribute('title') || '',
            placeholder: element.getAttribute('placeholder') || '',
            href: element.href || '',
            role: element.getAttribute('role') || '',
            input_type: element.getAttribute('type') || '',
            visible: Boolean(element.offsetParent) && rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none',
            y: rect.y
          };
        })
        """
    )


def score_element_snapshot(
    snapshot: dict[str, Any],
    *,
    target_variants: list[str],
    action: str,
    site_url: str,
    group_bonus: int = 0,
) -> int:
    if not snapshot.get("visible", True):
        return 0

    score = group_bonus
    tag = str(snapshot.get("tag") or "").lower()
    role = str(snapshot.get("role") or "").lower()
    input_type = str(snapshot.get("input_type") or "").lower()
    candidate_texts = [
        str(snapshot.get("text") or ""),
        str(snapshot.get("aria_label") or ""),
        str(snapshot.get("title") or ""),
        str(snapshot.get("placeholder") or ""),
        str(snapshot.get("href") or ""),
    ]
    normalized_fields = [normalize_for_match(text) for text in candidate_texts if text]
    normalized_variants = [normalize_for_match(text) for text in target_variants if text]

    if action == "click":
        if tag in {"a", "button"} or role in {"button", "link"}:
            score += 20
    elif action == "type":
        if tag in {"input", "textarea"} or role == "textbox" or input_type not in {"hidden", "submit", "button"}:
            score += 25
    elif action == "select":
        if tag == "select" or role in {"combobox", "listbox"}:
            score += 25

    if is_search_result_like_text(" ".join(target_variants)):
        if tag == "a" or "result" in role:
            score += 15

    for variant in normalized_variants:
        if not variant:
            continue
        for field in normalized_fields:
            if not field:
                continue
            if variant == field:
                score += 100
            elif variant in field or field in variant:
                score += 60

    site_host = normalized_host(site_url)
    href_host = normalized_host(str(snapshot.get("href") or ""))
    if site_host and href_host and site_host == href_host:
        score += 15

    if is_ordinal_target(" ".join(target_variants)):
        y_position = int(float(snapshot.get("y") or 0))
        score += max(0, 20 - min(y_position // 120, 20))

    return score


def normalize_for_match(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s\W_]+", "", normalized)


def target_text_variants(target: str) -> list[str]:
    cleaned = trim_reason_text(target, limit=400)
    if not cleaned:
        return []

    variants: list[str] = []
    variants.extend(extract_quoted_variants(cleaned))

    stripped = strip_abstract_prefix(cleaned)
    if stripped:
        variants.append(stripped)

    if cleaned not in variants:
        variants.append(cleaned)

    final_variants: list[str] = []
    seen = set()
    for variant in variants:
        normalized = trim_reason_text(variant, limit=400)
        key = normalize_for_match(normalized)
        if not normalized or not key or key in seen:
            continue
        seen.add(key)
        final_variants.append(normalized)
    return final_variants


def extract_quoted_variants(value: str) -> list[str]:
    variants: list[str] = []
    for pattern in [r"[“\"]([^”\"]+)[”\"]", r"[‘']([^’']+)[’']"]:
        for match in re.findall(pattern, value):
            text = trim_reason_text(match, limit=400)
            if text:
                variants.append(text)
    return variants


def strip_abstract_prefix(value: str) -> str:
    text = trim_reason_text(value, limit=400)
    if not text:
        return ""

    patterns = [
        r"^(必应|bing|百度|google|搜狗|360)?搜索结果列表第[一二三四五六七八九十0-9]+条的?(标题|链接|名称)?[:：\s]*",
        r"^(搜索结果列表第[一二三四五六七八九十0-9]+条|第[一二三四五六七八九十0-9]+条|首条|第一条|第1条)[:：\s]*",
        r"^(搜索结果|结果列表|列表|标题|链接|按钮|入口|结果|项|条目)[:：\s]*",
    ]
    stripped = text
    for pattern in patterns:
        stripped = re.sub(pattern, "", stripped, flags=re.IGNORECASE)

    if "的" in stripped:
        tail = stripped.split("的")[-1].strip()
        if tail and len(tail) < len(stripped):
            stripped = tail

    stripped = stripped.strip(" “”\"'‘’。．,，:：;；()（）[]【】{}<>")
    return stripped


def is_search_result_like(page: Page, step: ReplayStep) -> bool:
    haystack = " ".join([page.url or "", safe_page_title(page), step.target, step.selector_hint or ""]).lower()
    return is_search_result_like_text(haystack)


def is_search_result_like_text(value: str) -> bool:
    lowered = value.lower()
    keywords = ["bing", "search", "搜索结果", "结果列表", "必应", "搜索", "结果", "第一条", "首条"]
    return any(keyword in lowered for keyword in keywords)


def is_ordinal_target(value: str) -> bool:
    lowered = normalize_for_match(value)
    keywords = ["first", "second", "third", "firstresult", "firstitem", "第一条", "首条", "第1条", "第一个", "第一项"]
    return any(normalize_for_match(keyword) in lowered for keyword in keywords)


def adaptive_score_threshold(step: ReplayStep, target_variants: list[str]) -> int:
    base = ADAPTIVE_MIN_SCORE
    if step.action == "type":
        base += 5
    if is_search_result_like_text(" ".join(target_variants)):
        base -= 10
    if is_ordinal_target(" ".join(target_variants)):
        base -= 10
    return max(35, base)


def trim_reason_text(value: str, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def dedupe_preserving_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen = set()
    for value in values:
        key = value.strip()
        if not key:
            continue
        normalized = normalize_for_match(key)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(key)
    return deduped


def parse_scroll_amount(value: str | None) -> int:
    if not value:
        return 900
    text = str(value).strip().lower()
    number_match = re.search(r"-?\d+", text)
    if number_match:
        return int(number_match.group(0))
    if any(keyword in text for keyword in ["up", "上"]):
        return -900
    return 900


def click_dropdown_option(
    page: Page,
    desired: str,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    desired_variants = target_text_variants(desired)
    for attempt in range(12):
        result = page.evaluate(
            """
            ({ variants }) => {
              const normalize = (value) => (value || '').normalize('NFKC').toLowerCase().replace(/[\\s\\W_]+/g, '');
              const normalizedVariants = variants.map(normalize).filter(Boolean);
              const isVisible = (node) => {
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const optionSelector = [
                '[role="option"]',
                '[role="menuitem"]',
                '.el-select-dropdown__item',
                '.el-cascader-node',
                '.el-dropdown-menu__item',
                '.ant-select-item-option',
                '.ant-cascader-menu-item',
                '.select2-results__option',
                'li',
                'option'
              ].join(',');
              const options = Array.from(document.querySelectorAll(optionSelector)).filter(isVisible);
              const candidates = options.map((node, index) => {
                const text = (node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
                const normalizedText = normalize(text);
                let score = 0;
                for (const variant of normalizedVariants) {
                  if (!variant || !normalizedText) continue;
                  if (variant === normalizedText) score = Math.max(score, 100);
                  else if (normalizedText.includes(variant) || variant.includes(normalizedText)) score = Math.max(score, 70);
                }
                return { node, index, text, score };
              }).filter((item) => item.score > 0);
              candidates.sort((a, b) => b.score - a.score);
              const best = candidates[0];
              if (!best) {
                const scrollSelector = [
                  '.el-select-dropdown .el-scrollbar__wrap',
                  '.el-select-dropdown__wrap',
                  '.el-cascader-menu',
                  '.el-dropdown-menu',
                  '.ant-select-dropdown .rc-virtual-list-holder',
                  '.ant-cascader-dropdown .ant-cascader-menu',
                  '.select2-results__options',
                  '[role="listbox"]',
                  '[role="menu"]',
                  '.dropdown-menu'
                ].join(',');
                const containers = Array.from(document.querySelectorAll(scrollSelector)).filter(isVisible);
                let scrolled = false;
                for (const container of containers) {
                  const before = container.scrollTop;
                  const maxScroll = Math.max(0, container.scrollHeight - container.clientHeight);
                  if (maxScroll > before) {
                    container.scrollTop = Math.min(maxScroll, before + Math.max(220, container.clientHeight * 0.85));
                    container.dispatchEvent(new Event('scroll', { bubbles: true }));
                    scrolled = scrolled || container.scrollTop !== before;
                  }
                }
                return {
                  ok: false,
                  reason: 'option-not-visible',
                  scrolled,
                  visibleOptions: options.slice(0, 20).map((node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim())
                };
              }
              best.node.scrollIntoView({ block: 'center', inline: 'nearest' });
              best.node.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
              best.node.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
              best.node.click();
              return { ok: true, text: best.text, score: best.score };
            }
            """,
            {"variants": desired_variants},
        )
        if result and result.get("ok"):
            if progress_callback is not None:
                progress_callback(f"Selected dropdown option: {result.get('text')} (score={result.get('score')})")
            return True

        if not result or not result.get("scrolled"):
            page.mouse.wheel(0, 650)
        page.wait_for_timeout(250 + attempt * 75)
    return False


def verify_selection_state(page: Page, desired: str) -> None:
    desired_variants = target_text_variants(desired)
    normalized_variants = [normalize_for_match(text) for text in desired_variants if text]
    try:
        page.wait_for_timeout(400)
        body_text = page.locator("body").inner_text(timeout=1_500)
    except Exception:
        return
    normalized_body = normalize_for_match(body_text)
    if any(variant and variant in normalized_body for variant in normalized_variants):
        return
    raise ValueError(f"Selection verification failed; expected visible value: {desired}")
