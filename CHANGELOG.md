# Changelog

## 0.5.0 — 2026-11-09

- Let a deployment leave a call's tools to the harness's own sandbox with `DeepSeekEngine(sandbox=NativeSandbox())`. SRT reaches only the network destinations a policy names exactly and takes no wildcard, so a deployment that trusts its agents with any destination, such as one whose agents build and run services, had no way to say so. Under `NativeSandbox` the harness's own sandbox decides by the call's `Access`, on file effects alone: a `FULL_ACCESS` call is not confined at all, its tools read and write wherever the runtime process can and reach any network destination, and its shell starts from the runtime's environment less the credential-shaped names and `DSH_*` values the harness scrubs from every child. `sandbox=None` keeps deriving the default SRT policy.

Minor rather than patch because the surface is new, and compatible on its own: a deployment that does not declare `NativeSandbox` runs every call under SRT exactly as 0.4.1 did.

Validated on Linux 6.6 with Node 24 and Python 3.14: the 19 existing tests, lint and type checks. A real DSH session on the repository's local probe endpoint declared `NativeSandbox` with `Access.FULL_ACCESS`: its shell read a controller file SRT hides, wrote outside the workspace, reached `https://github.com` with HTTP 200 and kept an ordinary runtime variable while the credential-shaped probe key was absent, and the call submitted its answer. No external model API was called.

## 0.4.1 — 2026-11-06

- Read the recorded events of a call line by line on line feeds alone. Events are written without escaping non-ASCII text, so a tool result that holds U+2028, U+2029 or U+0085 puts that character inside a JSON string, and `str.splitlines()` cut the line there: reading the events of such a call, for its token usage or for a replay, raised `JSONDecodeError` although every line of the file was valid. The mailbox and the replay engine now split the bytes, which break only at line feeds and carriage returns, and JSON escapes both inside strings. Nothing written changes, so every existing mailbox reads as it is.

Validated on Linux 6.6 with Python 3.12: a recorded mailbox whose two event files held U+2028 inside tool results, which raised before, reads its 451 and 340 events; the existing tests, lint and type checks.

## 0.4.0 — 2026-11-05

- Let a call declare the Unix sockets and the local ports its tools may reach, as `SrtPolicy(unix_sockets=...)` and `SrtPolicy(local_ports=True)`. The policy refused both outright, so a deployment whose agent has to reach a container runtime, or answer its own queries over a local endpoint, had no way to say so and confined that call with something outside aifn instead. Both are grants like the file and network ones, so they travel in the same policy file and are read in the same place. Declaring neither leaves a call's behaviour unchanged.

A socket grant costs more on Linux than on macOS, and `SrtPolicy.unix_sockets` says so rather than hiding it: the kernel filter SRT uses there decides on the syscall, which carries no path, so a grant for one socket is a grant for every socket the namespace can see. macOS filters by path and receives exactly the declared names. Declare the sockets a call needs either way, and read the Linux grant as what it is.

Minor rather than patch because the surface is new, and compatible on its own: an existing policy file parses unchanged with both grants empty, which is the behaviour 0.3.0 had.

Validated on Linux 6.6 with Node 24 and Python 3.14: the 19 existing tests, lint and type checks. The policy schema was exercised directly against the installed Zod: an existing policy parses with `unixSockets: []` and `localPorts: false`, a declared socket and local-port grant parse, a relative socket path is refused, and an unknown network key is still refused.

## 0.3.0 — 2026-11-04

- Move the SRT tool set into the per-call policy file, next to the file and network grants it belongs with. `SrtPolicy(tools=...)` declares which registered tools a call may use, defaulting to the deployment set, and `SrtPolicy(mcp_servers=("incident",))` grants the `mcp__<serverName>__` namespace of an MCP server the call mounts. A tool set without `validate_output`, `submit_output` and `refuse_output` is refused when the policy is declared, not mid-session. Declaring neither leaves a call's behaviour unchanged.
- Enable SRT file and shell isolation for every `DeepSeekEngine` call using its existing workspace declaration. Keep controller configuration, receipts, trajectories and archive staging outside agent grants; deny tool network access by default.
- Rebuild SRT grants when restoring a session, using the current call's workspace and permissions. Refuse execution when isolation setup fails.
- Automatically grant read access to required tool executables installed outside system directories without exposing their parent directories.
- Pin AI Hub cache affinity to each aifn session: when a call's endpoint is AI Hub, directly or through a loopback proxy, its key carries the session id as `cache_task_id` and keeps the key's other options.
- Lower the FileLock floor to `filelock>=3.32.7`, so a consumer held to FileLock 3 by its other dependencies can still resolve aifn. Both FileLock 3.32.7 and 4.2.6 pass all existing checks.

This is a minor release rather than a patch because isolation is no longer something a call opts into. Every `DeepSeekEngine` call now runs under SRT using the workspace it already declares, with controller configuration, receipts, trajectories and archive staging outside the agent's grants and tool network access denied unless `allow_hosts` names a destination. An existing 0.2.1 consumer that upgrades without changing a line therefore gets a narrower call than it had: one that read a path or reached a host outside its own workspace declaration now fails where it used to succeed, and a session whose isolation setup fails refuses instead of running unconfined. The added surface — `SrtPolicy(tools=...)`, `SrtPolicy(mcp_servers=...)` and the exported `DEPLOYMENT_TOOLS` and `RESULT_TOOLS` — is compatible on its own, and declaring neither keeps a call's tool set exactly as it was; the default-on isolation is the part to read before upgrading.

Validated on Linux 6.6 with Node 24 and Python 3.14: 19 existing tests, lint and type checks, all four artifacts built and `twine`-checked, and both wheels installed into a clean environment where the same 19 tests and a real DSH/SRT run with the repository's local model fixture passed. The run checks private-file read/write denials, a shell confined to the workspace grant, network allowlisting with an explicit 403 for an unlisted destination, Node JSON Schema validation with nested Pydantic construction, and ATIF persistence and cleanup. A second run declares a restricted tool set with `mcp_servers=("incident",)`: the undeclared `str_replace_editor` is refused as outside the deployment tool set, the call still submits its result, and the policy file resolves the grant to the `mcp__incident__` namespace. No external model API was called in either run.

Refresh existing Harness homes with `python -m aifn install --dsh-home PATH` after upgrading, because the policy runtime sources changed.

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
