from __future__ import annotations

from pathlib import Path

from pydantic import JsonValue

type Tools = dict[str, JsonValue]


def bundle(directory: Path) -> Tools:
    return {"directory": str(directory.resolve())}
