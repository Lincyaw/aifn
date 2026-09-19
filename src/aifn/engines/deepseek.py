from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from aifn.engine import Session, Transcript

BUNDLE_NAME: Final = "@aifn/dsh-aifn-harness"
"""The dsh bundle's package name.

The bundle holds the mechanism for answering one call and knows no AI function's
name, so this side asks the profile for it by this name and nothing else.
"""

BUNDLE_DIR: Final = Path(__file__).resolve().parents[3] / "harness" / "aifn-harness"
"""Where that bundle's source is, which is what a profile is installed from."""

PROFILE: Final = "sdk"
"""Which profile to boot.

Backed by base, for its sandbox rows. `sdk-minimal` pins `danger-full-access` and
mounts nothing that confines a session, so a call there reads and writes anywhere
the runtime reaches, while the permission an `Access` declares only means something
under base's own rules.
"""

STAMP: Final = ".aifn-bundle"
"""A digest of the bundle the profile was installed from, beside its manifest.

The bundle is copied in, so an edit reaches a session only after a reinstall. With
no digest recorded, a profile holding a different bundle than this source tree is
silent in the worst way: the agent answers the mechanism it was installed with,
and the run is spent finding out.
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

    def run(self, session: Session) -> Transcript:
        """Run one session.

        The tools reach the runtime as a `--patch` layer written for this session
        alone, because the submit tool's schema comes from the output type and the
        installed bundle holds no schema of its own.

        The SDK's `run` is synchronous, so this blocks until the session ends, and
        concurrency is several workers each running their own engine.
        """
        raise NotImplementedError

    def prepare(self) -> None:
        """Fail unless the profile holds the bundle this source tree currently is."""
        raise NotImplementedError


def install_bundle(dsh_home: Path, *, reinstall: bool = False) -> bool:
    """Install the bundle into the profile; false when it is already the current one.

    With a `file:` spec. pnpm copies the bundle into the profile's package tree,
    which is where its peer imports resolve from and what `link:` cannot give it.
    The copy means an edit reaches a session only after reinstalling, and `add` on
    an unchanged spec does nothing, so a reinstall removes first.
    """
    raise NotImplementedError


def require_bundle(dsh_home: Path) -> None:
    """Answer installed and current together, with the exact command that fixes either."""
    raise NotImplementedError


def fingerprint() -> str:
    """What the bundle is now, over every file a session reads."""
    raise NotImplementedError
