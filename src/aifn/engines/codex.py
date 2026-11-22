from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import frontmatter
from filelock import FileLock, Timeout

from aifn.declare import Access
from aifn.engine import EngineCrashed, EngineTimedOut, Session, Transcript
from aifn.engines.codex_rpc import AppServer
from aifn.files import FileRequest, error_report
from aifn.sandbox import NativeSandbox
from aifn.storage import atomic_write, canonical, component
from aifn.terms import Event

CODEX_VERSION: Final = "0.169.0"
"""The dynamic-tool and named-permissions protocol this adapter is verified against."""


def _runtime(override: Path | str | None) -> Path:
    if override is None:
        try:
            from codex_cli_bin import bundled_codex_path

            return Path(bundled_codex_path()).resolve()
        except (ImportError, OSError) as error:
            raise EngineCrashed(
                "Install aifn[codex] or set codex_bin to a native Codex binary",
            ) from error
    executable = shutil.which(str(override))
    if executable is None:
        raise EngineCrashed(f"Cannot find Codex executable: {override}")
    return Path(executable).resolve()


def _toml(value: Any) -> str:
    """Render JSON configuration as TOML inline values for Codex's -c arguments."""
    if isinstance(value, dict):
        return "{" + ",".join(f"{canonical(k)}={_toml(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_toml(item) for item in value) + "]"
    if value is None:
        raise ValueError("Codex configuration cannot contain null")
    return canonical(value)


@dataclass(frozen=True, slots=True)
class CodexEngine:
    """Run file tasks through a versioned Codex app-server stdio connection.

    codex_home is deployment-owned authentication and durable session state, outside
    every business workspace and task directory. Log in there with CODEX_HOME set.
    Scoped tool processes receive a minimal environment without controller credentials.
    NativeSandbox delegates permissions to Codex and inherits the deployment environment.
    """

    codex_home: Path
    codex_bin: Path | str | None = None
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    model_providers: Mapping[str, Any] = field(default_factory=dict)
    """Optional Codex Responses-compatible provider definitions, keyed by provider id."""
    sandbox: NativeSandbox | None = None
    """Leave tools to Codex's Access-based native sandbox when explicitly configured."""

    def prepare(self) -> None:
        self._check_runtime(10)

    def _check_runtime(self, timeout_s: float) -> Path:
        if sys.platform != "linux":
            raise EngineCrashed("CodexEngine currently requires Linux sandbox support")
        executable = _runtime(self.codex_bin)
        try:
            version = subprocess.run(
                [str(executable), "--version"], check=True, capture_output=True,
                text=True, timeout=timeout_s,
            ).stdout.strip()
        except subprocess.TimeoutExpired as error:
            raise EngineTimedOut("Codex runtime check exceeded its time limit") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise EngineCrashed(f"Cannot check Codex runtime: {error}") from error
        if version != f"codex-cli {CODEX_VERSION}":
            raise EngineCrashed(f"CodexEngine requires codex-cli {CODEX_VERSION}; got {version}")
        return executable

    def run(self, session: Session) -> Transcript:
        started = time.monotonic()
        executable = self._check_runtime(min(10, session.timeout_s))
        if session.directory is None or session.files is None:
            raise EngineCrashed("CodexEngine requires a workspace and task files")
        if session.model.provider == "deepseek-official":
            raise EngineCrashed(
                "Codex tasks must declare Model(provider='openai') or a Codex provider",
            )
        if session.continues is not None and session.continues != session.id:
            raise ValueError("A resumed session must use the session id it continues")
        home = self.codex_home.expanduser().resolve()
        workspace, files = session.directory.resolve(), session.files.resolve()
        for left, right in ((home, workspace), (home, files), (workspace, files)):
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise EngineCrashed(
                    "Codex home, task files and business workspace must be separate",
                )
        if not {"AIFN_OUTPUT_ENDPOINT", "AIFN_OUTPUT_TOKEN"} <= session.env.keys():
            raise EngineCrashed("CodexEngine requires the worker's output validation service")
        home.mkdir(parents=True, exist_ok=True)
        state = home / "aifn-sessions" / f"{component(session.id)}.json"
        state.parent.mkdir(exist_ok=True)
        try:
            with FileLock(str(state.with_suffix(".lock")), timeout=max(
                0, session.timeout_s - (time.monotonic() - started),
            )):
                return self._run(
                    session, home, workspace, files, state, executable,
                    started + session.timeout_s,
                )
        except Timeout as error:
            raise EngineTimedOut("Codex session is already being used by another worker") from error

    def _run(
        self, session: Session, home: Path, workspace: Path, files: Path,
        state: Path, executable: Path, deadline: float,
    ) -> Transcript:
        scratch = files / "scratch"
        scratch.mkdir(exist_ok=True)
        access = Access(session.permission)
        filesystem = {
            ":minimal": "read", str(executable): "read",
            str(workspace): "read" if access == Access.READ_ONLY else "write",
            str(files): "read", str(files / "output"): "write", str(scratch): "write",
        }
        config = {
            "permissions.aifn": {"filesystem": filesystem, "network": {"enabled": False}},
            "default_permissions": "aifn", "approval_policy": "never",
            "project_doc_max_bytes": 0, "allow_login_shell": False, "web_search": "disabled",
            "projects": {str(workspace): {"trust_level": "untrusted"}},
            "shell_environment_policy": {
                "inherit": "none", "set": {
                    "PATH": "/usr/bin:/bin", "HOME": str(scratch), "TMPDIR": str(scratch),
                    "LANG": "C.UTF-8",
                },
            },
            "features.skip_host_skill_discovery": True, "skills.bundled.enabled": False,
            "features.plugins": False, "features.apps": False, "features.hooks": False,
            "features.codex_hooks": False, "features.plugin_hooks": False,
            "features.multi_agent": False, "features.multi_agent_v2": False,
            "features.code_mode": False, "features.code_mode_only": False,
            "features.js_repl": False, "features.shell_snapshot": False,
            "features.shell_snapshot_v2": False,
            "features.browser_use": False, "features.browser_use_external": False,
            "features.browser_use_full_cdp_access": False, "features.in_app_browser": False,
            "features.computer_use": False, "features.image_generation": False,
            "features.image_generation_animation": False, "features.memories": False,
            "features.memory_tool": False, "features.chronicle": False, "features.goals": False,
            "features.request_permissions": False, "features.request_permissions_tool": False,
        }
        if self.model_providers:
            config["model_providers"] = dict(self.model_providers)
        if isinstance(self.sandbox, NativeSandbox):
            del config["permissions.aifn"]
            del config["default_permissions"]
            config["sandbox_mode"] = access.value
            config["sandbox_workspace_write.writable_roots"] = [
                str(files / "output"), str(scratch),
            ]
            config["shell_environment_policy"] = {
                "inherit": "all",
                "exclude": ["*KEY*", "*TOKEN*", "*SECRET*", "*PASSWORD*", "DSH_*"],
                "set": {"TMPDIR": str(scratch)},
            }
        environment = {**os.environ, **self.env, "CODEX_HOME": str(home)}
        command = [str(executable), "app-server"]
        for key, value in config.items():
            command.extend(["-c", f"{key}={_toml(value)}"])
        active = _Attempt(session, deadline)
        client = AppServer(command, home, environment, deadline, active.handle)
        active.client = client
        try:
            client.request("initialize", {
                "clientInfo": {"name": "aifn", "version": "0.6.2"},
                "capabilities": {"experimentalApi": True},
            })
            client.send({"method": "initialized", "params": {}})
            # Home configuration belongs to the deployment, but its MCP tools are not
            # implicitly granted to a function. Project configuration is untrusted.
            loaded = client.request("config/read", {"includeLayers": False})
            servers = loaded.get("config", {}).get("mcp_servers", {})
            overrides = {"mcp_servers": {name: {"enabled": False} for name in servers}}
            params: dict[str, Any] = {
                "cwd": str(workspace), "model": session.model.name,
                "modelProvider": session.model.provider, "approvalPolicy": "never",
                "config": overrides,
                "developerInstructions": active.instructions(),
            }
            if isinstance(self.sandbox, NativeSandbox):
                params["sandbox"] = access.value
            else:
                params["permissions"] = "aifn"
            if session.continues is None:
                params["dynamicTools"] = active.tools()
                result = client.request("thread/start", params)
                active.thread = result["thread"]["id"]
                atomic_write(state, canonical({
                    "thread": active.thread, "provider": session.model.provider,
                }))
            else:
                if not state.is_file():
                    raise EngineCrashed("No saved Codex thread for the continued aifn session")
                saved = json.loads(state.read_text())
                if saved["provider"] != session.model.provider:
                    raise EngineCrashed("Cannot continue a Codex session with a different provider")
                params.update(threadId=saved["thread"], excludeTurns=True)
                result = client.request("thread/resume", params)
                active.thread = result["thread"]["id"]
            turn = client.request("turn/start", {
                "threadId": active.thread,
                "input": [{"type": "text", "text": session.prompt}],
                "effort": session.model.reasoning_effort,
            })
            active.turn = turn["turn"]["id"]
            while not active.completed:
                active.handle(client.receive())
        except (EngineCrashed, EngineTimedOut) as error:
            if not active.submitted:
                error.events = tuple(active.events)
                raise
            active.events.append({"type": "codex/shutdown", "data": {"detail": str(error)}})
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise EngineCrashed(f"Codex session failed: {error}", tuple(active.events)) from error
        finally:
            client.close()
        return Transcript(session=session.id, events=tuple(active.events))


class _Attempt:
    def __init__(self, session: Session, deadline: float) -> None:
        self.session = session
        self.deadline = deadline
        self.client: AppServer
        self.thread = ""
        self.turn = ""
        self.submitted = False
        self.completed = False
        self.events: list[Event] = []
        self.skills: dict[str, tuple[str, str]] = {}
        if session.skills is not None:
            for path in sorted(session.skills.glob("*/SKILL.md")):
                skill = frontmatter.load(str(path))
                self.skills[path.parent.name] = (str(skill["description"]), path.read_text())

    def instructions(self) -> str:
        text = (
            "Execute the aifn task in the supplied instruction file. Use native shell and editor "
            "tools to read and write files. Only the current business workspace, task files, "
            "runtime and scratch are accessible. Network access is disabled. "
            "Call validate_output, submit_output and refuse_output by their exact tool names. "
            "A final text response does not submit an answer. Stop when submission succeeds."
        )
        if self.skills:
            text += "\nOnly these skills are available for this attempt; load with skill(name):\n"
            text += "\n".join(f"{name}: {value[0]}" for name, value in self.skills.items())
        else:
            text += " This attempt has no declared skills; do not load skills from earlier turns."
        return text

    def tools(self) -> list[dict[str, Any]]:
        descriptions = {
            "validate_output": "Validate the candidate file and return errors for correction.",
            "submit_output": "Validate and submit the candidate file, ending the task on success.",
            "refuse_output": "Validate and submit the refusal file, ending the task on success.",
        }
        tools = [{
            "type": "function", "name": name, "description": description,
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                            "required": ["path"], "additionalProperties": False},
        } for name, description in descriptions.items()]
        # Codex persists dynamic tool definitions across resumes. Keep the loader stable;
        # dispatch resolves its name against the current attempt's catalog only.
        tools.append({
            "type": "function", "name": "skill", "description": "Load a declared task skill.",
            "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                            "required": ["name"], "additionalProperties": False},
        })
        return tools

    def handle(self, message: dict[str, Any]) -> None:
        method, params = message.get("method"), message.get("params", {})
        if method is None:
            return
        self.events.append({"type": method, "data": params})
        if "id" in message:
            if method == "item/tool/call":
                report = self.dispatch(params)
                self.client.send({"id": message["id"], "result": {
                    "contentItems": [{"type": "inputText", "text": canonical(report)}],
                    "success": report.get("valid", True),
                }})
            else:
                # A headless worker never grants interactive approvals or escalations.
                self.client.send({"id": message["id"], "error": {
                    "code": -32601, "message": "Interactive requests are disabled in aifn",
                }})
        elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
            self.events.append({"type": "assistant/message", "data": params["item"]})
        elif method == "turn/completed":
            turn = params["turn"]
            if self.turn and turn["id"] != self.turn:
                return
            self.completed = True
            if turn["status"] != "completed" and not self.submitted:
                raise EngineCrashed(f"Codex turn {turn['status']}: {canonical(turn.get('error'))}")

    def dispatch(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.submitted:
            return error_report("already_submitted", "This task is submitted. Stop editing.")
        if params.get("threadId") != self.thread:
            return error_report("session", "Tool call belongs to another thread")
        if self.turn and params.get("turnId") != self.turn:
            return error_report("session", "Tool call belongs to another turn")
        name, arguments = params.get("tool"), params.get("arguments")
        if not isinstance(name, str):
            return error_report("tool", "Tool name must be a string")
        if not isinstance(arguments, dict):
            return error_report("arguments", "Tool arguments must be an object")
        if name == "skill":
            skill_name = arguments.get("name")
            skill = self.skills.get(skill_name) if isinstance(skill_name, str) else None
            return {"content": skill[1]} if skill else error_report("skill", "Unknown task skill")
        action = {"validate_output": "validate", "submit_output": "submit",
                  "refuse_output": "refuse"}.get(name)
        if action is None:
            return error_report("tool", "Unknown aifn tool")
        try:
            request = FileRequest.model_validate({**arguments, "action": action})
        except ValueError as error:
            return error_report("arguments", str(error))
        endpoint = self.session.env["AIFN_OUTPUT_ENDPOINT"]
        token = self.session.env["AIFN_OUTPUT_TOKEN"]
        http = urllib.request.Request(
            endpoint, data=request.model_dump_json().encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EngineTimedOut("Codex output validation exceeded its time limit")
        try:
            # Validation stays in the trusted controller, independent of shell network grants.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(http, timeout=remaining) as response:
                report: dict[str, Any] = json.load(response)
        except TimeoutError as error:
            raise EngineTimedOut("Output validation exceeded its time limit") from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise EngineTimedOut("Output validation exceeded its time limit") from error
            raise EngineCrashed(f"Output validation service failed: {error.reason}") from error
        if report.get("submitted") is True:
            self.submitted = True
            self.client.deadline = min(self.deadline, time.monotonic() + 5)
            self.client.start_request("turn/interrupt", {
                "threadId": self.thread, "turnId": params["turnId"],
            })
        return report
