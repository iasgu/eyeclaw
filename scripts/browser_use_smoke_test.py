from __future__ import annotations

import asyncio
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.config import load_config_status


EDGE_EXE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PROFILE_DIR = ROOT / ".browser" / "browser-use-smoke-profile"
TEST_URL = "http://127.0.0.1:8765/"


class SmokeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>Eyeclaw Browser Use Smoke</title>
  </head>
  <body>
    <h1>Eyeclaw Browser Use Smoke</h1>
    <p id="status">Waiting</p>
    <button id="startBtn" onclick="document.getElementById('status').textContent='Clicked OK'">
      Start Test
    </button>
  </body>
</html>"""
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


async def run_smoke() -> object:
    from browser_use import Agent, BrowserSession, ChatOpenAI

    load_dotenv(ROOT / ".env", override=False)
    status = load_config_status()
    if not status.is_ready or status.config is None:
        raise RuntimeError(f"Config is not ready: {status.missing_fields}")
    if not EDGE_EXE.exists():
        raise FileNotFoundError(f"Edge executable not found: {EDGE_EXE}")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    llm = ChatOpenAI(
        model=status.config.deepseek_model,
        api_key=status.config.deepseek_api_key,
        base_url=status.config.deepseek_base_url,
        temperature=0,
        max_completion_tokens=2048,
    )
    browser_session = BrowserSession(
        channel="msedge",
        executable_path=str(EDGE_EXE),
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        no_viewport=True,
        keep_alive=False,
        enable_default_extensions=False,
        captcha_solver=False,
        chromium_sandbox=False,
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
        ],
    )
    agent = Agent(
        task=(
            f"Open {TEST_URL}. Click the button named 'Start Test'. "
            "After the click, report the exact text shown in the element with id 'status'."
        ),
        llm=llm,
        browser_session=browser_session,
        use_vision=False,
        max_actions_per_step=3,
        max_failures=2,
        llm_timeout=60,
        use_judge=False,
    )
    return await agent.run(max_steps=8)


async def main() -> None:
    os.chdir(ROOT)
    server = ThreadingHTTPServer(("127.0.0.1", 8765), SmokeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = await run_smoke()
        print("BROWSER_USE_SMOKE_RESULT")
        print(result.final_result() if hasattr(result, "final_result") else str(result))
    finally:
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
