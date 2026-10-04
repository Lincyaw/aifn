from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from aifn.declare import Access
from aifn.storage import atomic_write, component
from aifn.submit import Tools
from aifn.terms import SessionId

type Row = dict[str, JsonValue]
"""One patch row. Values are JSON, because a row configures a plugin and a plugin's configuration
is JSON: an output-token bound is a number, while a mode or a directory is a string."""
ROW: Final = "aifn-harness"


def layer(tools: Tools, permission: str) -> list[Row]:
    permission = Access(permission)
    rows: list[Row] = [
        {"id": ROW, "config": tools},
        {"id": "sandbox-policy", "config": {"mode": permission.value}},
        {"id": "approval", "config": {"policy": "never"}},
        {
            "id": "permission",
            "config": {
                "defaultPreset": permission.value,
                "presets": {
                    name: {"sandbox": permission.value, "approval": "never"}
                    for name in ("read-only", "workspace-write", "danger-full-access")
                },
            },
        },
    ]
    if permission == Access.READ_ONLY:
        rows.extend([
            {"id": "tool-bash", "disabled": True},
            {"id": "tool-pwsh", "disabled": True},
        ])
    return rows


def write(rows: Sequence[Row], directory: Path, session: SessionId) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{component(session)}.patch.json"
    atomic_write(path, json.dumps(rows, indent=2, sort_keys=True))
    return path
