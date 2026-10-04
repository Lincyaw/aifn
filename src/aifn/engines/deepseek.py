from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from aifn.bundle import require_bundle, runtime_bin
from aifn.declare import Access
from aifn.engine import EngineCrashed, EngineTimedOut, Session, Transcript
from aifn.patch import layer, write
from aifn.storage import component
from aifn.terms import Event

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


@dataclass(frozen=True, slots=True)
class DeepSeekEngine:
    """Run one session with DeepSeek Harness's Python SDK.

    One call is one session and one session is one `harness.run`: the same id
    continues the same durable conversation, so a second call reusing an id would
    arrive with the first call's tools and answers already in its history.
    """

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
        self.prepare()
        permission = Access(session.permission)
        component(session.id)
        rows = layer(dict(session.tools), permission)
        patch = write(rows, self.dsh_home / PATCHES, session.id)
        if session.files is None:
            raise ValueError("Session has no task directory")
        environment = {
            **self.env,
            **session.env,
            "DSH_PERMISSION_MODE": permission.value,
            "AIFN_TASK_DIRECTORY": str(session.files.resolve()),
        }
        harness = DeepSeekHarness(
            dsh_home=str(self.dsh_home.resolve()),
            dsh_bin=str(
                runtime_bin(self.dsh_home) if self.dsh_bin is None else self.dsh_bin.resolve()
            ),
            profile=PROFILE,
            patches=(*(str(path.resolve()) for path in self.patches), str(patch.resolve())),
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
        try:
            harness.start()
            remaining = session.timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Session initialization exceeded its time limit")
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            result = harness.run(session.prompt, session_id=session.id, on_notification=collect)
            if expired.is_set():
                raise TimeoutError("Session exceeded its time limit")
            if result.finish_reason == "error":
                raise EngineCrashed("Harness turn ended with an error", tuple(result.events))
            return Transcript(session=session.id, events=tuple(result.events))
        except TimeoutError as error:
            raise EngineTimedOut(str(error), tuple(events)) from error
        except HarnessError as error:
            if expired.is_set():
                raise EngineTimedOut("Session exceeded its time limit", tuple(events)) from error
            raise EngineCrashed(str(error), tuple(events)) from error
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            harness.close()

    def prepare(self) -> None:
        """Check that the selected profile has the bundle installed."""
        if "DSH_PERMISSION_MODE" in self.env:
            raise ValueError("Permission comes from the call's workspace")
        require_bundle(self.dsh_home, profile=PROFILE)
