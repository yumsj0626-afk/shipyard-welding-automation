"""Portable case IDs and repository-relative inputs shared by the run modes."""

import re


def validate_case_id(case: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", case) or re.fullmatch(
        r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", case, re.IGNORECASE,
    ):
        raise ValueError("Invalid case ID. Use letters, digits, hyphens or underscores.")


def case_input_files(case: str, *, include_reports: bool) -> tuple[str, ...]:
    validate_case_id(case)
    files = (
        f"data/dev/{case}/initial.json",
        f"data/dev/{case}/evidence.json",
        f"simulation/dev/{case}/events.jsonl",
    )
    if include_reports:
        files += (f"simulation/reports/{case}.jsonl",)
    return files + ("config/dev-policy.json",)
