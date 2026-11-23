"""Resolve the two OpenAI settings from a repository-local dotenv file."""

import os
from pathlib import Path


OPENAI_SETTINGS = ("OPENAI_PROJECT_ID", "OPENAI_API_KEY")


def read_openai_settings(dotenv_path: Path | None = None) -> dict[str, str]:
    values = {}
    if dotenv_path is not None and dotenv_path.is_file():
        for line in dotenv_path.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            if not separator or key not in OPENAI_SETTINGS or key in os.environ:
                continue
            value = value.split(" #", 1)[0].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key] = value
    return {key: os.environ.get(key, values.get(key, "")).strip() for key in OPENAI_SETTINGS}
