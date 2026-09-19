from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aifn.engine import Session, Transcript


@dataclass(frozen=True, slots=True)
class ReplayEngine:
    """Answers out of recorded transcripts, without running a model.

    It exists because it makes the whole execution path testable: leases, retries, validation,
    and telling a refusal from "no answer" are the judgments this library has to get right, and
    they are untestable when every test costs dozens of minutes and one model call. A recorded
    transcript is real model output, so this is not fabricated data.
    """

    directory: Path
    """The events directory `Mailbox.record` wrote."""

    def run(self, session: Session) -> Transcript:
        """Hand back the recorded transcript for this session, failing when there is none.

        Failing immediately rather than returning an empty one: an empty transcript is read as
        "the agent did not answer", so a missing recording would present itself as a model
        behaviour, which is the hardest kind of result to track down in a test.
        """
        raise NotImplementedError

    def prepare(self) -> None:
        """Confirm the recording directory is there."""
        raise NotImplementedError
