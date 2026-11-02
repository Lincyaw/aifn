"""Linux deployment example; no model call or filesystem mutation on import."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from aifn import (
    AiFunction,
    Mailbox,
    Model,
    Policy,
    Registry,
    SrtPolicy,
    TrajectoryArchive,
    Worker,
    Workspace,
)
from aifn.engines import DeepSeekEngine


class Request(BaseModel):
    file: str


class Answer(BaseModel):
    count: int = Field(ge=0)


def worker(identity: str) -> Worker:
    from dsh_trajectory.transport import HTTPStore

    function = AiFunction(
        name="count", request=Request, returns=Answer, model=Model(name="deepseek-chat"),
        instruction=Path("/srv/tasks/job-123/instruction.md"),
        workspace=Workspace(directory=Path("/srv/tasks/job-123")),
        validation="json-schema",
    )
    archive = TrajectoryArchive(
        staging=Path("/var/lib/aifn/spool"),
        sink=HTTPStore("https://trajectories.example.org",
                       token=os.environ["DSH_TRAJECTORY_TOKEN"]),
    )
    return Worker(
        mailbox=Mailbox(root=Path("/var/lib/aifn/mail")),
        registry=Registry(functions={function.name: function}),
        engine=DeepSeekEngine(
            dsh_home=Path("/opt/aifn/dsh"),
            env={"DEEPSEEK_API_KEY": os.environ["DEEPSEEK_API_KEY"]},
            sandbox=SrtPolicy(state_root=Path("/var/lib/aifn/control")),
            trajectory_directory=Path("/var/lib/aifn/trajectories"), archive=archive,
        ),
        policy=Policy(), id=identity, retain_events=False,
    )
