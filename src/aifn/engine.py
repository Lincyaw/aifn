from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aifn.call import Fault
from aifn.declare import Model
from aifn.terms import Event, JsonObject, SessionId


@dataclass(frozen=True, slots=True)
class Session:
    """Everything one session is given.

    Written out separately so that an engine's interface never mentions `AiFunction`: what an
    engine needs to know is which text to run, in which directory, with which tools
    registered, while the input and output types are the caller's side of things.
    """

    id: SessionId
    prompt: str
    directory: Path
    permission: str
    model: Model
    tools: JsonObject
    """The two tools and their schemas for this call, as `submit.bundle` gives them."""
    timeout_s: float
    env: dict[str, str]


@dataclass(frozen=True, slots=True)
class Transcript:
    """What one session leaves behind once it has run.

    Only the transcript: the answer is in it, and whether it is an answer, a refusal or no
    answer at all is read out of that same transcript by `extract`, not decided by the
    engine. This is what lets a recorded session be replayed whole.
    """

    session: SessionId
    events: tuple[Event, ...]


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
        """The one-time work, done before any session runs.

        The bundle is installed by being copied, so an edit takes a reinstall to reach a
        session, and the bundle installed being a different one than this source tree is
        silent: the agent answers through the mechanism it was installed with while this side
        expects another, and the run is spent finding out. This fails immediately.
        """
        ...


def faults_of(error: BaseException) -> Fault:
    """Which `Fault` an exception from an engine corresponds to.

    A function of its own carries this, because which exceptions count as a timeout and which
    as a crash is a judgment that has to be tested, and inside a worker's exception handling
    it cannot be. An exception that cannot be sorted is re-raised here, because a programming
    error sorted as `CRASHED` is hidden by the retry mechanism as an intermittent fault.
    """
    raise NotImplementedError


def describe(events: Sequence[Event]) -> str:
    """A one-line summary of a transcript, written into `Attempt.detail`.

    For the attempts that ended without an answer: what the agent said last is the only thing
    to go on in deciding whether the prompt needs changing or the call needs retrying.
    """
    raise NotImplementedError
