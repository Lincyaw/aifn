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


def layer(tools: Tools, permission: str, *, read_only_shell: bool) -> list[Row]:
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
    if permission == Access.READ_ONLY and not read_only_shell:
        rows.extend([
            {"id": "tool-bash", "disabled": True},
            {"id": "tool-pwsh", "disabled": True},
        ])
    return rows


def skill_layer(root: Path) -> list[Row]:
    """Offer the agent the skill bundles under `root` and nothing else.

    The base profile mounts the filesystem provider over the project and user skill directories
    and watches them, and aifn's bundle hides the `skill` tool; a call that declares skills gets
    the tool back with the provider held to its own copied bundles, so what it loads is what was
    submitted.
    """
    return [
        {"id": "skill-filesystem", "config": {
            "includeDefaultRoots": False, "customSkillDirs": [str(root.resolve())], "watch": False,
        }},
        {"id": "skill-office", "disabled": True},
        {"id": "tool-skill", "disabled": False},
    ]


def write(rows: Sequence[Row], directory: Path, session: SessionId) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{component(session)}.patch.json"
    atomic_write(path, json.dumps(rows, indent=2, sort_keys=True))
    return path
