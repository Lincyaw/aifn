from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from aifn import Returned
from aifn.files import FileRequest, OutputService, TaskFiles


class Answer(BaseModel):
    timeouts: dict[str, int]
    minimum: int = Field(ge=1)
    maximum: int

    @model_validator(mode="after")
    def ordered(self) -> Answer:
        if self.maximum < self.minimum:
            raise ValueError("maximum must be at least minimum")
        return self


def test_file_feedback_corrections_and_submission(tmp_path: Path) -> None:
    files = TaskFiles(tmp_path / "task")
    files.prepare("Read input.json", {}, Answer.model_json_schema(), [])
    with OutputService(files, Answer) as service:
        def write(content: str) -> None:
            files.output.write_text(content)

        write('{"minimum": }')
        report = service.dispatch(FileRequest(action="validate", path="output.json"))
        assert "json_syntax" in json.dumps(report) and '"column"' in json.dumps(report)
        write('{"timeouts": {"api": 30}, "minimum": 5, "maximum": 2}')
        report = service.dispatch(FileRequest(action="submit", path="output.json"))
        assert "maximum must be at least minimum" in json.dumps(report)
        assert not files.accepted.exists()
        write('{"timeouts": {"api": 30}, "minimum": 1, "maximum": 2}')
        assert service.dispatch(FileRequest(action="validate", path="output.json"))["valid"]
        assert not files.accepted.exists()
        assert service.dispatch(FileRequest(action="submit", path="output.json"))["submitted"]
        assert not service.dispatch(FileRequest(action="submit", path="output.json"))["valid"]
        result = service.outcome()
        assert isinstance(result, Returned)
        assert result.value.timeouts == {"api": 30}
