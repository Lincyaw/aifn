from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from dsh_trajectory import __version__
from dsh_trajectory.codec import (
    DEFAULT_MAX_BYTES,
    atomic_write,
    compress,
    decompress,
    digest,
    json_bytes,
    strict_json,
)
from dsh_trajectory.convert import convert_session
from dsh_trajectory.errors import TrajectoryError
from dsh_trajectory.native import Source, discover, read_log
from dsh_trajectory.validation import validate_trajectory

BUNDLE_FORMAT = "dsh-trajectory-bundle-v1"


@dataclass(frozen=True)
class Conversion:
    bundle: dict
    sources: tuple[Source, ...]
    payload: bytes


def build_bundle(
    input_path: Path,
    *,
    trial_id: str,
    attempt_id: str,
    harness_version: str,
    workers: int = 1,
    allow_incomplete: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Conversion:
    from importlib.resources import files

    if workers < 1 or max_bytes < 1 or not all((trial_id, attempt_id, harness_version)):
        raise TrajectoryError(
            "workers/max-bytes must be positive and run identity must be nonempty"
        )
    paths = discover(input_path)
    known = set(files("dsh_trajectory").joinpath("known_events.txt").read_text().splitlines())

    def convert(path: Path) -> tuple[dict, Source]:
        header, events, source = read_log(path, max_bytes)
        trajectory = convert_session(
            header,
            events,
            trial_id=trial_id,
            attempt_id=attempt_id,
            harness_version=harness_version,
            known_events=known,
            allow_incomplete=allow_incomplete,
        )
        validate_trajectory(trajectory)
        source.verify()
        return trajectory, source

    with ThreadPoolExecutor(max_workers=workers) as pool:
        converted = list(pool.map(convert, paths))
    trajectories = sorted((t for t, _ in converted), key=lambda t: t["trajectory_id"])
    if len({t["trajectory_id"] for t in trajectories}) != len(trajectories):
        raise TrajectoryError("Duplicate native session ID across input directories")
    # Absolute local paths are never sent to the ingestion service.
    sources = tuple(source for _, source in converted)
    manifest = [
        {
            "native_session_id": t["extra"]["dsh"]["native_session_id"],
            "sha256": source.sha256,
            "bytes": source.size,
        }
        for t, source in converted
    ]
    manifest.sort(key=lambda item: item["native_session_id"])
    bundle = {
        "format": BUNDLE_FORMAT,
        "converter_version": __version__,
        "trial_id": trial_id,
        "attempt_id": attempt_id,
        "sources": manifest,
        "trajectories": trajectories,
    }
    encoded = json_bytes(bundle)
    if len(encoded) > max_bytes:
        raise TrajectoryError(f"Bundle exceeds {max_bytes} decoded bytes; split input by trial")
    return Conversion(bundle, sources, compress(encoded))


def read_bundle(payload: bytes, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    if len(payload) > max_bytes:
        raise TrajectoryError(f"Compressed bundle exceeds {max_bytes} bytes")
    bundle = strict_json(decompress(payload, max_bytes))
    required = {"format", "converter_version", "trial_id", "attempt_id", "sources", "trajectories"}
    if not isinstance(bundle, dict) or set(bundle) != required or bundle["format"] != BUNDLE_FORMAT:
        raise TrajectoryError("Unsupported bundle envelope")
    if not all(
        isinstance(bundle[k], str) and bundle[k]
        for k in ("trial_id", "attempt_id", "converter_version")
    ):
        raise TrajectoryError("Bundle identity must be nonempty strings")
    if not isinstance(bundle["trajectories"], list) or not bundle["trajectories"]:
        raise TrajectoryError("Bundle must contain at least one trajectory")
    if not isinstance(bundle["sources"], list) or len(bundle["sources"]) != len(
        bundle["trajectories"]
    ):
        raise TrajectoryError("Bundle source count does not match trajectories")
    identities = set()
    for trajectory in bundle["trajectories"]:
        validate_trajectory(trajectory)
        if trajectory["trajectory_id"] in identities:
            raise TrajectoryError("Duplicate trajectory identity")
        identities.add(trajectory["trajectory_id"])
    return bundle


def save_bundle(conversion: Conversion, output: Path) -> dict:
    if output.exists() and output.read_bytes() != conversion.payload:
        raise TrajectoryError(f"Output already contains different content: {output}", code=5)
    atomic_write(output, conversion.payload)
    return {
        "bundle": str(output),
        "sha256": digest(conversion.payload),
        "source_bytes": sum(source.size for source in conversion.sources),
        "bundle_bytes": len(conversion.payload),
        "trajectories": len(conversion.bundle["trajectories"]),
    }
