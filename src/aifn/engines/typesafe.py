from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field
from typesafe_sdk import (
    JSONContent,
    Question,
    RetryPolicy,
    TypeSafeAPITimeoutError,
    TypeSafeClient,
    TypeSafeError,
)

from aifn.engine import EngineCrashed, EngineTimedOut, Session, Transcript


class TypeSafeRequest(BaseModel):
    """One state and independent questions, using the SDK's Choice, Score and Noul."""

    model_config = ConfigDict(extra="forbid")
    state: JSONContent
    questions: dict[str, Question] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class TypeSafeEngine:
    """Evaluate explicit input without reading or modifying files or a workspace.

    The caller owns the SDK client's lifetime. Questions are explicit input, and
    the complete SystemOneResponse is the output; Link.adapt handles composition.
    """

    client: TypeSafeClient = field(repr=False)

    def prepare(self) -> None:
        pass

    def run(self, session: Session) -> Transcript:
        request = TypeSafeRequest.model_validate(dict(session.request))
        try:
            response = self.client.system_one(
                state=request.state, questions=request.questions, model=session.model.name,
                timeout=session.timeout_s, retry=RetryPolicy(max_retries=0),
            )
        except TypeSafeAPITimeoutError as error:
            raise EngineTimedOut(str(error)) from error
        except TypeSafeError as error:
            raise EngineCrashed(str(error)) from error
        if response.answers.keys() != request.questions.keys():
            raise EngineCrashed("TypeSafe response does not contain every requested answer")
        value = response.model_dump(mode="json")
        return Transcript(session=session.id, events=({
            "type": "typesafe/response", "data": value,
        },), submission={"status": "returned", "value": value,
                        "submitted_json": response.model_dump_json()})
