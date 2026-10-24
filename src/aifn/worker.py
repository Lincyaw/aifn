from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from aifn.call import AnyOutcome, AnyReturn, Attempt, Call, Failed, Fault, Refused, Return, Returned
from aifn.declare import Access, Registry
from aifn.engine import (
    Engine,
    EngineCrashed,
    EngineTimedOut,
    Session,
    Transcript,
    describe,
    faults_of,
)
from aifn.files import OutputService, Receipt, RefusalFile, TaskFiles
from aifn.mailbox import SCRATCH, Claim, LeaseLost, Mailbox
from aifn.policy import Corrections, Policy
from aifn.storage import canonical
from aifn.submit import bundle
from aifn.terms import WorkerId


@dataclass(frozen=True, slots=True)
class Worker:
    mailbox: Mailbox
    registry: Registry
    engine: Engine
    policy: Policy
    id: WorkerId

    def work(self, budget: int | None = None) -> int:
        if budget is not None and budget < 0:
            raise ValueError("budget must be nonnegative")
        self.mailbox.prepare()
        self.engine.prepare()
        completed = 0
        while budget is None or completed < budget:
            if self.take_one() is None:
                break
            completed += 1
        return completed

    def serve(
        self, *, stop: threading.Event | None = None,
        poll_every: timedelta = timedelta(seconds=1),
    ) -> int:
        """Consume tasks continuously, finishing the current call before stopping."""
        interval = poll_every.total_seconds()
        if interval <= 0:
            raise ValueError("poll_every must be positive")
        signal = stop if stop is not None else threading.Event()
        self.mailbox.prepare()
        self.engine.prepare()
        completed = 0
        while not signal.is_set():
            if self.take_one() is None:
                signal.wait(interval)
            else:
                completed += 1
        return completed

    def take_one(self) -> AnyReturn | None:
        self.mailbox.prepare()
        for call in self.mailbox.pending():
            function = self.registry.lookup(call.function)
            claim = self.mailbox.claim(call.id, self.id, self.policy.lease)
            if claim is None:
                continue
            outcome = self.execute(call, claim)
            with _Heartbeat(self.mailbox, claim, self.policy.lease) as heartbeat:
                result: AnyReturn = Return[Any](
                    id=call.id, function=call.function, outcome=outcome,
                    attempts=self.mailbox.state(call.id).attempts, finished_at=datetime.now(UTC),
                )
                self.mailbox.deliver(result, claim)
                heartbeat.completed = True
            return self.mailbox.returned(call.id, function.returns)
        return None

    def execute(self, call: Call, claim: Claim) -> AnyOutcome:
        function = self.registry.lookup(call.function)
        policy = replace(self.policy, attempts=min(self.policy.attempts, function.attempts))
        with _Heartbeat(self.mailbox, claim, policy.lease) as heartbeat:
            state = self.mailbox.state(call.id)
            if state.outcome is not None:
                return state.outcome
            faults = [item.fault for item in state.attempts if item.fault is not None]
            corrections = Corrections()
            for item in state.attempts:
                if item.fault is not None:
                    corrections = corrections.after(item.fault, item.detail)
            while policy.again(faults):
                delay = policy.wait_s(len(state.attempts))
                if state.attempts:
                    elapsed = (datetime.now(UTC) - state.attempts[-1].finished_at).total_seconds()
                    delay = max(0, delay - elapsed)
                if delay:
                    time.sleep(delay)
                heartbeat.check()
                attempt, outcome = self.attempt(call, len(state.attempts), corrections, claim=claim)
                heartbeat.check()
                if outcome is not None:
                    return outcome
                if attempt.fault is None:
                    raise RuntimeError("Unsettled attempt must carry a fault")
                faults.append(attempt.fault)
                corrections = corrections.after(attempt.fault, attempt.detail)
                state = self.mailbox.state(call.id)
            if not state.attempts or state.attempts[-1].fault is None:
                raise RuntimeError("Retry policy stopped without a failed attempt")
            failure = Failed(fault=state.attempts[-1].fault, detail=state.attempts[-1].detail)
            self.mailbox.conclude(claim, failure)
            return failure

    def attempt(
        self, call: Call, index: int, corrections: Corrections, *, claim: Claim,
    ) -> tuple[Attempt, AnyOutcome | None]:
        function = self.registry.lookup(call.function)
        # A continuing call is prompted in the session it names; a retry of it stays there too.
        session_id = call.continues or f"{call.id}-attempt-{index}-{uuid4().hex}"
        started = datetime.now(UTC)
        active = Attempt(
            index=index, session=session_id, worker=self.id,
            started_at=started, finished_at=started,
        )
        self.mailbox.begin(claim, active)
        try:
            workspace = call.workspace or function.workspace
            directory = workspace.materialize(call.id, self.mailbox.root / SCRATCH) if (
                workspace is not None
            ) else None
            session = self.session(call, index, corrections, directory, session_id=session_id)
        except (OSError, ValueError) as error:
            completed = active.model_copy(update={
                "finished_at": datetime.now(UTC), "fault": Fault.SETUP, "detail": str(error),
            })
            self.mailbox.record(call.id, index, (), claim=claim)
            self.mailbox.finish(claim, completed, None)
            return completed, None
        transcript: Transcript
        try:
            if session.files is None:
                transcript = self.engine.run(session)
            else:
                with OutputService(TaskFiles(session.files), function.returns) as service:
                    session = replace(session, env={
                        "AIFN_OUTPUT_ENDPOINT": service.endpoint,
                        "AIFN_OUTPUT_TOKEN": service.token,
                    })
                    transcript = self.engine.run(session)
                    if service.files.accepted.exists():
                        receipt = Receipt.model_validate_json(service.files.accepted.read_bytes())
                        transcript = replace(transcript, submission=receipt.model_dump(mode="json"))
        except (EngineCrashed, EngineTimedOut, ConnectionError, TimeoutError) as error:
            fault = faults_of(error)
            events = error.events if isinstance(error, (EngineCrashed, EngineTimedOut)) else ()
            self.mailbox.record(call.id, index, events, claim=claim)
            result: AnyOutcome = Failed(fault=fault, detail=str(error))
        else:
            if transcript.session != session.id:
                raise ValueError("Engine returned another session's transcript")
            self.mailbox.record(call.id, index, transcript.events, claim=claim,
                                submission=transcript.submission)
            result = self.read(call, transcript)
        completed = active.model_copy(update={
            "finished_at": datetime.now(UTC),
            "fault": result.fault if isinstance(result, Failed) else None,
            "detail": result.detail if isinstance(result, Failed) else "",
        })
        conclusion = None if isinstance(result, Failed) else result
        self.mailbox.finish(claim, completed, conclusion)
        return completed, conclusion

    def session(
        self, call: Call, index: int, corrections: Corrections, directory: Path | None,
        *, session_id: str | None = None,
    ) -> Session:
        function = self.registry.lookup(call.function)
        workspace = call.workspace or function.workspace
        session_id = session_id or f"{call.id}-attempt-{index}-{uuid4().hex}"
        if directory is None:
            return Session(
                id=session_id, prompt="", directory=None, permission=Access.READ_ONLY,
                model=function.model, tools={}, timeout_s=function.timeout_s, env={},
                call=call.id, attempt=index, request=call.request, continues=call.continues,
            )
        # One attempt's task directory, which is not the session's: a call that continues a
        # session keeps that id across retries, and a retry that reused the directory would find
        # its own previous attempt's files already there.
        files = TaskFiles(self.mailbox.task_path(call.id) / "attempts" / f"{index}-{session_id}")
        instruction = call.instruction + (
            f"\n\n## Task files\n\n"
            f"Read input from `{files.root / 'input.json'}`.\n"
            f"Read the required JSON Schema from `{files.root / 'output.schema.json'}`.\n"
            f"Read previous attempt feedback from `{files.root / 'feedback.json'}`.\n"
            f"Write your JSON answer to `{files.output}` using write.\n"
            "Use edit or str_replace_editor for targeted corrections.\n"
            "Read existing files before editing them. Use absolute paths with file tools.\n"
            "The output directory is writable. Business workspace permissions still apply.\n"
            "Use validate_output to check the file and correct all reported errors.\n"
            "Use submit_output to validate and submit the final file.\n"
            f"If unanswerable, write `{files.refusal}` using the schema in "
            f"`{files.root / 'refusal.schema.json'}` and call refuse_output.\n"
            "Validation and submission accept absolute paths or output.json and refusal.json.\n"
        )
        instruction += f"The working directory is `{directory}`.\n"
        files.prepare(instruction, dict(call.request), function.returns.model_json_schema(),
                      corrections.reasons)
        tools = bundle(files.root)
        prompt = f"Read `{files.root / 'instruction.md'}` and execute the task in that file."
        return Session(
            id=session_id, prompt=prompt, directory=directory,
            permission=workspace.access if workspace is not None else Access.READ_ONLY,
            model=function.model, tools=tools,
            timeout_s=function.timeout_s, env={}, call=call.id, attempt=index, files=files.root,
            request=call.request, continues=call.continues,
        )

    def read(self, call: Call, transcript: Transcript) -> AnyOutcome:
        function = self.registry.lookup(call.function)
        try:
            if transcript.submission is None:
                return Failed(fault=Fault.NO_ANSWER, detail=describe(transcript.events))
            receipt = Receipt.model_validate(dict(transcript.submission))
            if receipt.status == "refused":
                return Refused(reason=RefusalFile.model_validate(receipt.value).reason)
            value = function.returns.model_validate_json(
                receipt.submitted_json or canonical(receipt.value)
            )
            return Returned(value=value, submitted_json=receipt.submitted_json)
        except ValidationError as error:
            return Failed(fault=Fault.INVALID, detail=str(error))


RENEW_RETRY_S = 5.0
"""How long a heartbeat waits before it retries a failed lease renewal."""


class _Heartbeat:
    def __init__(self, mailbox: Mailbox, claim: Claim, lease: timedelta) -> None:
        self.mailbox = mailbox
        self.claim = claim
        self.lease = lease
        self.stop = threading.Event()
        self.result: Future[None] = Future()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.completed = False

    def _run(self) -> None:
        try:
            while not self.stop.wait(self.lease.total_seconds() / 3):
                if not self._renew():
                    break
        except BaseException as error:
            self.result.set_exception(error)
        else:
            self.result.set_result(None)

    def _renew(self) -> bool:
        """Renew the lease, retrying a failed renewal while the current lease still holds.

        A store that answers late, as on an overloaded host, keeps the claim as long as one
        renewal lands before the lease runs out. Returns False when stopped while retrying.
        """
        expires = time.monotonic() + self.lease.total_seconds() * 2 / 3
        while True:
            started = time.monotonic()
            try:
                self.mailbox.renew(self.claim, self.lease)
                return True
            except LeaseLost:
                raise
            except Exception:
                if started + RENEW_RETRY_S >= expires:
                    raise
            if self.stop.wait(RENEW_RETRY_S):
                return False

    def check(self) -> None:
        if self.result.done():
            self.result.result()

    def __enter__(self) -> _Heartbeat:
        self.mailbox.renew(self.claim, self.lease)
        self.thread.start()
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop.set()
        self.thread.join()
        if error is None:
            # Successful delivery consumes the claim while its final renewal may still run.
            if self.completed and isinstance(self.result.exception(), LeaseLost):
                return
            self.check()
