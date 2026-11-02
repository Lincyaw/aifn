import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dsh_trajectory.bundle import read_bundle
from dsh_trajectory.codec import DEFAULT_MAX_BYTES, digest, json_bytes
from dsh_trajectory.errors import TrajectoryError

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    trial_id TEXT NOT NULL, attempt_id TEXT NOT NULL, sha256 TEXT NOT NULL,
    bundle BLOB NOT NULL, PRIMARY KEY(trial_id, attempt_id)
);
CREATE TABLE IF NOT EXISTS trajectories (
    trial_id TEXT NOT NULL, attempt_id TEXT NOT NULL, trajectory_id TEXT NOT NULL,
    session_id TEXT NOT NULL, parent_id TEXT, status TEXT NOT NULL, step_count INTEGER NOT NULL,
    PRIMARY KEY(trial_id, attempt_id, trajectory_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS steps USING fts5(
    trial_id UNINDEXED, attempt_id UNINDEXED, trajectory_id UNINDEXED, step_id UNINDEXED,
    source UNINDEXED, model, message, reasoning, tools, observations,
    tokenize='unicode61'
);
"""


class SQLiteStore:
    """Durable reference sink; deploy on the central machine, on a local filesystem."""

    def __init__(self, path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES):
        self.path = path.resolve()
        self.max_bytes = max_bytes
        self.identity = "sqlite:" + str(self.path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=60)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(DDL)
            yield connection
        finally:
            connection.close()

    def ingest(self, payload: bytes) -> dict:
        bundle = read_bundle(payload, self.max_bytes)
        identity = (bundle["trial_id"], bundle["attempt_id"])
        # Logical-content hashing makes compression-level differences idempotent.
        content_hash = digest(json_bytes(bundle))
        with self.connect() as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT sha256 FROM runs WHERE trial_id=? AND attempt_id=?", identity
                ).fetchone()
                if previous:
                    if previous["sha256"] != content_hash:
                        raise TrajectoryError(
                            "This trial/attempt already has different content; "
                            "use a new attempt ID",
                            code=5,
                        )
                else:
                    connection.execute(
                        "INSERT INTO runs VALUES (?,?,?,?)", (*identity, content_hash, payload)
                    )
                    for trajectory in bundle["trajectories"]:
                        self._index(connection, identity, trajectory)
            # The context manager committed with synchronous=FULL before this receipt.
        return {
            "committed": True,
            "trial_id": identity[0],
            "attempt_id": identity[1],
            "content_sha256": content_hash,
            "payload_sha256": digest(payload),
            "trajectories": len(bundle["trajectories"]),
            "duplicate": bool(previous),
        }

    @staticmethod
    def _index(connection: sqlite3.Connection, identity: tuple[str, str], trajectory: dict) -> None:
        metadata = trajectory.get("extra", {}).get("dsh", {})
        connection.execute(
            "INSERT INTO trajectories VALUES (?,?,?,?,?,?,?)",
            (
                *identity,
                trajectory["trajectory_id"],
                trajectory["session_id"],
                metadata.get("parent_trajectory_id"),
                metadata.get("status", "unknown"),
                len(trajectory["steps"]),
            ),
        )
        rows = []
        for step in trajectory["steps"]:
            rows.append(
                (
                    *identity,
                    trajectory["trajectory_id"],
                    step["step_id"],
                    step["source"],
                    step.get("model_name", ""),
                    step["message"]
                    if isinstance(step["message"], str)
                    else json.dumps(step["message"], ensure_ascii=False),
                    step.get("reasoning_content", ""),
                    json.dumps(step.get("tool_calls", []), ensure_ascii=False),
                    json.dumps(step.get("observation", {}), ensure_ascii=False),
                )
            )
        connection.executemany("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise TrajectoryError("Search limit must be between 1 and 1000")
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT trial_id,attempt_id,trajectory_id,step_id,source,model,message "
                "FROM steps WHERE steps MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get(self, trial_id: str, attempt_id: str) -> bytes:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT bundle FROM runs WHERE trial_id=? AND attempt_id=?", (trial_id, attempt_id)
            ).fetchone()
            if row is None:
                raise TrajectoryError("No stored bundle for trial/attempt", code=3)
            return row["bundle"]
