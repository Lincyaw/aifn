from __future__ import annotations

from dataclasses import dataclass

from aifn.engine import Engine, Session, Transcript


@dataclass(frozen=True, slots=True)
class RoutingEngine:
    """Open a conversation on one engine, continue an existing one on another.

    The two front ends differ in exactly that: the SDK server cannot reopen a session, and the
    ACP app cannot open one under the id the worker has already chosen. A worker holds one
    engine, so the choice is made here from what the attempt continues.
    """

    fresh: Engine
    continued: Engine

    def prepare(self) -> None:
        """Check both engines' runtimes, because either can be asked for on the next call."""
        self.fresh.prepare()
        self.continued.prepare()

    def run(self, session: Session) -> Transcript:
        return (self.continued if session.continues else self.fresh).run(session)
