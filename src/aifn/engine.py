from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aifn.call import Fault
from aifn.declare import Model
from aifn.terms import Event, JsonObject, SessionId


@dataclass(frozen=True, slots=True)
class Session:
    """一次会话的全部输入。

    把它单独写出来，是为了让 engine 的接口不提及 `AiFunction`：
    engine 要知道的是跑哪段文字、在哪个目录、注册哪些工具，
    而输入输出类型是调用方那一侧的事。
    """

    id: SessionId
    prompt: str
    directory: Path
    permission: str
    model: Model
    tools: JsonObject
    """工具名到参数 schema 的映射，由 `submit.bundle` 给出。"""
    timeout_s: float
    env: dict[str, str]


@dataclass(frozen=True, slots=True)
class Transcript:
    """一次会话跑完之后留下的东西。

    只有事件流：答案在事件流里，而它是回答、拒答还是根本没答，
    由 `extract` 从同一份事件流里读出来，engine 不做这个判断。
    这让一次录下来的会话可以完整地重放。
    """

    session: SessionId
    events: tuple[Event, ...]


class Engine(Protocol):
    """跑一次会话的那一层。

    写成协议的理由是一次录下来的事件流也能满足它：
    整个执行路径因此可以脱离模型被测试，
    而那条路径上关于重试、租约和校验的判断，才是这个库真正要说对的部分。
    """

    def run(self, session: Session) -> Transcript:
        """跑一次会话，把事件流交回来。

        进程崩溃、连接断开、超时都在这里抛出，不在这里被吞掉：
        worker 要把它们分成不同的 `Fault`，而一个返回空事件流的 engine
        会让 worker 把崩溃读成「没有回答」。
        """
        ...

    def prepare(self) -> None:
        """一次性的准备工作，跑任何会话之前做。

        bundle 是以复制的方式装进 profile 的，所以一次编辑要装过一遍才生效；
        装的是哪一份和这个库现在是哪一份不一致时，
        agent 会按装进去的那份 schema 回答，而这一侧用另一份去校验，
        整个流程要跑到最后才发现。
        这里立即失败。
        """
        ...


def faults_of(error: BaseException) -> Fault:
    """engine 抛出的异常对应哪一种 `Fault`。

    单独一个函数承担这件事，因为哪些异常算超时、哪些算崩溃是一个需要被测试的判断，
    而写在 worker 的异常处理里它就测不到。
    无法归类的异常在这里重新抛出，因为一个被归成 `CRASHED` 的编程错误
    会被重试机制掩盖成一次偶发故障。
    """
    raise NotImplementedError


def describe(events: Sequence[Event]) -> str:
    """事件流的一句话摘要，写进 `Attempt.detail`。

    给那些「会话结束了但没有回答」的尝试用：
    agent 最后说了什么，是判断该改 prompt 还是该重试的唯一依据。
    """
    raise NotImplementedError
