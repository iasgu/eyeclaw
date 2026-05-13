# Show Once

Show Once is a local hackathon MVP for teaching a browser workflow from a short demo video, turning it into a readable SOP plus a replayable automation plan for Edge.

## What It Does

- Reuses a local video such as `website.mp4`, or lets you upload another one
- Lets you analyze only the meaningful segment of a longer recording
- Uses a vision model to infer likely UI actions from key frames
- Uses a language model to normalize those actions into a compact replay DSL
- Replays the learned steps in a persistent Edge browser session after you manually scan-login

## Local Setup

1. Create a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

2. Install Python dependencies:

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
```

3. Optional: install additional Playwright browser support:

```powershell
.venv\Scripts\python -m playwright install msedge
```

4. Create `.env` from your model config:

```powershell
.venv\Scripts\python scripts\import_model_txt.py
```

If `.env` already exists, the importer will leave it alone.

5. Run the original Streamlit app:

```powershell
.venv\Scripts\streamlit run app.py
```

6. Run the standalone HTML console:

```powershell
.venv\Scripts\python -m uvicorn app_web:app --host 127.0.0.1 --port 8010
```

Then open `http://127.0.0.1:8010`.

## Required Inputs

- The target site is `https://eia.51dzhp.com/#/`
- The user manually logs into the site in Edge by scanning a QR code
- The default source video is `website.mp4`
- For live browser mode, open Edge with remote debugging enabled, for example:

```powershell
& 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe' `
  --remote-debugging-port=9222 `
  --user-data-dir=C:\Users\majia\Documents\liangzhu\.browser\edge-profile `
  https://eia.51dzhp.com/#/
```

## HTML Console Features

- Chat-style HTML interface for entering browser-task requirements
- MP4 upload plus default video fallback
- Step extraction and replay-plan rendering
- Live browser connection over CDP
- One-click 大众环评 workflow execution
- In-memory scheduled task creation for delayed browser runs

## Browser Listener

- Local browser listener panel backed by an unpacked Edge/Chrome extension
- Open-source extension source lives in `browser_listener_extension/`
- Captures URL changes, tab activity, clicks, inputs, change events, focus, visibility, and throttled scroll
- Captures screenshots for key candidate listener events so they can flow into multimodal analysis
- Default local ingest endpoint: `http://127.0.0.1:8018/api/browser-listener/events`
- Listener-to-multimodal analysis endpoint: `POST /api/browser-listener/analyze`
- Runtime video analysis now expects a listener session and uses listener-guided frame selection instead of uniform sampling
- Listener is now disabled by default and only starts after you click `开始监听` in the extension popup

Load it with:

1. Open `edge://extensions` or `chrome://extensions`
2. Turn on `Developer mode`
3. Click `Load unpacked`
4. Select `browser_listener_extension`

## 中文手册

完整中文使用说明见：

[`docs/操作手册.md`](docs/操作手册.md)

## Security Notes

- Do not commit `.env`
- Avoid sharing `model.txt` in screenshots or recordings
- Rotate any keys that have been exposed outside your own machine

## Current Constraints

- This MVP is designed for one target site and one repeated workflow
- The current demo video is `43` seconds long, so the app focuses on a selected segment instead of the full recording
- The goal is a reliable demo path, not production-grade general web automation
- If the model provider returns `429` or a connection error, check quota, rate limits, or local network policy before the live demo
