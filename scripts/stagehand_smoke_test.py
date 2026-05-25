from __future__ import annotations

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

from stagehand import Stagehand
from src.config import load_config_status


EDGE_EXE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PROFILE_DIR = ROOT / ".browser" / "stagehand-smoke-profile"
TEST_URL = "http://127.0.0.1:8766/"


class SmokeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>Eyeclaw Stagehand Smoke</title>
  </head>
  <body>
    <h1>Eyeclaw Stagehand Smoke</h1>
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


def main() -> None:
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env", override=False)
    status = load_config_status()
    if not status.is_ready or status.config is None:
        raise RuntimeError(f"Config is not ready: {status.missing_fields}")
    if not EDGE_EXE.exists():
        raise FileNotFoundError(f"Edge executable not found: {EDGE_EXE}")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 8766), SmokeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    client = Stagehand(
        server="local",
        model_api_key=status.config.deepseek_api_key,
        local_chrome_path=str(EDGE_EXE),
        local_headless=True,
        local_ready_timeout_s=45,
        timeout=120,
    )
    session = None
    try:
        stagehand_model_name = status.config.deepseek_model
        if "/" not in stagehand_model_name:
            stagehand_model_name = f"deepseek/{stagehand_model_name}"
        model = {
            "modelName": stagehand_model_name,
            "apiKey": status.config.deepseek_api_key,
            "baseURL": status.config.deepseek_base_url,
        }
        session = client.sessions.start(
            model_name=stagehand_model_name,
            browser={
                "type": "local",
                "launchOptions": {
                    "executablePath": str(EDGE_EXE),
                    "headless": True,
                    "userDataDir": str(PROFILE_DIR),
                    "args": [
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-extensions",
                    ],
                },
            },
            dom_settle_timeout_ms=1_000,
            system_prompt=(
                "When selecting an action, return only a JSON object matching this exact shape: "
                '{"action":{"elementId":"0-13","description":"button","method":"click","arguments":[]},"twoStep":false}. '
                "The action.arguments field must always be an array. The twoStep field must be top-level."
            ),
            verbose=1,
        )
        session.navigate(url=TEST_URL, options={"waitUntil": "domcontentloaded", "timeout": 15_000})
        act_response = session.act(
            input=(
                "Click the button named 'Start Test'. Return a valid action object with top-level "
                "twoStep=false and action.arguments as an empty array when no arguments are needed."
            ),
            options={"model": model, "timeout": 60_000},
        )
        extract_response = session.extract(
            instruction=(
                "Return the exact text inside the element with CSS selector #status. "
                'The JSON output must be exactly {"extraction":"Clicked OK"} with the actual text as extraction.'
            ),
            options={"model": model, "selector": "#status", "timeout": 60_000},
        )
        print("STAGEHAND_SMOKE_RESULT")
        print(f"act_success={getattr(act_response, 'success', None)}")
        print(f"extract={extract_response}")
    finally:
        if session is not None:
            try:
                session.end()
            except Exception:
                pass
        client.close()
        server.shutdown()


if __name__ == "__main__":
    main()
