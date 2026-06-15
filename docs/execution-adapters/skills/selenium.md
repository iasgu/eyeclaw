# Selenium Execution Skill

## Use When

Use Selenium as a conservative enterprise fallback when WebDriver compatibility, corporate browser policies, native forms, or existing RPA-style infrastructure matter. It is less expressive than Playwright for modern automation ergonomics, but stable and familiar in many enterprise environments.

## Required Skill Fields

- `browser`: Edge or Chrome.
- `start_url`: canonical target page.
- `webdriver_mode`: launch new browser or connect to an existing debugging session.
- `steps`: deterministic actions with stable selectors.
- `form_controls`: native select/input/button metadata.
- `download_expectations`: expected file type and directory.
- `success_criteria`: file exists, final URL, or extracted value.

## Execution Rules

- Use `selenium==4.44.0`.
- Prefer stable CSS/XPath selectors generated from listener DOM hints.
- For native dropdowns, use `selenium.webdriver.support.ui.Select`.
- For custom dropdowns, click the trigger and then locate visible option elements.
- Selenium has no first-class download event like Playwright; verify success through the filesystem and reject temporary suffixes such as `.crdownload`, `.part`, and `.tmp`.
- Keep waits explicit with WebDriverWait and expected conditions.

## Prompt Shape For Generated Skills

```text
Executor: selenium
Browser: edge
Start URL: <site_url>
Steps:
1. <action + selector + expected condition>
Download policy: verify completed file in task artifact directory.
Success: <file/final URL/schema>
Fallback: playwright or browser_use.
```

## References

- Selenium WebDriver docs: https://www.selenium.dev/documentation/webdriver/
- Select lists: https://www.selenium.dev/documentation/webdriver/elements/select_lists/

