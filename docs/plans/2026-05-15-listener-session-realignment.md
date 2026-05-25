# Listener Session Realignment Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Realign the unfinished listener-session changes with the README so start/stop behavior, session summary semantics, video-analysis entry points, and docs all describe and enforce the same workflow.

**Architecture:** Keep README as the source of truth for the minimal closed loop: start listening, start recording in sync, stop both together, keep one session timeline, and allow listener-guided analysis across screenshots plus either a session recording or an explicit video file. Fix the mismatch in three layers: extension state transitions, backend session-summary semantics, and frontend button gating / copy.

**Tech Stack:** Python 3.11, Starlette, plain HTML/JS frontend, Edge/Chrome extension service worker + offscreen document, pytest

---

### Task 1: Re-state the session-summary contract in backend code and tests

**Files:**
- Modify: `src/browser_listener.py`
- Modify: `src/webapp.py`
- Modify: `tests/test_browser_listener.py`
- Modify: `tests/test_browser_listener_api.py`

**Step 1: Write the failing backend regression tests**

Add tests that lock these rules:

- `listener_analysis_ready` means "there are screenshot-backed listener candidates"
- `session_recording_ready` means "this listener session has a usable uploaded recording"
- the backend must not imply that external video analysis is impossible just because the session recording is missing

Example test shape:

```python
def test_session_summary_separates_listener_analysis_from_session_recording() -> None:
    summary = store.session_summary("summary-session")
    assert summary["listener_analysis_ready"] is True
    assert summary["session_recording_ready"] is False
```

**Step 2: Run tests to verify they fail**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_browser_listener.py tests/test_browser_listener_api.py -q
```

Expected:

- FAIL because the new field names / semantics do not exist yet

**Step 3: Write the minimal backend implementation**

In `src/browser_listener.py`:

- replace the overloaded `video_analysis_ready` meaning with a session-scoped field such as `session_recording_ready`
- keep `key_event_count`, `has_screenshots`, and `has_recording`
- make `latest_session_summary()` and `status()` return the renamed field consistently

Implementation direction:

```python
return {
    "listener_analysis_ready": has_screenshots,
    "session_recording_ready": has_recording and key_event_count > 0,
}
```

In `src/webapp.py`:

- keep the existing `analyze_video()` behavior unchanged: explicit `video_path` remains valid, session recording remains the fallback only when `video_path` is omitted

**Step 4: Run tests to verify they pass**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_browser_listener.py tests/test_browser_listener_api.py -q
```

Expected:

- PASS

**Step 5: Commit**

```bash
git add src/browser_listener.py src/webapp.py tests/test_browser_listener.py tests/test_browser_listener_api.py
git commit -m "fix: clarify listener session readiness semantics"
```

### Task 2: Fix frontend gating so listener-guided video analysis matches README

**Files:**
- Modify: `web/index.html`

**Step 1: Write down the failing user-path checks**

Lock these UI rules before editing:

- `分析监听会话` should require screenshot-backed listener candidates
- `分析视频流程` should require a listener session with key events, but must not require a session recording if the user supplies `website.mp4` or an uploaded video
- the session summary card must describe session recording availability without implying that all video analysis is blocked

Manual repro checklist:

1. Start a listener session that generates key events and screenshots.
2. Do not upload / attach a session recording.
3. Leave `videoPath` as `website.mp4` or upload a local MP4.
4. Observe that the current UI disables `分析视频流程`.

Expected:

- Current behavior is wrong per README.

**Step 2: Implement the minimal frontend fix**

Update `web/index.html` to:

- rename the displayed label from `视频分析` to `会话录屏分析` or equivalent session-scoped wording
- derive button enablement separately from the summary badge
- enable `分析视频流程` when there is a latest listener session with `key_event_count > 0`
- keep `分析监听会话` gated by screenshot availability

Implementation direction:

```javascript
state.listenerAnalysisReady = !!(summary && summary.listener_analysis_ready);
state.videoAnalyzeReady = !!(summary && summary.key_event_count > 0);
state.sessionRecordingReady = !!(summary && summary.session_recording_ready);
```

**Step 3: Manually verify the corrected paths**

Check:

1. Listener session with screenshots but no recording: `分析监听会话` enabled, `分析视频流程` enabled.
2. Listener session with key events plus recording: both enabled, summary says session recording ready.
3. No listener session: both disabled.

Expected:

- UI now follows the README workflow instead of blocking explicit video input.

**Step 4: Commit**

```bash
git add web/index.html
git commit -m "fix: align video analysis gating with listener workflow"
```

### Task 3: Make extension stop-session behavior atomic and recoverable

**Files:**
- Modify: `browser_listener_extension/service-worker.js`
- Modify: `browser_listener_extension/popup.js`
- Review: `browser_listener_extension/offscreen.js`

**Step 1: Reproduce the stop/upload failure path manually**

Manual repro checklist:

1. Start a listener session successfully.
2. Stop the backend or force the recording upload endpoint to fail.
3. Click `停止监听`.
4. Observe whether popup state, `enabled`, and `activeRecordingSessionId` stay recoverable.

Expected:

- Current code can end up with collection disabled and recording state cleared before upload completes.

**Step 2: Implement the minimal state-machine fix**

In `browser_listener_extension/service-worker.js`:

- do not clear `activeRecordingSessionId` until offscreen stop/upload succeeds
- do not persist `enabled: false` until stop-recording completes successfully
- if stop/upload fails, return an error while preserving a recoverable state for a retry

Implementation direction:

```javascript
async function stopRecordingIfActive() {
  if (!activeRecordingSessionId) {
    return;
  }
  const response = await chrome.runtime.sendMessage({ target: "offscreen", type: "stop-recording" });
  if (!response || !response.ok) {
    throw new Error(...);
  }
  activeRecordingSessionId = null;
}
```

Then reorder the stop handler:

```javascript
await stopRecordingIfActive();
await saveSettings({ ..., enabled: false });
```

In `popup.js`:

- keep the error surfaced to the user
- avoid rendering a successful stopped state when stop/upload actually failed

**Step 3: Manually verify both success and failure paths**

Check:

1. Normal stop: collection stops, recording uploads, popup shows `未监听`.
2. Failed stop/upload: popup shows failure, state remains retryable, second stop attempt can succeed after backend recovers.

Expected:

- No partial "stopped but recording lost" state.

**Step 4: Commit**

```bash
git add browser_listener_extension/service-worker.js browser_listener_extension/popup.js
git commit -m "fix: make listener stop flow atomic"
```

### Task 4: Sync documentation to the README source of truth

**Files:**
- Modify: `docs/操作手册.md`
- Review: `README.md`
- Review: `browser_listener_extension/README.md`

**Step 1: Write the failing doc checklist**

Lock these documentation requirements:

- all local frontend URLs must use `http://127.0.0.1:8018`
- the extension API base must default to `http://127.0.0.1:8018`
- stop semantics must mention stopping collection and recording together
- video analysis instructions must say "listener session first, explicit video file allowed, session recording optional fallback"

Expected:

- `docs/操作手册.md` currently fails the URL requirement and still describes the older flow.

**Step 2: Update the docs**

Revise `docs/操作手册.md` so it matches README wording on:

- port `8018`
- strict start behavior
- strict stop behavior
- listener-guided video analysis behavior

**Step 3: Quick consistency review**

Verify these three files no longer disagree:

```powershell
rg -n "8010|8018|开始监听|停止监听|录屏|video" README.md docs\操作手册.md browser_listener_extension\README.md
```

Expected:

- No stale `8010` references
- No contradictory listener behavior descriptions

**Step 4: Commit**

```bash
git add docs/操作手册.md README.md browser_listener_extension/README.md
git commit -m "docs: sync listener workflow documentation"
```

### Task 5: Final verification for the minimal closed loop

**Files:**
- Verify: `src/browser_listener.py`
- Verify: `src/webapp.py`
- Verify: `web/index.html`
- Verify: `browser_listener_extension/service-worker.js`
- Verify: `browser_listener_extension/popup.js`
- Verify: `docs/操作手册.md`

**Step 1: Run backend regression tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_browser_listener.py tests/test_browser_listener_api.py
```

Expected:

- PASS

**Step 2: Smoke-test the UI / extension workflow manually**

Manual checklist:

1. Start backend on `8018`.
2. Load the unpacked extension.
3. Start listening.
4. Perform a few real browser actions.
5. Refresh listener state in the web console.
6. Confirm session summary, listener analysis, and video analysis buttons match README behavior.
7. Stop listening and confirm recording upload / retry behavior.

Expected:

- Start and stop are synchronized
- Session summary text is accurate
- Video analysis is available when a listener session exists and a video source is provided

**Step 3: Final commit**

```bash
git add -A
git commit -m "fix: realign listener workflow with README"
```
