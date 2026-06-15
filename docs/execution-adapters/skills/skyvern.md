# Skyvern Execution Skill

## Use When

Use Skyvern for long-running or managed browser tasks where task history, retries, workflow operations, credentials, and artifacts are important. Treat it as a task/workflow executor rather than a low-level replay engine.

## Local Install

Skyvern is installed in a separate D-drive virtual environment because `skyvern==1.0.36` requires `rich<14`, while `browser-use==0.12.7` in the main EyeClaw environment requires `rich==14.3.1`.

```powershell
D:\Codex\downloads\browser-automation-source\skyvern-venv\Scripts\skyvern.exe --help
```

## Required Skill Fields

- `task_goal`: business-level objective.
- `start_url`: target URL.
- `credentials_policy`: whether Skyvern may use stored credentials or must pause.
- `workflow_blocks`: reusable workflow block outline if known.
- `max_steps`: execution budget.
- `artifact_expectations`: final file, final URL, screenshot, or structured output.
- `manual_checkpoints`: login, captcha, high-risk submit, or missing credential.

## Execution Rules

- Use Skyvern CLI/service only from the separate Skyvern venv.
- Keep EyeClaw's main scheduler responsible for task registration and UI state.
- Store Skyvern task id, status URL, final artifacts, and error summary back into EyeClaw.
- Do not count Skyvern logs as deliverables. Only final files, final URLs, and explicit structured outputs are deliverables.
- For local testing, run `skyvern doctor`, then configure cloud/local execution before binding it as an EyeClaw task type.

## Prompt Shape For Generated Skills

```text
Executor: skyvern
Task goal: <business objective>
Start URL: <site_url>
Credentials: <stored/manual>
Artifacts required: <file/url/schema>
Risk checkpoints: <list>
Max steps: <number>
Completion: <observable final condition>
```

## References

- Skyvern docs: https://www.skyvern.com/docs
- Skyvern GitHub: https://github.com/Skyvern-AI/skyvern

