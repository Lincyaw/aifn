from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aifn.call import AnyOutcome, AnyReturn, Attempt, Call
from aifn.declare import Registry
from aifn.engine import Engine, Session, Transcript
from aifn.mailbox import Claim, Mailbox
from aifn.policy import Corrections, Policy
from aifn.terms import WorkerId


@dataclass(frozen=True, slots=True)
class Worker:
    """Claims the calls in an outbox, runs them, and writes the inbox.

    How many run at once is decided by the number of workers rather than by the caller: a
    caller can issue a thousand calls, while how many agents can run at once depends on the
    machine and the quota, and those two things should not be decided in one place. One worker
    runs one call at a time, and more of them is more concurrency.
    """

    mailbox: Mailbox
    registry: Registry
    engine: Engine
    policy: Policy
    id: WorkerId

    def work(self, budget: int | None = None) -> int:
        """Claim and run calls until none are left to claim, and return how many were finished.

        `budget` bounds how many this does: a long-lived worker has to be replaceable without
        cutting off the call in its hands, and exiting after an agreed number is the only
        replacement that loses no progress.
        """
        raise NotImplementedError

    def take_one(self) -> AnyReturn | None:
        """Claim one call and carry it to a conclusion, or none when there is nothing to claim.

        A method of its own, because it is the library's one place that both writes the inbox and
        runs a model, and a test has to be able to run it exactly once.
        """
        raise NotImplementedError

    def execute(self, call: Call, claim: Claim) -> AnyOutcome:
        """Carry one call to a conclusion, retrying where retrying is called for.

        The lease is renewed here, because only here is it known that an attempt is still
        running: renewal inside `work` could only happen between calls, and one call is itself
        longer than a lease.
        """
        raise NotImplementedError

    def attempt(
        self, call: Call, index: int, corrections: Corrections
    ) -> tuple[Attempt, AnyOutcome | None]:
        """Run one attempt.

        Two things come back: this attempt's record, and either a conclusion or none. None means
        this attempt reached no conclusion and the call is not settled, leaving `execute` to
        decide whether to try again. `REFUSED` is a conclusion here: a refusal is an answer, not
        a failure.
        """
        raise NotImplementedError

    def session(self, call: Call, index: int, corrections: Corrections, directory: Path) -> Session:
        """One attempt's session input.

        A new session id per attempt: the same id continues the same durable conversation, so
        reusing it has a retry continuing from the answer that was just rejected, while the whole
        point of a retry is to answer again.
        """
        raise NotImplementedError

    def read(self, call: Call, transcript: Transcript) -> AnyOutcome:
        """Read the conclusion out of a transcript, validation into the output type included.

        A pure function: a recorded transcript is enough to test it, and "which tool call counts
        as the accepted answer" is a judgment worth testing.
        """
        raise NotImplementedError
