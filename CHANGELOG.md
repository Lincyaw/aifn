# Changelog

## 0.2.0 — 2026-11-02

- Add SRT-backed filesystem and Bash providers. Task data, private controller configuration, submission receipts, and trajectories use separate directories.
- Add Node JSON Schema validation with private JSON result transport and nested Pydantic object construction. Unchanged output content reuses validation results; the existing Python validation mode remains the default.
- Add the `dsh-trajectory` 0.1.0 package: DSH v4 JSONL/Zstd to ATIF v1.8 conversion, idempotent SQLite/HTTP ingestion, and cleanup after a matching committed receipt.
- Allow mailbox events to retain a central archive pointer after successful ingestion.
- Fix Linux transport path lengths, proxy loopback resolution, writable-child mount ordering, and readonly isolation scaffolding. SRT 0.0.81 compatibility changes are SHA-256 checked before application.

Validated on Linux 6.6 with Node 24 and Python 3.14: 15 existing tests, lint/type checks, and a real DSH/SRT end-to-end run with a local model fixture. The run checks private-file read/write denials, network allowlisting, Pydantic results, and ATIF persistence/cleanup.

Python 3.12–3.14 is supported. SRT requires Linux x64/arm64, Node >=22.19, bubblewrap, socat, ripgrep and Bash. SRT and JSON Schema file transport currently support fresh SDK sessions; ACP resume is explicitly rejected for these modes. Harbor container permissions and a live external model service were not part of this verification.

Refresh existing Harness homes with `python -m aifn install --dsh-home PATH --reinstall` after upgrading.

This release is distributed through GitHub Release assets. The PyPI name `aifn` belongs to another project. Install the aifn and dsh-trajectory wheels together using the commands in README.md.
