from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from aifn.declare import Access
from aifn.patch import ROW, WITHOUT_A_SHELL, Row, layer, write
from aifn.submit import Tools, bundle


class Services(BaseModel):
    names: list[str]


def configured(rows: list[Row]) -> Tools:
    """The config of the row that carries this call's tools."""
    row = next(entry for entry in rows if entry["id"] == ROW)["config"]
    assert isinstance(row, dict)
    return row


def test_the_layer_carries_the_schema_rather_than_the_bundle_holding_it() -> None:
    """The row's config holds both tool names and the schema derived from `returns`.

    This is the whole reason a launch writes a layer: a bundle is installed by being copied,
    once, and a schema comes from a model that exists only in the calling process, so a bundle
    carrying a schema of its own could only ever hold the one it was installed with.
    """
    tools = configured(layer(bundle(Services, "list_services"), Access.READ_ONLY))
    assert tools["submit"] == "submit_list_services"
    assert tools["refuse"] == "refuse_list_services"
    assert tools["parameters"] == {
        "names": {"required": True, "type": "array", "items": {"type": "string"}}
    }


def test_a_read_only_call_has_no_shell_while_a_writable_one_does() -> None:
    """A read-only call's layer disables the shell rows; a writable call's layer does not.

    Removing the row is what makes "this call cannot run commands" structural: the tool is not
    in the roster at all, rather than being offered and denied when called. A denial still costs
    the turn it was spent in, and an agent that has a shell reaches for one.
    """
    read_only = layer(bundle(Services, "list_services"), Access.READ_ONLY)
    writable = layer(bundle(Services, "list_services"), Access.WORKSPACE_WRITE)
    assert [row["id"] for row in read_only[1:]] == list(WITHOUT_A_SHELL)
    assert all(row["disabled"] is True for row in read_only[1:])
    assert len(writable) == 1


def test_each_session_writes_its_own_layer_file(tmp_path: Path) -> None:
    """Two sessions write two files, because several workers share one dsh home.

    One shared path is overwritten by whichever call started last, and the loser boots holding
    another function's schema: its agent then answers a shape this side is not expecting, and
    the call is spent on that.
    """
    rows = layer(bundle(Services, "list_services"), Access.READ_ONLY)
    first = write(rows, tmp_path, "worker-0-attempt-0")
    second = write(rows, tmp_path, "worker-1-attempt-0")
    assert first != second
    assert json.loads(first.read_text(encoding="utf-8")) == rows
