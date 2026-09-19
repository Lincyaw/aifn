from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from aifn.call import Fault

TRANSIENT: frozenset[Fault] = frozenset({Fault.CRASHED, Fault.NO_ANSWER, Fault.INVALID})
"""The faults a retry still means something for.

`INVALID` is in it: a validation error can be written into the next prompt, and an agent
reading "the field you gave last time does not meet this requirement" and then getting it
right is this mechanism's most common use. `TIMED_OUT` is not: the same prompt under the same
limit will very likely time out again, and this timeout has already spent that long.
"""


@dataclass(frozen=True, slots=True)
class Policy:
    """What happens after one attempt fails.

    An object of its own, because whether a validation failure is worth asking again is the
    caller's judgment: a function whose calls take dozens of minutes and one whose calls take
    seconds have different answers to that on their face.
    """

    attempts: int = 2
    retry_on: frozenset[Fault] = TRANSIENT
    backoff: timedelta = timedelta(seconds=30)
    """How long to wait between two attempts.

    This exists because rate limits and dropped streams on the model's side arrive in patches:
    retrying at once spends an attempt and buys the same fault.
    """
    lease: timedelta = timedelta(minutes=5)
    """How long a lease runs.

    Shorter than any call's own limit, with the worker renewing it while it runs. A lease as
    long as a call's limit has a killed worker holding its call for hours; a lease that is
    never renewed has one ordinary long call taken over by another worker that read it as
    killed.
    """

    def again(self, faults: Sequence[Fault]) -> bool:
        """Whether to try once more, given the faults that have already happened.

        The whole sequence rather than only the last: three `INVALID` attempts in a row say
        something is wrong with this function's output type or its prompt, and trying again
        under those conditions produces the same thing.
        """
        raise NotImplementedError

    def wait_s(self, attempt: int) -> float:
        """How many seconds to wait before a given attempt."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Corrections:
    """Why the earlier attempts were not accepted, in order.

    It exists because it is the agent's only channel for correcting itself: a session ends and
    its process exits, so the last attempt's validation error can only arrive through the next
    attempt's prompt.
    """

    reasons: tuple[str, ...] = ()

    def after(self, fault: Fault, detail: str) -> Corrections:
        """Add one and return a new one.

        `CRASHED` and `TIMED_OUT` are dropped here: they are not about the content of an
        answer, and telling the agent that a process died last time only spends its context.
        """
        raise NotImplementedError
