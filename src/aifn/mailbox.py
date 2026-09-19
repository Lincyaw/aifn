from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from aifn.call import AnyReturn, Call, Return
from aifn.terms import CallId, Event, WorkerId

OUTBOX: Final = "outbox"
INBOX: Final = "inbox"
CLAIMS: Final = "claims"
EVENTS: Final = "events"
SCRATCH: Final = "scratch"
DIRECTORIES: Final = (OUTBOX, INBOX, CLAIMS, EVENTS, SCRATCH)


class Claim(BaseModel):
    """One worker's lease on one call.

    It carries an expiry rather than being only a marker: a killed worker has no chance to
    clean its own lease up, and a marker that never expires leaves that call with nobody to
    claim it. Expiring means it can be taken, and that is the only route by which an
    unfinished call returns to the queue after a crash.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    call: CallId
    worker: WorkerId
    claimed_at: datetime
    expires_at: datetime
    attempt: int = 0

    def expired(self, now: datetime) -> bool:
        """Whether this lease can already be taken by somebody else."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Mailbox:
    """A directory, and the only place the caller and the workers meet.

    The state is entirely in files rather than in a process, because a run that goes for hours
    will crash partway: the process that issues a call is not the one that runs it, and the one
    that reads a result is not the one that wrote it. The caller writes the outbox and returns,
    a worker claims, runs and writes the inbox, and there is no other channel between the two.
    """

    root: Path

    def prepare(self) -> None:
        """Create these directories, doing nothing where they already are."""
        raise NotImplementedError

    def issue(self, call: Call) -> bool:
        """Write one call into the outbox; false when it is already there or already concluded.

        A boolean rather than a silent overwrite: reissuing the same call is the ordinary case
        of a rerun, and overwriting a call that is being run has its worker write back a
        conclusion that does not match it.
        """
        raise NotImplementedError

    def pending(self) -> Iterator[Call]:
        """The calls with no conclusion and no live lease, in the order they were issued.

        By the moment they were issued rather than by file name: a file name is a digest of the
        content, its order has nothing to do with the order of issue, and a call issued earlier
        being run earlier is the only queueing behaviour a caller can count on.
        """
        raise NotImplementedError

    def claim(self, call: CallId, worker: WorkerId, lease: timedelta) -> Claim | None:
        """Take one call's lease, or none when it cannot be taken.

        Mutual exclusion is a lease file created with `O_EXCL`, so "one call is never claimed
        by two workers" is guaranteed by the filesystem and does not depend on any process
        being alive. An expired lease is taken over here, and taking over adds one to
        `attempt`: an attempt that was cut off has still been spent.
        """
        raise NotImplementedError

    def renew(self, claim: Claim, lease: timedelta) -> Claim:
        """Extend a lease.

        A lease has to be far shorter than one call's limit, or a killed worker holds its call
        for hours; and a short lease needs renewing while the call runs, or one ordinary long
        call is taken over by another worker that read it as killed.
        """
        raise NotImplementedError

    def release(self, claim: Claim) -> None:
        """Give up a lease without writing a conclusion.

        For when this attempt failed and the call is to be tried again: writing a `FAILED` would
        have the caller believe there is a conclusion already.
        """
        raise NotImplementedError

    def deliver(self, result: AnyReturn, claim: Claim) -> None:
        """Write the conclusion down and give the lease back, as one act.

        The inbox first and the lease second: the other order leaves a gap in which two workers
        both believe this call is theirs to run.
        """
        raise NotImplementedError

    def returned[O: BaseModel](self, call: CallId, returns: type[O]) -> Return[O] | None:
        """Read one call's conclusion, or none when there is not one yet.

        `returns` has to be passed: the inbox holds JSON, and which model it validates as is
        known only to the caller holding the declaration.
        """
        raise NotImplementedError

    def record(self, call: CallId, attempt: int, events: Sequence[Event]) -> Path:
        """Write one attempt's whole transcript down, and say where it went.

        The whole thing verbatim rather than only the extracted answer: why an attempt did not
        come up with one can only be seen in the transcript, and running it again costs the same
        time and the same money.
        """
        raise NotImplementedError

    def events(self, call: CallId, attempt: int) -> Sequence[Event]:
        """Read one attempt's transcript back.

        It exists so `extract` can be tested without a model: a recorded transcript is its only
        input.
        """
        raise NotImplementedError
