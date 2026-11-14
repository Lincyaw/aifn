from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from aifn.call import Fault
from aifn.declare import Model
from aifn.storage import canonical
from aifn.terms import Event, JsonObject, SessionId


@dataclass(frozen=True, slots=True)
class Session:
    """One attempt's input and execution context.

    Written out separately so that an engine's interface never mentions `AiFunction`: what an
    engine needs to know is which text to run, in which directory, with which tools
    registered, while the input and output types are the caller's side of things.
    """

    id: SessionId
    prompt: str
    directory: Path | None
    permission: str
    model: Model
    tools: JsonObject
    """Task directory configuration for the file tools."""
    timeout_s: float
    env: dict[str, str]
    call: str | None = None
    attempt: int = 0
    files: Path | None = None
    request: JsonObject = field(default_factory=dict)
    """Input passed directly to engines that do not use a workspace or file tools."""
    continues: SessionId | None = None
    """The session this attempt continues, where its work follows an earlier call's.

    An engine that cannot reopen a session refuses a session that carries this, and one that can
    uses it instead of opening a new conversation.
    """
    skills: Path | None = None
    """The directory of the skill bundles the agent may load; None offers no skill tool."""


@dataclass(frozen=True, slots=True)
class Transcript:
    """Session events and the submitted result, from a file or a direct API response."""

    session: SessionId
    events: tuple[Event, ...]
    submission: JsonObject | None = None
    archive_receipt: JsonObject | None = None


class EngineCrashed(RuntimeError):
    def __init__(self, detail: str, events: tuple[Event, ...] = ()) -> None:
        super().__init__(detail)
        self.events = events
        self.archive_receipt: JsonObject | None = None


class EngineTimedOut(TimeoutError):
    def __init__(self, detail: str, events: tuple[Event, ...] = ()) -> None:
        super().__init__(detail)
        self.events = events
        self.archive_receipt: JsonObject | None = None


class Engine(Protocol):
    """The layer that runs one session.

    A protocol because a recorded transcript also satisfies it: the whole execution path can
    then be tested without a model, and the judgments on that path about retries, leases and
    validation are the part of this library that has to be right.
    """

    def run(self, session: Session) -> Transcript:
        """Run one session and hand the transcript back.

        A crashed process, a dropped connection and a timeout are raised here rather than
        swallowed here: a worker has to sort them into different `Fault` values, and an
        engine that returns an empty transcript has the worker read a crash as "no answer".
        """
        ...

    def prepare(self) -> None:
        """Check that the engine's runtime dependencies are ready before execution."""
        ...


def faults_of(error: BaseException) -> Fault:
    """Which `Fault` an exception from an engine corresponds to.

    A function of its own carries this, because which exceptions count as a timeout and which
    as a crash is a judgment that has to be tested, and inside a worker's exception handling
    it cannot be. An exception that cannot be sorted is re-raised here, because a programming
    error sorted as `CRASHED` is hidden by the retry mechanism as an intermittent fault.
    """
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return Fault.TIMED_OUT
    if isinstance(error, (EngineCrashed, ConnectionError, subprocess.CalledProcessError)):
        return Fault.CRASHED
    raise error


def describe(events: Sequence[Event]) -> str:
    """A one-line summary of a transcript, written into `Attempt.detail`.

    For the attempts that ended without an answer: what the agent said last is the only thing
    to go on in deciding whether the prompt needs changing or the call needs retrying.
    """
    relevant = [event for event in events if event.get("type") in {
        "assistant/message", "tool/result", "llm/response", "error", "agent/error",
    }]
    return canonical(dict(relevant[-1])) if relevant else "Session ended without an accepted answer"
