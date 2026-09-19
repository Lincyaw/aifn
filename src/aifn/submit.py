from __future__ import annotations

from typing import Final

from pydantic import BaseModel

from aifn.terms import FunctionName, JsonObject

SUBMIT_PREFIX: Final = "submit_"
"""submit 工具的名字前缀，工具名是它加上函数名。

一个函数一个工具名，所有函数不共用一个 `submit`：
名字里带着函数名，agent 调错工具就成了「这次没有回答」，
而共用一个名字时它会成为「回答的形状不对」，两者在事件流里无从区分。
"""

REFUSE_PREFIX: Final = "refuse_"
"""拒答工具的名字前缀。

拒答必须也是一次工具调用，因为它是一个回答：
让 agent 用散文说「这个我答不了」，事件流里就只剩一段没有工具调用的会话，
和一次跑飞了的会话长得一模一样。
"""


class Refusal(BaseModel):
    """拒答工具的参数，所有 AI 函数共用这一种形状。

    只有一个理由字段：拒答不是这个函数的输出类型的一个特例，
    它是「这个输入没有输出」，因此它的形状和函数是什么无关。
    """

    reason: str


def submit_tool(function: FunctionName) -> str:
    """某个函数的 submit 工具叫什么。"""
    raise NotImplementedError


def refuse_tool(function: FunctionName) -> str:
    """某个函数的拒答工具叫什么。"""
    raise NotImplementedError


def parameters(returns: type[BaseModel]) -> JsonObject:
    """把输出类型翻译成 submit 工具的参数 schema。

    harness 的参数 DSL 是 JSON Schema 的一个子集：有类型、嵌套、数组和枚举，
    没有 pattern、没有取值范围、没有跨字段规则，并且用每个属性上的
    `required` 标记代替 required 数组。
    所以这里只负责把形状说对，剩下的约束由调用方这一侧的模型校验，
    而一个通过了工具参数校验却没通过模型校验的回答，算 `Fault.INVALID`。
    """
    raise NotImplementedError


def bundle(returns: type[BaseModel], function: FunctionName) -> JsonObject:
    """一个 AI 函数在 harness 那一侧需要的全部：两个工具及其 schema。

    返回的是一份数据，这里不生成 JavaScript：bundle 是一个装好的 dsh 插件，
    它按名字向 harness 要工具定义，所以这一侧要给出的只有定义本身。
    """
    raise NotImplementedError
