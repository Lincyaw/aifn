# Changelog

## Unreleased

- Move the SRT tool set into the per-call policy file, next to the file and network grants it belongs with. `SrtPolicy(tools=...)` declares which registered tools a call may use, defaulting to the deployment set, and `SrtPolicy(mcp_servers=("incident",))` grants the `mcp__<serverName>__` namespace of an MCP server the call mounts. A tool set without `validate_output`, `submit_output` and `refuse_output` is refused when the policy is declared, not mid-session. Declaring neither leaves a call's behaviour unchanged.
- Enable SRT file and shell isolation for every `DeepSeekEngine` call using its existing workspace declaration. Keep controller configuration, receipts, trajectories and archive staging outside agent grants; deny tool network access by default.
- Rebuild SRT grants when restoring a session, using the current call's workspace and permissions. Refuse execution when isolation setup fails.
- Automatically grant read access to required tool executables installed outside system directories without exposing their parent directories.

## 0.2.1 — 2026-11-02

- Update DeepSeek Harness SDK to 0.2.1rc2 and npm DSH to latest 0.2.1-rc.5. Refresh all Python dependencies, development tools and npm dependencies; use Zod 4.6.7. SRT latest remains 0.0.81.
- Restore persisted sessions with the official SDK `resume_session()` API, applying current model settings, working directory and file submission configuration.
- Remove `AcpEngine`, its handwritten JSON-RPC client, `RoutingEngine`, and their public exports. Use `DeepSeekEngine` for fresh and continued calls.
- Install and launch plugins with the same private DSH runtime through the SDK’s supported `dsh_bin` option. The error-propagation regression now installs its own profile without global-state dependencies or a skip.
- Update the trajectory package to 0.1.1 and record the current DSH version in ATIF metadata.
- Upgrade upstream pinned transitive dependencies to Zod 4.6.7, Undici 8.12.3, MCP client 2.4.0 and fflate 0.8.3. The production npm lockfile audit reports zero findings.
- Automatically refresh older installed plugin versions through the native plugin CLI; repeated installation is idempotent.

## 0.2.0 — 2026-11-02

- Add SRT-backed filesystem and Bash providers. Task data, private controller configuration, submission receipts, and trajectories use separate directories.
- Add Node JSON Schema validation with private JSON result transport and nested Pydantic object construction. Unchanged output content reuses validation results; the existing Python validation mode remains the default.
- Add the `dsh-trajectory` 0.1.0 package: DSH v4 JSONL/Zstd to ATIF v1.8 conversion, idempotent SQLite/HTTP ingestion, and cleanup after a matching committed receipt.
- Allow mailbox events to retain a central archive pointer after successful ingestion.
- Fix Linux transport path lengths, proxy loopback resolution, writable-child mount ordering, and readonly isolation scaffolding. SRT 0.0.81 compatibility changes are SHA-256 checked before application.
- Pin the DeepSeek Harness Python SDK to the verified 0.1.5rc1 release, including its bundled launcher used by ACP and explicit runtime selection.

Validated on Linux 6.6 with Node 24 and Python 3.14: 15 existing tests, lint/type checks, and a real DSH/SRT end-to-end run with a local model fixture. The run checks private-file read/write denials, network allowlisting, Pydantic results, and ATIF persistence/cleanup.

Python 3.12–3.14 is supported. SRT requires Linux x64/arm64, Node >=22.19, bubblewrap, socat, ripgrep and Bash. SRT and JSON Schema file transport currently support fresh SDK sessions; ACP resume is explicitly rejected for these modes. Harbor container permissions and a live external model service were not part of this verification.

Refresh existing Harness homes with `python -m aifn install --dsh-home PATH --reinstall` after upgrading.

This release is distributed through GitHub Release assets. The PyPI name `aifn` belongs to another project. Install the aifn and dsh-trajectory wheels together using the commands in README.md.
