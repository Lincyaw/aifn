from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from aifn.call import Failed, Fault, Outcome, Refused, Return, Returned, Status


class Answer(BaseModel):
    count: int


def test_returned_carries_the_declared_type_and_not_a_dict() -> None:
    """`Return[O]` validates the nested value, so `RETURNED` meaning `O` is a fact about types.

    A generic model treating its argument as an annotation only and leaving the nested value a
    dict is a real way for this to fail, and it fails only where that value is read.
    """
    parsed = Return[Answer].model_validate(
        {
            "id": "abc",
            "function": "count_things",
            "outcome": {"status": "returned", "value": {"count": 3}},
            "finished_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    assert isinstance(parsed.outcome, Returned)
    assert isinstance(parsed.outcome.value, Answer)


def test_returned_without_a_value_is_refused_by_the_union() -> None:
    """A line whose status is returned and which has no value does not read back in.

    This is the one property that writing the three outcomes as a union with a discriminator
    buys. One status beside three optional fields does not give it.
    """
    with pytest.raises(ValidationError):
        TypeAdapter(Outcome[Answer]).validate_python({"status": "returned"})


def test_each_status_selects_exactly_one_branch() -> None:
    """Each status selects exactly one branch, so reading a conclusion asks no shape first."""
    adapter: TypeAdapter[Outcome[Answer]] = TypeAdapter(Outcome[Answer])
    refused = adapter.validate_python({"status": "refused", "reason": "there is nothing to count"})
    assert isinstance(refused, Refused)
    failed = adapter.validate_python({"status": "failed", "fault": "crashed"})
    assert isinstance(failed, Failed)
    assert failed.fault is Fault.CRASHED


def test_refused_is_not_reachable_as_a_fault() -> None:
    """A refusal is not a fault, so `Failed` does not hold one.

    Merge the two and the caller cannot tell "this input has no answer" from "this never ran".
    """
    with pytest.raises(ValidationError):
        Failed.model_validate({"status": Status.FAILED, "fault": "refused"})
