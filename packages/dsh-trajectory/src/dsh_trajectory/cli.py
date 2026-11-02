import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlencode

import zstandard

from dsh_trajectory import __version__
from dsh_trajectory.bundle import build_bundle, read_bundle, save_bundle
from dsh_trajectory.codec import DEFAULT_MAX_BYTES, atomic_write, digest, json_bytes
from dsh_trajectory.errors import TrajectoryError
from dsh_trajectory.pipeline import confirm_receipt, process
from dsh_trajectory.store import SQLiteStore
from dsh_trajectory.transport import HTTPStore, make_server


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise TrajectoryError(message, code=2)


def parser() -> argparse.ArgumentParser:
    root = Parser(description="DeepSeek Harness v4 to ATIF-v1.8 ingestion pipeline")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--non-interactive", action="store_true", help="Never prompt (the default)")
    root.add_argument("--format", choices=["json"], default="json")
    nouns = root.add_subparsers(dest="noun", required=True)
    trajectory = nouns.add_parser("trajectory", help="Convert downloaded native session logs")
    verbs = trajectory.add_subparsers(dest="verb", required=True)
    for verb in ("convert", "process"):
        command = verbs.add_parser(
            verb,
            help="Convert"
            if verb == "convert"
            else "Convert, ingest, verify receipt, then optionally clean up",
        )
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--trial-id", required=True)
        command.add_argument("--attempt-id", required=True)
        command.add_argument("--harness-version", required=True)
        command.add_argument("--workers", type=int, default=1)
        command.add_argument("--allow-incomplete", action="store_true")
        command.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
        command.add_argument("--dry-run", action="store_true")
        if verb == "process":
            add_sink(command)
            command.add_argument(
                "--cleanup",
                choices=["none", "sources", "all"],
                default="none",
                help="Delete converted sources/all staged data only after commit",
            )
    bundle = nouns.add_parser("bundle", help="Validate, ingest or export a compressed ATIF bundle")
    verbs = bundle.add_subparsers(dest="verb", required=True)
    for verb in ("validate", "ingest", "export"):
        command = verbs.add_parser(verb)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
        if verb != "validate":
            command.add_argument("--dry-run", action="store_true")
        if verb == "ingest":
            add_sink(command)
        if verb == "export":
            command.add_argument(
                "--output",
                type=Path,
                required=True,
                help="Directory for standard ATIF trajectory JSON files",
            )
    store = nouns.add_parser("store", help="Run or query the reference central store")
    verbs = store.add_subparsers(dest="verb", required=True)
    serve = verbs.add_parser("serve", help="Serve SQLite ingestion behind an HTTPS reverse proxy")
    serve.add_argument("--store", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--allow-http",
        action="store_true",
        help="Allow binding plain HTTP on non-loopback interfaces",
    )
    serve.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    serve.add_argument("--dry-run", action="store_true")
    for verb in ("search", "get"):
        command = verbs.add_parser(verb)
        add_sink(command)
        if verb == "search":
            command.add_argument("--query", required=True, help="SQLite FTS5 query")
            command.add_argument("--limit", type=int, default=20)
        else:
            command.add_argument("--trial-id", required=True)
            command.add_argument("--attempt-id", required=True)
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--dry-run", action="store_true")
    return root


def add_sink(command) -> None:
    target = command.add_mutually_exclusive_group(required=True)
    target.add_argument("--store", type=Path, help="SQLite database on the central machine")
    target.add_argument("--endpoint", help="Central ingestion service base URL")
    command.add_argument("--ca-file", type=Path)
    command.add_argument("--allow-http", action="store_true")
    command.add_argument("--timeout", type=float, default=120)


def sink_for(args):
    if args.store:
        return SQLiteStore(args.store, max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES))
    return HTTPStore(
        args.endpoint,
        token=os.environ.get("DSH_TRAJECTORY_TOKEN", ""),
        ca_file=args.ca_file,
        allow_http=args.allow_http,
        timeout=args.timeout,
    )


def execute(args) -> dict:
    if args.noun == "trajectory":
        options = {
            key: getattr(args, key)
            for key in (
                "trial_id",
                "attempt_id",
                "harness_version",
                "workers",
                "allow_incomplete",
                "max_bytes",
            )
        }
        if args.verb == "process":
            return process(
                args.input,
                args.output,
                sink_for(args),
                cleanup=args.cleanup,
                dry_run=args.dry_run,
                **options,
            )
        if args.dry_run:
            return {"dry_run": True, "input": str(args.input), "output": str(args.output)}
        conversion = build_bundle(args.input, **options)
        if args.output.resolve() in {s.path for s in conversion.sources}:
            raise TrajectoryError("Output would replace a source log")
        return save_bundle(conversion, args.output)
    if args.noun == "bundle":
        payload = args.input.read_bytes()
        bundle = read_bundle(payload, args.max_bytes)
        if args.verb == "validate":
            return {
                "valid": True,
                "sha256": digest(payload),
                "trajectories": len(bundle["trajectories"]),
            }
        if args.dry_run:
            return {"dry_run": True, "operation": args.verb, "sha256": digest(payload)}
        if args.verb == "ingest":
            receipt = sink_for(args).ingest(payload)
            confirm_receipt(receipt, bundle, payload)
            return receipt
        destinations = []
        for trajectory in bundle["trajectories"]:
            identity = trajectory["trajectory_id"]
            # UUID filenames are derived locally instead of trusting uploaded path strings.
            from uuid import UUID

            filename = str(UUID(identity)) + ".json"
            output = args.output / filename
            if output.exists() and output.read_bytes() != json_bytes(trajectory):
                raise TrajectoryError(
                    f"Export already exists with different content: {output}", code=5
                )
            atomic_write(output, json_bytes(trajectory))
            destinations.append(str(output))
        return {"exported": destinations}
    if args.verb == "serve":
        if args.host not in ("127.0.0.1", "localhost") and not args.allow_http:
            raise TrajectoryError("Remote plain HTTP binding requires --allow-http")
        if args.dry_run:
            return {"dry_run": True, "host": args.host, "port": args.port, "store": str(args.store)}
        server = make_server(
            SQLiteStore(args.store, max_bytes=args.max_bytes),
            args.host,
            args.port,
            os.environ.get("DSH_TRAJECTORY_TOKEN", ""),
            args.max_bytes,
        )
        with server:
            print(json.dumps({"listening": server.server_address}), file=sys.stderr, flush=True)
            server.serve_forever()
    sink = sink_for(args)
    if args.verb == "search":
        if isinstance(sink, SQLiteStore):
            return {"results": sink.search(args.query, args.limit)}
        return sink.request("/v1/search?" + urlencode({"q": args.query, "limit": args.limit}))
    if args.dry_run:
        return {"dry_run": True, "output": str(args.output)}
    payload = (
        sink.get(args.trial_id, args.attempt_id)
        if isinstance(sink, SQLiteStore)
        else sink.request(
            "/v1/bundles?" + urlencode({"trial_id": args.trial_id, "attempt_id": args.attempt_id}),
            binary=True,
        )
    )
    read_bundle(payload)
    atomic_write(args.output, payload)
    return {"bundle": str(args.output), "sha256": digest(payload)}


def main(argv: list[str] | None = None) -> int:
    try:
        result = execute(parser().parse_args(argv))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        error = TrajectoryError("Interrupted; retry with the same IDs and staging paths", code=6)
    except TrajectoryError as caught:
        error = caught
    except FileNotFoundError as caught:
        error = TrajectoryError(str(caught), code=3)
    except PermissionError as caught:
        error = TrajectoryError(str(caught), code=4)
    except (ValueError, KeyError, TypeError, zstandard.ZstdError) as caught:
        error = TrajectoryError(f"Invalid input: {caught}; source files retained")
    except (OSError, sqlite3.Error) as caught:
        error = TrajectoryError(f"I/O or storage failure: {caught}", code=11, retryable=True)
    print(
        json.dumps(
            {
                "error": type(error).__name__,
                "message": str(error),
                "retryable": error.retryable,
                "exit_code": error.code,
            }
        ),
        file=sys.stderr,
    )
    return error.code
