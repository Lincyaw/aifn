from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from filelock import FileLock
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

from aifn.call import AnyOutcome, AnyReturn, Attempt, Call, Fault, Return, Returned
from aifn.storage import atomic_write, canonical, component, readable_json
from aifn.terms import CallId, Event, JsonObject, WorkerId

REQUESTS: Final = "requests"
CONCLUSIONS: Final = "conclusions"
CLAIMS: Final = "claims"
EVENTS: Final = "events"
SCRATCH: Final = "scratch"
DIRECTORIES: Final = (
    REQUESTS, CONCLUSIONS, CLAIMS, EVENTS, SCRATCH, "locks", "runs", "tasks", "results",
)


class LeaseLost(RuntimeError):
    """The lease expired or was replaced, so its worker can no longer update the task."""


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call: CallId
    worker: WorkerId
    claimed_at: AwareDatetime
    expires_at: AwareDatetime
    attempt: int = Field(default=0, ge=0)
    token: str = Field(default_factory=lambda: uuid4().hex)

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempts: tuple[Attempt, ...] = ()
    active: Attempt | None = None
    outcome: AnyOutcome | None = None


@dataclass(frozen=True, slots=True)
class Mailbox:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve())

    def prepare(self) -> None:
        for directory in DIRECTORIES:
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def _path(self, directory: str, call: CallId) -> Path:
        return self.root / directory / f"{component(call)}.json"

    def _lock(self, call: CallId) -> FileLock:
        return FileLock(str(self.root / "locks" / f"{component(call)}.lock"))

    def issue(self, call: Call) -> bool:
        self.prepare()
        with self._lock(call.id):
            path = self._path(REQUESTS, call.id)
            if path.exists():
                previous = Call.model_validate_json(path.read_text())
                if previous.model_dump(exclude={"issued_at"}) != call.model_dump(
                    exclude={"issued_at"}
                ):
                    raise ValueError(f"Call identity collision: {call.id}")
                return False
            if self._path(CONCLUSIONS, call.id).exists():
                return False
            task = self.task_path(call.id)
            atomic_write(task / "input.json", readable_json(dict(call.request)))
            atomic_write(task / "instruction.md", call.instruction)
            atomic_write(path, call.model_dump_json())
            return True

    def find(self, candidate: Call) -> Call | None:
        fields = candidate.model_dump(exclude={"id", "issued_at"})
        for path in (self.root / REQUESTS).glob("*.json"):
            call = Call.model_validate_json(path.read_bytes())
            if call.model_dump(exclude={"id", "issued_at"}) == fields:
                return call
        return None

    def enqueue(self, call: Call) -> tuple[Call, bool]:
        self.prepare()
        with FileLock(str(self.root / "locks" / "queue.lock")):
            existing = self.find(call)
            if existing is not None:
                return existing, False
            return call, self.issue(call)

    def task_path(self, call: CallId) -> Path:
        return self.root / "tasks" / component(call)

    def output_path(self, call: CallId) -> Path:
        return self.root / "results" / component(call) / "output.json"

    def contains(self, call: CallId) -> bool:
        return self._path(REQUESTS, call).exists() or self._path(CONCLUSIONS, call).exists()

    def pending(self) -> Iterator[Call]:
        calls = [
            Call.model_validate_json(path.read_text())
            for path in (self.root / REQUESTS).glob("*.json")
        ]
        for call in sorted(calls, key=lambda item: (item.issued_at, item.id)):
            with self._lock(call.id):
                if self._path(CONCLUSIONS, call.id).exists():
                    continue
                path = self._path(CLAIMS, call.id)
                if path.exists() and not Claim.model_validate_json(path.read_text()).expired(
                    datetime.now(UTC)
                ):
                    continue
            yield call

    def claim(self, call: CallId, worker: WorkerId, lease: timedelta) -> Claim | None:
        if lease.total_seconds() <= 0:
            raise ValueError("lease must be positive")
        with self._lock(call):
            if not self._path(REQUESTS, call).exists():
                raise KeyError(call)
            if self._path(CONCLUSIONS, call).exists():
                return None
            path = self._path(CLAIMS, call)
            now = datetime.now(UTC)
            if path.exists() and not Claim.model_validate_json(path.read_text()).expired(now):
                return None
            state = self.state(call)
            if state.active is not None:
                interrupted = state.active.model_copy(update={
                    "finished_at": now,
                    "fault": Fault.CRASHED,
                    "detail": "Worker lease expired before the attempt was completed",
                })
                state = state.model_copy(update={
                    "attempts": (*state.attempts, interrupted), "active": None,
                })
                self._save_state(call, state)
            claim = Claim(
                call=call, worker=worker, claimed_at=now, expires_at=now + lease,
                attempt=len(state.attempts),
            )
            atomic_write(path, claim.model_dump_json())
            return claim

    def _owned(self, claim: Claim) -> Claim:
        path = self._path(CLAIMS, claim.call)
        if not path.exists():
            raise LeaseLost(claim.call)
        current = Claim.model_validate_json(path.read_text())
        if current.token != claim.token or current.expired(datetime.now(UTC)):
            raise LeaseLost(claim.call)
        return current

    def renew(self, claim: Claim, lease: timedelta) -> Claim:
        if lease.total_seconds() <= 0:
            raise ValueError("lease must be positive")
        with self._lock(claim.call):
            renewed = self._owned(claim).model_copy(
                update={"expires_at": datetime.now(UTC) + lease}
            )
            atomic_write(self._path(CLAIMS, claim.call), renewed.model_dump_json())
            return renewed

    def release(self, claim: Claim) -> None:
        with self._lock(claim.call):
            self._owned(claim)
            state = self.state(claim.call)
            if state.active is not None:
                interrupted = state.active.model_copy(update={
                    "finished_at": datetime.now(UTC), "fault": Fault.CRASHED,
                    "detail": "Worker released an unfinished attempt",
                })
                self._save_state(claim.call, state.model_copy(update={
                    "active": None, "attempts": (*state.attempts, interrupted),
                }))
            self._path(CLAIMS, claim.call).unlink()

    def deliver(self, result: AnyReturn, claim: Claim) -> None:
        if result.id != claim.call:
            raise ValueError("Result does not belong to this claim")
        with self._lock(claim.call):
            self._owned(claim)
            call = Call.model_validate_json(self._path(REQUESTS, claim.call).read_text())
            if result.function != call.function:
                raise ValueError("Result function does not match the call")
            path = self._path(CONCLUSIONS, result.id)
            if path.exists():
                raise ValueError(f"Call already concluded: {result.id}")
            if isinstance(result.outcome, Returned):
                value = result.outcome.value
                payload = value.model_dump(mode="json", by_alias=True) if isinstance(
                    value, BaseModel
                ) else value
                atomic_write(self.output_path(result.id), readable_json(payload))
            atomic_write(path, result.model_dump_json(by_alias=True))
            self._path(CLAIMS, claim.call).unlink()

    def returned[O: BaseModel](self, call: CallId, returns: type[O]) -> Return[O] | None:
        path = self._path(CONCLUSIONS, call)
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        outcome = raw.get("outcome", {})
        if outcome.get("status") == "returned" and outcome.get("submitted_json") is not None:
            outcome["value"] = json.loads(outcome["submitted_json"])
        result = Return[returns].model_validate_json(canonical(raw))  # type: ignore[valid-type]
        if result.id != call:
            raise ValueError("Conclusion id does not match its filename")
        return result

    def record(
        self, call: CallId, attempt: int, events: Sequence[Event], *, claim: Claim | None = None,
        submission: JsonObject | None = None,
    ) -> Path:
        if attempt < 0:
            raise ValueError("attempt must be nonnegative")
        path = self.root / EVENTS / component(call) / f"{attempt}.jsonl"
        with self._lock(call):
            if claim is not None:
                if claim.call != call:
                    raise ValueError("Transcript does not belong to this claim")
                self._owned(claim)
            atomic_write(path, "".join(canonical(dict(event)) + "\n" for event in events))
            if submission is not None:
                atomic_write(path.with_suffix(".submission.json"), canonical(dict(submission)))
        return path

    def events(self, call: CallId, attempt: int) -> Sequence[Event]:
        path = self.root / EVENTS / component(call) / f"{attempt}.jsonl"
        adapter = TypeAdapter(list[dict[str, Any]])
        return adapter.validate_python([json.loads(line) for line in path.read_text().splitlines()])

    def state(self, call: CallId) -> RunState:
        path = self._path("runs", call)
        return RunState.model_validate_json(path.read_text()) if path.exists() else RunState()

    def _save_state(self, call: CallId, state: RunState) -> None:
        atomic_write(self._path("runs", call), state.model_dump_json(by_alias=True))

    def begin(self, claim: Claim, attempt: Attempt) -> None:
        with self._lock(claim.call):
            self._owned(claim)
            state = self.state(claim.call)
            if state.active is not None or state.outcome is not None:
                raise ValueError("Call already has an active attempt or a conclusion")
            if attempt.index != len(state.attempts):
                raise ValueError("Attempt index does not match durable history")
            self._save_state(claim.call, state.model_copy(update={"active": attempt}))

    def finish(self, claim: Claim, attempt: Attempt, outcome: AnyOutcome | None) -> None:
        with self._lock(claim.call):
            self._owned(claim)
            state = self.state(claim.call)
            if state.active is None or state.active.session != attempt.session:
                raise ValueError("Attempt does not match the active session")
            self._save_state(claim.call, RunState(
                attempts=(*state.attempts, attempt), outcome=outcome,
            ))

    def conclude(self, claim: Claim, outcome: AnyOutcome) -> None:
        with self._lock(claim.call):
            self._owned(claim)
            state = self.state(claim.call)
            if state.active is not None:
                raise ValueError("Cannot conclude an active attempt")
            self._save_state(claim.call, state.model_copy(update={"outcome": outcome}))
