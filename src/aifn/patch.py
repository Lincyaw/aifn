from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from aifn.declare import Access
from aifn.storage import atomic_write, component
from aifn.submit import Tools
from aifn.terms import SessionId

type Row = dict[str, Tools | str | bool]
ROW: Final = "aifn-harness"
WITHOUT_A_SHELL: Final = ("tool-bash", "tool-pwsh")


def layer(tools: Tools, permission: str) -> list[Row]:
    permission = Access(permission)
    disabled: list[Row] = (
        [{"id": row, "disabled": True} for row in WITHOUT_A_SHELL]
        if permission == Access.READ_ONLY else []
    )
    return [
        {"id": ROW, "config": tools},
        {"id": "sandbox-policy", "config": {"mode": permission.value}},
        {"id": "approval", "config": {"policy": "never"}},
        {"id": "permission", "config": {
            "defaultPreset": permission.value,
            "presets": {name: {"sandbox": permission.value, "approval": "never"}
                        for name in ("read-only", "workspace-write", "danger-full-access")},
        }},
        *disabled,
    ]


def write(rows: Sequence[Row], directory: Path, session: SessionId) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{component(session)}.patch.json"
    atomic_write(path, json.dumps(rows, indent=2, sort_keys=True))
    return path
