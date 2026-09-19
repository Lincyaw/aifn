from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from aifn.call import Fault

TRANSIENT: frozenset[Fault] = frozenset({Fault.CRASHED, Fault.NO_ANSWER, Fault.INVALID})
"""重试还有意义的那些故障。

`INVALID` 在里面：校验错误可以写进下一次的 prompt，而 agent 读到
「你上次给的这个字段不符合要求」之后改对，是这个机制最常见的用法。
`TIMED_OUT` 不在里面：同一段 prompt 在同一个时限下再跑一次
很可能同样超时，而这次超时已经花掉了那么长时间。
"""


@dataclass(frozen=True, slots=True)
class Policy:
    """一次尝试失败之后做什么。

    单独一个对象，因为「校验失败算不算值得再问一次」是调用方的判断：
    一个每次调用要几十分钟的函数和一个几秒钟的函数，
    在这件事上的答案本来就不同。
    """

    attempts: int = 2
    retry_on: frozenset[Fault] = TRANSIENT
    backoff: timedelta = timedelta(seconds=30)
    """两次尝试之间等多久。

    有这一项是因为模型侧的限流和断流都是成片出现的：
    立刻重试会用掉一次尝试却得到同一个故障。
    """
    lease: timedelta = timedelta(minutes=5)
    """租约多长。

    比任何一次调用的时限都短，而 worker 执行期间续租。
    租约取得和调用时限一样长时，一个被中止的 worker 会把它的调用扣住几小时；
    而没有续租时，一次正常的长调用会被另一个 worker 当成中止的抢走。
    """

    def again(self, faults: Sequence[Fault]) -> bool:
        """已经发生过这些故障之后，还要不要再试一次。

        看整个序列，不只看最后一次：连着三次 `INVALID` 说明这个函数的
        输出类型或者 prompt 有问题，这种情形再试也是同样的结果。
        """
        raise NotImplementedError

    def wait_s(self, attempt: int) -> float:
        """第几次尝试之前等多少秒。"""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Corrections:
    """此前几次尝试为什么没被接受，按顺序。

    存在的理由是它是 agent 自我纠正的唯一通道：
    一次会话结束进程就退出了，上一次的校验错误只能通过下一次的 prompt 到达它。
    """

    reasons: tuple[str, ...] = ()

    def after(self, fault: Fault, detail: str) -> Corrections:
        """加上一条，返回新的一份。

        `CRASHED` 和 `TIMED_OUT` 在这里被丢掉：它们不是关于回答内容的，
        告诉 agent「上次那个进程崩了」只会占掉它的上下文。
        """
        raise NotImplementedError
