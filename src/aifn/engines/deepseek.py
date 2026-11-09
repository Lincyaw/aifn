from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from aifn.bundle import require_bundle, runtime_bin
from aifn.declare import Access
from aifn.engine import EngineCrashed, EngineTimedOut, Session, Transcript
from aifn.engines.aihub import is_aihub, session_api_key
from aifn.patch import Row, layer, write
from aifn.sandbox import NativeSandbox, SandboxedLaunch, SrtPolicy
from aifn.storage import canonical, component
from aifn.terms import Event
from aifn.trajectory import TrajectoryArchive

PROFILE: Final = "sdk"
"""Which profile to boot.

Backed by base, for its sandbox rows. `sdk-minimal` pins `danger-full-access` and
mounts nothing that confines a session, so a call there reads and writes anywhere
the runtime reaches, while the permission an `Access` declares only means something
under base's own rules.
"""

PATCHES: Final = "aifn-patches"
"""Where under the dsh home one call's layer is written.

In the harness home rather than beside the mailbox: the layer is what this launch
tells the runtime, its lifetime is the session, and a mailbox is the record of what
the calls themselves were.
"""


def _turn_error(events: Sequence[Event]) -> str:
    ended = next((event for event in reversed(events) if event.get("type") == "turn/end"), None)
    if ended is None:
        raise ValueError("Harness error finish has no turn/end event")
    data = ended["data"]
    if not isinstance(data, dict):
        raise ValueError("Harness turn/end data must be an object")
    reason = data["reason"]
    if not isinstance(reason, dict) or reason.get("kind") != "error":
        raise ValueError("Harness error finish disagrees with the last turn/end event")
    error = reason["error"]
    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
        raise ValueError("Harness turn/end error requires an object with a string message")
    return canonical(error)


@dataclass(frozen=True, slots=True)
class DeepSeekEngine:
    """Run fresh or explicitly resumed sessions through the official Python SDK."""

    dsh_home: Path
    env: Mapping[str, str] = field(default_factory=dict)
    """Environment variables handed to the runtime.

    `DSH_PERMISSION_MODE` is not among them: it comes from each function's declared
    `Access`, while this map is the credentials and endpoints one engine shares
    across every call it runs.
    """

    patches: tuple[Path, ...] = ()
    """Extra patch layers prepended to the one this engine writes per call."""

    dsh_bin: Path | None = None
    """What to launch instead of the bundled runtime, when a deployment wraps it.

    The launcher a worker hands to the runtime is where a deployment puts whatever the process
    has to have before the session starts, such as the filesystem the agent may see. The runtime
    that wrapper finally executes is its own business; this field only replaces the argv head.
    """

    sandbox: SrtPolicy | NativeSandbox | None = None
    """SRT grants, or `NativeSandbox` to leave the tools to the harness's own sandbox; None
    derives the SRT policy from the call's workspace.

    SRT confines file/shell providers. Its default private state lives at
    dsh_home/aifn-control; the trusted DSH controller stays outside.
    """
    archive: TrajectoryArchive | None = None
    trajectory_directory: Path | None = None
    """Native logs, outside task workspaces; defaults to dsh_home/trajectories."""

    def run(self, session: Session) -> Transcript:
        """Run one session.

        A per-session patch configures the task directory for the file tools.
        The worker provides validation through a local endpoint for this attempt.

        The SDK's `run` is synchronous, so this blocks until the session ends, and
        concurrency is several workers each running their own engine.
        """
        from deepseek_harness import DeepSeekHarness
        from deepseek_harness.errors import HarnessError
        from deepseek_harness.models import Notification

        if session.directory is None:
            raise EngineCrashed("DeepSeekEngine requires a workspace")
        if session.continues is not None and session.continues != session.id:
            raise ValueError("A resumed session must use the session id it continues")
        self.prepare()
        permission = Access(session.permission)
        component(session.id)
        workspace = session.directory.resolve()
        if (
            self.dsh_home.resolve().is_relative_to(workspace)
            or workspace.is_relative_to(self.dsh_home.resolve())
        ):
            raise EngineCrashed("DSH home must be separate from the task workspace")
        tools = dict(session.tools)
        receipt = self.dsh_home.resolve() / "aifn-receipts" / f"{component(session.id)}.json"
        if tools.get("validation") == "json-schema":
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.unlink(missing_ok=True)
            tools["receipt"] = str(receipt)
        trajectory_root = (self.trajectory_directory or self.dsh_home / "trajectories").resolve()
        if session.directory.resolve() in (
            trajectory_root, *trajectory_root.parents,
        ):
            raise EngineCrashed("Trajectory directory must be outside the task workspace")
        rows: list[Row] = [*layer(tools, permission)]
        rows.append({"id": "session-persistence-jsonl", "config": {"root": str(trajectory_root)}})
        patch = write(rows, self.dsh_home / PATCHES, session.id)
        if session.files is None:
            raise ValueError("Session has no task directory")
        environment = {
            **self.env,
            **session.env,
            "DSH_PERMISSION_MODE": permission.value,
            "AIFN_TASK_DIRECTORY": str(session.files.resolve()),
        }
        upstream = self.env.get("DEEPSEEK_BASE_URL", os.environ.get("DEEPSEEK_BASE_URL", ""))
        endpoint = environment.get("DEEPSEEK_BASE_URL", upstream)
        local_proxy = urlsplit(endpoint).hostname in {"127.0.0.1", "localhost", "::1"}
        if is_aihub(endpoint) or (local_proxy and is_aihub(upstream)):
            api_key = environment.get("DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
            if api_key:
                environment["DEEPSEEK_API_KEY"] = session_api_key(api_key, session.id)
        executable = runtime_bin(self.dsh_home) if self.dsh_bin is None else self.dsh_bin.resolve()
        home = self.dsh_home.resolve()
        selected: SandboxedLaunch | None = None
        if not isinstance(self.sandbox, NativeSandbox):
            sandbox = self.sandbox or SrtPolicy(state_root=home / "aifn-control")
            private = (trajectory_root, *((self.archive.staging,) if self.archive else ()))
            try:
                selected = sandbox.prepare(session, home, private)
            except subprocess.TimeoutExpired as error:
                raise EngineTimedOut("SRT isolation self-check timed out") from error
            except subprocess.CalledProcessError as error:
                raise EngineCrashed(f"SRT isolation self-check failed: {error.stderr}") from error
            except (OSError, ValueError, RuntimeError) as error:
                raise EngineCrashed(f"SRT preparation failed: {error}") from error
        harness = DeepSeekHarness(
            dsh_home=str(home),
            dsh_bin=str(executable),
            profile=PROFILE,
            patches=(*(str(path.resolve()) for path in self.patches), str(patch.resolve()),
                     *(() if selected is None else (str(selected.patch),))),
            cwd=str(session.directory),
            provider=session.model.provider,
            model=session.model.name,
            max_tokens=session.model.max_tokens,
            reasoning_effort=session.model.reasoning_effort,
            env=environment,
            initialize_timeout_seconds=session.timeout_s,
            request_timeout_seconds=session.timeout_s,
        )
        events: list[Event] = []
        expired = threading.Event()
        timer: threading.Timer | None = None

        def collect(notification: Notification) -> None:
            if (
                notification.method == "session.event"
                and notification.payload.get("sessionId") == session.id
            ):
                event = notification.payload.get("event")
                if isinstance(event, dict):
                    events.append(event)

        def expire() -> None:
            expired.set()
            harness.close()

        started = time.monotonic()
        archived = None
        try:
            harness.start()
            if selected is not None:
                try:
                    selected.check_ready()
                except RuntimeError as error:
                    raise EngineCrashed(str(error)) from error
            remaining = session.timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Session initialization exceeded its time limit")
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            if session.continues is None:
                active = harness.start_session(session.id)
            else:
                active = harness.resume_session(session.id)
                active.set_working_directory(str(workspace))
            result = active.run(session.prompt, on_notification=collect)
            if expired.is_set():
                raise TimeoutError("Session exceeded its time limit")
            if result.finish_reason == "error":
                raise EngineCrashed(_turn_error(result.events), tuple(result.events))
            submission = json.loads(receipt.read_text()) if (
                tools.get("validation") == "json-schema" and receipt.is_file()
            ) else None
            transcript = Transcript(session=session.id, events=tuple(result.events),
                                    submission=submission)
        except TimeoutError as error:
            raise EngineTimedOut(str(error), tuple(events)) from error
        except HarnessError as error:
            if expired.is_set():
                raise EngineTimedOut("Session exceeded its time limit", tuple(events)) from error
            raise EngineCrashed(str(error), tuple(events)) from error
        finally:
            failure = sys.exception()
            if timer is not None:
                timer.cancel()
                timer.join()
            harness.close()
            if self.archive is not None:
                try:
                    archived = self.archive.ingest_session(
                        trajectory_root, session.id, trial_id=session.call or session.id,
                        attempt_id=str(session.attempt),
                    )
                    if isinstance(failure, (EngineCrashed, EngineTimedOut)):
                        failure.archive_receipt = archived
                except Exception as error:
                    # Storage outages retain data without rerunning a paid model call.
                    logging.getLogger(__name__).warning(
                        "Trajectory archive retained for retry: %s", error,
                    )
        if archived is not None:
            transcript = replace(transcript, archive_receipt=archived)
        return transcript

    def prepare(self) -> None:
        """Check that the selected profile has the bundle installed."""
        if "DSH_PERMISSION_MODE" in self.env:
            raise ValueError("Permission comes from the call's workspace")
        require_bundle(self.dsh_home, profile=PROFILE)
