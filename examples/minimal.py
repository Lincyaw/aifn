from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic import BaseModel

from aifn import (
    AiFunction,
    Mailbox,
    Model,
    Policy,
    Registry,
    Stage,
    Worker,
    Workspace,
)
from aifn.engines import DeepSeekEngine


class Request(BaseModel):
    file: str


class Answer(BaseModel):
    timeout_s: int


read_timeout = AiFunction(
    name="read_timeout",
    request=Request,
    returns=Answer,
    instruction=Path(__file__).with_name("read_timeout.md"),
    model=Model(name=os.environ.get("AIFN_MODEL", "deepseek-v4-flash")),
)
root = Path("mailboxes/demo").resolve()
mailbox = Mailbox(root=root / "queue")
stage = Stage(function=read_timeout, mailbox=mailbox)


def make_worker(worker_id: str) -> Worker:
    return Worker(
        mailbox=mailbox,
        registry=Registry(functions={read_timeout.name: read_timeout}),
        engine=DeepSeekEngine(dsh_home=Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))),
        policy=Policy(),
        id=worker_id,
    )


async def main() -> None:
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    config = source / "config.txt"
    if not config.exists():
        config.write_text("timeout_s = 30\n", encoding="utf-8")
    workspaces = (
        Workspace(directory=source),
        Workspace(directory=source, private=True),
        Workspace(directory=root / "empty", create=True),
    )
    request = Path(__file__).with_name("input.json")
    handles = [stage.issue_file(request, workspace=item) for item in workspaces]
    results = await asyncio.gather(*(handle.wait_async(timeout_s=600) for handle in handles))
    for result in results:
        print(result.model_dump_json(indent=2))
    for handle in handles:
        if handle.output_path.exists():
            print(handle.output_path)


if __name__ == "__main__":
    asyncio.run(main())
