from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import frontmatter

from aifn.storage import atomic_write

SKILL_FILE: Final = "SKILL.md"
SKILL_NAME: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
"""The names dsh's skill registry accepts."""


def check_bundle(directory: Path) -> None:
    """Fail on a bundle the harness would not list.

    dsh skips a skill whose frontmatter it cannot use with only a warning in its own log, so the
    agent would run without it and nothing in the result would say so.
    """
    if not directory.is_dir():
        raise NotADirectoryError(directory)
    path = directory / SKILL_FILE
    metadata = frontmatter.load(str(path)).metadata
    if metadata.get("name") != directory.name or not SKILL_NAME.fullmatch(directory.name):
        raise ValueError(f"{path} must have a kebab-case name equal to its directory name")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{path} must have a description")


def read_bundles(directories: tuple[Path, ...]) -> dict[str, str]:
    """Every file of the bundles, keyed by its path below the skills root."""
    return {
        path.relative_to(directory.parent).as_posix(): path.read_text(encoding="utf-8")
        for directory in directories
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def write_bundles(files: Mapping[str, str], root: Path) -> None:
    for name, content in files.items():
        atomic_write(root / name, content)
