from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aifn.terms import Event, JsonObject


@dataclass(frozen=True, slots=True)
class Submitted:
    """事件流里那一次被接受的工具调用。

    工具名和参数一起带出来，因为读答案和判断这是回答还是拒答是同一件事：
    两者都是工具调用，区别只在调用了哪个工具。
    """

    tool: str
    arguments: JsonObject


def accepted(events: Sequence[Event], tools: Sequence[str]) -> Submitted | None:
    """事件流里最后一次没有被拒绝的目标工具调用，没有时返回空。

    「丢掉被拒绝的，取剩下的最后一个」这个读法是事件流的性质决定的：
    工具的参数校验在 `execute` 之前跑，被拒绝的那次调用既不结束轮次也不触发守卫，
    模型会在同一轮里重试，而两次调用都会留下 `tool/call` 事件。
    读第一次会把一个改对了的回答读成一个错的；
    读最后一次而不排除被拒绝的，会把一次没答上来读成一个空回答。
    """
    raise NotImplementedError


def refused_call_ids(events: Sequence[Event]) -> frozenset[str]:
    """`tool/result` 是错误的那些 callId：schema 拒绝，或者守卫拦下。

    判据是被拒绝的那次拿到的错误结果，不是被接受的那次拿到的成功结果：
    submit 工具在 `execute` 内部结束轮次，它自己的结果可能出现在
    这次 run 返回的活动区间之后，所以要求看到成功结果
    会把每一个正确的回答都丢掉。
    """
    raise NotImplementedError
