from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aifn.engine import Session, Transcript


@dataclass(frozen=True, slots=True)
class ReplayEngine:
    """从录下来的事件流里回答，不跑模型。

    存在的理由是它让整个执行路径可以被测试：
    租约、重试、校验、拒答和「没有回答」的区分，都是这个库要说对的判断，
    而每测一次都花掉几十分钟和一次模型调用，这些判断就测不到。
    录下来的事件流是真实的模型输出，所以这不是伪造的数据。
    """

    directory: Path
    """`Mailbox.record` 写下的那个 events 目录。"""

    def run(self, session: Session) -> Transcript:
        """交回这个 session 对应的那份录下来的事件流，没有录过时立即失败。

        没有录过时立即失败，不返回一份空的：一份空的事件流会被读成
        「agent 没有回答」，于是一个缺失的录制会表现为一次模型行为，
        而那是测试里最难查的一种结果。
        """
        raise NotImplementedError

    def prepare(self) -> None:
        """确认录制目录存在。"""
        raise NotImplementedError
