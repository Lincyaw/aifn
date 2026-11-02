import json
from datetime import datetime
from importlib.resources import files

from jsonschema import Draft202012Validator

from dsh_trajectory.errors import TrajectoryError


def validate_trajectory(trajectory: dict) -> None:
    schema = json.loads(files("dsh_trajectory").joinpath("atif.schema.json").read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(trajectory), key=lambda e: str(e.path))
    if errors:
        error = errors[0]
        raise TrajectoryError(f"Invalid ATIF at {list(error.path)}: {error.message}")
    if trajectory["schema_version"] != "ATIF-v1.8":
        raise TrajectoryError("This exporter requires ATIF-v1.8")
    seen_calls = set()
    for expected, step in enumerate(trajectory["steps"], 1):
        if step["step_id"] != expected:
            raise TrajectoryError("ATIF step IDs must be contiguous from 1")
        if step.get("timestamp"):
            datetime.fromisoformat(step["timestamp"].replace("Z", "+00:00"))
        if step["source"] != "agent" and any(
            key in step
            for key in (
                "model_name",
                "reasoning_effort",
                "reasoning_content",
                "tool_calls",
                "metrics",
            )
        ):
            raise TrajectoryError("ATIF agent-only field on a non-agent step")
        if step.get("llm_call_count") == 0 and any(
            key in step for key in ("metrics", "reasoning_content")
        ):
            raise TrajectoryError("Deterministic dispatch cannot contain LLM metrics or reasoning")
        calls = {call["tool_call_id"] for call in step.get("tool_calls", [])}
        if len(calls) != len(step.get("tool_calls", [])) or calls & seen_calls:
            raise TrajectoryError("Duplicate ATIF tool call ID")
        seen_calls.update(calls)
        for result in step.get("observation", {}).get("results", []):
            if result.get("source_call_id") and result["source_call_id"] not in calls:
                raise TrajectoryError("ATIF observation refers to a call outside its step")
