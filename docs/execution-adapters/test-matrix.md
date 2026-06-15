# Execution Adapter Test Matrix

Use the same EyeClaw skills across all adapters so the comparison measures executor behavior, not different prompts.

## Baseline Cases

| Case | Purpose | Required deliverable |
| --- | --- | --- |
| Search/filter/open detail | Tests navigation, click, custom dropdown, and final URL | Final detail page URL |
| Download PDF | Tests file download handling | Real downloaded PDF path |
| Native form select | Tests native dropdowns | Final confirmation URL or extracted value |
| Custom UI select | Tests role/listbox/cascader recovery | Selected state plus final URL |
| Long-running queue | Tests progress, stop, retry, history | Task history and final artifact |

## Per-Adapter Metrics

| Metric | Meaning |
| --- | --- |
| `time_to_first_action` | Startup and page connection overhead |
| `action_success_rate` | Successful steps over attempted steps |
| `download_success_rate` | Real files produced over download tasks |
| `manual_checkpoint_accuracy` | Correctly paused instead of guessing |
| `final_deliverable_accuracy` | Only final files/URLs reported |
| `case_memory_reuse` | Whether previous success/failure improves the next run |
| `operator_visibility` | Whether the user can see current page, status, and stop safely |

## First Test Order

1. `execution_adapter_benchmark` on a saved deterministic skill; compare every wired adapter with the same objective and selected skill.
2. `smart_router_live_workflow` on the same skill; verify route logs show the chosen order and the first successful executor.
3. `playwright_browser_use_live_workflow` on a saved deterministic skill; verify Playwright replay succeeds without Browser Use.
4. The same hybrid task after deliberately weakening one selector; verify it escalates to Browser Use and skips duplicate preflight.
5. Browser Use direct mode on an open-ended task without a reliable recorded plan.
6. Selenium on native form and file download cases.
7. Stagehand on observe/act/extract for a search/filter page after local/remote Stagehand is configured.
8. Skyvern on a long-running task after cloud/local credentials are configured.
9. AutoGLM as OpenClaw-style external agent comparison.

## Pass Criteria

- A task is not successful unless it produces the required final file, final URL, or structured result.
- Logs, screenshots, and intermediate URLs are diagnostics only.
- The UI must keep task history after refresh.
- Progress must be based on real executor events or shown as activity mode.
- The task must be stoppable or cancellable from EyeClaw.
