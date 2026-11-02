from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Final

from filelock import FileLock

BUNDLE_NAME: Final = "@aifn/dsh-aifn-harness"
"""The dsh bundle's package name.

The bundle holds the mechanism for answering one call and knows no AI function's
name, so this side asks the profile for it by this name and nothing else.
"""

BUNDLE_DIR: Final = (
    Path(__file__).resolve().parent / "harness"
    if (Path(__file__).resolve().parent / "harness").is_dir()
    else Path(__file__).resolve().parents[2] / "harness" / "aifn-harness"
)
"""Where that bundle's source is, which is what a profile is installed from."""

SRT_DIR: Final = (
    Path(__file__).resolve().parent / "srt"
    if (Path(__file__).resolve().parent / "srt").is_dir()
    else BUNDLE_DIR.parent / "dsh-access-policy"
)


def runtime_bin(dsh_home: Path) -> Path:
    directory = dsh_home.expanduser().resolve() / "aifn-runtime"
    manifest = directory / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    expected = json.loads((BUNDLE_DIR / "runtime" / "package.json").read_text())[
        "dependencies"
    ]["@deepseek-ai/dsh"]
    executable = directory / "node_modules" / ".bin" / (
        "dsh.cmd" if os.name == "nt" else "dsh"
    )
    if (
        not manifest.is_file()
        or json.loads(manifest.read_text())["version"] != expected
        or not executable.is_file()
    ):
        raise RuntimeError(
            f"Install dsh {expected} with: python -m aifn install "
            f"--dsh-home {shlex.quote(str(dsh_home.resolve()))} --reinstall"
        )
    return executable


def install_runtime(dsh_home: Path, *, reinstall: bool = False) -> Path:
    directory = dsh_home / "aifn-runtime"
    directory.mkdir(parents=True, exist_ok=True)
    changed = False
    # Keep launcher code next to this npm root so its pinned imports resolve here.
    for source in [SRT_DIR / "package.json", *(SRT_DIR / "src").glob("*.mjs")]:
        target = directory / "policy" / source.relative_to(SRT_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            shutil.copyfile(source, target)
            changed = True
    for name in ("package.json", "package-lock.json"):
        source = BUNDLE_DIR / "runtime" / name
        target = directory / name
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            shutil.copyfile(source, target)
            changed = True
    if reinstall or changed or not (directory / "node_modules" / ".package-lock.json").is_file():
        subprocess.run(
            ["npm.cmd" if os.name == "nt" else "npm", "ci", "--prefix", str(directory),
             "--no-audit", "--no-fund"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    return runtime_bin(dsh_home)


def install_bundle(dsh_home: Path, *, profile: str = "sdk", reinstall: bool = False) -> bool:
    """Install the bundle into one profile; false when it is already the current one.

    With a `file:` spec. pnpm copies the bundle into the profile's package tree,
    which is where its peer imports resolve from and what `link:` cannot give it.
    The copy means an edit reaches a session only after reinstalling, and `add` on
    an unchanged spec does nothing, so a reinstall removes first.

    Which profile matters because the bundle is what turns a session into a call: the `sdk`
    profile serves programmatic clients and the `acp` profile serves session-aware ones, and a
    call that has to reopen an earlier session runs under the second.
    """
    dsh_home = dsh_home.expanduser().resolve()
    dsh_home.mkdir(parents=True, exist_ok=True)
    with FileLock(str(dsh_home / ".aifn-install.lock")):
        executable = install_runtime(dsh_home, reinstall=reinstall)
        directory = dsh_home / "profiles" / profile
        manifest = directory / "package.json"
        installed = False
        if manifest.exists():
            installed = BUNDLE_NAME in json.loads(manifest.read_text()).get("dependencies", {})
        if installed and not reinstall:
            require_bundle(dsh_home, profile=profile)
            return False
        environment = {**os.environ, "DSH_HOME": str(dsh_home)}
        launch = (str(executable),)
        commands = []
        if not manifest.exists():
            commands.append(["--profile", profile, "--dump-default-config"])
        if installed:
            commands.append(["plugin", "--profile", profile, "remove", BUNDLE_NAME])
        commands.append(["plugin", "--profile", profile, "add", f"file:{BUNDLE_DIR}"])
        for command in commands:
            subprocess.run(
                [*launch, *command],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
        require_bundle(dsh_home, profile=profile)
        return True


def require_bundle(dsh_home: Path, *, profile: str = "sdk") -> None:
    """Report the installation command when the bundle is missing."""
    directory = dsh_home.expanduser().resolve() / "profiles" / profile
    installed = directory / "node_modules" / "@aifn" / "dsh-aifn-harness"
    manifest = directory / "package.json"
    if (
        not installed.is_dir()
        or not manifest.is_file()
        or BUNDLE_NAME not in json.loads(manifest.read_text()).get("dependencies", {})
    ):
        command = shlex.join(
            [
                "python",
                "-m",
                "aifn",
                "install",
                "--dsh-home",
                str(dsh_home.resolve()),
                "--profile",
                profile,
                "--reinstall",
            ]
        )
        raise RuntimeError(f"Install the aifn bundle with: {command}")
