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
    """outbox 的一行多出一个字段时立即失败。

    worker 读到的是文件，而一个被静默忽略的字段意味着调用方以为自己
    交待了某件事，而执行那次调用的进程从来没有看到它。
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
    """默认权限是只读，而只读的调用可以共用一个目录。

    默认值就是并发行为：只读时同一个目录被几十次调用同时使用是安全的，
    而默认可写会让第一个并发的函数声明静默地相互干扰。
    """
    function = AiFunction(
        name="count_things",
        request=Request,
        returns=Answer,
        instruction=Template(body="数一数 {path}。"),
        workspace=Workspace(directory=Path("/srv")),
        model=Model(name="DeepSeek-V4-pro"),
    )
    assert function.workspace.access is Access.READ_ONLY
    assert function.workspace.private is False


def test_a_concrete_declaration_fits_the_registry_table() -> None:
    """一个具体的声明装得进 registry 那张表。

    mypy 检查这一行，而这是它唯一能检查到的地方：
    泛型参数是不变的，所以一张 `AiFunction[BaseModel, BaseModel]` 的表
    装不进任何真实的声明，而这件事只在有人真的往里装的时候才暴露。
    """
    function = AiFunction(
        name="count_things",
        request=Request,
        returns=Answer,
        instruction=Template(body="数一数 {path}。"),
        workspace=Workspace(directory=Path("/srv")),
        model=Model(name="DeepSeek-V4-pro"),
    )
    registry = Registry(functions={function.name: function})
    assert registry.functions["count_things"].returns is Answer
