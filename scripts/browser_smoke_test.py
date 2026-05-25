from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / "browser_listener_extension"
PROFILE_DIR = ROOT / ".browser" / "playwright-edge-profile"
ARTIFACTS_DIR = ROOT / ".browser" / "test-artifacts"
APP_URL = "http://127.0.0.1:8018/app"
TARGET_PORT = 8038
TARGET_URL = f"http://127.0.0.1:{TARGET_PORT}/"


class SmokeTargetHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        html = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>Eyeclaw Smoke Target</title>
    <style>
      body { font-family: sans-serif; padding: 32px; min-height: 1800px; }
      button, input { font-size: 18px; margin: 12px 0; padding: 10px 14px; }
      .card { border: 1px solid #ddd; border-radius: 16px; padding: 20px; max-width: 560px; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1>Eyeclaw Smoke Target</h1>
      <input id="query" placeholder="Type here" />
      <button id="confirm" onclick="document.body.dataset.clicked='1'">Confirm Smoke Action</button>
    </div>
  </body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        return


def start_target_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", TARGET_PORT), SmokeTargetHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def extension_bridge(page, message_type: str, payload: dict | None = None, timeout_ms: int = 8000) -> dict:
    return page.evaluate(
        """async ({ messageType, payload, timeoutMs }) => {
          return await new Promise((resolve) => {
            const requestId = `smoke-${Date.now()}-${Math.random().toString(16).slice(2)}`;
            const timer = window.setTimeout(() => {
              window.removeEventListener("message", handleResponse);
              resolve({ ok: false, error: "timeout" });
            }, timeoutMs);
            function handleResponse(event) {
              if (event.source !== window) {
                return;
              }
              const data = event.data || {};
              if (data.source !== "eyeclaw-listener-extension" || data.requestId !== requestId) {
                return;
              }
              window.clearTimeout(timer);
              window.removeEventListener("message", handleResponse);
              resolve(data);
            }
            window.addEventListener("message", handleResponse);
            window.postMessage({
              source: "eyeclaw-user-app",
              type: messageType,
              payload: payload || {},
              requestId
            }, "*");
          });
        }""",
        {"messageType": message_type, "payload": payload or {}, "timeoutMs": timeout_ms},
    )


def wait_for_session_summary(page, session_id: str, timeout_seconds: float = 12.0) -> dict | None:
    deadline = time.time() + timeout_seconds
    last_payload: dict | None = None
    while time.time() < deadline:
        response = page.request.get(
            f"http://127.0.0.1:8018/api/browser-listener/session?session_id={session_id}"
        )
        if response.ok:
            last_payload = response.json()
            if last_payload.get("session_recording_ready") and last_payload.get("event_count", 0) > 0:
                return last_payload
        time.sleep(0.5)
    return last_payload


def main() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    summary: dict[str, object] = {
        "url": APP_URL,
        "target_url": TARGET_URL,
        "page_loaded": False,
        "extension_badge_before": None,
        "extension_badge_after_start": None,
        "extension_badge_after_stop": None,
        "screen_recording_active_after_start": False,
        "session_id": None,
        "session_summary": None,
        "recording_ready": False,
        "event_count": 0,
        "screenshot_count": 0,
        "screenshots": [],
        "errors": [],
    }

    server = start_target_server()
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="msedge",
                headless=False,
                args=[
                    f"--disable-extensions-except={EXTENSION_DIR}",
                    f"--load-extension={EXTENSION_DIR}",
                    "--enable-usermedia-screen-capturing",
                    "--allow-http-screen-capture",
                    "--auto-select-desktop-capture-source=Eyeclaw",
                    "--use-fake-ui-for-media-stream",
                ],
                no_viewport=True,
            )

            try:
                app_page = context.new_page()
                app_page.request.post("http://127.0.0.1:8018/api/browser-listener/events/clear")
                app_page.goto(APP_URL, wait_until="domcontentloaded")
                app_page.wait_for_load_state("networkidle")
                summary["page_loaded"] = True

                app_page.locator("#extensionBadge").wait_for(timeout=12000)
                app_page.wait_for_timeout(1500)
                summary["extension_badge_before"] = app_page.locator("#extensionBadge").text_content()

                app_page.locator("#skillRequest").fill("Smoke test: listener plus recording")
                app_page.locator("#startDemoBtn").click(timeout=8000)
                app_page.wait_for_timeout(1200)
                summary["extension_badge_after_start"] = app_page.locator("#extensionBadge").text_content()

                settings_response = extension_bridge(app_page, "eyeclaw-listener-get-settings")
                settings = (settings_response.get("response") or {}).get("settings") or {}
                summary["screen_recording_active_after_start"] = "屏幕录屏中" in (
                    app_page.locator("#extensionBadge").text_content() or ""
                )
                summary["session_id"] = settings.get("sessionId")

                target_page = context.new_page()
                target_page.goto(TARGET_URL, wait_until="domcontentloaded")
                target_page.locator("#query").fill("eyeclaw smoke")
                target_page.locator("#confirm").click()
                target_page.evaluate("window.scrollTo(0, 900)")
                target_page.wait_for_timeout(3500)

                app_page.bring_to_front()
                app_page.locator("#stopDemoBtn").click(timeout=8000)
                app_page.wait_for_timeout(1200)
                summary["extension_badge_after_stop"] = app_page.locator("#extensionBadge").text_content()

                if summary["session_id"]:
                    session_summary = wait_for_session_summary(app_page, str(summary["session_id"]))
                    summary["session_summary"] = session_summary
                    if session_summary:
                        summary["recording_ready"] = bool(session_summary.get("session_recording_ready"))
                        summary["event_count"] = session_summary.get("event_count", 0)
                        summary["screenshot_count"] = session_summary.get("screenshot_count", 0)

                initial_path = ARTIFACTS_DIR / "app-after-smoke.png"
                app_page.screenshot(path=str(initial_path), full_page=True)
                summary["screenshots"].append(str(initial_path))
            except PlaywrightTimeoutError as exc:
                summary["errors"].append(f"timeout: {exc}")
            except Exception as exc:  # pragma: no cover - smoke helper
                summary["errors"].append(str(exc))
            finally:
                context.close()
    finally:
        server.shutdown()

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
