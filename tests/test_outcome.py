from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from aifn.call import Failed, Fault, Outcome, Refused, Return, Returned, Status


class Answer(BaseModel):
    count: int


def test_returned_carries_the_declared_type_and_not_a_dict() -> None:
    """`Return[O]` 校验嵌套的值，所以 `RETURNED` 意味着拿到了 `O` 是类型上的事实。

    泛型模型把参数只当注解、把嵌套的值留成 dict 是一种真实的失败方式，
    而它只会在读那个值的地方崩溃。
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
    """status 为 returned 而没有值的行读不进来。

    这是把三种结果写成带 discriminator 的联合所要买到的那一条性质。
    一个 status 加三个可选字段的写法给不出它。
    """
    with pytest.raises(ValidationError):
        TypeAdapter(Outcome[Answer]).validate_python({"status": "returned"})


def test_each_status_selects_exactly_one_branch() -> None:
    """三种 status 各自只对应一支，所以读结论不需要先判断形状。"""
    adapter: TypeAdapter[Outcome[Answer]] = TypeAdapter(Outcome[Answer])
    assert isinstance(adapter.validate_python({"status": "refused", "reason": "答不了"}), Refused)
    failed = adapter.validate_python({"status": "failed", "fault": "crashed"})
    assert isinstance(failed, Failed)
    assert failed.fault is Fault.CRASHED


def test_refused_is_not_reachable_as_a_fault() -> None:
    """拒答不是一种故障，所以 `Failed` 装不下它。

    两者合成一种，调用方就无法区分「这个输入没有答案」和「这次没跑起来」。
    """
    with pytest.raises(ValidationError):
        Failed.model_validate({"status": Status.FAILED, "fault": "refused"})
