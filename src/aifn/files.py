from __future__ import annotations

import json
import math
import os
import secrets
import stat
import threading
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Never

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from aifn.call import AnyOutcome, Refused, Returned
from aifn.schema import construct_model, schema_hash
from aifn.skills import write_bundles
from aifn.storage import atomic_write, canonical, readable_json


@dataclass(frozen=True, slots=True)
class TaskFiles:
    root: Path

    @property
    def output(self) -> Path:
        return self.root / "output" / "output.json"

    @property
    def refusal(self) -> Path:
        return self.root / "output" / "refusal.json"

    @property
    def accepted(self) -> Path:
        return self.root / "accepted.json"

    @property
    def skills(self) -> Path:
        return self.root / "skills"

    def prepare(
        self, instruction: str, request: object, schema: object, rejected: object,
        skills: Mapping[str, str],
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        self.output.parent.mkdir()
        atomic_write(self.root / "instruction.md", instruction)
        write_bundles(skills, self.skills)
        atomic_write(self.root / "input.json", readable_json(request))
        atomic_write(self.root / "output.schema.json", readable_json(schema))
        atomic_write(self.root / "feedback.json", readable_json(rejected))
        atomic_write(
            self.root / "refusal.schema.json", readable_json(RefusalFile.model_json_schema())
        )


class RefusalFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)


class FileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["validate", "submit", "refuse"]
    path: str


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["returned", "refused"]
    value: JsonValue
    submitted_json: str | None = None
    validation: Literal["pydantic", "json-schema"] = "pydantic"
    schema_sha256: str | None = None


def error_report(code: str, message: str, **extra: JsonValue) -> dict[str, JsonValue]:
    return {"valid": False, "errors": [{"code": code, "message": message, **extra}]}


def validate_file(
    path: Path, model: type[BaseModel]
) -> tuple[dict[str, JsonValue], BaseModel | None]:
    try:
        content = _read_candidate(path)
    except FileNotFoundError:
        return error_report("file_missing", "File not found. Write the output file first.",
                            file=str(path)), None
    return _validate_json(content, model)


def _read_candidate(path: Path) -> bytes:
    """Open one stable regular-file descriptor; never follow a swapped symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("Output must be a regular file")
        content = stream.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024:
            raise OSError("Output exceeds 16 MiB")
        return content


def _validate_json(
    content: bytes, model: type[BaseModel],
) -> tuple[dict[str, JsonValue], BaseModel | None]:
    try:
        json.loads(content)
    except json.JSONDecodeError as error:
        return error_report("json_syntax", error.msg, line=error.lineno, column=error.colno), None
    except UnicodeDecodeError:
        return error_report("encoding", "The file must use UTF-8 encoding."), None
    try:
        value = model.model_validate_json(content)
    except ValidationError as error:
        errors: list[JsonValue] = []
        for item in error.errors(include_url=False, include_context=False):
            pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1")
                                     for part in item["loc"])
            errors.append({"path": pointer, "code": item["type"], "message": item["msg"],
                           "actual": TypeAdapter(JsonValue).validate_python(item.get("input"))})
        return {"valid": False, "errors": errors}, None
    return {"valid": True, "errors": []}, value


class OutputService:
    """Validate output files in the trusted worker and persist accepted snapshots."""

    def __init__(
        self, files: TaskFiles, model: type[BaseModel],
        *, validation: Literal["pydantic", "json-schema"] = "pydantic",
    ) -> None:
        self.files = files
        self.model = model
        self.validation = validation
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.error: BaseException | None = None
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.service = self
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/output"

    def __enter__(self) -> OutputService:
        self.thread.start()
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if error is None and self.error is not None:
            raise self.error

    def dispatch(self, request: FileRequest) -> dict[str, JsonValue]:
        with self.lock:
            if self.error is not None:
                raise self.error
            if self.files.accepted.exists():
                return error_report("already_submitted", "This task is submitted. Stop editing.")
            path = Path(request.path)
            if not path.is_absolute():
                path = self.files.output.parent / path
            expected = self.files.refusal if request.action == "refuse" else self.files.output
            allowed = {self.files.output, self.files.refusal} if request.action == "validate" else {
                expected
            }
            if (
                path.absolute() not in allowed or path.is_symlink()
                or path.resolve() != path.absolute()
            ):
                return error_report("output_path", "Use this task's designated output files only.")
            model = RefusalFile if path == self.files.refusal else self.model
            try:
                content = _read_candidate(path)
            except FileNotFoundError:
                return error_report("file_missing", "File not found. Write the output file first.",
                                    file=str(path))
            except OSError:
                return error_report("file_read", "Read a regular output file of at most 16 MiB.")
            if self.validation == "json-schema":
                report, payload = _validate_schema(content, model)
            else:
                report, value = _validate_json(content, model)
                payload = (
                    value.model_dump(mode="json", by_alias=True) if value is not None else None
                )
            if not report["valid"] or request.action == "validate":
                return report
            receipt = Receipt(
                status="refused" if request.action == "refuse" else "returned",
                value=payload,
                submitted_json=(
                    canonical(payload) if self.validation == "json-schema"
                    else content.decode("utf-8")
                ),
                validation=self.validation,
                schema_sha256=schema_hash(model) if self.validation == "json-schema" else None,
            )
            atomic_write(self.files.accepted, receipt.model_dump_json())
            return {"valid": True, "submitted": True}

    def outcome(self) -> AnyOutcome | None:
        if not self.files.accepted.exists():
            return None
        receipt = Receipt.model_validate_json(self.files.accepted.read_bytes())
        if receipt.validation == "json-schema":
            if receipt.status == "refused":
                return Refused(reason=RefusalFile.model_validate(receipt.value).reason)
            return Returned(
                value=construct_model(self.model, receipt.value),
                submitted_json=receipt.submitted_json, validation="json-schema",
                schema_sha256=receipt.schema_sha256,
            )
        if receipt.status == "refused":
            return Refused(reason=RefusalFile.model_validate(receipt.value).reason)
        value = self.model.model_validate_json(
            receipt.submitted_json or canonical(receipt.value)
        )
        return Returned(value=value, submitted_json=receipt.submitted_json)


def _validate_schema(
    content: bytes, model: type[BaseModel],
) -> tuple[dict[str, JsonValue], JsonValue]:
    # DSH can validate in its Node plugin. Other harnesses use this trusted endpoint;
    # loading aifn or running DSH does not require the optional Python validator.
    from jsonschema import Draft202012Validator, validators

    try:
        value: JsonValue = json.loads(
            content, parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except (ValueError, UnicodeDecodeError):
        return error_report("json_syntax", "Write valid UTF-8 JSON."), None

    def defaults(validator: Any, properties: Any, instance: Any, schema: Any) -> Any:
        if isinstance(instance, dict):
            for name, definition in properties.items():
                if "default" in definition:
                    instance.setdefault(name, deepcopy(definition["default"]))
        yield from Draft202012Validator.VALIDATORS["properties"](
            validator, properties, instance, schema,
        )

    validator = validators.extend(Draft202012Validator, {"properties": defaults})(
        model.model_json_schema(), format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    errors: list[JsonValue] = []
    for error in validator.iter_errors(value):
        pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1")
                                 for part in error.absolute_path)
        errors.append({"path": pointer, "code": str(error.validator),
                       "message": error.message, "actual": error.instance})
    return {"valid": not errors, "errors": errors}, value


def _reject_constant(value: str) -> Never:
    raise ValueError(f"{value} is not a JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must have a finite representation")
    return number


class _Server(ThreadingHTTPServer):
    service: OutputService
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        server = self.server
        assert isinstance(server, _Server)
        service = server.service
        if self.path != "/output" or self.headers.get("Authorization") != f"Bearer {service.token}":
            self.send_error(403)
            return
        try:
            content = self.rfile.read(int(self.headers["Content-Length"]))
            request = FileRequest.model_validate_json(content)
            report = service.dispatch(request)
        except ValidationError as error:
            self._respond(400, error_report("request", str(error)))
            return
        except Exception as error:
            service.error = error
            self._respond(500, error_report(
                "validator_error", "The validator failed. Stop the task.",
            ))
            return
        self._respond(200, report)

    def _respond(self, status: int, payload: dict[str, JsonValue]) -> None:
        content = canonical(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)
