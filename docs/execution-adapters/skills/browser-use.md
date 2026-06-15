# Browser Use Execution Skill

## Use When

Use Browser Use when deterministic replay fails, when the page changed since the demonstration, or when the task needs local reasoning over page state. It should be a fallback or repair executor for EyeClaw skills, not the only execution layer.

## Required Skill Fields

- `goal`: one concrete business objective, not a long SOP essay.
- `start_url`: canonical target URL.
- `selected_skill_steps`: compact list of recorded actions and intent.
- `blocked_domains`: EyeClaw console domains such as `127.0.0.1:8018`.
- `deliverable_policy`: only final downloaded files and final useful URLs count as deliverables.
- `manual_checkpoints`: login, captcha, QR code, irreversible submit, or unclear file choice.

## Execution Rules

- Use `browser-use==0.12.7` with a CDP-connected visible browser when the user needs to see the task.
- Use a dedicated execution page, not the EyeClaw console page.
- Set `downloads_path` per task and enable accepted downloads/PDF auto-download when the task requests files.
- For DeepSeek, avoid native tool forcing on thinking/reasoner models. EyeClaw currently uses a compatibility JSON-mode adapter for DeepSeek to avoid `tool_choice` errors.
- Keep `max_steps`, `max_actions_per_step`, `llm_timeout`, and `step_timeout` explicit per task.
- Treat Browser Use history as diagnostics; success requires a final URL or real file artifact.

## Prompt Shape For Generated Skills

```text
Executor: browser_use
Goal: <task objective>
Start URL: <site_url>
Recorded skill steps: <short numbered list>
Constraints:
- Do not operate on the EyeClaw console.
- If the goal asks for a file, continue until a real file is downloaded.
- Final answer must list downloaded_file paths or final_url only.
Manual checkpoints: <list>
```

## Known Failure Patterns

- `Thinking mode does not support this tool_choice`: caused by using a DeepSeek thinking/reasoner model with tool forcing. Use EyeClaw's compatibility adapter or a tool-compatible model.
- Slow page-state collection: reduce iframe scanning, vision, highlights, and network idle waits for fast mode.
- False delivery: reaching a download page is not enough. Require a real file in the task download directory.

## References

- Browser Use docs: https://docs.browser-use.com/
- Browser Use GitHub: https://github.com/browser-use/browser-use

