from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aifn.terms import Event, JsonObject


@dataclass(frozen=True, slots=True)
class Submitted:
    """The one tool call in a transcript that was accepted.

    The tool's name comes out alongside the arguments, because reading the answer and
    deciding whether it is an answer or a refusal are the same act: both are tool calls, and
    the only difference is which tool was called.
    """

    tool: str
    arguments: JsonObject


def accepted(events: Sequence[Event], tools: Sequence[str]) -> Submitted | None:
    """The last call to one of these tools that was not rejected, or none.

    "Drop the rejected ones and take the last of what is left" follows from what a
    transcript is: a tool's argument check runs before `execute`, so the rejected call
    neither ends the turn nor trips the guard, the model retries within the same turn, and
    both calls leave a `tool/call` event behind. Reading the first turns a corrected answer
    into a wrong one; reading the last without excluding the rejected turns a failure to
    answer into an empty answer.
    """
    raise NotImplementedError


def refused_call_ids(events: Sequence[Event]) -> frozenset[str]:
    """The callIds whose `tool/result` is an error: refused by the schema, or held by a guard.

    The test is the error result the rejected call received, not the success result the
    accepted one received: a submit tool ends the turn inside `execute`, so its own result
    can arrive after the window this run returns, and requiring a success would drop every
    correct answer there is.
    """
    raise NotImplementedError
