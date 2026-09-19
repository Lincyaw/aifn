from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from aifn import (
    Access,
    AiFunction,
    Batch,
    Flow,
    Link,
    Mailbox,
    Model,
    Policy,
    Registry,
    Stage,
    Template,
    Worker,
    Workspace,
)


class Repository(BaseModel):
    path: str


class Services(BaseModel):
    names: list[str]


class Service(BaseModel):
    repository: str
    name: str


class Timeouts(BaseModel):
    values: dict[str, int]


def pipeline_of(
    found: Batch[Services], inspect: Stage[Service, Timeouts]
) -> Flow[Services, Service, Timeouts]:
    """README 那条流水线的接法，三个类型参数全部由推断得出。

    单独一个有返回类型的函数，因为这是 mypy 唯一会去核对
    `Link.adapt` 的返回元素类型和 `downstream` 的输入类型是否相符的写法。
    """
    return Flow(
        upstream=found,
        downstream=inspect,
        link=Link(
            adapt=lambda services: [
                Service(repository="/srv/target", name=name) for name in services.names
            ]
        ),
    )


def test_the_readme_pipeline_type_checks() -> None:
    """README 那个例子是这个库的主要说明，所以它必须能通过 mypy。

    一个类型上讲不通的例子会让每一个照着写的人都撞上同一个错误，
    而 docstring 里的例子没有任何检查会碰到它。
    """
    workspace = Workspace(directory=Path("/srv/target"), access=Access.READ_ONLY)
    model = Model(name="DeepSeek-V4-pro")

    list_services = AiFunction(
        name="list_services",
        request=Repository,
        returns=Services,
        instruction=Template(body="列出 {path} 这个仓库里的全部服务。"),
        workspace=workspace,
        model=model,
    )
    read_timeouts = AiFunction(
        name="read_timeouts",
        request=Service,
        returns=Timeouts,
        instruction=Template(body="在 {repository} 里找出 {name} 这个服务的全部超时配置。"),
        workspace=workspace,
        model=model,
        timeout_s=600.0,
    )

    registry = Registry(functions={f.name: f for f in (list_services, read_timeouts)})
    mailbox = Mailbox(root=Path("mailboxes/timeouts"))

    scan = Stage(function=list_services, mailbox=mailbox)
    inspect = Stage(function=read_timeouts, mailbox=mailbox)
    pipeline = pipeline_of(Batch[Services](handles=()), inspect)

    assert pipeline.downstream is inspect
    assert scan.function.returns is Services
    assert registry.functions["read_timeouts"].timeout_s == 600.0
    assert Policy().attempts == 2
    assert Worker.__dataclass_fields__.keys() >= {"mailbox", "registry", "engine", "policy", "id"}
