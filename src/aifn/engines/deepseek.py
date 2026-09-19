from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from filelock import FileLock

from aifn.declare import Access
from aifn.engine import EngineCrashed, EngineTimedOut, Session, Transcript
from aifn.patch import layer, write
from aifn.storage import component
from aifn.terms import Event

BUNDLE_NAME: Final = "@aifn/dsh-aifn-harness"
"""The dsh bundle's package name.

The bundle holds the mechanism for answering one call and knows no AI function's
name, so this side asks the profile for it by this name and nothing else.
"""

BUNDLE_DIR: Final = (
    Path(__file__).resolve().parents[1] / "harness"
    if (Path(__file__).resolve().parents[1] / "harness").is_dir()
    else Path(__file__).resolve().parents[3] / "harness" / "aifn-harness"
)
"""Where that bundle's source is, which is what a profile is installed from."""

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

    def run(self, session: Session) -> Transcript:
        """Run one session.

        A per-session patch configures the task directory for the file tools.
        The worker provides validation through a local endpoint for this attempt.

        The SDK's `run` is synchronous, so this blocks until the session ends, and
        concurrency is several workers each running their own engine.
        """
        from deepseek_harness import DeepSeekHarness
        from deepseek_harness.errors import TransportClosedError
        from deepseek_harness.models import Notification

        self.prepare()
        permission = Access(session.permission)
        component(session.id)
        rows = layer(dict(session.tools), permission)
        patch = write(rows, self.dsh_home / PATCHES, session.id)
        environment = {**self.env, **session.env, "DSH_PERMISSION_MODE": permission.value}
        harness = DeepSeekHarness(
            dsh_home=str(self.dsh_home.resolve()), profile=PROFILE,
            patches=(*(str(path.resolve()) for path in self.patches), str(patch.resolve())),
            cwd=str(session.directory), provider=session.model.provider, model=session.model.name,
            max_tokens=session.model.max_tokens, reasoning_effort=session.model.reasoning_effort,
            env=environment, initialize_timeout_seconds=session.timeout_s,
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
        except TransportClosedError as error:
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
        require_bundle(self.dsh_home)


def install_bundle(dsh_home: Path, *, reinstall: bool = False) -> bool:
    """Install the bundle into the profile; false when it is already the current one.

    With a `file:` spec. pnpm copies the bundle into the profile's package tree,
    which is where its peer imports resolve from and what `link:` cannot give it.
    The copy means an edit reaches a session only after reinstalling, and `add` on
    an unchanged spec does nothing, so a reinstall removes first.
    """
    from deepseek_harness_runtime import resolve_bundled_launch_args

    dsh_home = dsh_home.expanduser().resolve()
    dsh_home.mkdir(parents=True, exist_ok=True)
    with FileLock(str(dsh_home / ".aifn-install.lock")):
        profile = dsh_home / "profiles" / PROFILE
        manifest = profile / "package.json"
        installed = False
        if manifest.exists():
            installed = BUNDLE_NAME in json.loads(manifest.read_text()).get("dependencies", {})
        if installed and not reinstall:
            require_bundle(dsh_home)
            return False
        environment = {**os.environ, "DSH_HOME": str(dsh_home)}
        launch = resolve_bundled_launch_args()
        commands = []
        if not manifest.exists():
            commands.append(["--profile", PROFILE, "--dump-default-config"])
        if installed:
            commands.append(["plugin", "--profile", PROFILE, "remove", BUNDLE_NAME])
        commands.append(["plugin", "--profile", PROFILE, "add", f"file:{BUNDLE_DIR}"])
        for command in commands:
            subprocess.run([*launch, *command], env=environment, check=True, capture_output=True,
                           text=True, timeout=300)
        require_bundle(dsh_home)
        return True


def require_bundle(dsh_home: Path) -> None:
    """Report the installation command when the bundle is missing."""
    profile = dsh_home.expanduser().resolve() / "profiles" / PROFILE
    installed = profile / "node_modules" / "@aifn" / "dsh-aifn-harness"
    manifest = profile / "package.json"
    if (
        not installed.is_dir()
        or not manifest.is_file()
        or BUNDLE_NAME not in json.loads(manifest.read_text()).get("dependencies", {})
    ):
        command = shlex.join([
            "python", "-m", "aifn", "install", "--dsh-home", str(dsh_home.resolve()), "--reinstall",
        ])
        raise RuntimeError(f"Install the aifn bundle with: {command}")
