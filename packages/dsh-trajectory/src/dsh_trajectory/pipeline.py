import fcntl
from pathlib import Path
from typing import Protocol

from dsh_trajectory.bundle import build_bundle, read_bundle, save_bundle
from dsh_trajectory.codec import (
    DEFAULT_MAX_BYTES,
    atomic_write,
    digest,
    json_bytes,
    strict_json,
    sync_directory,
)
from dsh_trajectory.errors import TrajectoryError
from dsh_trajectory.native import LOG_NAME, Source


class Sink(Protocol):
    identity: str

    def ingest(self, payload: bytes) -> dict: ...


def confirm_receipt(receipt: dict, bundle: dict, payload: bytes) -> None:
    expected = {
        "committed": True,
        "trial_id": bundle["trial_id"],
        "attempt_id": bundle["attempt_id"],
        "payload_sha256": digest(payload),
        "content_sha256": digest(json_bytes(bundle)),
    }
    if receipt.get("committed") is not True or any(
        receipt.get(key) != value for key, value in expected.items()
    ):
        raise TrajectoryError(
            "Persistence receipt does not match this bundle; retaining data",
            code=11,
            retryable=True,
        )


def process(
    input_path: Path,
    output: Path,
    sink: Sink,
    *,
    trial_id: str,
    attempt_id: str,
    harness_version: str,
    cleanup: str = "none",
    workers: int = 1,
    allow_incomplete: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    dry_run: bool = False,
) -> dict:
    if cleanup not in ("none", "sources", "all"):
        raise TrajectoryError("cleanup must be none, sources or all")
    input_path, output = input_path.absolute(), output.absolute()
    if output == input_path or output.is_symlink():
        raise TrajectoryError("Output must differ from the input and must not be a symlink")
    if dry_run:
        return {
            "dry_run": True,
            "input": str(input_path),
            "bundle": str(output),
            "cleanup": cleanup,
            "trial_id": trial_id,
            "attempt_id": attempt_id,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.with_name(output.name + ".lock")
    state_path = output.with_name(output.name + ".state.json")
    if lock_path.is_symlink() or state_path.is_symlink():
        raise TrajectoryError("Lock/state must not be symbolic links")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _process_locked(
            input_path,
            output,
            state_path,
            sink,
            trial_id=trial_id,
            attempt_id=attempt_id,
            harness_version=harness_version,
            cleanup=cleanup,
            workers=workers,
            allow_incomplete=allow_incomplete,
            max_bytes=max_bytes,
        )


def _process_locked(
    input_path: Path,
    output: Path,
    state_path: Path,
    sink: Sink,
    *,
    trial_id: str,
    attempt_id: str,
    harness_version: str,
    cleanup: str,
    workers: int,
    allow_incomplete: bool,
    max_bytes: int,
) -> dict:
    identity = {
        "trial_id": trial_id,
        "attempt_id": attempt_id,
        "harness_version": harness_version,
        "input": str(input_path),
        "allow_incomplete": allow_incomplete,
        "sink": sink.identity,
    }
    if state_path.exists():
        state = strict_json(state_path.read_bytes())
        if state.get("identity") != identity:
            raise TrajectoryError(
                "Existing process state belongs to different input/options", code=5
            )
        if not output.exists() and state.get("cleanup") == "all":
            return {
                **state["receipt"],
                "cleanup": "all",
                "already_finished": True,
                "state": str(state_path),
            }
        payload = output.read_bytes()
        if digest(payload) != state["payload_sha256"]:
            raise TrajectoryError("Staged bundle changed; retaining source files", code=5)
        bundle = read_bundle(payload, max_bytes)
        sources = tuple(Source(Path(s["path"]), s["sha256"], s["size"]) for s in state["sources"])
    else:
        conversion = build_bundle(
            input_path,
            trial_id=trial_id,
            attempt_id=attempt_id,
            harness_version=harness_version,
            workers=workers,
            allow_incomplete=allow_incomplete,
            max_bytes=max_bytes,
        )
        if output in (source.path for source in conversion.sources):
            raise TrajectoryError("Output would replace a source log")
        save_bundle(conversion, output)
        payload, bundle, sources = conversion.payload, conversion.bundle, conversion.sources
        state = {
            "identity": identity,
            "payload_sha256": digest(payload),
            "sources": [{"path": str(s.path), "sha256": s.sha256, "size": s.size} for s in sources],
            "cleanup": "none",
        }
        atomic_write(state_path, json_bytes(state))
    receipt = sink.ingest(payload)
    confirm_receipt(receipt, bundle, payload)
    state["receipt"] = receipt
    atomic_write(state_path, json_bytes(state))
    if cleanup != "none":
        scope = input_path.resolve() if input_path.is_dir() else input_path.parent.resolve()
        # Validate the complete deletion set before removing its first member.
        for source in sources:
            if not source.path.resolve().is_relative_to(scope) or not LOG_NAME.fullmatch(
                source.path.name
            ):
                raise TrajectoryError("Cleanup source is outside the explicit input scope")
            if source.path.exists() or source.path.is_symlink():
                source.verify()
        for source in sources:
            if source.path.exists():
                source.verify()
                source.path.unlink()
                sync_directory(source.path.parent)
    state["cleanup"] = cleanup
    atomic_write(state_path, json_bytes(state))
    if cleanup == "all":
        if digest(output.read_bytes()) != state["payload_sha256"]:
            raise TrajectoryError("Bundle changed before cleanup", code=5)
        output.unlink()
        sync_directory(output.parent)
    return {
        **receipt,
        "cleanup": cleanup,
        "state": str(state_path),
        "source_bytes": sum(s.size for s in sources),
        "bundle_bytes": len(payload),
    }
