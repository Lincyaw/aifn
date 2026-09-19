from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from aifn import (
    AiFunction,
    Attempt,
    Call,
    Fault,
    Flow,
    LeaseLost,
    Link,
    Mailbox,
    Model,
    Return,
    Returned,
    Stage,
    Workspace,
)


class Request(BaseModel):
    file: str


class Answer(BaseModel):
    count: int


def stage_at(path: Path) -> Stage[Request, Answer]:
    instruction = path / "instruction.md"
    instruction.write_text("Read input.json and count the entries in its file.")
    return Stage(function=AiFunction(
        name="count", request=Request, returns=Answer, instruction=instruction,
        model=Model(name="test"), workspace=Workspace(directory=path),
    ), mailbox=Mailbox(root=path / "mail"))


def enqueue(root: Path, call: Call) -> tuple[str, bool]:
    queued, issued = Mailbox(root=root).enqueue(call)
    return queued.id, issued


def claim(root: Path, call: str) -> bool:
    return Mailbox(root=root).claim(call, "worker", timedelta(seconds=10)) is not None


def test_processes_deduplicate_and_claim_one_task(tmp_path: Path) -> None:
    stage = stage_at(tmp_path)
    calls = [stage.call(Request(file="a")) for _ in range(8)]
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        queued = list(pool.map(enqueue, [stage.mailbox.root] * 8, calls))
        assert len({item[0] for item in queued}) == 1
        assert sum(item[1] for item in queued) == 1
        assert sum(pool.map(claim, [stage.mailbox.root] * 8, [queued[0][0]] * 8)) == 1


def test_expired_attempt_recovers_and_blocks_old_worker(tmp_path: Path) -> None:
    stage = stage_at(tmp_path)
    handle = stage.issue(Request(file="a"))
    old = stage.mailbox.claim(handle.id, "old", timedelta(milliseconds=100))
    assert old is not None
    now = datetime.now(UTC)
    stage.mailbox.begin(old, Attempt(index=0, session="interrupted", worker="old",
                                    started_at=now, finished_at=now))
    time.sleep(0.15)
    new = stage.mailbox.claim(handle.id, "new", timedelta(seconds=10))
    assert new is not None and new.attempt == 1
    assert stage.mailbox.state(handle.id).attempts[0].fault is Fault.CRASHED
    result: Return[Answer] = Return(id=handle.id, function="count",
                                  outcome=Returned(value=Answer(count=7)), finished_at=now)
    with pytest.raises(LeaseLost):
        stage.mailbox.deliver(result, old)
    stage.mailbox.deliver(result, new)
    assert handle.wait().unwrap().count == 7
    assert stage.issue(Request(file="a")).id == handle.id


def test_flow_reconstructs_without_duplicate_downstream(tmp_path: Path) -> None:
    stage = stage_at(tmp_path)
    upstream = stage.issue_all([Request(file="a"), Request(file="b")])
    link = Link[Answer, Request](adapt=lambda answer: [Request(file=str(answer.count))])
    flow = Flow(upstream=upstream, downstream=stage, link=link)
    for index, handle in enumerate(upstream.handles):
        owner = stage.mailbox.claim(handle.id, "test", timedelta(seconds=10))
        assert owner is not None
        stage.mailbox.deliver(Return(
            id=handle.id, function="count", outcome=Returned(value=Answer(count=index)),
            finished_at=datetime.now(UTC),
        ), owner)
        assert flow.advance() == 1
        assert flow.advance() == 0
    rebuilt = Flow(upstream=upstream, downstream=stage, link=link)
    assert rebuilt.done()
    assert len(rebuilt.issued().handles) == 2
