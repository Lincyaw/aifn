from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from deepseek_harness import DeepSeekHarness
from deepseek_harness_runtime import resolve_bundled_launch_args
from pydantic import BaseModel

from aifn.declare import Access
from aifn.engines import BUNDLE_DIR, BUNDLE_NAME, PROFILE
from aifn.patch import layer, write
from aifn.submit import bundle

ROUTE = Path(__file__).resolve().parent / "probe-route.patch.yml"
FUNCTION = "list_services"
ANSWER: dict[str, Any] = {"names": ["ts-order-service"]}


class Services(BaseModel):
    names: list[str]


class Endpoint(ThreadingHTTPServer):
    """A model route that records what it was sent and answers with a scripted response.

    A real OpenAI-compatible endpoint rather than a stand-in for the harness: what has to be
    checked is that the bundle registered its tools inside a booted session and that the schema
    which reached the model is the one this side derived, and only the model's own side of the
    wire sees both of those.
    """

    responses: list[list[dict[str, Any]]]
    requests: list[dict[str, Any]]

    def script(self, *responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests = []


def call(index: int, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One tool call in a response, spelled the way a streamed delta spells it."""
    return {
        "index": index,
        "id": f"call-{index}",
        "type": "function",
        "function": {"name": tool, "arguments": json.dumps(arguments)},
    }


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    payload = {
        "id": "probe-0",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "aifn-probe",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        pass

    def do_POST(self) -> None:
        endpoint = self.server
        assert isinstance(endpoint, Endpoint)
        endpoint.requests.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
        calls = endpoint.responses.pop(0) if endpoint.responses else []
        pieces = [_chunk({"role": "assistant", "content": ""})]
        if calls:
            pieces += [_chunk({"tool_calls": calls}), _chunk({}, "tool_calls")]
        else:
            pieces.append(_chunk({"content": "done"}, "stop"))
        pieces.append(b"data: [DONE]\n\n")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        for piece in pieces:
            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


@pytest.fixture
def endpoint() -> Iterator[Endpoint]:
    server = Endpoint(("127.0.0.1", 0), Handler)
    server.script()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    """A dsh home of this test's own, with the bundle installed into it.

    Its own home rather than the developer's: installing rewrites the profile's manifest and
    runs pnpm inside it, and a test doing that to a home another run is using would take that
    run's bundle out from under it.
    """
    home = tmp_path / "dsh"
    home.mkdir()
    environment = {**os.environ, "DSH_HOME": str(home)}
    for arguments in (
        ["--profile", PROFILE, "--dump-default-config"],
        ["plugin", "--profile", PROFILE, "add", f"file:{BUNDLE_DIR}"],
    ):
        done = subprocess.run(
            [*resolve_bundled_launch_args(), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            pytest.fail(f"dsh {' '.join(arguments)}:\n{done.stderr or done.stdout}")
    manifest = json.loads((home / "profiles" / PROFILE / "package.json").read_text())
    assert BUNDLE_NAME in manifest["dependencies"]
    return home


def answered(
    home: Path, endpoint: Endpoint, session: str, workspace: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one session against the recording endpoint, and return what each side saw.

    The tools the model was offered come back beside the transcript, because they are the two
    halves of the same question: the schema the runtime advertised, and what registering it made
    possible.
    """
    patch = write(
        layer(bundle(Services, FUNCTION), Access.READ_ONLY), home / "aifn-patches", session
    )
    with DeepSeekHarness(
        dsh_home=str(home),
        profile=PROFILE,
        patches=(str(patch), str(ROUTE)),
        provider="probe",
        model="aifn-probe",
        cwd=str(workspace),
        env={
            "AIFN_PROBE_BASE_URL": f"http://127.0.0.1:{endpoint.server_address[1]}/v1",
            "AIFN_PROBE_API_KEY": "probe",
            "DSH_PERMISSION_MODE": Access.READ_ONLY,
        },
        initialize_timeout_seconds=120.0,
        request_timeout_seconds=180.0,
    ) as harness:
        result = harness.run("List the services.", session_id=session)
    offered = {tool["function"]["name"]: tool["function"] for tool in endpoint.requests[0]["tools"]}
    return offered, list(result.events)


def test_the_bundle_registers_this_calls_two_tools_with_the_schema_this_side_derived(
    installed: Path, endpoint: Endpoint, tmp_path: Path
) -> None:
    """The bundle loads, and the tools the model is offered are this call's two.

    The one check that covers the whole seam. Each part of it can be right alone and still not
    compose: a layer that parses, a bundle row that loads, and a schema the DSL accepts still
    leave room for a session where the tool was registered under a name nobody submits to, or
    with a schema the runtime rewrote on the way out. The answer to all of that is the roster
    that reached the model, which is visible on the model's side of the wire alone.
    """
    endpoint.script([call(0, f"submit_{FUNCTION}", ANSWER)])
    offered, events = answered(installed, endpoint, "test-registered", tmp_path)

    assert offered.keys() >= {f"submit_{FUNCTION}", f"refuse_{FUNCTION}"}
    assert "bash" not in offered
    assert offered[f"submit_{FUNCTION}"]["parameters"] == {
        "type": "object",
        "properties": {"names": {"type": "array", "items": {"type": "string"}}},
        "required": ["names"],
    }
    assert offered[f"refuse_{FUNCTION}"]["parameters"]["required"] == ["reason"]

    submitted = [
        event
        for event in events
        if event.get("type") == "tool/call" and event["data"]["name"] == f"submit_{FUNCTION}"
    ]
    assert len(submitted) == 1
    assert json.loads(submitted[0]["data"]["arguments"]) == ANSWER


def test_a_second_answer_in_one_response_is_refused_while_the_first_stands(
    installed: Path, endpoint: Endpoint, tmp_path: Path
) -> None:
    """One call answers once: a second submit call in the same response is refused.

    `concludeTurn` ends the turn the accepted call was made in, and the calls already in that
    same response would otherwise still run, each recording an answer. The caller reads the last
    accepted call, so a second one that ran would replace the answer with whatever the model
    sent after it had already finished.
    """
    endpoint.script([call(0, f"submit_{FUNCTION}", ANSWER), call(1, f"submit_{FUNCTION}", ANSWER)])
    _, events = answered(installed, endpoint, "test-twice", tmp_path)

    assert [
        content["isError"]
        for event in events
        if event.get("type") == "tool/result"
        for content in event["data"]["message"]["content"]
    ] == [False, True]


def test_an_answer_the_schema_refuses_leaves_the_call_answerable_in_the_same_turn(
    installed: Path, endpoint: Endpoint, tmp_path: Path
) -> None:
    """An answer of the wrong shape is refused, and the corrected one is accepted.

    This is the property `extract` reads a transcript by: the argument check runs before
    `execute`, so a refused call neither ends the turn nor trips the guard and the model gets to
    answer again within the same turn. Both calls leave a `tool/call` event, which is why
    reading the first one would read a corrected answer as a wrong one.
    """
    endpoint.script(
        [call(0, f"submit_{FUNCTION}", {"names": "ts-order-service"})],
        [call(1, f"submit_{FUNCTION}", ANSWER)],
    )
    _, events = answered(installed, endpoint, "test-corrected", tmp_path)

    results = [
        (event["data"]["message"]["source"]["callId"], content["isError"])
        for event in events
        if event.get("type") == "tool/result"
        for content in event["data"]["message"]["content"]
    ]
    assert results == [("call-0", True), ("call-1", False)]


def test_booting_without_a_call_layer_fails_at_startup(installed: Path, tmp_path: Path) -> None:
    """A session with no layer does not boot, rather than booting with no way to answer.

    The bundle's row is inserted with no config, because a bundle cannot know what the AI
    functions are. Failing here is the whole reason the names are checked in `apply`: a bundle
    that tolerated the missing config would register a nameless tool, the agent would have
    nothing to submit to, and the caller would read a session that produced no answer and retry
    it, which fails the same way every time.
    """
    with pytest.raises(Exception, match="`submit` must be a tool name"), DeepSeekHarness(
        dsh_home=str(installed),
        profile=PROFILE,
        provider="probe",
        model="aifn-probe",
        cwd=str(tmp_path),
        initialize_timeout_seconds=120.0,
    ):
        pass


def test_a_refusal_is_a_tool_call_and_not_a_session_that_said_nothing(
    installed: Path, endpoint: Endpoint, tmp_path: Path
) -> None:
    """A refusal reaches the transcript as an accepted call to the refusal tool.

    A refusal has to be a tool call, because it is an answer: said in prose it leaves a session
    holding no tool call at all, which is what a session that ran off the rails looks like, and
    the caller would then retry an input that has no answer.
    """
    endpoint.script([call(0, f"refuse_{FUNCTION}", {"reason": "the directory holds no services"})])
    _, events = answered(installed, endpoint, "test-refused", tmp_path)

    calls = [event["data"] for event in events if event.get("type") == "tool/call"]
    assert [entry["name"] for entry in calls] == [f"refuse_{FUNCTION}"]
    assert json.loads(calls[0]["arguments"])["reason"] == "the directory holds no services"
    assert all(
        content["isError"] is False
        for event in events
        if event.get("type") == "tool/result"
        for content in event["data"]["message"]["content"]
    )
