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
    """一次已经发起的调用，加上读它结论所需要的类型。

    调用方拿到的是这个句柄，此刻还没有值：写 outbox 就返回，
    结论几分钟或者几小时之后才出现，而 `id` 此刻就是确定的。
    它可以被写下来，下一个进程拿着它照样能读到结论。
    """

    id: CallId
    returns: type[O]
    mailbox: Mailbox

    def poll(self) -> Return[O] | None:
        """结论出来了没有，没有时返回空并且立即返回。

        不阻塞，因为调用方发起了上千次调用，
        在任何一次上阻塞都会让其余的结论无人处理。
        """
        raise NotImplementedError

    def wait(self, timeout_s: float | None = None) -> Return[O]:
        """等到有结论，或者等到超时时立即失败。

        给那些只发起一次调用的程序用。
        发起了一批的程序应当用 `Batch.settled`：
        一批里最慢的一个决定全部的等待时间，而按完成顺序处理不会。
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Stage[I: BaseModel, O: BaseModel]:
    """一个 AI 函数，绑定到一个 mailbox 上。

    把声明和 mailbox 绑在一起，是为了让发起一次调用不必每次都提两样东西；
    而声明本身不知道 mailbox，所以同一个函数可以在几个 mailbox 里各跑一遍。
    """

    function: AiFunction[I, O]
    mailbox: Mailbox

    def issue(self, request: I, sample: int = 0) -> Handle[O]:
        """发起一次调用，立即返回它的句柄。

        重复发起同一个请求得到同一个句柄而不会多跑一次：
        `id` 是请求内容的摘要，而 outbox 里已经有那一行时 `issue` 什么也不做。
        重跑一段中途崩溃的程序因此就是再运行一次它，不需要记住上次跑到哪。
        """
        raise NotImplementedError

    def issue_all(self, requests: Iterable[I]) -> Batch[O]:
        """一次发起一批，立即返回。

        并发度不在这里：这里只是往 outbox 写几行，几行都很快，
        而同时跑几个 agent 由 worker 的数量决定。
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Batch[O: BaseModel]:
    """一批已经发起的调用。

    存在的理由是 `settled`：一批调用的结论出现的顺序和发起顺序无关，
    而按发起顺序去读，会在第一个慢的上面把后面已经好了的全都堵住。
    """

    handles: tuple[Handle[O], ...]

    def settled(self, poll_every: timedelta = timedelta(seconds=5)) -> Iterator[Return[O]]:
        """结论按出现的顺序产出，每个只产出一次，全部出来之后结束。

        用轮询实现：状态在文件里，写 inbox 的进程和读它的进程之间
        没有别的通道，而一个几小时的流程里每隔几秒看一次目录的代价可以忽略。
        """
        raise NotImplementedError

    def returned(self) -> Iterator[Return[O]]:
        """此刻已经有结论的那些，不等待。

        给那些自己掌握循环的调用方用：
        一个同时推进几个阶段的程序不能在任何一个阶段里等。
        """
        raise NotImplementedError

    def pending(self) -> tuple[Handle[O], ...]:
        """还没有结论的那些。"""
        raise NotImplementedError

    def values(self, poll_every: timedelta = timedelta(seconds=5)) -> Iterator[O]:
        """只要校验通过的值，拒答和故障跳过。

        跳过它们，整批继续：一批调用里有几个答不了是正常的，
        而让一次拒答中止整批会把其余几百个已经付过费的结论一起丢掉。
        哪些被跳过了从 inbox 里读得到。
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Link[A: BaseModel, B: BaseModel]:
    """上一个阶段的一个结论，怎么变成下一个阶段的请求。

    一个结论可以变成零个、一个或者几个请求，所以是列表：
    一次 scan 出四十个服务，下一个阶段就是四十次调用；
    而一个拒答变成零个请求，流水线在那里自然收窄。
    """

    adapt: Callable[[A], Sequence[B]]
    on: frozenset[Status] = frozenset({Status.RETURNED})
    """哪些结论往下走。

    默认只有 `RETURNED`：把一个没有值的结论往下传，
    下一个阶段就要为「上游没答上来」准备一个分支，而那不是它的问题。
    """


@dataclass(frozen=True, slots=True)
class Flow[A: BaseModel, B: BaseModel, C: BaseModel]:
    """两个阶段接起来，上游出一个结论下游就能开始。

    不等上游全部完成：一个阶段几十次调用，每次几十分钟，
    等它全部结束再开始下一个阶段，整个流程的时长就是各阶段时长之和，
    而按结论推进时它接近其中最长的那一个。

    推进的方式是每次重新读一遍 mailbox，不在进程里记住已经处理过谁。
    `Stage.issue` 对已经发起过的调用什么也不做，
    所以「把上游每一个结论都发给下游」这件事重复执行是安全的，
    而这正是崩溃之后接着跑所需要的性质。
    """

    upstream: Batch[A]
    downstream: Stage[B, C]
    link: Link[A, B]

    def advance(self) -> int:
        """读一遍上游的结论，把该发起的下游调用发起，返回新发起了几个。

        不阻塞也不循环：一个同时推进几条流水线的程序要自己掌握循环，
        而一个只有一条的程序就是反复调用它直到 `done` 为真。
        """
        raise NotImplementedError

    def done(self) -> bool:
        """上游全部有了结论，并且它们该发起的下游调用都已经发起。"""
        raise NotImplementedError

    def issued(self) -> Batch[C]:
        """到此刻为止下游已经发起的全部调用。

        每次重新从上游的结论算出来，不在这个对象里累积：
        进程会崩溃，而累积在进程里的那份记录崩溃之后就没有了。
        """
        raise NotImplementedError


class Advancing(Protocol):
    """能被反复推进直到完成的东西。

    写成协议而没有直接收 `Flow`，因为一条流水线上各段的类型两两不同，
    而 `drain` 只需要推进和问一句完成了没有，不需要知道那些类型。
    要求它知道，就只能把类型参数填成 `BaseModel`，那是一句谎话。
    """

    def advance(self) -> int: ...

    def done(self) -> bool: ...


def drain(flows: Sequence[Advancing], poll_every: timedelta) -> None:
    """反复推进这些流水线，直到全部完成。

    给最常见的那种调用方用：它只要整条流水线跑完的结果，
    自己不需要在中途做别的事。
    """
    raise NotImplementedError
