import asyncio
from pathlib import Path
from typing import Protocol

from dsh_trajectory.codec import digest
from dsh_trajectory.errors import TrajectoryError
from dsh_trajectory.pipeline import Sink, process


class Environment(Protocol):
    async def download_dir(self, source_dir: str, target_dir: Path | str): ...


async def collect_and_ingest(
    environment: Environment,
    *,
    remote_sessions: str,
    staging_dir: Path,
    sink: Sink,
    trial_id: str,
    attempt_id: str,
    harness_version: str,
    allow_incomplete: bool = False,
    workers: int = 1,
) -> dict:
    """Call after the agent stops writing, before Harbor destroys the environment."""
    if not remote_sessions.startswith("/") or remote_sessions == "/":
        raise TrajectoryError("remote_sessions must name the absolute session directory")
    key = digest(f"{trial_id!r}:{attempt_id!r}".encode())
    directory = staging_dir / key
    native = directory / "native"
    bundle = directory / "bundle.json.zst"
    directory.mkdir(parents=True, exist_ok=True)
    if not bundle.with_name(bundle.name + ".state.json").exists():
        await environment.download_dir(remote_sessions, native)
    return await asyncio.to_thread(
        process,
        native,
        bundle,
        sink,
        trial_id=trial_id,
        attempt_id=attempt_id,
        harness_version=harness_version,
        cleanup="all",
        workers=workers,
        allow_incomplete=allow_incomplete,
    )
