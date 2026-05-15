# Eyeclaw Edge MVP Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a 4-hour local MVP that turns a recorded Edge browser demo into a human-readable SOP plus a replayable browser-automation script for the already logged-in target site.

**Architecture:** Use a Python-first stack because Python 3.11 is available locally while `node`, `npm`, `ffmpeg`, and `opencv` are not currently installed. The app is a single local Streamlit UI that uploads or reuses `website.mp4`, extracts sparse key frames, sends them to `GLM-4.6V` for action understanding, asks `deepseek v4 pro` to normalize those actions into a compact DSL, then uses Playwright with a persistent Edge profile so the user can scan-login once and replay the learned flow.

**Tech Stack:** Python 3.11, Streamlit, Playwright (Python), OpenCV or imageio-ffmpeg for frame extraction, `requests` or OpenAI-compatible clients for model calls, Pydantic for DSL validation, Edge persistent browser profile.

---

### Task 1: Bootstrap the Local App

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `README.md`
- Create: `app.py`
- Create: `src/__init__.py`
- Create: `src/config.py`

**Step 1: Write the minimal dependency list**

Create `requirements.txt` with:
- `streamlit`
- `playwright`
- `opencv-python-headless`
- `pydantic`
- `python-dotenv`
- `requests`

**Step 2: Define environment variables without leaking secrets**

Create `.env.example` with placeholders:
- `DEEPSEEK_MODEL=deepseek-chat`
- `DEEPSEEK_BASE_URL=...`
- `DEEPSEEK_API_KEY=...`
- `GLM_MODEL=GLM-4.6V`
- `GLM_BASE_URL=...`
- `GLM_API_KEY=...`
- `EDGE_USER_DATA_DIR=./.browser/edge-profile`
- `EDGE_CHANNEL=msedge`

**Step 3: Add a short setup README**

Document:
- how to create a virtualenv
- how to install requirements
- how to run `playwright install`
- how to launch Streamlit
- that the user must manually log into `https://eia.51dzhp.com/#/`

**Step 4: Create config loading**

In `src/config.py`, read env vars, expose a typed config object, and fail fast with a helpful message when required keys are missing.

**Step 5: Create a minimal Streamlit shell**

In `app.py`, render:
- title
- one-sentence pitch
- video source summary
- placeholder sections for upload, analysis result, and replay

### Task 2: Secure the Provided Model Configuration

**Files:**
- Modify: `README.md`
- Create: `scripts/import_model_txt.py`

**Step 1: Add a one-shot importer for `model.txt`**

Create `scripts/import_model_txt.py` that:
- reads `model.txt`
- extracts model name, base URL, and API key lines
- writes a local `.env` file only if `.env` does not already exist
- never prints secrets back to stdout

**Step 2: Document the safer flow**

Update `README.md` to say:
- keep `model.txt` out of screenshots and commits
- run the importer once
- use `.env` for the app afterward

**Step 3: Add a smoke check**

Make `scripts/import_model_txt.py` print only:
- which providers were found
- whether `.env` was created

### Task 3: Add Video Intake and Sparse Frame Extraction

**Files:**
- Create: `src/video.py`
- Create: `tests/test_video_plan.py`
- Modify: `app.py`

**Step 1: Write the failing test for extraction planning**

In `tests/test_video_plan.py`, add a test that asserts:
- a 43-second video gets reduced to a small list of timestamps
- timestamps stay within a user-selected window
- the extractor caps frames to a fixed maximum such as `8`

**Step 2: Implement timestamp planning first**

In `src/video.py`, add a pure function like:
- `plan_frame_timestamps(duration_seconds, start_second, end_second, max_frames)`

Keep this testable without reading actual video bytes.

**Step 3: Implement real frame extraction**

Add a function that:
- opens the mp4 with OpenCV
- seeks to each planned timestamp
- saves extracted frames into `artifacts/frames/<job_id>/frame_XX.png`

**Step 4: Add Streamlit controls**

In `app.py`, add:
- local file detection for `website.mp4`
- optional upload override
- start/end second inputs
- a button to extract and preview planned frames

**Step 5: Show the current hackathon-safe default**

Default to:
- `start_second = 0`
- `end_second = 20`
- `max_frames = 8`

This avoids sending all 43 seconds to the VLM.

### Task 4: Build the VLM Analysis Layer

**Files:**
- Create: `src/glm_client.py`
- Create: `src/prompts.py`
- Create: `src/analyze.py`
- Create: `tests/test_action_schema.py`

**Step 1: Define the action schema first**

In `tests/test_action_schema.py`, assert the parser accepts only:
- `open`
- `click`
- `type`
- `select`
- `wait`
- `scroll`

and requires fields such as:
- `step_number`
- `action`
- `target`
- optional `value`
- optional `evidence`

**Step 2: Create the prompt templates**

In `src/prompts.py`, write:
- one prompt for GLM frame understanding
- one prompt for DeepSeek normalization

The GLM prompt should ask for:
- page state summary
- visible UI labels
- likely action transitions between adjacent frames
- uncertainty notes

**Step 3: Implement the GLM client**

In `src/glm_client.py`, wrap HTTP calls for `GLM-4.6V` and support:
- image input
- strict timeout
- JSON-only response mode when possible

**Step 4: Implement raw action extraction**

In `src/analyze.py`, call the GLM client with frame batches and produce:
- a list of raw inferred steps
- a compact session summary

### Task 5: Normalize Raw Steps into a Replay DSL

**Files:**
- Create: `src/deepseek_client.py`
- Create: `src/dsl.py`
- Create: `tests/test_dsl_validation.py`
- Modify: `src/analyze.py`

**Step 1: Write the DSL validator test**

In `tests/test_dsl_validation.py`, assert:
- step numbers are ascending
- only supported actions are allowed
- empty `target` values are rejected for `click`, `type`, `select`

**Step 2: Define Pydantic models**

In `src/dsl.py`, add:
- `ReplayStep`
- `ReplayPlan`

with validation and a `to_json()` helper.

**Step 3: Implement the DeepSeek normalizer**

In `src/deepseek_client.py`, send the raw VLM result plus a strict schema instruction, and request:
- concise SOP bullets
- replay DSL
- assumptions/risk notes

**Step 4: Combine analysis into one pipeline**

In `src/analyze.py`, add a top-level function like:
- `build_replay_plan(frame_paths, site_url)`

Return:
- SOP text
- validated DSL
- raw notes

### Task 6: Render the Analysis Result in Streamlit

**Files:**
- Modify: `app.py`
- Create: `src/state.py`

**Step 1: Add session state helpers**

In `src/state.py`, keep:
- current video path
- selected analysis window
- extracted frames
- SOP output
- validated replay plan

**Step 2: Build the main analysis UI**

In `app.py`, show:
- selected video
- extracted frame thumbnails
- generated SOP
- generated DSL as JSON
- a warning box for uncertain steps

**Step 3: Add a “happy path first” UX**

Use a linear flow:
1. Pick video
2. Pick analysis segment
3. Analyze
4. Review SOP
5. Replay in Edge

### Task 7: Add Local Edge Replay with Manual Login Support

**Files:**
- Create: `src/replay.py`
- Create: `tests/test_selector_strategy.py`
- Modify: `app.py`

**Step 1: Write selector fallback tests**

In `tests/test_selector_strategy.py`, cover a helper that builds selector candidates from:
- exact text
- placeholder text
- role/name combinations
- safe fallback CSS if manually supplied

**Step 2: Implement Edge persistent launch**

In `src/replay.py`, use Playwright to:
- launch persistent context with `channel="msedge"`
- reuse `EDGE_USER_DATA_DIR`
- open `https://eia.51dzhp.com/#/`

**Step 3: Add a login pause step**

Expose a function that:
- launches Edge
- waits for the user to scan login manually
- resumes only when the user clicks “Continue” in Streamlit

**Step 4: Implement replay execution**

Support:
- `click`
- `type`
- `select`
- `wait`
- `scroll`

For the hackathon build, match targets using visible text first.

**Step 5: Show live step status**

Update the Streamlit UI during execution with:
- current step
- status text
- success/failure at the end

### Task 8: Add Demo Guardrails and Recovery

**Files:**
- Modify: `app.py`
- Modify: `src/replay.py`
- Create: `src/demo_mode.py`

**Step 1: Add a manual selector override**

Allow the user to paste a one-off selector for any failed step.

**Step 2: Add a dry-run mode**

In `src/demo_mode.py`, render what the agent intends to do without clicking, for safer demo validation.

**Step 3: Add a retry affordance**

When a replay step fails, offer:
- retry once
- skip step
- stop replay

This is valuable for live demo resilience.

### Task 9: Polish the Hackathon Narrative

**Files:**
- Modify: `README.md`
- Create: `DEMO_SCRIPT.md`

**Step 1: Write the two-minute demo script**

In `DEMO_SCRIPT.md`, script:
1. open the app
2. show `website.mp4`
3. analyze only the meaningful segment
4. review SOP
5. pause for Edge login if needed
6. replay the workflow

**Step 2: Add one-sentence positioning**

Use:
- “Eyeclaw, automate later.”
- “You don’t prompt the browser. You teach it.”

**Step 3: Record current environment blockers**

List current local blockers discovered in this workspace:
- `node` not installed
- `npm` not installed
- `ffmpeg` not installed
- `opencv` not installed yet
- no git repo initialized

### Task 10: Final Verification Checklist

**Files:**
- Modify: `README.md`

**Step 1: Add a pre-demo checklist**

Include:
- `.env` created
- dependencies installed
- Playwright browsers installed
- Edge opens successfully
- user can scan login
- analysis segment chosen
- replay plan generated

**Step 2: Add a post-demo fallback**

If replay is flaky, still show:
- extracted frames
- inferred SOP
- validated replay JSON

That preserves the product story even if the final click sequence fails.
