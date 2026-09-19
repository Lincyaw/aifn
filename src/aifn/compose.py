from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from pydantic import BaseModel

from aifn.call import Return, Status
from aifn.declare import AiFunction
from aifn.mailbox import Mailbox
from aifn.terms import CallId


@dataclass(frozen=True, slots=True)
class Handle[O: BaseModel]:
    """One call already issued, plus the type its conclusion needs to be read with.

    This handle is what the caller gets, and it holds no value yet: the outbox is written and
    the call returns, the conclusion appears minutes or hours later, and `id` is settled right
    now. It can be written down, and the next process holding it reads the conclusion just the
    same.
    """

    id: CallId
    returns: type[O]
    mailbox: Mailbox

    def poll(self) -> Return[O] | None:
        """Whether the conclusion is out, returning none and returning at once when it is not.

        It does not block, because a caller has issued a thousand calls and blocking on any one
        of them leaves the other conclusions with nobody to handle them.
        """
        raise NotImplementedError

    def wait(self, timeout_s: float | None = None) -> Return[O]:
        """Wait for a conclusion, failing immediately on timing out.

        For programs that issue a single call. A program that issued a batch should use
        `Batch.settled`: the slowest of a batch decides how long all of it is waited on, and
        handling them as they finish does not.
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Stage[I: BaseModel, O: BaseModel]:
    """One AI function, bound to one mailbox.

    Binding a declaration to a mailbox is what keeps issuing a call from naming two things every
    time; and a declaration itself knows no mailbox, so one function can run in several mailboxes
    at once.
    """

    function: AiFunction[I, O]
    mailbox: Mailbox

    def issue(self, request: I, sample: int = 0) -> Handle[O]:
        """Issue one call and return its handle at once.

        Issuing the same request twice gets the same handle and does not run it twice: `id` is a
        digest of the request, and `issue` does nothing when the outbox already holds that line.
        Rerunning a program that crashed partway is therefore just running it again, with nothing
        to remember about where it got to.
        """
        raise NotImplementedError

    def issue_all(self, requests: Iterable[I]) -> Batch[O]:
        """Issue a batch at once and return immediately.

        Concurrency is not here: this only writes a few lines into an outbox, which is quick
        however many there are, and how many agents run at once is decided by the number of
        workers.
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Batch[O: BaseModel]:
    """A batch of calls already issued.

    It exists for `settled`: the order in which a batch's conclusions appear has nothing to do
    with the order they were issued in, and reading them in the order of issue has the first slow
    one hold up every one behind it that is already done.
    """

    handles: tuple[Handle[O], ...]

    def settled(self, poll_every: timedelta = timedelta(seconds=5)) -> Iterator[Return[O]]:
        """Yield conclusions as they appear, each once, ending when all of them are out.

        Implemented by polling: the state is in files, the process writing an inbox and the one
        reading it share no other channel, and looking at a directory every few seconds over a run
        of hours costs nothing worth counting.
        """
        raise NotImplementedError

    def returned(self) -> Iterator[Return[O]]:
        """The ones that have a conclusion right now, without waiting.

        For the callers that run their own loop: a program advancing several stages at once cannot
        wait inside any one of them.
        """
        raise NotImplementedError

    def pending(self) -> tuple[Handle[O], ...]:
        """The ones with no conclusion yet."""
        raise NotImplementedError

    def values(self, poll_every: timedelta = timedelta(seconds=5)) -> Iterator[O]:
        """Only the values that validated, skipping refusals and faults.

        Skipping them and carrying on with the batch: a few calls in a batch being unanswerable is
        ordinary, and letting one refusal end the batch throws away the several hundred other
        conclusions that have already been paid for. Which ones were skipped can be read from the
        inbox.
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Link[A: BaseModel, B: BaseModel]:
    """How one of the previous stage's conclusions becomes the next stage's requests.

    One conclusion can become zero, one or several requests, hence a list: one scan turning up
    forty services makes forty calls in the next stage, while a refusal becomes zero requests and
    the pipeline narrows there of its own accord.
    """

    adapt: Callable[[A], Sequence[B]]
    on: frozenset[Status] = frozenset({Status.RETURNED})
    """Which conclusions go downstream.

    `RETURNED` alone by default: pass a conclusion with no value on, and the next stage has to have
    a branch for "upstream did not come up with one", which is not its problem.
    """


@dataclass(frozen=True, slots=True)
class Flow[A: BaseModel, B: BaseModel, C: BaseModel]:
    """Two stages joined up, the downstream one starting on each upstream conclusion.

    It does not wait for all of upstream: with dozens of calls in a stage each taking dozens of
    minutes, waiting for a stage to end before the next begins makes the whole run as long as the
    sum of its stages, while advancing per conclusion makes it close to the longest one.

    Advancing means reading the mailbox again each time, with nothing remembered in the process
    about who has been handled. `Stage.issue` does nothing for a call already issued, so "issue
    every upstream conclusion downstream" is safe to run repeatedly, and that is exactly the
    property resuming after a crash needs.
    """

    upstream: Batch[A]
    downstream: Stage[B, C]
    link: Link[A, B]

    def advance(self) -> int:
        """Read upstream's conclusions once, issue what is due downstream, return how many are new.

        It neither blocks nor loops: a program advancing several pipelines has to run its own loop,
        and a program with one pipeline calls this until `done` is true.
        """
        raise NotImplementedError

    def done(self) -> bool:
        """All of upstream has a conclusion, and every downstream call they call for is issued."""
        raise NotImplementedError

    def issued(self) -> Batch[C]:
        """Every downstream call issued up to this moment.

        Computed again from upstream's conclusions each time rather than accumulated in this
        object: a process crashes, and a record accumulated inside one is gone when it does.
        """
        raise NotImplementedError


class Advancing(Protocol):
    """Something that can be advanced repeatedly until it is done.

    A protocol rather than taking a `Flow` directly, because the stages of one pipeline have
    pairwise different types, while `drain` needs only to advance and to ask whether it is done. To
    require that it know those types is to fill the arguments in as `BaseModel`, which is a lie.
    """

    def advance(self) -> int: ...

    def done(self) -> bool: ...


def drain(flows: Sequence[Advancing], poll_every: timedelta) -> None:
    """Advance these pipelines repeatedly until all of them are done.

    For the most common caller: it wants the result of a whole pipeline and has nothing else to do
    in the meantime.
    """
    raise NotImplementedError
