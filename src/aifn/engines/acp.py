from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from aifn.bundle import install_bundle, require_bundle
from aifn.declare import Access
from aifn.engine import EngineCrashed, EngineTimedOut, Session, Transcript
from aifn.patch import Row, layer, write
from aifn.terms import JsonObject

type Block = dict[str, JsonValue]
"""A mutable JSON object, for the values this client builds rather than reads."""

ACP_PROFILE: Final = "acp"
"""Which profile serves the session-aware protocol.

One runtime, two front ends. The `sdk` server speaks three methods and cannot reopen a session;
the `acp` app speaks the session lifecycle, and its `agentCapabilities` advertise
`sessionCapabilities.resume`. Measured: it also lists the sessions the `sdk` profile created,
because the session store lives under the home rather than under a profile.
"""

PROTOCOL_VERSION: Final = 1
"""The ACP version this client speaks, as the runtime's own schema declares it."""

PATCHES: Final = "aifn-patches"
"""Where per-session patch overlays are written, beside the ones the SDK engine writes."""

PERMISSION_KINDS: Final = ("allow_always", "allow_once")
"""The option kinds this client prefers, so a resumed session never stops to ask a person."""


@dataclass(frozen=True, slots=True)
class AcpEngine:
    """Continue an earlier session through the runtime's ACP front end.

    Resuming is the whole job: the SDK engine owns opening a conversation, because the worker
    has already chosen the id a new session must carry, and ACP's `session/new` hands back an id
    of the agent's choosing. A session that continues nothing is therefore refused here.
    """

    dsh_home: Path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 1800.0
    """One resumed turn's limit, alongside the call-level limit the worker already enforces."""

    patches: tuple[Path, ...] = ()

    def run(self, session: Session) -> Transcript:
        if session.continues is None:
            raise ValueError("AcpEngine continues a session; this attempt continues none")
        if session.directory is None or session.files is None:
            raise EngineCrashed("AcpEngine needs a workspace and a task directory")
        self.prepare()
        permission = Access(session.permission)
        patch = write(
            [*layer(dict(session.tools), permission.value), *self._model(session)],
            self.dsh_home / PATCHES,
            session.id,
        )
        environment = {
            **self.env,
            **session.env,
            "DSH_PERMISSION_MODE": permission.value,
            "AIFN_TASK_DIRECTORY": str(session.files.resolve()),
        }
        client = _Client(
            [
                *_launch(), "--profile", ACP_PROFILE,
                *(part for path in self.patches for part in ("--patch", str(path.resolve()))),
                "--patch", str(patch),
            ],
            cwd=str(session.directory),
            env={**os.environ, **environment, "DSH_HOME": str(self.dsh_home.resolve())},
            timeout_s=self.timeout_s,
        )
        try:
            client.call(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": True},
                        "terminal": True,
                    },
                },
            )
            resumed = client.call(
                "session/resume",
                {
                    "sessionId": session.continues,
                    "cwd": str(session.directory),
                    "mcpServers": [],
                },
            )
            self._configure(client, session, resumed)
            client.call(
                "session/prompt",
                {
                    "sessionId": session.continues,
                    "prompt": [{"type": "text", "text": session.prompt}],
                },
            )
        except TimeoutError as error:
            raise EngineTimedOut(str(error), tuple(client.events)) from error
        except (OSError, RuntimeError) as error:
            raise EngineCrashed(str(error), tuple(client.events)) from error
        finally:
            client.close()
        return Transcript(session=session.continues, events=tuple(client.events))

    def _configure(self, client: _Client, session: Session, state: JsonObject) -> None:
        """Apply this call's model and reasoning settings to the resumed session."""
        options = {str(option["id"]): option for option in _objects(state.get("configOptions"))}
        selected = json.dumps([session.model.provider, session.model.name], separators=(",", ":"))
        model = options.get("model")
        if model is None:
            raise RuntimeError("Resumed session did not advertise model configuration")
        if model.get("currentValue") != selected:
            state = client.call(
                "session/set_config_option",
                {"sessionId": session.continues, "configId": "model", "value": selected},
            )
            options = {
                str(option["id"]): option for option in _objects(state.get("configOptions"))
            }
        effort = session.model.reasoning_effort
        if effort is not None:
            reasoning = options.get("reasoning_effort")
            if reasoning is None:
                raise RuntimeError("Resumed model did not advertise reasoning configuration")
            if reasoning.get("currentValue") != effort:
                client.call(
                    "session/set_config_option",
                    {
                        "sessionId": session.continues,
                        "configId": "reasoning_effort",
                        "value": effort,
                    },
                )

    def _model(self, session: Session) -> list[Row]:
        """Rows that point the ACP app at this call's model and output bound.

        The app ships its own defaults, and they are not the ones the endpoint accepts: measured,
        its default model id is rejected by the gateway and its default output bound is above the
        model's maximum. The SDK engine never meets them because it passes both at initialize.
        """
        selection: Block = {"provider": session.model.provider, "model": session.model.name}
        return [
            {"id": ACP_PROFILE, "config": selection},
            {"id": "agent-default-model", "config": selection},
            {"id": "llm-deepseek", "config": {"maxTokens": session.model.max_tokens}},
        ]

    def prepare(self) -> None:
        """Check that the ACP profile has the bundle, installing it when it does not."""
        install_bundle(self.dsh_home, profile=ACP_PROFILE)
        require_bundle(self.dsh_home, profile=ACP_PROFILE)


def _launch() -> tuple[str, ...]:
    """The runtime's own launcher, which is the binary this wheel ships."""
    from deepseek_harness_runtime import resolve_bundled_launch_args

    return tuple(str(one) for one in resolve_bundled_launch_args())


class _Client:
    """One ACP conversation over the runtime's stdio, until its prompt returns.

    NDJSON either way: requests out, responses and notifications in, and the agent's own
    requests answered as they arrive. A tool approval is granted with the widest option offered,
    because the workspace and the sandbox mode already say what this call may do.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> None:
        self.timeout_s = timeout_s
        self.events: list[Block] = []
        self.text: list[str] = []
        self._next = 0
        self._pending: dict[int, Block] = {}
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._errors: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    def call(self, method: str, params: Block) -> JsonObject:
        with self._lock:
            self._next += 1
            identifier = self._next
            self._pending[identifier] = {}
        self._send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                answer = self._pending.get(identifier, {})
            if "error" in answer:
                raise RuntimeError(f"{method}: {answer['error']}")
            if "result" in answer:
                return _object(answer["result"])
            if self._process.poll() is not None:
                raise RuntimeError(f"{method}: runtime exited with {self._process.returncode}")
            time.sleep(0.05)
        raise TimeoutError(f"{method} did not answer within {self.timeout_s:.0f}s")

    def close(self) -> None:
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
            self._process.wait(timeout=10)
        except Exception:
            self._process.kill()

    def _send(self, message: JsonObject) -> None:
        if self._process.stdin is None:
            raise RuntimeError("runtime has no stdin")
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def _read(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "method" in message and "id" in message:
                self._answer(message)
            elif "id" in message:
                with self._lock:
                    slot = self._pending.setdefault(message["id"], {})
                    for key in ("error", "result"):
                        if key in message:
                            slot[key] = message[key]
            else:
                self._notify(message)

    def _answer(self, message: JsonObject) -> None:
        method = str(message.get("method"))
        result: Block = {}
        if method == "session/request_permission":
            params = _object(message.get("params"))
            options = _objects(params.get("options"))
            picked = next(
                (one for kind in PERMISSION_KINDS for one in options if one.get("kind") == kind),
                options[0] if options else None,
            )
            result = (
                {"outcome": {"outcome": "selected", "optionId": picked["optionId"]}}
                if (picked)
                else {"outcome": {"outcome": "cancelled"}}
            )
        self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _notify(self, message: JsonObject) -> None:
        if message.get("method") != "session/update":
            return
        update = _object(_object(message.get("params")).get("update"))
        self.events.append(update)
        if update.get("sessionUpdate") == "agent_message_chunk":
            text = _object(update.get("content")).get("text")
            if text:
                self.text.append(str(text))

    def _drain(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._errors.append(line.rstrip())


def _object(value: object) -> Block:
    """A JSON object from a value the wire only promises is JSON."""
    return dict(value) if isinstance(value, dict) else {}


def _objects(value: object) -> list[Block]:
    """The JSON objects among a value the wire only promises is JSON."""
    return [dict(one) for one in value if isinstance(one, dict)] if isinstance(value, list) else []
