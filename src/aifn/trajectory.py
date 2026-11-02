"""Archive stopped native DSH sessions as compressed ATIF, then clean on receipt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from aifn.storage import component


class TrajectorySink(Protocol):
    identity: str

    def ingest(self, payload: bytes) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class TrajectoryArchive:
    """Run only after DSH has closed its writers.

    Keep resumable sessions by using cleanup='none'. Use cleanup='all' only
    for finished sessions whose native journals will no longer be resumed.
    A failed upload retains both source logs and the staged bundle for retry.
    """

    staging: Path
    sink: TrajectorySink
    harness_version: str = "0.2.1-rc.5"
    cleanup: str = "all"
    allow_incomplete: bool = False

    def ingest_session(
        self, trajectory_root: Path, session_id: str, *, trial_id: str, attempt_id: str,
    ) -> dict[str, Any]:
        root = trajectory_root.resolve()
        matches = [entry for entry in root.glob(f"*/{component(session_id)}")
                   if not entry.is_symlink() and not entry.parent.is_symlink()
                   and entry.is_dir() and entry.resolve().is_relative_to(root)]
        if len(matches) != 1:
            raise ValueError(f"Expected one stopped native log directory for {session_id}")
        source = matches[0]
        return self.process(source, trial_id=trial_id, attempt_id=attempt_id)

    def process(
        self, source: Path, *, trial_id: str, attempt_id: str,
    ) -> dict[str, Any]:
        from dsh_trajectory.pipeline import process

        output = self.staging / component(trial_id) / f"{component(attempt_id)}.atif.json.zst"
        result: dict[str, Any] = process(
            source, output, self.sink, trial_id=trial_id, attempt_id=attempt_id,
            harness_version=self.harness_version, cleanup=self.cleanup,
            allow_incomplete=self.allow_incomplete,
        )
        return result
