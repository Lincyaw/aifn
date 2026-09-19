from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from aifn.call import Call
from aifn.declare import Access, AiFunction, Model, Registry, Template, Workspace


class Request(BaseModel):
    path: str


class Answer(BaseModel):
    count: int


def test_an_outbox_line_refuses_a_field_nothing_declares() -> None:
    """A line in an outbox with one field too many fails immediately.

    What a worker reads is a file, and a field silently ignored means the caller believed it
    had said something that the process running the call never saw.
    """
    with pytest.raises(ValidationError):
        Call.model_validate(
            {
                "id": "abc",
                "function": "count_things",
                "request": {"path": "/srv"},
                "issued_at": datetime(2026, 1, 1, tzinfo=UTC),
                "priority": 3,
            }
        )


def test_a_declaration_defaults_to_read_only_and_a_shared_directory() -> None:
    """The default permission is read-only, and read-only calls can share a directory.

    The default is the concurrency behaviour: read-only makes one directory safe for dozens of
    calls at once, while defaulting to writable has the first concurrent declaration silently
    interfering with itself.
    """
    function = AiFunction(
        name="count_things",
        request=Request,
        returns=Answer,
        instruction=Template(body="Count what is in {path}."),
        workspace=Workspace(directory=Path("/srv")),
        model=Model(name="DeepSeek-V4-pro"),
    )
    assert function.workspace.access is Access.READ_ONLY
    assert function.workspace.private is False


def test_a_concrete_declaration_fits_the_registry_table() -> None:
    """One concrete declaration fits the registry's table.

    mypy checks this line, and this is the only place it can: a generic argument is invariant,
    so a table of `AiFunction[BaseModel, BaseModel]` holds no real declaration at all, and that
    only comes out when somebody actually puts one in.
    """
    function = AiFunction(
        name="count_things",
        request=Request,
        returns=Answer,
        instruction=Template(body="Count what is in {path}."),
        workspace=Workspace(directory=Path("/srv")),
        model=Model(name="DeepSeek-V4-pro"),
    )
    registry = Registry(functions={function.name: function})
    assert registry.functions["count_things"].returns is Answer
