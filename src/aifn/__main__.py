from __future__ import annotations

import argparse
import importlib
import signal
import sys
import threading
from pathlib import Path
from types import FrameType

from aifn.worker import Worker


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "trajectory":
        from dsh_trajectory.cli import main as trajectory_main

        arguments = sys.argv[2:]
        if arguments and arguments[0] in ("convert", "process"):
            arguments.insert(0, "trajectory")
        raise SystemExit(trajectory_main(arguments))
    parser = argparse.ArgumentParser(prog="python -m aifn")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--dsh-home", type=Path, required=True)
    install.add_argument("--profile", default="sdk")
    install.add_argument("--reinstall", action="store_true")
    commands.add_parser(
        "trajectory", add_help=False, help="Convert/archive/query ATIF trajectories",
    )
    worker = commands.add_parser("worker")
    worker.add_argument("factory", help="module:function returning a Worker")
    worker.add_argument("--id", required=True)
    worker.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.command == "install":
        from aifn.engines import install_bundle

        install_bundle(args.dsh_home, profile=args.profile, reinstall=args.reinstall)
        return
    module, separator, name = args.factory.partition(":")
    if not separator:
        parser.error("factory must be module:function")
    instance = getattr(importlib.import_module(module), name)(args.id)
    if not isinstance(instance, Worker):
        raise TypeError("factory must return a Worker")
    stop = threading.Event()

    def shutdown(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if args.once:
        instance.work()
    else:
        instance.serve(stop=stop)


if __name__ == "__main__":
    main()
