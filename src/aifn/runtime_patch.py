"""A hash-checked compatibility patch for the pinned SRT 0.0.81 Linux backend."""

from __future__ import annotations

import stat
from hashlib import sha256
from pathlib import Path

from filelock import FileLock

from aifn.storage import atomic_write

PATCHED_SHA256 = "fd9d5ccc0a674a7b08a8297740e387a82d2fe1a4dacd0e3446e508b6fac227dd"
MOUNT_ORDER_SHA256 = "218822d3f73fbf1b0e27abcb9ee80431b8a6d44e776c025175a1908d2e0c7889"
UPSTREAM_SHA256 = "3880ba176ec09e5fdda364b1135fbccd291df5dcd9dd1c3c70da02e15f4f6621"


def patch_srt(runtime: Path) -> None:
    """Keep readonly parents from hiding explicitly writable child mounts.

    SRT emits denyWrite after this function, so all write denials still win.
    An unrecognized upstream source is refused before an agent starts.
    """
    source = runtime / (
        "node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/linux-sandbox-utils.js"
    )
    with FileLock(str(runtime / ".aifn-srt-patch.lock")):
        text = source.read_text()
        digest = sha256(text.encode()).hexdigest()
        if digest == PATCHED_SHA256:
            return
        if digest not in (UPSTREAM_SHA256, MOUNT_ORDER_SHA256):
            raise RuntimeError("Unrecognized SRT Linux source; review the pinned version")
        marker = "    // aifn: restore explicit writable children after readonly parent mounts."
        start = text.index("function pushReadDenyDirMounts(args, unit) {")
        tail = text.index("\n/**\n * Generate filesystem bind mount arguments", start)
        function = text[start:tail] if digest == MOUNT_ORDER_SHA256 else text[start:tail].replace(
            "        args.push('--bind', placement.source, placement.dest);\n", "", 1,
        ).replace(
            "    return { restoredWrites, restoredReads };",
            marker + "\n    for (const placement of restoredWrites) {\n"
            "        args.push('--bind', placement.source, placement.dest);\n"
            "    }\n    return { restoredWrites, restoredReads };", 1,
        )
        patched = text[:start] + function + text[tail:]
        readonly = (
            "    // aifn: deny-read scaffolding is readonly; "
            "explicit writable submounts stay writable.\n"
            "    for (const unit of readDenyTmpfsUnits) {\n"
            "        if (!isWithinAnyAllowedWritePath(unit.landing)) {\n"
            "            args.push('--remount-ro', unit.landing);\n"
            "        }\n"
            "    }\n"
        )
        patched = patched.replace("    return { args, mandatoryDenyArgs };",
                                  readonly + "    return { args, mandatoryDenyArgs };", 1)
        if sha256(patched.encode()).hexdigest() != PATCHED_SHA256:
            raise RuntimeError("SRT compatibility patch did not match its reviewed output")
        mode = stat.S_IMODE(source.stat().st_mode)
        atomic_write(source, patched)
        source.chmod(mode)
