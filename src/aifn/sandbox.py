"""Keep the trusted DSH controller outside SRT; confine its file/shell providers."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

from aifn.declare import Access
from aifn.engine import Session
from aifn.patch import Row
from aifn.runtime_patch import patch_srt
from aifn.storage import atomic_write, component

RESULT_TOOLS: Final = ("validate_output", "submit_output", "refuse_output")
"""aifn's result protocol: a tool set without these cannot deliver an answer at all."""
DEPLOYMENT_TOOLS: Final = (
    "read", "write", "edit", "str_replace_editor", "bash", *RESULT_TOOLS,
)
"""The tool set a call gets when it declares none: dsh's file and editor tools plus the above."""


@dataclass(frozen=True, slots=True)
class SandboxedLaunch:
    patch: Path
    ready: Path
    nonce: str

    def check_ready(self) -> None:
        deadline = time.monotonic() + 3
        while not self.ready.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.ready.is_file() or (
            json.loads(self.ready.read_text()).get("nonce") != self.nonce
        ):
            raise RuntimeError("SRT providers did not become ready; refusing prompt")


@dataclass(frozen=True, slots=True)
class SrtPolicy:
    """Grant only task data and scratch to agent tools.

    state_root contains controller configuration, outside every task directory.
    Only its per-session scratch child is mounted into the agent namespaces.
    Native trajectories stay in DeepSeekEngine.trajectory_directory.
    """

    state_root: Path
    read: tuple[Path, ...] = ()
    write: tuple[Path, ...] = ()
    runtime_read: tuple[Path, ...] = ()
    allow_hosts: tuple[str, ...] = ()
    unix_sockets: tuple[Path, ...] = ()
    """Unix socket paths the agent's tools may connect to, such as a container runtime's.

    Granting one costs more on Linux than on macOS and the difference is not ours to hide: the
    kernel filter SRT uses there decides on the syscall, which carries no path, so a grant for
    one socket is a grant for every socket the namespace can see. macOS filters by path and
    receives exactly these. Declare the sockets a call needs either way, and read the Linux grant
    as what it is: every socket reachable from the session.
    """
    local_ports: bool = False
    """Whether the agent's tools may reach services the deployment runs on localhost.

    A deployment that answers its agent's queries over a local endpoint has to allow this; one
    whose agent only talks to the model and the files it was granted does not.
    """
    tools: tuple[str, ...] = DEPLOYMENT_TOOLS
    """Which registered tools the agent may call.

    The same kind of grant as the file and network ones, so it travels in the same policy file
    and is read in the same place. The default is the deployment set: dsh's file and editor
    tools, `bash`, and aifn's three result tools. Replacing it narrows a call to the tools its
    task actually needs; `RESULT_TOOLS` must stay in it, because a call that cannot submit or
    refuse has no way to deliver an answer, and that failure would only show up as a session
    that ran to its timeout.
    """
    mcp_servers: tuple[str, ...] = ()
    """Which MCP servers' tools the agent may call, by the `serverName` they are mounted under.

    Separate from `tools`, by server rather than by tool name, because the mcp-client plugin
    owns the `mcp__<serverName>__` namespace and composes each public name from the raw one,
    normalizing or hashing the tail when it does not fit the model's function-name contract:
    the namespace is what a caller mounting the server can predict, and it is exactly as wide
    as the mount it goes with. A mounted server is advertised to the model and then refused at
    its first tool call until it is named here.
    """

    def __post_init__(self) -> None:
        # Here rather than at prepare(): a declaration missing the result protocol is a mistake
        # in the deployment, and a session that discovers it has already spent the model call.
        missing = [name for name in RESULT_TOOLS if name not in self.tools]
        if missing:
            raise ValueError(
                f"A declared SRT tool set must keep the result protocol: add {', '.join(missing)}"
            )

    def prepare(
        self, session: Session, trusted_home: Path, private_paths: tuple[Path, ...] = (),
    ) -> SandboxedLaunch:
        if sys.platform != "linux":
            raise RuntimeError("SRT execution requires Linux; schema mode works on other systems")
        if session.directory is None or session.files is None:
            raise ValueError("SRT needs a workspace and a task directory")
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("SRT needs Node.js >=22.19 on PATH")
        for root in (*self.read, *self.write, *self.runtime_read):
            if root.expanduser().absolute() != root.expanduser().resolve():
                raise ValueError("Configure canonical real paths for SRT grants; no symlink roots")
        base = self.state_root.expanduser().absolute()
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        if base.resolve() != base:
            raise ValueError("SRT state_root must be a canonical path")
        workspace = session.directory.resolve()
        runtime = trusted_home.resolve() / "aifn-runtime"
        patch_srt(runtime)
        for private in (base, *(p.resolve() for p in private_paths)):
            for code in (runtime, Path(node).resolve()):
                if private.is_relative_to(code) or code.is_relative_to(private):
                    raise ValueError("Private state/trajectory must be separate from runtime code")
        # Reject broad grants that would reveal config, profiles, receipts, or trajectory.
        protected = [base, trusted_home.resolve(), *(p.resolve() for p in private_paths)]
        grants = [*self.read, *self.write, workspace, session.files]
        if session.files.resolve().is_relative_to(workspace):
            raise ValueError("Task exchange files must be separate from the business workspace")
        for root in grants:
            for private in protected:
                if private.is_relative_to(root.resolve()) or root.resolve().is_relative_to(private):
                    raise ValueError("Task data grants must be disjoint from controller paths")
        runtime_read = self.runtime_read or (
            *(p for p in (Path("/usr/bin"), Path("/usr/lib"), Path("/usr/lib64")) if p.exists()),
            *(Path(binary).resolve() for name in ("bash", "bwrap", "socat", "rg")
              if (binary := shutil.which(name))),
        )
        for root in runtime_read:
            if any(p.is_relative_to(root.resolve()) or root.resolve().is_relative_to(p)
                   for p in protected):
                raise ValueError("Runtime read grants must not expose controller directories")
        control = base / component(session.id)
        control.mkdir(mode=0o700, exist_ok=True)
        if control.resolve() != control:
            raise ValueError("SRT control directory must not be a symlink")
        scratch = control / "scratch"
        scratch.mkdir(mode=0o700, exist_ok=True)
        if scratch.resolve() != scratch:
            raise ValueError("SRT scratch directory must not be a symlink")
        writable = [*self.write, session.files / "output"]
        if Access(session.permission) != Access.READ_ONLY:
            writable.append(workspace)
        policy = {
            "version": 1, "cwd": str(workspace), "stateDirectory": str(scratch),
            "files": {
                "read": [str(p.resolve()) for p in (*self.read, workspace, session.files)],
                "write": [str(p.resolve()) for p in writable],
                "runtimeRead": [str(p.resolve()) for p in (
                    *runtime_read, Path(node).resolve(), runtime,
                )],
            },
            "network": {
                "allow": list(self.allow_hosts),
                "unixSockets": [str(p.resolve()) for p in self.unix_sockets],
                "localPorts": self.local_ports,
            },
            "environment": {"inherit": []},
            "tools": {"allow": list(self.tools), "mcpServers": list(self.mcp_servers)},
        }
        policy_file = control / "policy.json"
        atomic_write(policy_file, json.dumps(policy))
        cli = runtime / "policy" / "src" / "cli.mjs"
        # Check the real kernel boundary before DSH can receive any agent prompt.
        subprocess.run([node, str(cli), "doctor", "--policy", str(policy_file)],
                       check=True, capture_output=True, text=True, timeout=30)
        ready = control / "ready.json"
        ready.unlink(missing_ok=True)
        nonce = uuid4().hex
        module = runtime / "policy" / "src"
        rows: list[Row] = [
            {"id": name, "disabled": True} for name in (
                "sandbox", "bash-sandbox", "pwsh-sandbox", "fs-sandbox", "aifn-filesystem",
            )
        ]
        rows.append({"insert": [
            {"id": "aifn-srt-boundary", "name": str(module / "services.mjs"), "config": {
                "policyFile": str(policy_file), "readyFile": str(ready), "nonce": nonce,
            }},
            {"id": "aifn-srt-fs", "name": str(module / "remote-fs.mjs")},
            {"id": "aifn-srt-shell", "name": str(module / "shell.mjs")},
        ]})
        patch = control / "cordis.patch.json"
        atomic_write(patch, json.dumps(rows))
        return SandboxedLaunch(patch=patch, ready=ready, nonce=nonce)
