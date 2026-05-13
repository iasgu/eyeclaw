from __future__ import annotations

import re
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
MODEL_FILE = WORKSPACE_ROOT / "model.txt"
ENV_FILE = WORKSPACE_ROOT / ".env"


def parse_model_sections(raw_text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current_section: str | None = None

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        lower_line = line.lower()
        if line.startswith("##") and "deepseek" in lower_line:
            current_section = "deepseek"
            sections.setdefault(current_section, {})
            continue
        if line.startswith("##") and ("glm" in lower_line or "bigmodel" in lower_line):
            current_section = "glm"
            sections.setdefault(current_section, {})
            continue
        if "model:deepseek" in lower_line or "api.deepseek.com" in lower_line:
            current_section = "deepseek"
            sections.setdefault(current_section, {})
        if "model:glm" in lower_line or "open.bigmodel.cn" in lower_line:
            current_section = "glm"
            sections.setdefault(current_section, {})

        if current_section is None or ":" not in line:
            continue

        key, value = [part.strip() for part in line.split(":", 1)]
        normalized_key = re.sub(r"\s+", "_", key.lower())
        sections[current_section][normalized_key] = value

    return sections


def build_env_lines(sections: dict[str, dict[str, str]]) -> list[str]:
    env_lines = [
        "EDGE_USER_DATA_DIR=.browser/edge-profile",
        "EDGE_CHANNEL=msedge",
        "TARGET_SITE_URL=https://eia.51dzhp.com/#/",
        "",
    ]

    deepseek = sections.get("deepseek", {})
    if deepseek:
        env_lines.extend(
            [
                f"DEEPSEEK_MODEL={deepseek.get('model', '')}",
                f"DEEPSEEK_BASE_URL={deepseek.get('base_url', '')}",
                f"DEEPSEEK_API_KEY={deepseek.get('api_key', '')}",
                "",
            ]
        )

    glm = sections.get("glm", {})
    if glm:
        env_lines.extend(
            [
                f"GLM_MODEL={glm.get('model', '')}",
                f"GLM_BASE_URL={glm.get('base_url', '')}",
                f"GLM_API_KEY={glm.get('api_key', '')}",
                "",
            ]
        )

    return env_lines


def main() -> None:
    if not MODEL_FILE.exists():
        raise SystemExit("model.txt not found in the workspace root.")

    if ENV_FILE.exists():
        print(".env already exists. Skipping creation.")
        return

    sections = parse_model_sections(MODEL_FILE.read_text(encoding="utf-8"))
    env_lines = build_env_lines(sections)
    ENV_FILE.write_text("\n".join(env_lines), encoding="utf-8")

    found_sections = [name for name in ("deepseek", "glm") if sections.get(name)]
    if found_sections:
        print("Found providers: " + ", ".join(found_sections))
    else:
        print("No known providers were parsed from model.txt.")
    print(".env created.")


if __name__ == "__main__":
    main()
