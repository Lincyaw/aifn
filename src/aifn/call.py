from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aifn.terms import CallId, FunctionName, JsonObject, SessionId

_STRICT = ConfigDict(extra="forbid", frozen=True)


class Status(StrEnum):
    """一次调用最终处于哪一种结果。

    三种，因为它们对调用方意味着三件不同的事：
    `RETURNED` 有一个校验通过的值可以读；
    `REFUSED` 没有值，而再问一次也不会有，因为 agent 判定这个输入答不了；
    `FAILED` 也没有值，但原因在执行本身，换一次尝试就可能有。
    把后两者合成一种，等于让调用方无法区分「这个输入没有答案」和「这次没跑起来」。
    """

    RETURNED = "returned"
    REFUSED = "refused"
    FAILED = "failed"


class Fault(StrEnum):
    """一次尝试因为什么没有产出值。

    分得这么清楚，是因为重试的判断完全由它决定：
    `TIMED_OUT` 重试一次很可能同样超时，`INVALID` 把校验错误写进下一次的 prompt 往往就好了。
    一个只说「失败了」的理由，不足以让 `Policy` 做这个判断。
    """

    CRASHED = "crashed"
    """harness 进程退出或者连接断开，模型那一侧发生了什么无从得知。"""
    TIMED_OUT = "timed_out"
    """超过了函数声明的时限，会话被中止。"""
    NO_ANSWER = "no_answer"
    """会话结束了，但事件流里没有一次被接受的 submit 调用。

    agent 写了散文而没有调用工具，或者一直在读文件直到轮次结束。
    这既不是拒答也不是崩溃：拒答要通过工具说出来，才算一个回答。
    """
    INVALID = "invalid"
    """submit 调用的参数没能校验成声明的输出类型。

    harness 那一侧的 schema 是一个子集，表达不了跨字段规则和取值范围，
    所以通过了工具的参数校验并不意味着通过了这里的模型校验。
    """


class Call(BaseModel):
    """一次待执行的调用，写进 outbox 的那一行。

    `request` 是已经序列化的 JSON，没有具体的请求模型：
    worker 必须先读到这一行才知道该找哪个声明，因此 outbox 的格式
    不能依赖于只有查过 registry 才能知道的类型。
    类型在 worker 查到声明之后立即恢复。
    """

    model_config = _STRICT

    id: CallId
    function: FunctionName
    request: JsonObject
    issued_at: datetime
    sample: int = 0
    """同一个函数、同一个请求要跑第几份。

    它参与 `id` 的计算，所以这是有意重复采样和无意重复提交之间唯一的区别。
    重复提交会得到同一个 `id`，因而什么也不做。
    重新采样换一个 `sample`，得到一次新的调用。
    """


class Attempt(BaseModel):
    """一次调用的其中一次尝试。

    留着全部尝试，不只留最后一次：一个答了三次才通过校验的函数
    和一个一次就通过的函数，输出类型相同而质量不同，
    而这个差别只在这里看得见。
    """

    model_config = _STRICT

    index: int
    session: SessionId
    worker: str
    started_at: datetime
    finished_at: datetime
    fault: Fault | None = None
    detail: str = ""
    """这次尝试为什么没成，原文照抄。

    它会被渲染进下一次尝试的 prompt，所以一段读不懂的摘要
    等于放弃了让 agent 自我纠正的机会。
    """


class Returned[O: BaseModel](BaseModel):
    """有一个校验通过的值。

    值的类型就是声明里的 `returns`，所以「returned 意味着拿到了那个类型」
    是一件类型上的事实，不是一条需要调用方记住的约定。
    """

    model_config = _STRICT

    status: Literal[Status.RETURNED] = Status.RETURNED
    value: O


class Refused(BaseModel):
    """agent 判定这个输入答不了，并说明了为什么。

    这是一个关于输入的结论，不是一次故障，所以它是终态：
    重试同一个输入只会得到同一句话，并且付一次模型调用的代价。
    """

    model_config = _STRICT

    status: Literal[Status.REFUSED] = Status.REFUSED
    reason: str


class Failed(BaseModel):
    """尝试用完了，最后一次的故障就是这个调用的结论。"""

    model_config = _STRICT

    status: Literal[Status.FAILED] = Status.FAILED
    fault: Fault
    detail: str = ""


type Outcome[O: BaseModel] = Annotated[
    Returned[O] | Refused | Failed,
    Field(discriminator="status"),
]
"""三种结果之一。

写成带 discriminator 的联合。
「一个 status 加三个可选字段」的写法允许出现 status 为 returned 而 value 为空的行，
而那种行只有在读它的地方崩溃时才会被发现。
"""


class Return[O: BaseModel](BaseModel):
    """一次调用的结论，出现在 inbox 里的那个文件。

    带上全部尝试记录，因为这个文件是这次调用留下的唯一痕迹：
    进程早就退出了，而结论为什么是这个，只能从这里读出来。
    """

    model_config = _STRICT

    id: CallId
    function: FunctionName
    outcome: Outcome[O]
    attempts: tuple[Attempt, ...] = ()
    finished_at: datetime

    @property
    def status(self) -> Status:
        """结论的种类，不必先判断 `outcome` 是哪一支就能读到。"""
        raise NotImplementedError

    def unwrap(self) -> O:
        """取出值，不是 `RETURNED` 时立即失败。

        给那些「拒答和故障都无法继续」的调用方用：
        它们本来也要在下一行崩溃，在这里崩溃能带上 `outcome` 里的理由。
        """
        raise NotImplementedError


type AnyOutcome = Outcome[Any]
"""输出类型未知的一个结论。

worker 这一侧只有 `type[BaseModel]` 而没有类型参数，所以它处理的结论是这个。
写成 `Any`，因为泛型参数是不变的：
一个 `Returned[Answer]` 并不是一个 `Returned[BaseModel]`，
所以 `Outcome[BaseModel]` 并不更宽，它只是一个装不下任何真实结论的类型。
"""

type AnyReturn = Return[Any]
"""输出类型未知的一份结论文件，理由同 `AnyOutcome`。"""


def identity(function: FunctionName, request: BaseModel, sample: int = 0) -> CallId:
    """一次调用的 id，由函数名、请求内容和采样序号算出来。

    用内容摘要，不用递增序号或随机串，这是全库最要紧的一个决定：
    它让重复提交等于什么也不做，于是一个跑了几小时中途崩溃的流程，
    重新运行同一段程序就能接着跑。
    已经有结论的调用直接读到结论，还没有结论的调用认出自己已经在 outbox 里。
    进程里因此不需要记住任何东西，因为它记不住。
    """
    raise NotImplementedError
