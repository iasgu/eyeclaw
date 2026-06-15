# Playwright Execution Skill

## Use When

Use Playwright when EyeClaw has already generated a concrete replay plan from a demonstration: known start URL, known sequence of actions, and enough DOM hints to locate controls. This should be the default fast path for saved skills.

In the product UI this is exposed as `playwright_browser_use_live_workflow`: run fast replay first, then escalate to Browser Use only when replay cannot complete the task.

## Required Skill Fields

- `site_url`: where to open the task, never the EyeClaw console.
- `steps`: atomic `open`, `click`, `type`, `select`, `scroll`, and `wait` actions.
- `selector_hints`: CSS selector, role, label, placeholder, visible text, frame id, and nearby text.
- `download_expectations`: expected file type, expected filename fragment when available, timeout, and target directory.
- `success_criteria`: downloaded file exists, final URL matches, or structured extraction matches schema.

## Execution Rules

- Connect to the user's visible Edge through CDP when preserving login state matters.
- Open a dedicated execution page for each task unless the user asks to run in the current page.
- For native dropdowns, prefer `locator.select_option(...)`.
- For custom dropdowns, click the combobox, then select by `role=option`, visible text, stable `data-*`, or keyboard fallback.
- For downloads, wrap the triggering click in Playwright download handling and only mark success after a real file is saved.
- Report progress as `completed_steps / total_steps`; do not estimate percentage from elapsed time.

## Prompt Shape For Generated Skills

```text
Executor: playwright
Start URL: <site_url>
Preconditions: <login/environment>
Inputs: <runtime fields>
Steps:
1. <action + locator hints + expected state>
Download policy: save to task artifact directory and report only final files.
Success: <file exists/final URL/schema>
Fallback: Browser Use starts automatically if replay cannot complete, including missing selectors, custom dropdown failure, or missing required downloaded file.
```

## EyeClaw Fit

Playwright is the best match for EyeClaw's "process + result learning" path because it turns observed human behavior into fast deterministic actions. Browser Use or Stagehand should be fallback/refinement layers, not the first tool for every saved skill.

## References

- Playwright Python intro: https://playwright.dev/python/docs/intro
- Downloads: https://playwright.dev/python/docs/downloads
- Input and select controls: https://playwright.dev/python/docs/input
