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
    """认领 outbox 里的调用，跑完写 inbox。

    并发度由 worker 的数量决定而不由调用方决定：
    调用方可以一次发起上千次调用，而同时能跑几个 agent
    取决于机器和额度，这两件事不该由同一个地方决定。
    一个 worker 一次跑一个调用，多开几个就是多几路并发。
    """

    mailbox: Mailbox
    registry: Registry
    engine: Engine
    policy: Policy
    id: WorkerId

    def work(self, budget: int | None = None) -> int:
        """认领并执行调用，直到没有待认领的为止，返回完成了几个。

        `budget` 限制这次最多做几个：一个长期驻留的 worker 要能被换掉
        而不中止手上的调用，做完约定的个数就退出是唯一不会丢掉进度的换法。
        """
        raise NotImplementedError

    def take_one(self) -> AnyReturn | None:
        """认领一个调用并把它做到有结论，没有可认领的时返回空。

        单独一个方法，因为它是整个库里唯一一个既写 inbox 也跑模型的地方，
        测试要能只跑它一次。
        """
        raise NotImplementedError

    def execute(self, call: Call, claim: Claim) -> AnyOutcome:
        """把一次调用做到有结论，该重试就重试。

        租约在这里续，因为只有这里知道一次尝试还在跑：
        续租放在 `work` 里，它就只能在两次调用之间续，
        而一次调用本身就比租约长。
        """
        raise NotImplementedError

    def attempt(
        self, call: Call, index: int, corrections: Corrections
    ) -> tuple[Attempt, AnyOutcome | None]:
        """跑一次尝试。

        返回两样：这次尝试的记录，以及一个结论或者空。
        空的意思是这次没有结论而调用还没有完结，由 `execute` 决定还要不要再试。
        `REFUSED` 在这里就是结论：拒答是一个回答，不是一次失败。
        """
        raise NotImplementedError

    def session(self, call: Call, index: int, corrections: Corrections, directory: Path) -> Session:
        """一次尝试的会话输入。

        每次尝试一个新的 session id：同一个 id 续接同一段持久对话，
        复用它会让重试带着上一次被拒绝的答案继续，
        而重试的全部意义在于重新回答一次。
        """
        raise NotImplementedError

    def read(self, call: Call, transcript: Transcript) -> AnyOutcome:
        """从事件流读出结论，包括把答案校验成声明的输出类型。

        纯函数：给它一份录下来的事件流就能测，
        而「哪一次工具调用算被接受的答案」这个判断值得被测。
        """
        raise NotImplementedError
