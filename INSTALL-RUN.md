# EyeClaw Windows Install And Run Guide

This package is intended to run on another Windows computer after unzip.

## Requirements

- Windows 10/11.
- Python 3.11 or 3.12, available as `py` or `python`.
- Microsoft Edge installed.
- Internet access for first-time Python dependency installation.
- Your own model API keys. Do not reuse another person's `.env`.

## Quick Start

Open PowerShell in the unzipped `liangzhu` folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install_windows.ps1
notepad .env
.\scripts\run_windows.ps1
```

Then open:

```text
http://127.0.0.1:8018/app
```

`run_windows.ps1` also tries to open a dedicated Edge profile with the local listener extension loaded.

## Configure `.env`

The installer creates `.env` from `.env.example` if it does not exist.

At minimum, set:

```text
LLM_API_KEY=...
VLM_API_KEY=...
BROWSER_USE_LLM_API_KEY=...
```

If the same provider/key is used for all three, keep the example's `${LLM_API_KEY}` references.

## Browser Extension

Preferred path:

```powershell
.\scripts\run_windows.ps1
```

This starts Edge with:

- a dedicated local profile under `.browser/edge-profile`
- `--remote-debugging-port=9222`
- `--load-extension=browser_listener_extension`

Manual fallback:

1. Open `edge://extensions`.
2. Enable developer mode.
3. Click "Load unpacked".
4. Select `browser_listener_extension`.
5. Refresh `http://127.0.0.1:8018/app`.

## Health Check

Run:

```powershell
.\scripts\doctor_windows.ps1
```

It checks Python, virtual environment imports, Edge, `.env`, and the app port.

## Automated Smoke Test

Run this after installation to verify that the package works on the current computer:

```powershell
.\scripts\auto_test_windows.ps1
```

The smoke test starts the local server if needed, then checks:

- required project files
- Python runtime imports
- extension manifest permissions
- `/api/status` and core API endpoints
- frontend HTML markers
- browser-listener event persistence
- session recording upload/listing
- headless Edge frontend rendering

Reports are written to:

```text
artifacts/auto_tests/<run-id>/report.md
artifacts/auto_tests/<run-id>/report.json
```

Use this lighter variant when Edge UI automation is not available:

```powershell
.\scripts\auto_test_windows.ps1 -SkipUi
```

## Common Problems

- Port `8018` is occupied: stop the other process or run with another port:

```powershell
.\scripts\run_windows.ps1 -Port 8021
```

- Extension shows "Receiving end does not exist": reload the EyeClaw Listener extension, then refresh `/app`.
- No recording video: make sure you used the dedicated Edge window opened by `run_windows.ps1`, or manually load the extension.
- Model/tool error: confirm the model supports tool calls for Browser Use. Keep thinking mode disabled for GLM/DeepSeek tool execution.

## What Not To Share

Do not share these files or folders:

- `.env`
- `model.txt`
- `.browser/`
- `.venv/`
- `artifacts/`
- any screenshots, videos, or downloaded business files produced during runs
