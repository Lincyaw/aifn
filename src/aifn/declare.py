from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from aifn.terms import CallId, FunctionName


class Access(StrEnum):
    """What the agent may do in its working directory.

    A read-only call cannot break the target, so any number of them run side by side
    without affecting each other; a writable call leaves traces, so it has to be given a
    directory of its own. The permission is therefore not only a safety rule, it decides
    whether one directory can serve two calls at once.
    """

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


@dataclass(frozen=True, slots=True)
class Workspace:
    """Which directory the agent runs in, and what it may do there.

    A prompt decides what the agent was asked, a working directory decides what it can see,
    and the second often sways an answer more than the first, so it belongs to the
    declaration rather than to a worker's configuration.
    """

    directory: Path
    access: Access = Access.READ_ONLY
    private: bool = False
    """Whether each call gets a copy of the directory to itself.

    When writable concurrent calls share one directory, one call reads the half-written
    state another left, and that interference does not show up in the results, it only
    makes the answers impossible to explain.
    """

    def materialize(self, call: CallId, scratch: Path) -> Path:
        """Prepare the directory one call actually uses, and return that path.

        The directory in the declaration is an intention while a worker needs a path that
        exists right now: with `private` false the two are the same, and with it true this
        copies one, which is a cost only a worker can pay.
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Model:
    """Which model answers, and how much one answer may be.

    Swapping the model of an AI function swaps its implementation while its input and
    output types hold, so the model is the one item of a declaration that can be replaced
    on its own.
    """

    name: str
    provider: str = "deepseek-official"
    max_tokens: int = 32_768
    reasoning_effort: str | None = None


class Instruction[I: BaseModel](Protocol):
    """How a request becomes the text the agent reads.

    This is the only place in the library that decides the quality of an answer, so it is
    an object that can be replaced and tested on its own: rendering touches neither the
    network nor the harness, and one request is enough to see what an agent would read.
    """

    def render(self, request: I, rejected: Sequence[str]) -> str:
        """One call's whole prompt, `rejected` being why the earlier attempts were not accepted.

        `rejected` has no default: a session ends and its process exits, so the last
        attempt's validation error reaches the agent only through the next attempt's
        prompt, and a renderer that may ignore this argument is one that never corrects
        itself.
        """
        ...


@dataclass(frozen=True, slots=True)
class AiFunction[I: BaseModel, O: BaseModel]:
    """A function whose signature is like any other and whose body is one agent call.

    It fixes both sides down: the caller sees the two types `request` and `returns`, and
    the worker sees how to render, which directory, which model and which timeout. There is
    nothing else agreed between the two, so the caller does not have to know how the agent
    is asked, and the worker does not have to know who will read the result.
    """

    name: FunctionName
    request: type[I]
    returns: type[O]
    instruction: Instruction[I]
    workspace: Workspace
    model: Model
    timeout_s: float = 1800.0
    """How long one attempt is given.

    Seconds through to hours are all ordinary, which is why the function declares this
    itself: a call that reads dozens of services and one that reads a single file can only
    share a timeout by taking the longer of the two.
    """
    attempts: int = 2
    """How many times one call is tried at most.

    Every attempt is a whole model call and the cost is not negligible, so the number of
    retries is the function's own choice, while which faults are worth retrying is
    `Policy`'s.
    """


@dataclass(frozen=True, slots=True)
class Registry:
    """The map from names to declarations.

    What the outbox holds is a function's name, and a worker has to find the declaration by
    that name before it can render or validate, so a name plus this table shared by both
    sides is the only way an AI function crosses a process boundary.
    """

    functions: dict[FunctionName, AiFunction[Any, Any]]

    def declare[I: BaseModel, O: BaseModel](self, function: AiFunction[I, O]) -> None:
        """Record one declaration, failing immediately on a name already taken.

        Two different declarations under one name means a worker validates one's answer
        against the other's output type, and that mismatch shows up in the results as
        validation failures nobody can account for.
        """
        raise NotImplementedError

    def lookup(self, name: FunctionName) -> AiFunction[Any, Any]:
        """Take a declaration back by name, failing immediately when there is none.

        Both of the returned declaration's arguments are `Any`. A generic argument is
        invariant, so an `AiFunction[Request, Answer]` is not an
        `AiFunction[BaseModel, BaseModel]`, and a table written as the latter is one nothing
        fits into. `Any` says that the type really is unknown here, and this is the
        library's one erasure point: from here a worker works only through the two
        `type[BaseModel]` values `request` and `returns`, while the caller's side has its
        types restored by `compose`.
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Template[I: BaseModel]:
    """An `Instruction` made of one piece of template text.

    It exists so that "rendering is an object" costs nothing in the simplest case: a
    function that only needs a request's fields dropped into a fixed piece of text should
    not have to be given a class.
    """

    body: str
    """A `str.format` template; the request's fields go in by name."""
    rejected_heading: str = "Why the earlier answers were not accepted"

    def render(self, request: I, rejected: Sequence[str]) -> str:
        """Fill the template in, then append why the earlier attempts were not accepted."""
        raise NotImplementedError
