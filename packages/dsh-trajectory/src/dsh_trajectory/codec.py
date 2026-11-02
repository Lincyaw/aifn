import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import zstandard as zstd

from dsh_trajectory.errors import TrajectoryError

DEFAULT_MAX_BYTES = 256 * 1024 * 1024


def json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def strict_json(value: bytes | str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-JSON number: {value}")

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = item
        return result

    return json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_keys)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def decompress(value: bytes, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """Decode concatenated frames and reject every incomplete physical frame."""
    result = bytearray()
    remaining = value
    while remaining:
        decoder = zstd.ZstdDecompressor().decompressobj()
        # Small compressed chunks limit transient allocations on compressible input.
        position = 0
        while position < len(remaining) and not decoder.eof:
            chunk = remaining[position : position + 1024]
            position += len(chunk)
            try:
                result.extend(decoder.decompress(chunk))
            except zstd.ZstdError as error:
                raise TrajectoryError(
                    "Corrupt Zstandard data; source files were retained"
                ) from error
            if len(result) > max_bytes:
                raise TrajectoryError(f"Decoded content exceeds {max_bytes} bytes")
        if not decoder.eof:
            raise TrajectoryError("Incomplete Zstandard frame; source files were retained")
        remaining = decoder.unused_data + remaining[position:]
    if not result:
        raise TrajectoryError("Empty compressed input")
    return bytes(result)


def compress(value: bytes) -> bytes:
    return zstd.ZstdCompressor(level=3, write_checksum=True).compress(value)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".dsh-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
