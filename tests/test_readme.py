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
    """The README's pipeline as it is joined, with all three arguments inferred.

    A separate function with a return type, because this is the only spelling under which mypy
    checks that `Link.adapt`'s element type matches `downstream`'s input type.
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
    """The README's example is this library's main explanation, so mypy has to accept it.

    An example that does not hold up in types has everybody who copies it hitting the same
    error, and no check reaches an example inside a docstring.
    """
    workspace = Workspace(directory=Path("/srv/target"), access=Access.READ_ONLY)
    model = Model(name="DeepSeek-V4-pro")

    list_services = AiFunction(
        name="list_services",
        request=Repository,
        returns=Services,
        instruction=Template(body="List every service in the repository at {path}."),
        workspace=workspace,
        model=model,
    )
    read_timeouts = AiFunction(
        name="read_timeouts",
        request=Service,
        returns=Timeouts,
        instruction=Template(
            body="In {repository}, find every timeout the {name} service configures."
        ),
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
