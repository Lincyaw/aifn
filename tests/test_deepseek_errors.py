from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, JsonValue, TypeAdapter

from aifn import (
    AiFunction,
    Failed,
    Fault,
    Mailbox,
    Model,
    Policy,
    Registry,
    Stage,
    Worker,
    Workspace,
)
from aifn.bundle import install_bundle, runtime_bin
from aifn.engines import DeepSeekEngine
from aifn.engines.deepseek import _turn_error
from aifn.terms import Event

RECORDINGS = Path(__file__).with_name("recordings")
# The rate limit event came from RCA call 34fbb993f1ba408994afd259a734effc.
# The completion event came from RCA call 2a927a70994f4a7492966685cf8ac909.


def recording(name: str) -> Event:
    return TypeAdapter(dict[str, JsonValue]).validate_json((RECORDINGS / name).read_bytes())


def test_recorded_rate_limit_preserves_the_complete_error() -> None:
    event = recording("rate-limit-turn.json")
    error = json.loads(_turn_error((event,)))
    assert error == json.loads((RECORDINGS / "rate-limit-turn.json").read_bytes())[
        "data"
    ]["reason"]["error"]
    assert error["code"] == "RATE_LIMIT"
    assert "429" in error["message"]
    assert "project_rpm,limit=500" in error["message"]


def test_last_turn_error_is_selected() -> None:
    ended = recording("completed-turn.json")
    failed = recording("rate-limit-turn.json")
    assert _turn_error((ended, failed)) == _turn_error((failed,))


def test_completed_turn_cannot_report_an_earlier_error() -> None:
    failed = recording("rate-limit-turn.json")
    ended = recording("completed-turn.json")
    with pytest.raises(ValueError, match="last turn/end"):
        _turn_error((failed, ended))


class Request(BaseModel):
    file: str


class Answer(BaseModel):
    count: int


def test_real_harness_error_reaches_the_saved_result_and_unwrap(tmp_path: Path) -> None:
    home = tmp_path / "dsh"
    install_bundle(home)
    executable = runtime_bin(home)
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Read the supplied event file and report its record count.")
    function = AiFunction(
        name="recording_count", request=Request, returns=Answer, instruction=instruction,
        model=Model(name="deepseek-v4-flash", provider="deepseek-official"),
        workspace=Workspace(directory=tmp_path), attempts=1, timeout_s=20,
    )
    mailbox = Mailbox(root=tmp_path / "mail")
    worker = Worker(
        mailbox=mailbox, registry=Registry(functions={function.name: function}),
        engine=DeepSeekEngine(dsh_home=home, dsh_bin=executable, env={
            "DEEPSEEK_API_KEY": "",
        }),
        policy=Policy(attempts=1), id="error-propagation",
    )
    handle = Stage(function=function, mailbox=mailbox).issue(Request(
        file=str(RECORDINGS / "rate-limit-turn.json"),
    ))
    assert worker.work() == 1
    result = handle.wait(1)
    assert isinstance(result.outcome, Failed)
    assert result.outcome.fault is Fault.CRASHED
    error = json.loads(result.outcome.detail)
    assert error["code"] == "MISSING_CREDENTIAL"
    assert "DEEPSEEK_API_KEY" in error["message"]
    assert result.attempts[0].detail == result.outcome.detail
    ends = [event for event in mailbox.events(handle.id, 0) if event["type"] == "turn/end"]
    assert len(ends) == 1
    assert json.loads(json.dumps(dict(ends[0])))["data"]["reason"]["error"] == error
    persisted = json.loads((mailbox.root / "conclusions" / f"{handle.id}.json").read_bytes())
    assert persisted["outcome"]["detail"] == result.outcome.detail
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        result.unwrap()
