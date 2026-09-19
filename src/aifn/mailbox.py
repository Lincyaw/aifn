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
    """一个 worker 对一次调用的租约。

    带过期时间，不只是一个占位标记：被中止的 worker 没有机会清理自己的租约，
    而一个永不过期的标记会让那次调用永远没有人认领。
    到期就能被抢走，这是崩溃之后未完成的调用重新回到队列的唯一途径。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    call: CallId
    worker: WorkerId
    claimed_at: datetime
    expires_at: datetime
    attempt: int = 0

    def expired(self, now: datetime) -> bool:
        """这个租约是否已经可以被别人抢走。"""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Mailbox:
    """一个目录，调用方和 worker 只在这里相遇。

    状态全在文件里而不在进程里，因为一个跑几小时的流程中途会崩溃：
    发起调用的进程不是执行它的进程，读结果的进程也不是写结果的那个。
    调用方写 outbox 就返回，worker 认领、执行、写 inbox，
    除此以外两侧之间没有任何通道。
    """

    root: Path

    def prepare(self) -> None:
        """把这些目录建出来，已经存在时什么也不做。"""
        raise NotImplementedError

    def issue(self, call: Call) -> bool:
        """把一次调用写进 outbox；它已经在那里或者已经有结论时返回假。

        返回布尔值，不静默覆盖：重复发起同一次调用是流程重跑的正常情形，
        而覆盖一个正在被执行的调用会让它的 worker 写回一份对不上的结论。
        """
        raise NotImplementedError

    def pending(self) -> Iterator[Call]:
        """还没有结论、也没有被有效租约占住的调用，按发出时刻排序。

        按时刻排序，不按文件名：文件名是内容摘要，它的顺序和发起顺序没有关系，
        而一个先发起的调用先被执行，是调用方唯一能指望的排队行为。
        """
        raise NotImplementedError

    def claim(self, call: CallId, worker: WorkerId, lease: timedelta) -> Claim | None:
        """抢下一次调用的租约，抢不到时返回空。

        靠 `O_EXCL` 创建租约文件来互斥，所以「同一个调用不会被两个 worker 认领」
        这件事由文件系统保证，不依赖任何一个进程活着。
        过期的租约在这里被接管，而接管会把 `attempt` 加一：
        一次尝试即使是被中止的，它也已经花掉了。
        """
        raise NotImplementedError

    def renew(self, claim: Claim, lease: timedelta) -> Claim:
        """延长租约。

        租约必须比一次调用的时限短得多，否则一个被中止的 worker 会把它的调用
        扣住几个小时；而短租约需要执行过程中续租，不然一次正常的长调用
        会被另一个 worker 当成中止的抢走。
        """
        raise NotImplementedError

    def release(self, claim: Claim) -> None:
        """放弃租约而不写结论。

        用在这次尝试失败但调用还要再试的时候：
        写一份 `FAILED` 会让调用方以为已经有结论了。
        """
        raise NotImplementedError

    def deliver(self, result: AnyReturn, claim: Claim) -> None:
        """写下结论并交还租约，两件事一起完成。

        先写 inbox 再删租约：反过来的顺序留出一个两个 worker 都认为
        自己该执行这次调用的间隙。
        """
        raise NotImplementedError

    def returned[O: BaseModel](self, call: CallId, returns: type[O]) -> Return[O] | None:
        """读一次调用的结论，还没有时返回空。

        要传 `returns`：inbox 里是 JSON，而它该校验成哪个模型，
        只有拿着声明的调用方知道。
        """
        raise NotImplementedError

    def record(self, call: CallId, attempt: int, events: Sequence[Event]) -> Path:
        """把一次尝试的完整事件流写下来，返回写到哪里。

        原样写下整份，不只留提取出来的答案：一次尝试为什么没答上来
        只能从事件流里看出来，而重跑一次要花掉同样的时间和费用。
        """
        raise NotImplementedError

    def events(self, call: CallId, attempt: int) -> Sequence[Event]:
        """读回某一次尝试的事件流。

        存在的理由是让 `extract` 可以脱离模型被测试：
        录下来的事件流是它唯一的输入。
        """
        raise NotImplementedError
