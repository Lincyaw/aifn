from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from aifn.engine import Session, Transcript

BUNDLE_NAME: Final = "@aifn/dsh-aifn-harness"
"""dsh bundle 的包名。

bundle 按名字注册 submit 工具，Python 这一侧按同一个名字要求 profile 里有它。
"""

PROFILE: Final = "sdk"
"""用哪个 profile。

以 base 为底。
`sdk-minimal` 固定 `danger-full-access` 并且不挂载任何限制，
一次调用就能读写运行时够得到的任何地方，
而 `Access` 声明的权限只有在 base 的沙箱规则下才有意义。
"""

STAMP: Final = ".aifn-bundle"
"""装进 profile 的那份 bundle 的摘要，放在它的 manifest 旁边。

bundle 是复制进去的，所以编辑过的 schema 要重装一次才生效。
不记这个摘要时，装进去的和这一侧现在的不一致是静默的：
agent 按装进去的那份回答，这一侧用另一份去校验，整个流程跑完才发现。
"""


@dataclass(frozen=True, slots=True)
class DeepSeekEngine:
    """用 DeepSeek Harness 的 Python SDK 跑一次会话。

    一次调用一个 session，一个 session 一次 `harness.run`：
    同一个 id 续接同一段持久对话，所以第二次调用复用同一个 id
    会带着第一次的工具和历史回答。
    """

    dsh_home: Path
    env: Mapping[str, str] = field(default_factory=dict)
    """传给运行时的环境变量。

    `DSH_PERMISSION_MODE` 不在这里：它来自每个函数声明的 `Access`，
    而这份 env 是整个 engine 共用的凭据和地址。
    """

    def run(self, session: Session) -> Transcript:
        """跑一次会话。

        SDK 的 `run` 是同步的，所以这个方法阻塞到会话结束，
        并发由几个 worker 各跑一份 engine 来实现。
        """
        raise NotImplementedError

    def prepare(self) -> None:
        """确认 profile 里装着当前这一份 bundle，不是时立即失败。"""
        raise NotImplementedError


def install_bundle(dsh_home: Path, bundle: Path, *, reinstall: bool = False) -> bool:
    """把 bundle 装进 profile，已经是当前这一份时返回假。

    用 `file:` 这种 spec。
    pnpm 会把 bundle 复制进 profile 的包目录，
    而那里才是它的 peer import 能解析到的位置，`link:` 做不到这一点。
    复制意味着一次编辑要重装一遍才到得了会话，
    而对一个没有变化的 spec 执行 `add` 什么也不做，所以重装要先移除。
    """
    raise NotImplementedError


def require_bundle(dsh_home: Path, bundle: Path) -> None:
    """装了没有、是不是当前这一份，两个问题一起回答并给出确切的修复命令。"""
    raise NotImplementedError


def fingerprint(bundle: Path) -> str:
    """bundle 现在是什么，覆盖一次会话读到的每个文件。"""
    raise NotImplementedError
