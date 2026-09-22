from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, Json, computed_field, model_validator

from aifn import (
    Access,
    AiFunction,
    Call,
    Corrections,
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
from aifn.engines import DeepSeekEngine, ReplayEngine, install_bundle

ROUTE = Path(__file__).with_name("probe-route.patch.yml")


class Endpoint(ThreadingHTTPServer):
    responses: list[list[dict[str, Any]]]
    requests: list[dict[str, Any]]
    tasks: Path


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        server = self.server
        assert isinstance(server, Endpoint)
        server.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        calls = server.responses.pop(0) if server.responses else []
        if calls:
            directory = max(
                server.tasks.glob("*/attempts/*"), key=lambda path: path.stat().st_mtime
            )
            paths = {"$output": str(directory / "output" / "output.json"),
                     "$refusal": str(directory / "output" / "refusal.json"),
                     "$input": str(directory / "input.json"),
                     "$escape": str(directory / "output" / "outside.json")}
            for call in calls:
                arguments = json.loads(call["function"]["arguments"])
                for key in ("file_path", "path"):
                    if key in arguments:
                        arguments[key] = paths.get(arguments[key], arguments[key])
                call["function"]["arguments"] = json.dumps(arguments)
        delta = {"role": "assistant", "tool_calls": calls} if calls else {"content": "done"}
        payload = {"id": "response", "object": "chat.completion.chunk", "created": 0,
                   "model": "aifn-probe", "choices": [{"index": 0, "delta": delta,
                   "finish_reason": "tool_calls" if calls else "stop"}]}
        body = ("data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def endpoint() -> Iterator[Endpoint]:
    server = Endpoint(("127.0.0.1", 0), Handler)
    server.responses = []
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def tool(name: str, **arguments: object) -> list[dict[str, Any]]:
    return [{"index": 0, "id": f"call-{name}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}}]


class Request(BaseModel):
    file: str


class Answer(BaseModel):
    count: int = Field(ge=1)


def runtime(path: Path, endpoint: Endpoint) -> tuple[Stage[Request, Answer], Worker]:
    instruction = path / "instruction.md"
    instruction.write_text("Read input.json and count entries. Submit the result file.")
    function = AiFunction(name="count", request=Request, returns=Answer, instruction=instruction,
                          model=Model(name="aifn-probe", provider="probe"),
                          workspace=Workspace(directory=path), timeout_s=20)
    home = path / "dsh"
    install_bundle(home)
    mailbox = Mailbox(root=path / "mail")
    endpoint.tasks = mailbox.root / "tasks"
    worker = Worker(mailbox=mailbox, registry=Registry(functions={"count": function}),
                    engine=DeepSeekEngine(dsh_home=home, patches=(ROUTE,), env={
                        "AIFN_PROBE_BASE_URL": f"http://127.0.0.1:{endpoint.server_address[1]}/v1",
                        "AIFN_PROBE_API_KEY": "probe",
                    }), policy=Policy(backoff=timedelta(0)), id="worker")
    return Stage(function=function, mailbox=mailbox), worker


@pytest.mark.parametrize("access", [Access.READ_ONLY, Access.WORKSPACE_WRITE])
def test_harness_validates_corrects_and_submits_in_one_session(
    tmp_path: Path, endpoint: Endpoint, access: Access,
) -> None:
    stage, worker = runtime(tmp_path, endpoint)
    source = tmp_path / "request.json"
    source.write_text('{"file":"data.csv"}')
    business = tmp_path / "business.txt"
    business.write_text("unchanged")
    handle = stage.issue_file(source, workspace=Workspace(directory=tmp_path, access=access))
    endpoint.responses = [
        tool("write", file_path="$output", content='{"count":0}'),
        tool("validate_output", path="output.json"),
        tool("edit", file_path="$output", old_string='"count":0', new_string='"count":2'),
        tool("str_replace_editor", command="str_replace", path="$output",
             old_str='"count":2', new_str='"count":3'),
        tool("read", file_path=str(business)),
        tool("edit", file_path=str(business), old_string="unchanged", new_string="changed"),
        tool("read", file_path="$input"),
        tool("write", file_path="$input", content="{}"),
        tool("submit_output", path="output.json"),
    ]
    assert worker.work() == 1
    result = handle.wait(1)
    assert result.unwrap().count == 3
    assert len(result.attempts) == 1
    assert json.loads(handle.output_path.read_text()) == {"count": 3}
    assert "greater_than_equal" in json.dumps(endpoint.requests[2])
    offered = {item["function"]["name"] for item in endpoint.requests[0]["tools"]}
    assert {"write", "edit", "str_replace_editor", "validate_output", "submit_output"} <= offered
    assert ("bash" not in offered) == (access == Access.READ_ONLY)
    assert business.read_text() == ("unchanged" if access == Access.READ_ONLY else "changed")
    events = stage.mailbox.events(handle.id, 0)
    denied = [event for event in events if event.get("type") == "tool/result"
              and "FS_SANDBOX_DENIED" in json.dumps(event)]
    assert len(denied) == (2 if access == Access.READ_ONLY else 1)
    task = stage.mailbox.task_path(handle.id)
    attempt = next((task / "attempts").iterdir())
    assert json.loads((attempt / "input.json").read_text()) == {"file": "data.csv"}
    assert "minimum" in (attempt / "output.schema.json").read_text()
    assert stage.mailbox.enqueue(stage.call(
        Request(file="data.csv"), workspace=Workspace(directory=tmp_path, access=access),
    ))[1] is False
    replay_mail = Mailbox(root=tmp_path / "replay")
    original = Call.model_validate_json(
        (stage.mailbox.root / "requests" / f"{handle.id}.json").read_bytes()
    )
    replay_mail.issue(original)
    replay = replace(worker, mailbox=replay_mail,
                     engine=ReplayEngine(directory=stage.mailbox.root / "events"))
    assert replay.work() == 1
    replayed = replay_mail.returned(handle.id, Answer)
    assert replayed is not None and replayed.unwrap().count == 3


def test_harness_retries_missing_submission_and_accepts_refusal_file(
    tmp_path: Path, endpoint: Endpoint,
) -> None:
    stage, worker = runtime(tmp_path, endpoint)
    handle = stage.issue(Request(file="absent.csv"))
    endpoint.responses = [
        [], tool("write", file_path="$refusal", content='{"reason":"Missing file"}'),
        tool("refuse_output", path="refusal.json"),
    ]
    assert worker.work() == 1
    result = handle.wait(1)
    assert result.status == "refused"
    assert len(result.attempts) == 2
    assert result.attempts[0].fault is Fault.NO_ANSWER


def test_harness_timeout_writes_failed_result(tmp_path: Path, endpoint: Endpoint) -> None:
    stage, worker = runtime(tmp_path, endpoint)
    function = replace(stage.function, timeout_s=0.01)
    stage = Stage(function=function, mailbox=stage.mailbox)
    worker = replace(worker, registry=Registry(functions={"count": function}))
    handle = stage.issue(Request(file="data.csv"))
    assert worker.work() == 1
    assert handle.wait().attempts[0].fault is Fault.TIMED_OUT


class ConvertedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(validation_alias="input_count", serialization_alias="total")
    payload: Json[list[int]]

    @model_validator(mode="after")
    def increment(self) -> ConvertedAnswer:
        self.count += 1
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def double(self) -> int:
        return self.count * 2


def test_submitted_value_survives_recovery_and_replay(tmp_path: Path, endpoint: Endpoint) -> None:
    original_stage, worker = runtime(tmp_path, endpoint)
    function = AiFunction(
        name="count", request=Request, returns=ConvertedAnswer,
        instruction=original_stage.function.instruction, model=original_stage.function.model,
        workspace=original_stage.function.workspace, timeout_s=20,
    )
    stage = Stage(function=function, mailbox=worker.mailbox)
    worker = replace(worker, registry=Registry(functions={function.name: function}))
    handle = stage.issue(Request(file="data"))
    candidate = '{"input_count":1,"payload":"[1,2]"}'
    endpoint.responses = [tool("write", file_path="$output", content=candidate),
                          tool("submit_output", path="output.json")]
    # Simulate a worker stopping after its successful attempt but before delivery.
    call = stage.call(Request(file="data"))
    claim = worker.mailbox.claim(handle.id, worker.id, worker.policy.lease)
    assert claim is not None
    _, outcome = worker.attempt(call, 0, Corrections(), claim=claim)
    assert outcome is not None
    worker.mailbox.release(claim)
    assert worker.work() == 1
    assert len(endpoint.requests) == 2
    expected = {"total": 2, "payload": [1, 2], "double": 4}
    for _ in range(2):
        value = handle.wait(1).unwrap()
        assert value.count == 2 and value.payload == [1, 2]
        assert value.model_dump(mode="json", by_alias=True) == expected
    assert json.loads(handle.output_path.read_text()) == expected

    replay_mailbox = Mailbox(root=tmp_path / "replay")
    replay_mailbox.issue(call)
    replay = replace(worker, mailbox=replay_mailbox,
                     engine=ReplayEngine(directory=worker.mailbox.root / "events"))
    assert replay.work() == 1
    result = replay_mailbox.returned(handle.id, ConvertedAnswer)
    assert result is not None and result.unwrap().count == 2
    assert json.loads(replay_mailbox.output_path(handle.id).read_text()) == expected


def test_preparation_failure_does_not_stop_worker(tmp_path: Path, endpoint: Endpoint) -> None:
    stage, worker = runtime(tmp_path, endpoint)
    invalid = stage.issue(Request(file="bad"), workspace=Workspace(directory=tmp_path / "missing"))
    valid = stage.issue(Request(file="good"))
    endpoint.responses = [tool("write", file_path="$output", content='{"count":3}'),
                          tool("submit_output", path="output.json")]
    assert worker.work() == 2
    result = invalid.wait(1)
    assert isinstance(result.outcome, Failed) and result.outcome.fault is Fault.SETUP
    assert "missing" in result.outcome.detail and len(result.attempts) == 1
    assert worker.mailbox.state(invalid.id).active is None
    assert valid.wait(1).unwrap().count == 3


def test_sdk_error_is_recorded_and_worker_continues(tmp_path: Path, endpoint: Endpoint) -> None:
    stage, worker = runtime(tmp_path, endpoint)
    invalid_function = replace(stage.function, name="bad", model=Model(
        name="test", provider="does-not-exist",
    ))
    worker.registry.declare(invalid_function)
    invalid = Stage(function=invalid_function, mailbox=worker.mailbox).issue(Request(file="bad"))
    valid = stage.issue(Request(file="good"))
    endpoint.responses = [tool("write", file_path="$output", content='{"count":3}'),
                          tool("submit_output", path="output.json")]
    assert worker.work() == 2
    result = invalid.wait(1)
    assert isinstance(result.outcome, Failed) and result.outcome.fault is Fault.CRASHED
    assert "does-not-exist" in result.outcome.detail
    assert len(result.attempts) == 2 and worker.mailbox.state(invalid.id).active is None
    assert valid.wait(1).unwrap().count == 3
