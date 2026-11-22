from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

from aifn import AiFunction, Mailbox, Model, Policy, Registry, Stage, Worker, Workspace
from aifn.engines import CodexEngine


class Request(BaseModel):
    file: str


class Answer(BaseModel):
    timeout_s: int


def main() -> None:
    root = Path("mailboxes/codex-demo").resolve()
    workspace = root / "source"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "config.txt").write_text("timeout_s = 30\n", encoding="utf-8")
    function = AiFunction(
        name="read_timeout", request=Request, returns=Answer,
        instruction=Path(__file__).with_name("read_timeout.md"),
        model=Model(name=os.environ.get("AIFN_MODEL", "gpt-6.1-sol"), provider="openai"),
    )
    mailbox = Mailbox(root=root / "queue")
    stage = Stage(function=function, mailbox=mailbox)
    handle = stage.issue(Request(file="config.txt"), workspace=Workspace(directory=workspace))
    worker = Worker(
        mailbox=mailbox, registry=Registry(functions={function.name: function}),
        engine=CodexEngine(codex_home=Path(os.environ.get(
            "CODEX_HOME", str(Path.home() / ".aifn-codex"),
        ))),
        policy=Policy(), id="codex-demo",
    )
    worker.work()
    print(handle.wait().model_dump_json(indent=2))
    print(handle.output_path)


if __name__ == "__main__":
    main()
