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
from pydantic import BaseModel, Field

from aifn import AiFunction, Call, Fault, Mailbox, Model, Policy, Registry, Stage, Worker, Workspace
from aifn.engines import DeepSeekEngine, ReplayEngine, install_bundle

ROUTE = Path(__file__).with_name("probe-route.patch.yml")


class Endpoint(ThreadingHTTPServer):
    responses: list[list[dict[str, Any]]]
    requests: list[dict[str, Any]]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        server = self.server
        assert isinstance(server, Endpoint)
        server.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        calls = server.responses.pop(0) if server.responses else []
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
    worker = Worker(mailbox=mailbox, registry=Registry(functions={"count": function}),
                    engine=DeepSeekEngine(dsh_home=home, patches=(ROUTE,), env={
                        "AIFN_PROBE_BASE_URL": f"http://127.0.0.1:{endpoint.server_address[1]}/v1",
                        "AIFN_PROBE_API_KEY": "probe",
                    }), policy=Policy(backoff=timedelta(0)), id="worker")
    return Stage(function=function, mailbox=mailbox), worker


def test_harness_validates_corrects_and_submits_in_one_session(
    tmp_path: Path, endpoint: Endpoint,
) -> None:
    stage, worker = runtime(tmp_path, endpoint)
    source = tmp_path / "request.json"
    source.write_text('{"file":"data.csv"}')
    handle = stage.issue_file(source)
    endpoint.responses = [
        tool("write_output", path="output.json", content='{"count":0}'),
        tool("validate_output", path="output.json"),
        tool("write_output", path="output.json", content='{"count":3}'),
        tool("submit_output", path="output.json"),
    ]
    assert worker.work() == 1
    result = handle.wait(1)
    assert result.unwrap().count == 3
    assert len(result.attempts) == 1
    assert json.loads(handle.output_path.read_text()) == {"count": 3}
    assert "greater_than_equal" in json.dumps(endpoint.requests[2])
    offered = {item["function"]["name"] for item in endpoint.requests[0]["tools"]}
    assert {"write_output", "validate_output", "submit_output", "refuse_output"} <= offered
    assert "bash" not in offered
    task = stage.mailbox.task_path(handle.id)
    attempt = next((task / "attempts").iterdir())
    assert json.loads((attempt / "input.json").read_text()) == {"file": "data.csv"}
    assert "minimum" in (attempt / "output.schema.json").read_text()
    assert stage.mailbox.enqueue(stage.call(Request(file="data.csv")))[1] is False
    replay_mail = Mailbox(root=tmp_path / "replay")
    original = Call.model_validate_json(
        (stage.mailbox.root / "outbox" / f"{handle.id}.json").read_bytes()
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
        [], tool("write_output", path="refusal.json", content='{"reason":"Missing file"}'),
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
