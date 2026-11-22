from __future__ import annotations

import json
import os
import queue
import select
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any

from aifn.engine import EngineCrashed, EngineTimedOut
from aifn.storage import canonical


class AppServer:
    """One private stdio connection, with a deadline covering every protocol wait."""

    def __init__(
        self, command: list[str], directory: Path, env: dict[str, str], deadline: float,
        handle: Callable[[dict[str, Any]], None],
    ) -> None:
        self.deadline = deadline
        self.handle = handle
        self.sequence = 0
        self.messages: queue.SimpleQueue[dict[str, Any] | BaseException | None] = (
            queue.SimpleQueue()
        )
        self.resources = ExitStack()
        self.stderr = self.resources.enter_context(
            tempfile.TemporaryFile(),  # noqa: SIM115 -- close() owns this resource's lifetime.
        )
        try:
            self.process = subprocess.Popen(
                command, cwd=directory, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.stderr, start_new_session=True,
            )
        except OSError as error:
            self.resources.close()
            raise EngineCrashed(f"Cannot start Codex: {error}") from error
        assert self.process.stdin is not None
        os.set_blocking(self.process.stdin.fileno(), False)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            pending = b""
            while chunk := os.read(self.process.stdout.fileno(), 65536):
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("Codex protocol message must be an object")
                    self.messages.put(value)
            if pending:
                raise ValueError("Codex closed an incomplete protocol message")
        except (OSError, ValueError) as error:
            self.messages.put(error)
        finally:
            self.messages.put(None)

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        descriptor = self.process.stdin.fileno()
        pending = memoryview((canonical(message) + "\n").encode())
        try:
            while pending:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise EngineTimedOut("Codex protocol write exceeded its time limit")
                if not select.select([], [descriptor], [], remaining)[1]:
                    raise EngineTimedOut("Codex protocol write exceeded its time limit")
                try:
                    pending = pending[os.write(descriptor, pending):]
                except BlockingIOError:
                    continue
        except EngineTimedOut:
            raise
        except OSError as error:
            raise EngineCrashed("Codex closed its protocol input") from error

    def start_request(self, method: str, params: dict[str, Any]) -> int:
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        return self.sequence

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        expected = self.start_request(method, params)
        while True:
            message = self.receive()
            if message.get("id") == expected and "method" not in message:
                if "error" in message:
                    raise EngineCrashed(f"Codex {method}: {canonical(message['error'])}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise EngineCrashed(f"Codex {method} returned a non-object result")
                return result
            self.handle(message)

    def receive(self) -> dict[str, Any]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EngineTimedOut("Codex session exceeded its time limit")
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty as error:
            raise EngineTimedOut("Codex session exceeded its time limit") from error
        if message is None:
            raise EngineCrashed("Codex exited before the active request completed")
        if isinstance(message, BaseException):
            raise EngineCrashed(f"Invalid Codex protocol stream: {message}") from message
        return message

    def close(self) -> None:
        # Interrupting the turn happens before closing the transport. Killing the process
        # group also reaps helpers on initialization failures and unresponsive shutdowns.
        try:
            try:
                if self.process.stdin is not None:
                    self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
        finally:
            # Helpers may outlive an already exited leader and still own its stdout.
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            if self.process.stdout is not None:
                self.process.stdout.close()
            self.reader.join(timeout=1)
            self.resources.close()
