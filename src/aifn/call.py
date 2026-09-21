from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from aifn.declare import Workspace
from aifn.terms import CallId, FunctionName, JsonObject, SessionId

_STRICT = ConfigDict(extra="forbid", frozen=True)


class Status(StrEnum):
    """Which of the three kinds of conclusion a call ended in.

    Three, because they mean three different things to the caller: `RETURNED` has a
    validated value to read; `REFUSED` has none and asking again will not produce one,
    because the agent judged the input unanswerable; `FAILED` has none either, but the
    reason is in the execution, so another attempt might. Merging the last two leaves the
    caller unable to tell "this input has no answer" from "this never ran".
    """

    RETURNED = "returned"
    REFUSED = "refused"
    FAILED = "failed"


class Fault(StrEnum):
    """Why one attempt produced no value.

    Told apart this precisely because the decision to retry is made entirely from it: a
    `TIMED_OUT` attempt will very likely time out again, while an `INVALID` one is often
    fixed by putting the validation error into the next attempt's prompt. A reason that
    only says "it failed" is not enough for `Policy` to make that call.
    """

    CRASHED = "crashed"
    """The harness process exited or the connection dropped, and what happened on the
    model's side is unknowable from here."""
    TIMED_OUT = "timed_out"
    """The function's declared limit was passed and the session was cut off."""
    NO_ANSWER = "no_answer"
    """The session ended with no accepted submit call in its transcript.

    The agent wrote prose without calling a tool, or read files until the turn ended.
    This is neither a refusal nor a crash: a refusal has to be said through a tool before
    it counts as an answer.
    """
    INVALID = "invalid"
    """The saved output did not validate against the declared model."""
    SETUP = "setup"
    """The workspace or task files could not be prepared."""


class Call(BaseModel):
    """One call waiting to be run: the line written into the outbox.

    `request` is already serialized JSON, with no request model of its own: a worker has
    to read this line before it knows which declaration to look up, so the outbox's format
    cannot depend on a type that is only knowable after the registry has been consulted.
    The type is restored the moment the worker finds the declaration.
    """

    model_config = _STRICT

    id: CallId
    function: FunctionName
    request: JsonObject
    issued_at: AwareDatetime
    sample: int = Field(default=0, ge=0)
    """A different sample creates a new task for otherwise identical inputs."""
    workspace: Workspace | None = None
    instruction: str = ""


class Attempt(BaseModel):
    """One of a call's attempts.

    Every attempt is kept rather than only the last: a function that took three tries to
    pass validation and one that passed first time have the same output type and different
    quality, and that difference is visible only here.
    """

    model_config = _STRICT

    index: int = Field(ge=0)
    session: SessionId
    worker: str
    started_at: datetime
    finished_at: datetime
    fault: Fault | None = None
    detail: str = ""
    """Why this attempt did not work out, copied verbatim.

    It is rendered into the next attempt's prompt, so a summary nobody can read is the
    same as giving up on having the agent correct itself.
    """


class Returned[O: BaseModel](BaseModel):
    """There is a value, and it validated.

    The value's type is the `returns` from the declaration, which makes "returned means
    that type was produced" a fact about types rather than a convention the caller has to
    remember.
    """

    model_config = _STRICT

    status: Literal[Status.RETURNED] = Status.RETURNED
    value: O
    submitted_json: str | None = Field(default=None, repr=False)
    """Original accepted input, used to reconstruct the value without cumulative transforms."""


class Refused(BaseModel):
    """The agent judged this input unanswerable, and said why.

    A conclusion about the input rather than a fault, which makes it final: retrying the
    same input buys the same sentence and one model call's worth of cost.
    """

    model_config = _STRICT

    status: Literal[Status.REFUSED] = Status.REFUSED
    reason: str


class Failed(BaseModel):
    """The attempts ran out, and the last one's fault is the call's conclusion."""

    model_config = _STRICT

    status: Literal[Status.FAILED] = Status.FAILED
    fault: Fault
    detail: str = ""


type Outcome[O: BaseModel] = Annotated[
    Returned[O] | Refused | Failed,
    Field(discriminator="status"),
]
"""One of the three kinds of conclusion.

Written as a union with a discriminator. One status beside three optional fields permits
a line whose status is returned and whose value is absent, and such a line is found only
when whatever reads it crashes.
"""


class Return[O: BaseModel](BaseModel):
    """One call's conclusion: the file that appears in the inbox.

    Carrying every attempt, because this file is the only trace the call leaves: the
    processes exited long ago, and why the conclusion is this one can only be read here.
    """

    model_config = _STRICT

    id: CallId
    function: FunctionName
    outcome: Outcome[O]
    attempts: tuple[Attempt, ...] = ()
    finished_at: datetime

    @property
    def status(self) -> Status:
        """Which kind of conclusion this is, without first asking which branch `outcome` is."""
        return self.outcome.status

    def unwrap(self) -> O:
        """Take the value out, failing immediately when this is not `RETURNED`.

        For the callers that cannot continue from either a refusal or a fault: they were
        going to crash on the next line anyway, and crashing here carries the reason from
        `outcome` with it.
        """
        if isinstance(self.outcome, Returned):
            return self.outcome.value
        if isinstance(self.outcome, Refused):
            raise RuntimeError(f"{self.function} refused: {self.outcome.reason}")
        raise RuntimeError(f"{self.function} failed ({self.outcome.fault}): {self.outcome.detail}")


type AnyOutcome = Outcome[Any]
"""A conclusion whose output type is unknown.

This is what the worker's side handles, holding a `type[BaseModel]` and no type argument.
Written with `Any`, because a generic argument is invariant: a `Returned[Answer]` is not a
`Returned[BaseModel]`, so `Outcome[BaseModel]` is not wider, it is a type that no real
conclusion fits into.
"""

type AnyReturn = Return[Any]
"""A conclusion file whose output type is unknown, for the same reason as `AnyOutcome`."""
