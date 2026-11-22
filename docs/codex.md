# Run aifn tasks with Codex

`CodexEngine` runs file tasks through Codex App Server and submits answers through aifn's validation service.
It supports Linux, Python 3.12 through 3.14, and the pinned Codex CLI 0.169.0 runtime.
The adapter refuses another runtime version because dynamic tools use an experimental protocol.

## Install and authenticate

From the source checkout:

```sh
uv sync --extra codex
export CODEX_HOME="$HOME/.aifn-codex"
uv run python -c 'import subprocess; from codex_cli_bin import bundled_codex_path; subprocess.run([str(bundled_codex_path()), "login"], check=True)'
uv run python -m examples.codex
```

The example creates its workspace and mailbox under `mailboxes/codex-demo`.
Expect a returned answer containing `"timeout_s": 30` and the path of its `output.json`.
Select a model available to your account with `AIFN_MODEL`.

For an API-key deployment, set `OPENAI_API_KEY` in the controller environment and authenticate the same private home:

```sh
uv run python -c 'import os, subprocess; from codex_cli_bin import bundled_codex_path; subprocess.run([str(bundled_codex_path()), "login", "--with-api-key"], input=os.environ["OPENAI_API_KEY"], text=True, check=True)'
```

`codex_home` contains credentials, native Codex journals and aifn session mappings.
Keep it outside the business workspace and task directories.
The engine does not modify your home configuration or copy authentication from another home.
To reuse an installed Codex subscription, pass its existing home and executable, for example `codex_home=Path.home() / ".codex", codex_bin="codex"`.
No additional login is needed when that home is already authenticated.

## Configure a worker

Declare `Model(provider="openai", name=...)` on the function and pass a `CodexEngine` to its worker:

```python
from pathlib import Path
from aifn.engines import CodexEngine

engine = CodexEngine(codex_home=Path.home() / ".aifn-codex")
engine.prepare()
```

[The complete example](../examples/codex.py) constructs the function, stage and worker.
Use separate mailboxes for workers with different engines.
An explicit `codex_bin` can select an installed native executable of the pinned version.
The default uses the binary supplied by `aifn[codex]` and does not depend on a global npm shim.

For a custom Responses-compatible route, declare its provider id in the function's `Model.provider` and supply the matching Codex provider definition:

```python
engine = CodexEngine(
    codex_home=Path.home() / ".aifn-codex",
    model_providers={
        "internal": {
            "name": "Internal model service",
            "base_url": "https://model.example.com/v1",
            "env_key": "INTERNAL_MODEL_API_KEY",
            "wire_api": "responses",
        },
    },
)
```

`env` supplies controller credentials or endpoint environment variables.
`Model.reasoning_effort` is forwarded to Codex.
Codex chooses its output-token allowance; `Model.max_tokens` is not a hard limit for this engine.

## Permissions and submission

Each attempt starts a private stdio app-server connection.
The controller communicates with the model service outside the native tool sandbox.
The adapter configures Codex's named filesystem permissions for every call:

| Resource | Access |
| --- | --- |
| Business workspace with `Access.READ_ONLY` | Read |
| Business workspace with `Access.WORKSPACE_WRITE` or `Access.FULL_ACCESS` | Read and write |
| Current instruction, input, schemas and skills | Read |
| Current output and scratch directories | Read and write |
| Required system runtime and exact Codex executable | Read |
| Other mailbox tasks, controller credentials and native journals | Outside tool grants |
| Tool network access | Denied |

With the default policy, `Access.FULL_ACCESS` retains this boundary, as it does with the default DSH SRT policy.
Shell tools remain available for read-only calls and run with a minimal environment.
To use Codex's native permissions instead, configure `CodexEngine(..., sandbox=NativeSandbox())` with `NativeSandbox` imported from `aifn`.
The call's `Access` then selects Codex's sandbox mode directly.
An `Access.FULL_ACCESS` call can use every file, network destination and container runtime available to the worker.
Native shell tools inherit the deployment environment with credential-shaped variables excluded, and use the attempt's scratch directory for temporary files.
Home MCP servers, apps, plugins, hooks, browser tools, computer use, image generation, memories, project instructions and host skill discovery are disabled for these function calls.
The function's snapshotted skills are catalogued in the prompt and loaded through `skill(name)`.
The loader remains registered across a conversation and serves only the current attempt's declared skills.

`validate_output`, `submit_output` and `refuse_output` call the trusted worker's output service.
The default mode executes the full Pydantic model and business validators.
`validation="json-schema"` uses Python JSON Schema validation and constructs the model without running Python validators.
Schema defaults are inserted and validated before the accepted JSON snapshot is saved.
Validation errors return to Codex so it can edit and try again within the same attempt.
A final text response alone does not submit an answer.

After a successful submission, the adapter interrupts the turn and closes the process group.
The accepted receipt remains authoritative if shutdown times out or the connection closes.
Already executed workspace side effects remain subject to the normal aifn retry and lease rules.

## Continue and inspect a session

Pass a previous result's `attempts[-1].session` to `Stage.issue(..., continues=...)`.
Keep the same Codex home and model provider so the adapter can resolve and resume the native thread.
The new call applies its current workspace, filesystem grants, task files and skill catalog.
Its conversation history still includes earlier turns.
A file lock serializes calls continuing the same aifn session within one Codex home.
An unknown session or a provider change fails explicitly.

Mailbox events contain the App Server notifications and the mapping to the native thread.
Codex keeps its resumable journals in `codex_home`.
The DSH-specific `TrajectoryArchive` and `dsh-trajectory` ATIF converter do not process Codex journals.
With no Codex archive configured, the mailbox keeps full events even when `retain_events=False`.

## Diagnose startup failures

`engine.prepare()` checks the platform, executable and exact runtime version before the worker consumes tasks.
Refresh the environment with `uv sync --extra codex` if the bundled binary is missing.
Authenticate the home passed to `CodexEngine`, and select a model available to that account.
The business workspace, task directory and Codex home must be separate directories.
Sandbox failures return execution failures; the adapter never retries a tool outside the configured policy.

The protocol and provider configuration follow the [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server).
