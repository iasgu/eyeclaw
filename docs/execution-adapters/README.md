# EyeClaw Execution Adapter Plan

EyeClaw should treat browser automation as a replaceable execution layer. The core product value is still demonstration capture, multimodal analysis, skill sedimentation, and case-based improvement. Browser automation tools are adapters that apply learned skills in real software.

## Installed Status

The machine-readable registry is `D:\Codex\liangzhu\config\execution_adapters.json`. The web app exposes it through `/api/execution-adapters` and uses it for the task execution selector. Use this registry as the source of truth for frontend executor selection, scheduler routing, and adapter test status.

| Adapter | Local status | Install/source location | Primary role |
| --- | --- | --- | --- |
| Multi-adapter benchmark | Wired in EyeClaw scheduler | `execution_adapter_benchmark` in `D:\Codex\liangzhu\src\webapp.py` | Run the same task across adapters and compare speed, progress events, and final success rate |
| Playwright Python | Installed in `D:\Codex\liangzhu\.venv` | `playwright==1.59.0`; wheel in `D:\Codex\downloads\browser-automation-source\pypi` | Fast deterministic replay from EyeClaw steps |
| Browser Use | Installed in `D:\Codex\liangzhu\.venv` | `browser-use==0.12.7`; wheel in `D:\Codex\downloads\browser-automation-source\pypi` | AI fallback when recorded steps do not transfer cleanly |
| Stagehand | Installed in `D:\Codex\liangzhu\.venv` | `stagehand==3.20.0`; wheel in `D:\Codex\downloads\browser-automation-source\pypi` | Observe/act/extract adapter for semi-structured pages |
| Selenium | Wired in EyeClaw scheduler | `selenium==4.44.0`; wheel in `D:\Codex\downloads\browser-automation-source\pypi` | Enterprise deterministic fallback, especially native forms |
| Skyvern | Installed in separate venv | `D:\Codex\downloads\browser-automation-source\skyvern-venv`; wheel in `D:\Codex\downloads\browser-automation-source\pypi` | Managed long-running task/workflow executor |
| AutoGLM Browser Agent | Already configured through MCP | `config/mcporter.json`; local skill under `C:\Users\majia\.agents\skills\autoglm-browser-agent` | External web-agent option |

Direct GitHub shallow clone was attempted for Browser Use, Stagehand, Skyvern, and Playwright Python, but the current network could not connect to `github.com:443`. The PyPI wheels are under `D:\Codex\downloads\browser-automation-source\pypi`, and the exact installed Python package source used by the local runtimes is copied under `D:\Codex\downloads\browser-automation-source\installed-packages`, so local inspection and integration can continue.

## Routing Policy

1. Use `smart_router_live_workflow` by default. It asks the configured LLM for an execution order, falls back to rules if the LLM is unavailable, and then tries each enabled adapter until one succeeds.
2. Use `execution_adapter_benchmark` when validating a skill or execution environment: it runs the same task across enabled adapters and records duration, real progress event count, final files/URLs, and success rate.
3. Use `playwright_browser_use_live_workflow` when you explicitly want recorded-skill replay first: Playwright-style fast replay runs first, and Browser Use starts only when replay cannot complete.
4. Use `browser_use_live_workflow` directly when there is no reliable recorded plan or the task is intentionally open-ended.
5. Use Selenium for enterprise pages where native form controls, WebDriver compatibility, or corporate browser policies make it easier than Playwright.
6. Use Stagehand when the page is mostly stable but selectors are brittle, and the skill can be expressed as observe/act/extract intents.
7. Use Skyvern for long-running managed tasks where task status, workflow history, credentials, retries, and artifacts are more important than low-level control.
8. Use AutoGLM as an external agent option, especially when OpenClaw/MCP integration matters more than local Python control.

## Skill Creation Contract

Every saved EyeClaw skill should carry an execution profile in addition to human-readable SOP text:

| Field | Meaning |
| --- | --- |
| `site_url` | Canonical start page, never the EyeClaw console URL |
| `executor_preferences` | Ordered list such as `["playwright", "browser_use"]` |
| `preconditions` | Login state, selected tenant, required role, environment, expected page state |
| `steps` | Atomic actions from listener/video analysis |
| `selector_hints` | DOM selectors, roles, labels, frame info, nearby text, and visual anchors |
| `data_inputs` | Runtime parameters that can vary by task |
| `deliverable_policy` | Final files and final URLs only; logs are not deliverables |
| `success_criteria` | Observable page state, file existence, final URL, extracted result schema |
| `manual_checkpoints` | Login, captcha, payment, irreversible submit, or ambiguous destructive action |
| `case_memory_keys` | Site, skill, target UI component, failure reason, and recovery action |

## Progress Semantics

Do not invent a fake percentage for AI-driven adapters. Use real counters:

- Playwright/Selenium: completed deterministic step count over total steps.
- Browser Use: agent round count, current action, elapsed time, and final success signal.
- Multi-adapter benchmark: completed adapter attempts over total attempts, with per-adapter duration and progress-event counts.
- Stagehand: session action stream, observe/act/extract phase, cache hit/miss.
- Skyvern: remote/local task status, workflow step state, artifact completion.

## Official References

- Browser Use docs: https://docs.browser-use.com/
- Playwright Python docs: https://playwright.dev/python/docs/intro
- Playwright downloads: https://playwright.dev/python/docs/downloads
- Playwright input/select controls: https://playwright.dev/python/docs/input
- Stagehand docs: https://docs.stagehand.dev/
- Skyvern docs: https://www.skyvern.com/docs
- Selenium WebDriver docs: https://www.selenium.dev/documentation/webdriver/
- Selenium select lists: https://www.selenium.dev/documentation/webdriver/elements/select_lists/
