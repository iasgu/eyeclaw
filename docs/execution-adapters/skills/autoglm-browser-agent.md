# AutoGLM Browser Agent Execution Skill

## Use When

Use AutoGLM Browser Agent when EyeClaw needs to delegate a browser task through the existing MCP/OpenClaw-style agent channel. This is a good comparison point for OpenClaw integration, but it should not replace EyeClaw's own skill memory and task scheduler.

## Local Status

The project already has an MCP config at `D:\Codex\liangzhu\config\mcporter.json`, pointing to:

```text
C:\Users\majia\.agents\skills\autoglm-browser-agent\dist\mcp_server.exe
```

Logs are configured under:

```text
D:\Codex\liangzhu\.browser\autoglm-mcp-output
```

## Required Skill Fields

- `task_goal`: concise natural language instruction.
- `start_url`: first URL.
- `browser_policy`: whether to use Edge/Chrome and whether extension setup is required.
- `max_steps`: hard execution budget.
- `deliverable_policy`: final files and useful URLs only.
- `manual_checkpoints`: login, extension missing, captcha, or irreversible action.

## Execution Rules

- Keep AutoGLM execution isolated from the EyeClaw console.
- Store screenshots/logs as diagnostics, not as final deliverables.
- If an AutoGLM run succeeds, convert its final actions/results back into EyeClaw case memory.
- If it fails, classify the failure by page state, model decision, browser extension, or missing credential.

## Prompt Shape For Generated Skills

```text
Executor: autoglm_browser_agent
Goal: <business objective>
Start URL: <site_url>
Constraints:
- Do not operate on EyeClaw console.
- Stop at manual checkpoints.
- Return only final files and final useful URLs as deliverables.
```

