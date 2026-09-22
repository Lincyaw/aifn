from __future__ import annotations

import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from filelock import FileLock
from pydantic import BaseModel

from aifn.storage import component
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
    """Execution directory and permissions, configured per function or per call."""

    directory: Path
    access: Access = Access.READ_ONLY
    private: bool = False
    """Whether each call gets a copy of the directory to itself.

    When writable concurrent calls share one directory, one call reads the half-written
    state another left, and that interference does not show up in the results, it only
    makes the answers impossible to explain.
    """

    create: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "directory", self.directory.expanduser().resolve())
        object.__setattr__(self, "access", Access(self.access))
        if self.create and self.private:
            raise ValueError("create and private cannot both be enabled")

    def materialize(self, call: CallId, scratch: Path) -> Path:
        """Prepare the directory one call actually uses, and return that path.

        The directory in the declaration is an intention while a worker needs a path that
        exists right now: with `private` false the two are the same, and with it true this
        copies one, which is a cost only a worker can pay.
        """
        if self.create:
            self.directory.mkdir(parents=True, exist_ok=True)
        if not self.directory.is_dir():
            raise NotADirectoryError(self.directory)
        if not self.private:
            return self.directory
        scratch = scratch.resolve()
        if scratch.is_relative_to(self.directory):
            raise ValueError("Private workspace scratch must be outside the source directory")
        scratch.mkdir(parents=True, exist_ok=True)
        destination = scratch / component(call)
        with FileLock(str(scratch / f".{call}.lock")):
            if destination.is_dir():
                return destination
            temporary = Path(tempfile.mkdtemp(prefix=f".{call}-", dir=scratch))
            try:
                shutil.copytree(self.directory, temporary, dirs_exist_ok=True, symlinks=False)
                temporary.rename(destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        return destination


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

    def __post_init__(self) -> None:
        if not self.name or not self.provider or self.max_tokens <= 0:
            raise ValueError("Model name, provider and positive max_tokens are required")


@dataclass(frozen=True, slots=True)
class AiFunction[I: BaseModel, O: BaseModel]:
    """A typed AI call executed by a worker's engine.

    It fixes both sides down: the caller sees the two types `request` and `returns`, and
    the worker sees how to render, which directory, which model and which timeout. There is
    nothing else agreed between the two, so the caller does not have to know how the agent
    is asked, and the worker does not have to know who will read the result.
    """

    name: FunctionName
    request: type[I]
    returns: type[O]
    model: Model
    instruction: Path | None = None
    workspace: Workspace | None = None
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

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,57}", self.name):
            raise ValueError("Function name must match [A-Za-z0-9_-]{1,57}")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0 or self.attempts < 1:
            raise ValueError("timeout_s and attempts must be positive")

    def instructions(self) -> str:
        """Read the Markdown instructions when the caller submits the task."""
        return self.instruction.read_text(encoding="utf-8") if self.instruction is not None else ""


@dataclass(frozen=True, slots=True)
class Registry:
    """The map from names to declarations.

    What a request holds is a function's name, and a worker has to find the declaration by
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
        if function.name in self.functions:
            raise ValueError(f"Function already declared: {function.name}")
        self.functions[function.name] = function

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
        return self.functions[name]
