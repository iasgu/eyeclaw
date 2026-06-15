# Stagehand Execution Skill

## Use When

Use Stagehand for pages where the workflow is known but selectors are brittle: dashboards, admin systems, search/filter pages, and pages where the same intent maps to slightly different UI across tenants. Stagehand fits a "observe once, act repeatedly, extract structured result" pattern.

## Required Skill Fields

- `session_model`: model name and provider configured for Stagehand.
- `start_url`: canonical page.
- `observe_intents`: what controls or regions to observe before acting.
- `act_intents`: short action instructions tied to observed controls.
- `extract_schema`: structured result schema when the task asks for page data.
- `cache_key`: site + skill + UI version + observed action hash.
- `success_criteria`: extracted schema validity, final URL, or downloaded file.

## Execution Rules

- Use the Python SDK session API: `start`, `navigate`, `observe`, `act`, `extract`, `execute`, and `replay`.
- Prefer `observe` before `act` when creating a reusable skill, because observed actions can become case memory.
- Keep actions narrow: one UI intent per `act`.
- Use `extract` for final structured results instead of scraping arbitrary page text.
- Stagehand requires either the remote service credentials or a working local Stagehand binary/server.
- Store cache hits/misses and observed action summaries in EyeClaw case memory.

## Prompt Shape For Generated Skills

```text
Executor: stagehand
Start URL: <site_url>
Observe:
- <control/search area/result list>
Act:
- <one intent per action>
Extract schema:
{ ... }
Success: <schema/final URL/file>
Fallback: playwright when cached selectors are valid; browser_use when semantic recovery is needed.
```

## EyeClaw Fit

Stagehand is useful when EyeClaw wants skills to become more precise over time: first run observes and caches semantic actions, later runs replay cached actions or fall back to fresh observation when UI changes.

## References

- Stagehand docs: https://docs.stagehand.dev/
- Stagehand GitHub: https://github.com/browserbase/stagehand

