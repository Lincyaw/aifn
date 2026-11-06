from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from aifn.engine import Session, Transcript
from aifn.storage import component


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
        self.prepare()
        path = self.directory / f"{component(session.id)}.jsonl"
        if not path.exists():
            if session.call is not None:
                path = self.directory / component(session.call) / f"{session.attempt}.jsonl"
            else:
                call, separator, attempt = session.id.rpartition("-attempt-")
                if not separator or not attempt.isdigit():
                    raise FileNotFoundError(path)
                path = self.directory / component(call) / f"{int(attempt)}.jsonl"
        adapter = TypeAdapter(dict[str, JsonValue])
        # Recorded events can hold U+2028 or U+0085 inside a string, which only a split of
        # the bytes leaves whole.
        events = tuple(
            adapter.validate_python(json.loads(line)) for line in path.read_bytes().splitlines()
        )
        receipt = path.with_suffix(".submission.json")
        submission = adapter.validate_json(receipt.read_bytes()) if receipt.exists() else None
        return Transcript(session=session.id, events=events, submission=submission)

    def prepare(self) -> None:
        """Confirm the recording directory is there."""
        if not self.directory.is_dir():
            raise NotADirectoryError(self.directory)
