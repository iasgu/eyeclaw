from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

import requests


ROOT = Path(__file__).resolve().parents[1]
VALID_MODES = {"smoke", "integration", "full", "extension"}
PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z7nQAAAAASUVORK5CYII="
)


class AutoTest:
    def __init__(
        self,
        *,
        mode: str,
        host: str,
        port: int,
        skip_ui: bool,
        use_existing_server: bool,
        keep_server: bool,
    ) -> None:
        self.mode = mode
        self.host = host
        self.port = port
        self.skip_ui = skip_ui
        self.use_existing_server = use_existing_server
        self.keep_server = keep_server
        self.base_url = f"http://{host}:{port}"
        self.run_id = time.strftime("%Y%m%d-%H%M%S")
        self.artifact_dir = ROOT / "artifacts" / "auto_tests" / self.run_id
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.results: list[dict[str, Any]] = []
        self.server_process: subprocess.Popen[str] | None = None
        self.session_id = f"auto-test-{uuid4().hex}"

    def add_result(self, name: str, status: str, details: str = "", data: dict[str, Any] | None = None) -> None:
        item = {
            "name": name,
            "status": status,
            "details": details,
            "data": data or {},
        }
        self.results.append(item)
        marker = {"pass": "PASS", "fail": "FAIL", "warn": "WARN"}.get(status, status.upper())
        print(f"[{marker}] {name}" + (f" - {details}" if details else ""))

    def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = requests.request(method, f"{self.base_url}{path}", timeout=kwargs.pop("timeout", 12), **kwargs)
        response.raise_for_status()
        if not response.text:
            return None
        return response.json()

    def wait_for_server(self, timeout_seconds: float = 25.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                response = requests.get(f"{self.base_url}/api/status", timeout=2)
                if response.ok:
                    return True
            except requests.RequestException:
                pass
            time.sleep(0.5)
        return False

    def start_server_if_needed(self) -> None:
        if self.wait_for_server(timeout_seconds=2.0):
            self.add_result("server", "pass", f"Using existing server at {self.base_url}.")
            return

        if self.use_existing_server:
            self.add_result("server", "fail", f"No existing server responded at {self.base_url}.")
            return

        stdout_path = self.artifact_dir / "server.out.log"
        stderr_path = self.artifact_dir / "server.err.log"
        stdout = stdout_path.open("w", encoding="utf-8")
        stderr = stderr_path.open("w", encoding="utf-8")
        env = {**os.environ, "PYTHONUTF8": "1"}
        self.server_process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app_web:app", "--host", self.host, "--port", str(self.port)],
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            text=True,
            env=env,
        )
        if self.wait_for_server():
            self.add_result("server", "pass", f"Started server at {self.base_url}.")
        else:
            self.add_result(
                "server",
                "fail",
                f"Server did not become ready. Logs: {stdout_path}, {stderr_path}",
            )

    def check_environment(self) -> None:
        version_ok = sys.version_info >= (3, 11)
        self.add_result(
            "python",
            "pass" if version_ok else "fail",
            f"{sys.version.split()[0]} at {sys.executable}",
        )

        venv_path = ROOT / ".venv" / "Scripts" / "python.exe"
        if venv_path.exists() and Path(sys.executable).resolve() == venv_path.resolve():
            self.add_result("virtual-env", "pass", f"using {venv_path}")
        elif venv_path.exists():
            self.add_result("virtual-env", "warn", f".venv exists, but running with {sys.executable}")
        else:
            self.add_result("virtual-env", "fail", ".venv is missing. Run scripts/install_windows.ps1 first.")

        missing: list[str] = []
        modules = [
            "uvicorn",
            "starlette",
            "multipart",
            "playwright",
            "browser_use",
            "stagehand",
            "selenium",
            "cv2",
            "pydantic",
            "dotenv",
            "requests",
        ]
        for module in modules:
            if importlib.util.find_spec(module) is None:
                missing.append(module)
        self.add_result(
            "python-imports",
            "pass" if not missing else "fail",
            "core imports available" if not missing else f"missing modules: {', '.join(missing)}",
        )

        required_files = [
            "app_web.py",
            "web/app.html",
            "browser_listener_extension/manifest.json",
            "requirements.txt",
            ".env.example",
        ]
        missing_files = [path for path in required_files if not (ROOT / path).exists()]
        self.add_result(
            "project-files",
            "pass" if not missing_files else "fail",
            "required files exist" if not missing_files else f"missing: {', '.join(missing_files)}",
        )

        env_path = ROOT / ".env"
        if not env_path.exists():
            self.add_result("env-file", "warn", ".env missing. The app can start, but model analysis/execution needs keys.")
        else:
            text = env_path.read_text(encoding="utf-8", errors="ignore")
            if "your-zhipu-api-key" in text or ("your-" in text and "api-key" in text):
                self.add_result("env-file", "warn", ".env still contains placeholder keys.")
            else:
                self.add_result("env-file", "pass", ".env exists.")

    def check_extension_manifest(self) -> None:
        manifest_path = ROOT / "browser_listener_extension" / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.add_result("extension-manifest", "fail", str(exc))
            return
        permissions = set(manifest.get("permissions") or [])
        required = {"storage", "tabs", "tabCapture", "offscreen"}
        missing = sorted(required - permissions)
        self.add_result(
            "extension-manifest",
            "pass" if not missing else "fail",
            "required permissions present" if not missing else f"missing permissions: {', '.join(missing)}",
            {"version": manifest.get("version"), "permissions": sorted(permissions)},
        )

    def check_extension_static_recording_support(self) -> None:
        files = {
            "service-worker.js": ROOT / "browser_listener_extension" / "service-worker.js",
            "offscreen.js": ROOT / "browser_listener_extension" / "offscreen.js",
            "offscreen.html": ROOT / "browser_listener_extension" / "offscreen.html",
            "popup.js": ROOT / "browser_listener_extension" / "popup.js",
        }
        missing_files = [name for name, path in files.items() if not path.exists()]
        if missing_files:
            self.add_result("extension-files", "fail", f"missing: {', '.join(missing_files)}")
            return

        service_worker = files["service-worker.js"].read_text(encoding="utf-8", errors="ignore")
        offscreen = files["offscreen.js"].read_text(encoding="utf-8", errors="ignore")
        popup = files["popup.js"].read_text(encoding="utf-8", errors="ignore")
        marker_checks = {
            "service-worker creates offscreen document": "chrome.offscreen.createDocument" in service_worker,
            "service-worker requests tabCapture stream": "chrome.tabCapture.getMediaStreamId" in service_worker,
            "service-worker handles uploaded recording": "eyeclaw-listener-recording-uploaded" in service_worker,
            "offscreen uses MediaRecorder": "new MediaRecorder" in offscreen,
            "offscreen uploads session recording": "/api/browser-listener/session-recording" in offscreen,
            "popup exposes recording state": "recordingState" in popup,
        }
        failed = [name for name, ok in marker_checks.items() if not ok]
        self.add_result(
            "extension-recording-static",
            "pass" if not failed else "fail",
            "recording implementation markers found" if not failed else f"missing markers: {', '.join(failed)}",
            marker_checks,
        )

    def check_extension_manual_recording_note(self) -> None:
        self.add_result(
            "extension-real-recording",
            "warn",
            "Manual assisted: start the app with scripts/run_windows.ps1, click Start Demo in Edge, operate a normal page, then click Stop Demo and confirm a new recording appears.",
        )

    def check_http_api(self) -> None:
        try:
            status = self.request_json("GET", "/api/status")
            if isinstance(status, dict):
                status_name = "pass"
                details = "config ready" if status.get("config_ready") else f"config missing: {status.get('missing_fields')}"
            else:
                status_name = "fail"
                details = f"unexpected status payload: {type(status).__name__}"
            self.add_result("api-status", status_name, details, status)
        except Exception as exc:
            self.add_result("api-status", "fail", str(exc))
            return

        for name, path in [
            ("execution-adapters", "/api/execution-adapters"),
            ("schedules", "/api/schedules"),
            ("listener-status", "/api/browser-listener/status"),
            ("recordings", "/api/browser-listener/recordings?limit=3"),
        ]:
            try:
                payload = self.request_json("GET", path)
                self.add_result(name, "pass", "endpoint ok", {"keys": sorted(payload.keys()) if isinstance(payload, dict) else []})
            except Exception as exc:
                self.add_result(name, "fail", str(exc))

    def check_frontend_html(self) -> None:
        try:
            response = requests.get(f"{self.base_url}/app", timeout=10)
            response.raise_for_status()
            html = response.text
            required_markers = ["startDemoBtn", "stopDemoBtn", "recordingList", "taskList"]
            missing = [marker for marker in required_markers if marker not in html]
            self.add_result(
                "frontend-html",
                "pass" if not missing else "fail",
                "required UI markers found" if not missing else f"missing markers: {', '.join(missing)}",
            )
        except Exception as exc:
            self.add_result("frontend-html", "fail", str(exc))

    def check_listener_roundtrip(self) -> None:
        try:
            now_ms = int(time.time() * 1000)
            payload = {
                "client_name": "auto-test",
                "browser_name": "auto-test",
                "session_id": self.session_id,
                "events": [
                    {
                        "event_type": "page_loaded",
                        "source": "extension_background",
                        "page_url": "https://example.test/workflow",
                        "page_title": "Auto Test Workflow",
                        "client_timestamp_ms": now_ms,
                        "key_candidate": True,
                        "screenshot_data_url": PNG_DATA_URL,
                        "screenshot_reason": "auto-test-page-loaded",
                    },
                    {
                        "event_type": "click",
                        "source": "extension_content",
                        "page_url": "https://example.test/workflow",
                        "page_title": "Auto Test Workflow",
                        "target_text": "Confirm",
                        "target_selector": "#confirm",
                        "target_tag": "button",
                        "client_timestamp_ms": now_ms + 100,
                        "key_candidate": True,
                        "screenshot_data_url": PNG_DATA_URL,
                        "screenshot_reason": "auto-test-click",
                    },
                ],
            }
            ingest = self.request_json("POST", "/api/browser-listener/events", json=payload)
            summary = self.request_json("GET", f"/api/browser-listener/session?session_id={self.session_id}")
            ok = summary.get("event_count", 0) >= 2 and summary.get("screenshot_count", 0) >= 1
            self.add_result(
                "listener-roundtrip",
                "pass" if ok else "fail",
                f"events={summary.get('event_count')} screenshots={summary.get('screenshot_count')}",
                {"ingest": ingest, "summary": summary},
            )
        except Exception as exc:
            self.add_result("listener-roundtrip", "fail", str(exc))

    def check_recording_upload(self) -> None:
        try:
            fake_webm = b"\x1a\x45\xdf\xa3" + b"eyeclaw-auto-test-webm"
            files = {"video": (f"{self.session_id}.webm", fake_webm, "video/webm")}
            data = {
                "session_id": self.session_id,
                "started_at_ms": str(int(time.time() * 1000) - 1000),
                "ended_at_ms": str(int(time.time() * 1000)),
                "mime_type": "video/webm",
            }
            upload = requests.post(f"{self.base_url}/api/browser-listener/session-recording", data=data, files=files, timeout=12)
            upload.raise_for_status()
            upload_payload = upload.json()
            summary = self.request_json("GET", f"/api/browser-listener/session?session_id={self.session_id}")
            recordings = self.request_json("GET", "/api/browser-listener/recordings?limit=10")
            listed = any(item.get("session_id") == self.session_id and item.get("has_recording") for item in recordings.get("recordings", []))
            ok = summary.get("has_recording") and listed
            self.add_result(
                "recording-upload",
                "pass" if ok else "fail",
                f"path={upload_payload.get('recording_path')}",
                {"upload": upload_payload, "summary": summary, "listed": listed},
            )
        except Exception as exc:
            self.add_result("recording-upload", "fail", str(exc))

    def resolve_edge(self) -> str | None:
        candidates = [
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("LocalAppData", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return shutil.which("msedge")

    def check_frontend_with_browser(self) -> None:
        if self.skip_ui:
            self.add_result("frontend-browser", "warn", "Skipped by flag.")
            return
        edge_path = self.resolve_edge()
        if not edge_path:
            self.add_result("frontend-browser", "warn", "Microsoft Edge not found; skipped browser UI smoke.")
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self.add_result("frontend-browser", "warn", f"Playwright import failed; skipped browser UI smoke: {exc}")
            return

        fatal_errors: list[str] = []
        warnings: list[str] = []
        screenshot_path = self.artifact_dir / "frontend-browser.png"

        def handle_request_failed(request: Any) -> None:
            if not request.url.startswith(self.base_url):
                return
            failure = str(request.failure or "")
            message = f"requestfailed: {request.url} {failure}"
            if "/api/browser-listener/recording-video" in request.url and "ERR_ABORTED" in failure:
                warnings.append(message)
                return
            fatal_errors.append(message)

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=edge_path)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda exc: fatal_errors.append(f"pageerror: {exc}"))
                page.on("console", lambda msg: warnings.append(f"console {msg.type}: {msg.text}") if msg.type in {"error", "warning"} else None)
                page.on("requestfailed", handle_request_failed)
                page.on(
                    "response",
                    lambda response: fatal_errors.append(f"http {response.status}: {response.url}")
                    if response.url.startswith(self.base_url) and response.status >= 500
                    else None,
                )
                page.goto(f"{self.base_url}/app", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1200)
                start_count = page.locator("#startDemoBtn").count()
                task_count = page.locator("#taskList").count()
                page.screenshot(path=str(screenshot_path), full_page=True)
                browser.close()
            ok = start_count == 1 and task_count == 1 and not fatal_errors
            self.add_result(
                "frontend-browser",
                "pass" if ok else "fail",
                f"startDemoBtn={start_count}, taskList={task_count}, screenshot={screenshot_path}",
                {"fatal_errors": fatal_errors, "warnings": warnings, "screenshot": str(screenshot_path)},
            )
            if warnings:
                self.add_result("frontend-browser-warnings", "warn", f"{len(warnings)} console warning/error message(s)", {"warnings": warnings[:20]})
        except Exception as exc:
            self.add_result("frontend-browser", "fail", str(exc))

    def recommendations(self) -> list[str]:
        advice: list[str] = []
        for item in self.results:
            if item["status"] not in {"fail", "warn"}:
                continue
            name = item["name"]
            if name in {"virtual-env", "python-imports"}:
                advice.append("Run scripts/install_windows.ps1, then rerun scripts/auto_test_windows.ps1 from PowerShell.")
            elif name == "env-file":
                advice.append("Copy .env.example to .env and fill model/API keys before model analysis or Browser Use execution.")
            elif name == "server":
                advice.append(f"Check whether port {self.port} is occupied, or inspect server.out.log/server.err.log in the report directory.")
            elif name == "api-status":
                advice.append("The backend responded but configuration is incomplete; status can be usable for smoke tests while model-backed analysis remains disabled.")
            elif name.startswith("extension"):
                advice.append("Load browser_listener_extension in Edge, keep the extension enabled, and test Start Demo/Stop Demo on a normal http/https tab.")
            elif name.startswith("frontend"):
                advice.append("Open the frontend screenshot in the report directory and check browser console output for the listed local API errors.")
            elif name == "listener-roundtrip":
                advice.append("Check POST /api/browser-listener/events and artifacts/browser_listener for session event persistence.")
            elif name == "recording-upload":
                advice.append("Check POST /api/browser-listener/session-recording and artifacts/session_recordings for saved WebM files.")
        deduped: list[str] = []
        for item in advice:
            if item not in deduped:
                deduped.append(item)
        return deduped

    def write_report(self) -> None:
        failed = [item for item in self.results if item["status"] == "fail"]
        warnings = [item for item in self.results if item["status"] == "warn"]
        recommendations = self.recommendations()
        report = {
            "run_id": self.run_id,
            "mode": self.mode,
            "base_url": self.base_url,
            "session_id": self.session_id,
            "artifact_dir": str(self.artifact_dir),
            "summary": {
                "passed": sum(1 for item in self.results if item["status"] == "pass"),
                "failed": len(failed),
                "warnings": len(warnings),
            },
            "recommendations": recommendations,
            "results": self.results,
        }
        (self.artifact_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [
            "# EyeClaw Auto Test Report",
            "",
            f"- Mode: `{self.mode}`",
            f"- Run ID: `{self.run_id}`",
            f"- Base URL: `{self.base_url}`",
            f"- Session ID: `{self.session_id}`",
            f"- Passed: `{report['summary']['passed']}`",
            f"- Failed: `{report['summary']['failed']}`",
            f"- Warnings: `{report['summary']['warnings']}`",
            "",
            "## Results",
            "",
        ]
        for item in self.results:
            lines.append(f"- `{item['status'].upper()}` {item['name']}: {item['details']}")
        if recommendations:
            lines.extend(["", "## Recommendations", ""])
            for item in recommendations:
                lines.append(f"- {item}")
        (self.artifact_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
        print("")
        print(f"Report: {self.artifact_dir / 'report.md'}")

    def stop_server(self) -> None:
        if self.keep_server or self.server_process is None:
            return
        self.server_process.terminate()
        try:
            self.server_process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.server_process.kill()

    def run(self) -> int:
        try:
            self.check_environment()
            self.check_extension_manifest()
            if self.mode in {"full", "extension"}:
                self.check_extension_static_recording_support()
            self.start_server_if_needed()
            if any(item["name"] == "server" and item["status"] == "fail" for item in self.results):
                return 1
            self.check_http_api()
            self.check_frontend_html()
            if self.mode in {"integration", "full", "extension"}:
                self.check_listener_roundtrip()
                self.check_recording_upload()
            if self.mode == "full":
                self.check_frontend_with_browser()
                self.check_extension_manual_recording_note()
            elif self.mode == "extension":
                self.check_extension_manual_recording_note()
            failed = [item for item in self.results if item["status"] == "fail"]
            return 1 if failed else 0
        finally:
            self.write_report()
            self.stop_server()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run EyeClaw automated smoke tests.")
    parser.add_argument("--mode", choices=sorted(VALID_MODES), default="integration", help="Test depth to run.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8018)
    parser.add_argument("--skip-ui", action="store_true", help="Skip headless Edge frontend smoke test.")
    parser.add_argument("--use-existing-server", action="store_true", help="Fail if no server is already running.")
    parser.add_argument("--keep-server", action="store_true", help="Keep a server started by this script running after tests.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return AutoTest(
        mode=args.mode,
        host=args.host,
        port=args.port,
        skip_ui=args.skip_ui,
        use_existing_server=args.use_existing_server,
        keep_server=args.keep_server,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
