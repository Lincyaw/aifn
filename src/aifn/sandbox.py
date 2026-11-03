"""Keep the trusted DSH controller outside SRT; confine its file/shell providers."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from aifn.declare import Access
from aifn.engine import Session
from aifn.patch import Row
from aifn.runtime_patch import patch_srt
from aifn.storage import atomic_write, component


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
            "network": {"allow": list(self.allow_hosts)},
            "environment": {"inherit": []},
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
