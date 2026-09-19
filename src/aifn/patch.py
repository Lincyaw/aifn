from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from aifn.declare import Access
from aifn.submit import Tools
from aifn.terms import SessionId

type Row = dict[str, Tools | str | bool]
"""One entry of a patch layer: a row id, and what to do to the row of that id.

A loader patch is either an override of a row's config or a flag turning the row off, so
this holds both shapes rather than one per kind: the layer is a list of them in order, and
the runtime reads the keys each entry carries.
"""

ROW: Final = "aifn-harness"
"""The id of the row the bundle's own layer inserts.

A call's layer targets that row by this id to fill in its configuration, so the id
is the one name the two layers have to agree on.
"""

WITHOUT_A_SHELL: Final = ("tool-bash", "tool-pwsh")
"""The rows a read-only call does not get.

A read-only call answers by reading, and a shell is not a capability it happens to
have no use for, it is the widest way it could change the directory it was given.
Removing the rows makes that structural: the capability is not there at all, rather
than being there and denied by a sandbox rule that has to keep holding. Both shells
are named although only one of them is enabled per platform, so that the layer says
the same thing wherever a worker runs.

`write` and `edit` stay, because `tool-fs` registers them alongside `read` and has no
configuration separating the three, so removing them costs the way the agent reads its
workspace. Under read-only the sandbox denies them where they are called, and the
denial reaches the agent as a tool result it can read.
"""


def layer(tools: Tools, permission: str) -> list[Row]:
    """One call's patch layer: what its tools are, and what it has no way to do.

    A layer rather than the bundle's own configuration, because a submit tool's
    schema comes from a Pydantic model that exists only in this process. The bundle
    is installed once and holds the mechanism for answering a call; which tools
    this call has, and the shape of the one that carries its answer, arrive as this
    layer, which applies after every bundle's.

    The configuration row comes first so it is applied before anything is disabled.
    Both target rows that exist by then, and reading the file in the order the
    session is built from is what makes a mistake here visible: a patch that
    matches nothing is skipped with a warning rather than refused.
    """
    disabled: list[Row] = (
        [{"id": row, "disabled": True} for row in WITHOUT_A_SHELL]
        if permission == Access.READ_ONLY
        else []
    )
    return [{"id": ROW, "config": tools}, *disabled]


def write(rows: Sequence[Row], directory: Path, session: SessionId) -> Path:
    """Write one call's layer where the runtime can read it, and say where that is.

    JSON rather than the YAML dialect the profile's own layers are written in: a
    generated layer carries a generated schema, and block scalars and `!!js` tags
    are what a generator has to get exactly right in YAML and never has to in JSON.
    The runtime's patch reader takes either.

    One file per session, because a worker runs one call at a time while several
    workers share one dsh home: a single shared path would be overwritten by
    whichever call started last, and the loser would boot holding another
    function's schema.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.patch.json"
    path.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
    return path
