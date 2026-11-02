import os
import re
from dataclasses import dataclass
from pathlib import Path

from dsh_trajectory.codec import DEFAULT_MAX_BYTES, decompress, digest, strict_json
from dsh_trajectory.errors import TrajectoryError

LOG_NAME = re.compile(r"session(?:\.v([1-9][0-9]*))?\.jsonl(?:\.zstd)?\Z")
MAX_SAFE_INTEGER = (1 << 53) - 1


@dataclass(frozen=True)
class Source:
    path: Path
    sha256: str
    size: int

    def verify(self) -> None:
        if self.path.is_symlink() or not self.path.is_file():
            raise TrajectoryError(f"Source changed or disappeared: {self.path}", code=5)
        if digest(self.path.read_bytes()) != self.sha256:
            raise TrajectoryError(f"Source changed; refusing cleanup: {self.path}", code=5)


def discover(path: Path) -> list[Path]:
    if path.is_symlink():
        raise TrajectoryError("Input must not be a symbolic link")
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_file():
        if not LOG_NAME.fullmatch(path.name):
            raise TrajectoryError("Input filename must be session[.vN].jsonl[.zstd]")
        return [path.resolve()]
    selected: dict[Path, tuple[int, Path]] = {}
    for directory, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            match = LOG_NAME.fullmatch(name)
            if match is None:
                continue
            candidate = Path(directory) / name
            if candidate.is_symlink():
                raise TrajectoryError(f"Session log is a symbolic link: {candidate}")
            version = int(match[1] or 0)
            previous = selected.get(candidate.parent)
            if previous and previous[0] == version:
                raise TrajectoryError(f"Ambiguous physical encodings in {candidate.parent}")
            if previous is None or version > previous[0]:
                selected[candidate.parent] = (version, candidate.resolve())
    if not selected:
        raise TrajectoryError(f"No canonical session logs under {path}", code=3)
    return sorted(item[1] for item in selected.values())


def read_log(path: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[dict, list[dict], Source]:
    if path.is_symlink():
        raise TrajectoryError(f"Session log is a symbolic link: {path}")
    before = path.stat()
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise TrajectoryError(f"Log is still changing: {path}", code=5, retryable=True)
    if len(raw) > max_bytes:
        raise TrajectoryError(f"Input exceeds {max_bytes} bytes: {path}")
    decoded = decompress(raw, max_bytes) if path.suffix == ".zstd" else raw
    if not decoded.endswith(b"\n"):
        raise TrajectoryError(f"Incomplete JSONL final line: {path}")
    try:
        records = [strict_json(line) for line in decoded.splitlines()]
    except ValueError as error:
        raise TrajectoryError(f"Invalid JSONL in {path}: {error}") from error
    if not records or not isinstance(records[0], dict):
        raise TrajectoryError(f"Missing session header: {path}")
    header, events = records[0], records[1:]
    if header.get("type") != "session" or type(header.get("version")) is not int:
        raise TrajectoryError(f"Invalid session header: {path}")
    if header["version"] != 4:
        raise TrajectoryError(f"Unsupported Harness format v{header['version']}; expected v4")
    match = LOG_NAME.fullmatch(path.name)
    if match is None or int(match[1] or 0) != header["version"]:
        raise TrajectoryError(f"Header version disagrees with canonical filename: {path}")
    for field in ("createdAt", "delegationDepth"):
        value = header.get(field)
        if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
            raise TrajectoryError(f"Invalid header {field}: {path}")
    for field in ("id",):
        if not isinstance(header.get(field), str) or not header[field]:
            raise TrajectoryError(f"Invalid header {field}: {path}")
    if type(header.get("isSeeded")) is not bool:
        raise TrajectoryError(f"Missing isSeeded in header: {path}")
    for field in ("cwd", "parentSession", "agentPreset"):
        if field in header and not isinstance(header[field], str):
            raise TrajectoryError(f"Invalid header {field}: {path}")
    if "origin" in header and header["origin"] != "subagent":
        raise TrajectoryError(f"Invalid header origin: {path}")
    for index, event in enumerate(events):
        if not isinstance(event, dict) or type(event.get("seq")) is not int:
            raise TrajectoryError(f"Invalid event envelope at row {index + 2}: {path}")
        if event["seq"] != index or not isinstance(event.get("type"), str):
            raise TrajectoryError(f"Non-contiguous event sequence at row {index + 2}: {path}")
        if (
            type(event.get("time")) is not int
            or not -MAX_SAFE_INTEGER <= event["time"] <= MAX_SAFE_INTEGER
            or not isinstance(event.get("data"), dict)
        ):
            raise TrajectoryError(f"Invalid event payload at seq {index}: {path}")
        if "ignorable" in event and event["ignorable"] is not True:
            raise TrajectoryError(f"Invalid ignorable flag at seq {index}: {path}")
    return header, events, Source(path, digest(raw), len(raw))
