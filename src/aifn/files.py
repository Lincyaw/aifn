from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from aifn.call import AnyOutcome, Refused, Returned
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

    def prepare(self, instruction: str, request: object, schema: object, rejected: object) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        self.output.parent.mkdir()
        atomic_write(self.root / "instruction.md", instruction)
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


def error_report(code: str, message: str, **extra: JsonValue) -> dict[str, JsonValue]:
    return {"valid": False, "errors": [{"code": code, "message": message, **extra}]}


def validate_file(
    path: Path, model: type[BaseModel]
) -> tuple[dict[str, JsonValue], BaseModel | None]:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return error_report("file_missing", "File not found. Write the output file first.",
                            file=str(path)), None
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
    """Validate files in the worker using the full Pydantic model held by that process."""

    def __init__(self, files: TaskFiles, model: type[BaseModel]) -> None:
        self.files = files
        self.model = model
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.error: BaseException | None = None
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.service = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

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
            report, value = validate_file(path, model)
            if value is None or request.action == "validate":
                return report
            payload = value.model_dump(mode="json", by_alias=True)
            receipt = Receipt(
                status="refused" if request.action == "refuse" else "returned",
                value=payload,
            )
            atomic_write(self.files.accepted, receipt.model_dump_json())
            return {"valid": True, "submitted": True}

    def outcome(self) -> AnyOutcome | None:
        if not self.files.accepted.exists():
            return None
        receipt = Receipt.model_validate_json(self.files.accepted.read_bytes())
        if receipt.status == "refused":
            return Refused(reason=RefusalFile.model_validate(receipt.value).reason)
        return Returned(value=self.model.model_validate_json(canonical(receipt.value)))


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
