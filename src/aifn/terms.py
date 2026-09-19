from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

type CallId = str
"""一次调用的身份，由调用方生成。

outbox 的文件名、claims 的目录名、inbox 的文件名都是它，所以它必须是文件名安全的。
调用方生成而非 worker 生成：调用方写完 outbox 就返回，它此刻已经需要一个能用来等结果的名字。
"""

type FunctionName = str
"""一个 AI 函数的名字。

worker 从 outbox 读到的是名字，要靠它在 registry 里找回声明，所以名字是跨进程的唯一引用方式。
"""

type WorkerId = str
"""一个 worker 进程的身份。

租约上记着它，所以一个被中止的 worker 留下的租约能和活着的 worker 区分开。
"""

type SessionId = str
"""一次 agent 会话的身份。

同一个 id 续接同一段持久对话，所以每次尝试都要一个新的：
重试如果复用上一次的 id，模型会带着上一次被拒绝的答案继续回答。
"""

type Event = Mapping[str, JsonValue]
"""事件流里的一条。

harness 给出的是 JSON，这里不为它建模：读答案只关心 `tool/call` 和 `tool/result` 两种，
为其余几十种事件写类型只会在 harness 增加事件种类时变成需要维护的负担。
"""

type JsonObject = Mapping[str, JsonValue]
"""一个 JSON 对象。

请求体和 submit 工具的参数在进程之间是 JSON，两侧各自校验成声明好的模型，中间没有别的表示。
"""
