from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from aifn.terms import CallId, FunctionName


class Access(StrEnum):
    """agent 在工作目录里能做什么。

    一个只读的调用不可能改坏目标，所以并发多少次都互不影响；
    一个可写的调用会留下痕迹，因此它必须拿到自己的一份目录。
    权限因此不只是一条安全规则，它决定了同一个目录能不能被两次调用同时使用。
    """

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


@dataclass(frozen=True, slots=True)
class Workspace:
    """agent 跑在哪个目录里，以及它在那里能做什么。

    prompt 决定 agent 被问了什么，工作目录决定它能看到什么，
    而后者往往比前者更能左右答案，所以它是声明的一部分，不是 worker 的配置。
    """

    directory: Path
    access: Access = Access.READ_ONLY
    private: bool = False
    """每次调用是否要拿到自己的一份目录副本。

    可写且并发的调用共用一个目录时，一次调用会读到另一次写下的中间状态，
    而这种干扰在结果里看不出来，只会让答案无法解释。
    """

    def materialize(self, call: CallId, scratch: Path) -> Path:
        """给一次调用准备好它实际使用的目录，返回那个路径。

        声明里的目录是一个意图，worker 要的是一个此刻存在的路径：
        `private` 为假时两者相同，为真时这里复制一份，而复制的代价只有 worker 付得起。
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Model:
    """用哪个模型回答，以及一次回答能写多长。

    同一个 AI 函数换一个模型就是换一个实现，而输入输出类型不变，
    所以模型是声明里可以单独替换的一项。
    """

    name: str
    provider: str = "deepseek-official"
    max_tokens: int = 32_768
    reasoning_effort: str | None = None


class Instruction[I: BaseModel](Protocol):
    """把一个请求渲染成 agent 读到的那段文字。

    这是整个库里唯一决定回答质量的地方，所以它是一个可以单独替换和单独测试的对象：
    渲染不碰网络也不碰 harness，给它一个请求就能看到 agent 会读到什么。
    """

    def render(self, request: I, rejected: Sequence[str]) -> str:
        """一次调用的全部 prompt，`rejected` 是此前几次尝试为什么没被接受。

        `rejected` 没有默认值：一次会话结束后进程就退出了，
        上一次尝试的校验错误只有通过下一次的 prompt 才能到达 agent，
        而一个可以忽略这个参数的渲染器就是一个永远不会自我纠正的渲染器。
        """
        ...


@dataclass(frozen=True, slots=True)
class AiFunction[I: BaseModel, O: BaseModel]:
    """一个签名像普通函数、实现是一次 agent 调用的函数。

    它把两侧固定下来：调用方看到的是 `request` 和 `returns` 两个类型，
    worker 看到的是渲染方式、工作目录、模型和超时。
    两侧之间没有别的约定，所以调用方不需要知道 agent 怎么被问，
    worker 也不需要知道结果会被谁读。
    """

    name: FunctionName
    request: type[I]
    returns: type[O]
    instruction: Instruction[I]
    workspace: Workspace
    model: Model
    timeout_s: float = 1800.0
    """一次尝试等多久。

    从秒到小时都是正常的，所以这个值由函数自己声明：
    一个读几十个服务的调用和一个读一个文件的调用，用同一个超时只能取其中更长的那个。
    """
    attempts: int = 2
    """同一个调用最多尝试几次。

    每次尝试都是一次完整的模型调用，代价不可忽略，
    所以重试次数是函数自己的选择，而哪些故障值得重试由 `Policy` 决定。
    """


@dataclass(frozen=True, slots=True)
class Registry:
    """名字到声明的映射。

    outbox 里存的是函数名，worker 要靠名字找回声明才能渲染和校验，
    所以跨进程传递一个 AI 函数的唯一方式是它的名字加上两侧共享的这张表。
    """

    functions: dict[FunctionName, AiFunction[Any, Any]]

    def declare[I: BaseModel, O: BaseModel](self, function: AiFunction[I, O]) -> None:
        """登记一个声明，名字重复时立即失败。

        两个不同的声明共用一个名字，意味着 worker 会用其中一个的输出类型
        去校验另一个的答案，而这种错配在结果里表现为莫名其妙的校验失败。
        """
        raise NotImplementedError

    def lookup(self, name: FunctionName) -> AiFunction[Any, Any]:
        """按名字取回声明，没有时立即失败。

        取回的声明两个参数都是 `Any`。
        泛型参数是不变的，所以一个 `AiFunction[Request, Answer]`
        并不是一个 `AiFunction[BaseModel, BaseModel]`，写成后者的表根本装不进东西。
        `Any` 说的是这里确实不知道类型，这是全库唯一一个擦除点：
        worker 之后只通过 `request` 和 `returns` 两个 `type[BaseModel]` 工作，
        而调用方那一侧的类型由 `compose` 重新恢复。
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Template[I: BaseModel]:
    """由一段模板文字构成的 `Instruction`。

    存在的理由是让「渲染是一个对象」这件事不必为最简单的情形付代价：
    一个只需要把请求的字段填进一段固定文字的函数，不应该为此写一个类。
    """

    body: str
    """`str.format` 的模板，请求的字段按名字代入。"""
    rejected_heading: str = "此前的回答为什么没有被接受"

    def render(self, request: I, rejected: Sequence[str]) -> str:
        """模板填好之后，把此前几次尝试的拒绝理由附在末尾。"""
        raise NotImplementedError
