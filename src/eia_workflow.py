from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from playwright.sync_api import BrowserContext, Page

from src.replay import ReplaySession


ProgressCallback = Callable[[str], None]

DEFAULT_CDP_URL = "http://127.0.0.1:9222"

TEXT_CANGJINGGE = "藏经阁"
TEXT_LOGIN_MODAL = "使用微信扫码登录"
TEXT_USE_NOW = "立即使用"

VALUE_MAJOR_INDUSTRY = "畜牧业"
VALUE_SUB_INDUSTRY = "牲畜饲养 031"
VALUE_SUB_INDUSTRY_FALLBACK = "牲畜饲养"
VALUE_PROVINCE = "浙江省"
VALUE_CITY = "杭州市"
VALUE_REPORT_FORM = "报告表"
VALUE_REPORT_BOOK = "报告书"
VALUE_REPORT_REGISTER = "登记表"


class ManualCheckpointRequired(RuntimeError):
    """Raised when a live browser step needs the human to act before continuing."""


@dataclass
class EIAWorkflowState:
    page_role: str
    page_index: int
    url: str
    title: str
    summary: str


@dataclass
class EIAFilterSpec:
    major_industry: str = VALUE_MAJOR_INDUSTRY
    sub_industry: str | None = VALUE_SUB_INDUSTRY
    sub_industry_fallback: str | None = VALUE_SUB_INDUSTRY_FALLBACK
    province: str = VALUE_PROVINCE
    city: str = VALUE_CITY
    report_level: str | None = None
    preview_row_index: int = 1


def parse_eia_request(user_request: str | None) -> EIAFilterSpec:
    spec = EIAFilterSpec()
    text = (user_request or "").strip()
    if not text:
        return spec

    if "金属制品业" in text:
        spec.major_industry = "金属制品业"
        spec.sub_industry = None
        spec.sub_industry_fallback = None
    elif "畜牧业" in text:
        spec.major_industry = VALUE_MAJOR_INDUSTRY

    if "报告表" in text:
        spec.report_level = VALUE_REPORT_FORM
    elif "报告书" in text:
        spec.report_level = VALUE_REPORT_BOOK
    elif "登记表" in text:
        spec.report_level = VALUE_REPORT_REGISTER

    if "浙江省" in text:
        spec.province = "浙江省"
    if "杭州市" in text:
        spec.city = "杭州市"

    return spec


def detect_eia_state(session: ReplaySession) -> EIAWorkflowState:
    context = _require_context(session)
    pages = list(enumerate(context.pages))

    for index, page in pages:
        if "/previewPdf" in page.url:
            return EIAWorkflowState("pdf_preview", index, page.url, safe_title(page), "PDF preview tab is open.")
    for index, page in pages:
        if "/eia/environmentalReport" in page.url:
            return EIAWorkflowState("report_list", index, page.url, safe_title(page), "Filtered report list page is open.")
    for index, page in pages:
        if "/login?" in page.url:
            return EIAWorkflowState("post_login_bridge", index, page.url, safe_title(page), "Login bridge page is waiting to jump.")
    for index, page in pages:
        body = safe_body_text(page)
        if TEXT_LOGIN_MODAL in body:
            return EIAWorkflowState("login_modal", index, page.url, safe_title(page), "WeChat QR login modal is visible.")
    for index, page in pages:
        body = safe_body_text(page)
        if "快速登陆" in body and TEXT_CANGJINGGE in body:
            return EIAWorkflowState("homepage", index, page.url, safe_title(page), "Public homepage is open.")

    page = context.pages[0]
    return EIAWorkflowState("unknown", 0, page.url, safe_title(page), "Unable to classify the current page.")


def run_eia_live_workflow(
    session: ReplaySession,
    progress_callback: ProgressCallback | None = None,
    filter_spec: EIAFilterSpec | None = None,
) -> list[str]:
    logs: list[str] = []
    spec = filter_spec or EIAFilterSpec()

    def emit(message: str) -> None:
        logs.append(message)
        if progress_callback is not None:
            progress_callback(message)

    state = detect_eia_state(session)
    emit(f"Detected page state: {state.page_role} ({state.title})")

    if state.page_role == "pdf_preview":
        preview_page = _page_by_index(session, state.page_index)
        trigger_pdf_download(preview_page)
        emit("Triggered the PDF download from the existing preview tab.")
        return logs

    if state.page_role == "homepage":
        page = _page_by_index(session, state.page_index)
        open_cangjingge_entry(page)
        emit("Clicked the 藏经阁 entry on the homepage.")
        state = detect_eia_state(session)
        emit(f"New page state: {state.page_role} ({state.title})")

    if state.page_role == "login_modal":
        raise ManualCheckpointRequired("Scan the WeChat QR code in Edge, then click Continue in the app.")

    if state.page_role == "post_login_bridge":
        page = _page_by_index(session, state.page_index)
        click_bridge_continue(page)
        emit("Clicked the post-login bridge button.")
        state = detect_eia_state(session)
        emit(f"New page state: {state.page_role} ({state.title})")

    if state.page_role == "homepage":
        page = _page_by_index(session, state.page_index)
        open_cangjingge_route(page)
        emit("Opened the 环评藏经阁 route from the functional dropdown.")
        state = detect_eia_state(session)
        emit(f"New page state: {state.page_role} ({state.title})")

    if state.page_role != "report_list":
        raise RuntimeError(f"Unable to reach the 环评藏经阁 list page. Current state: {state.page_role}")

    page = _page_by_index(session, state.page_index)
    page.bring_to_front()
    apply_filters(page, spec)
    emit(
        "Applied filters: "
        f"{spec.major_industry}"
        + (f" -> {spec.sub_industry}" if spec.sub_industry else "")
        + f" -> {spec.province} -> {spec.city}"
        + (f" -> {spec.report_level}" if spec.report_level else "")
        + "."
    )

    open_preview(page, row_index=spec.preview_row_index)
    emit("Opened the report preview tab.")

    state = detect_eia_state(session)
    if state.page_role != "pdf_preview":
        raise RuntimeError(f"Preview tab did not open as expected. Current state: {state.page_role}")

    preview_page = _page_by_index(session, state.page_index)
    trigger_pdf_download(preview_page)
    emit("Triggered the PDF download from the preview frame.")
    return logs


def open_cangjingge_entry(page: Page) -> None:
    page.bring_to_front()
    page.wait_for_timeout(1_500)
    locator = page.locator("xpath=(//*[contains(normalize-space(.), '藏经阁') and contains(@class, 'tool-card')])[1]")
    if locator.count() == 0:
        raise RuntimeError("The 藏经阁 homepage card was not found.")
    locator.first.click(force=True, timeout=5_000)
    page.wait_for_timeout(4_000)


def click_bridge_continue(page: Page) -> None:
    locator = page.locator("xpath=(//*[contains(normalize-space(.), '立即使用')])[last()]")
    if locator.count() == 0:
        raise RuntimeError("The post-login bridge button was not found.")
    locator.first.click(force=True, timeout=5_000)
    page.wait_for_timeout(5_000)


def open_cangjingge_route(page: Page) -> None:
    trigger = page.locator(".el-dropdown").first
    if trigger.count() == 0:
        raise RuntimeError("The functional dropdown trigger was not found on the homepage.")
    trigger.hover(timeout=5_000)
    page.wait_for_timeout(1_000)
    result = page.evaluate(
        """() => {
            const items = Array.from(document.querySelectorAll('.el-dropdown-menu__item'));
            const visible = items.filter((item) => {
                const style = window.getComputedStyle(item);
                return style.display !== 'none' && style.visibility !== 'hidden' && item.offsetParent !== null;
            });
            const target = visible[0] || items[0];
            if (!target) return { ok: false, reason: 'dropdown item not found' };
            target.click();
            return { ok: true, text: (target.innerText || target.textContent || '').replace(/\\s+/g, ' ').trim() };
        }"""
    )
    if not result.get("ok"):
        raise RuntimeError(f"Unable to open the 环评藏经阁 route: {result}")
    page.wait_for_timeout(6_000)


def apply_filters(page: Page, spec: EIAFilterSpec) -> None:
    clear_filters(page)

    _open_select(page, 0)
    _click_visible_select_option(page, spec.major_industry)

    if spec.sub_industry:
        _open_select(page, 1)
        try:
            _click_visible_select_option(page, spec.sub_industry)
        except RuntimeError:
            if not spec.sub_industry_fallback:
                raise
            _click_visible_select_option(page, spec.sub_industry_fallback)

    _open_select(page, 2)
    _click_visible_select_option(page, spec.province)

    _open_select(page, 3)
    _click_visible_select_option(page, spec.city)

    if spec.report_level:
        _click_smallest_visible_text(page, spec.report_level)

    page.wait_for_timeout(4_000)


def clear_filters(page: Page) -> None:
    try:
        _click_smallest_visible_text(page, "清空")
    except RuntimeError:
        return
    page.wait_for_timeout(1_500)


def open_preview(page: Page, row_index: int = 1) -> None:
    previews = page.locator(".viewReport")
    count = previews.count()
    if count == 0:
        raise RuntimeError("No preview buttons were found after filtering.")
    target = previews.nth(row_index) if count > row_index else previews.first
    target.click(force=True, timeout=5_000)
    page.wait_for_timeout(6_000)


def trigger_pdf_download(page: Page) -> None:
    pdf_frame = _pdf_frame(page)
    pdf_frame.evaluate(
        """() => {
            const button = document.querySelector('#download') || document.querySelector('#secondaryDownload');
            if (!button) throw new Error('PDF download button not found');
            button.click();
        }"""
    )
    page.wait_for_timeout(3_000)


def _page_by_index(session: ReplaySession, page_index: int) -> Page:
    return _require_context(session).pages[page_index]


def _require_context(session: ReplaySession) -> BrowserContext:
    if session.context is None:
        raise RuntimeError("Replay session has no browser context.")
    return session.context


def _open_select(page: Page, select_index: int) -> None:
    select = page.locator(".el-select").nth(select_index)
    box = select.bounding_box()
    if not box:
        raise RuntimeError(f"Unable to locate filter dropdown #{select_index}.")
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(1_000)


def _click_visible_select_option(page: Page, option_text: str) -> None:
    result = page.evaluate(
        """(target) => {
            const dropdowns = Array.from(document.querySelectorAll('.el-select-dropdown'));
            const visible = dropdowns.filter((node) => {
                const style = window.getComputedStyle(node);
                return style.display !== 'none' && style.visibility !== 'hidden' && node.offsetParent !== null;
            });
            const scope = visible.length ? visible[visible.length - 1] : null;
            if (!scope) return { ok: false, reason: 'No visible dropdown found' };
            const items = Array.from(scope.querySelectorAll('.el-select-dropdown__item'));
            const picked = items.find((item) => {
                const text = (item.innerText || item.textContent || '').replace(/\\s+/g, ' ').trim();
                return text === target || text.includes(target);
            });
            if (!picked) {
                return {
                    ok: false,
                    reason: 'Option not found',
                    options: items.slice(0, 40).map((item) => (item.innerText || item.textContent || '').replace(/\\s+/g, ' ').trim()),
                };
            }
            picked.click();
            return { ok: true, picked: (picked.innerText || picked.textContent || '').replace(/\\s+/g, ' ').trim() };
        }""",
        option_text,
    )
    if not result.get("ok"):
        raise RuntimeError(f"Unable to select '{option_text}': {result}")
    page.wait_for_timeout(1_500)


def _click_smallest_visible_text(page: Page, target_text: str) -> None:
    result = page.evaluate(
        """(target) => {
            const nodes = Array.from(document.querySelectorAll('*'))
              .filter((el) => {
                const content = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const style = window.getComputedStyle(el);
                return content === target && style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
              })
              .map((el) => ({
                el,
                area: el.getBoundingClientRect().width * el.getBoundingClientRect().height,
                top: el.getBoundingClientRect().top,
                left: el.getBoundingClientRect().left,
              }))
              .sort((a, b) => a.area - b.area || a.top - b.top || a.left - b.left);
            if (!nodes.length) return { ok: false };
            nodes[0].el.click();
            return { ok: true };
        }""",
        target_text,
    )
    if not result.get("ok"):
        raise RuntimeError(f"No visible text node matched '{target_text}'.")
    page.wait_for_timeout(1_000)


def _pdf_frame(page: Page):
    for frame in page.frames:
        if "/pdf/web/viewer.html" in frame.url:
            return frame
    raise RuntimeError("The embedded PDF preview frame was not found.")


def safe_body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=8_000)
    except Exception:
        return ""


def safe_title(page: Page) -> str:
    try:
        return page.title()
    except Exception:
        return ""
