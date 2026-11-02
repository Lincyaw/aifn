from collections import Counter
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from dsh_trajectory.codec import strict_json
from dsh_trajectory.errors import TrajectoryError


def trajectory_id(trial_id: str, attempt_id: str, session_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"dsh:{trial_id!r}:{attempt_id!r}:{session_id!r}"))


def tool_call(block: dict) -> dict:
    arguments = block["arguments"]
    if not isinstance(arguments, str):
        raise TrajectoryError("Harness tool arguments must be the original JSON string")
    try:
        parsed = strict_json(arguments)
    except ValueError:
        parsed = None
    result = {
        "tool_call_id": block["id"],
        "function_name": block["name"],
        "arguments": parsed if isinstance(parsed, dict) else {},
    }
    if not isinstance(parsed, dict):
        result["extra"] = {"dsh": {"raw_arguments": arguments, "invalid_arguments": True}}
    return result


def message_content(message: dict) -> tuple[str, str, list[dict], list[dict]]:
    text, reasoning, calls, updates = [], [], [], []
    if not isinstance(message.get("content"), list):
        raise TrajectoryError("Message content must be a block array")
    for block in message["content"]:
        kind = block["type"]
        if kind in ("text", "reasoning"):
            if not isinstance(block.get("text"), str):
                raise TrajectoryError(f"Invalid {kind} block")
            (text if kind == "text" else reasoning).append(block["text"])
        elif kind == "tool-call":
            calls.append(tool_call(block))
        elif kind in ("tool-addition", "tool-removal"):
            updates.append(block)
        else:
            raise TrajectoryError(
                f"Unsupported content block {kind!r}; this release handles text, reasoning, "
                "tool calls and tool updates. Source files were retained."
            )
    return "".join(text), "".join(reasoning), calls, updates


def convert_session(
    header: dict,
    events: list[dict],
    *,
    trial_id: str,
    attempt_id: str,
    harness_version: str,
    known_events: set[str],
    allow_incomplete: bool = False,
) -> dict:
    steps: list[dict] = []
    calls: dict[str, tuple[dict, dict]] = {}
    results: set[str] = set()
    opened_turns: set[int] = set()
    opened_steps: set[tuple[int, int]] = set()
    omitted: Counter = Counter()
    route: dict[str, Any] = {}
    endings: list[dict] = []
    seed_cuts = [
        e["seq"]
        for e in events
        if e["type"] == "session/end-seed" and e["data"].get("inherited") is True
    ]
    cut = seed_cuts[-1] if seed_cuts else 0
    if header["isSeeded"] != bool(seed_cuts):
        raise TrajectoryError("Seeded header and inherited end-seed marker disagree")

    def add_step(event: dict, source: str, message: str, **fields: Any) -> dict:
        metadata = {"event_seq": event["seq"], "event_type": event["type"]}
        for key in ("surfaceOp", "sourceEventSeqs"):
            if key in event:
                metadata[key] = event[key]
        step = {
            "step_id": len(steps) + 1,
            "timestamp": datetime.fromtimestamp(event["time"] / 1000, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "source": source,
            "message": message,
            "extra": {"dsh": metadata},
            **fields,
        }
        if event["seq"] < cut:
            step["is_copied_context"] = True
        steps.append(step)
        return step

    def register(step: dict, call: dict) -> None:
        identity = call["tool_call_id"]
        if not isinstance(identity, str) or not identity:
            raise TrajectoryError("Tool call ID must be a nonempty string")
        if identity in calls:
            raise TrajectoryError(f"Duplicate tool call ID: {identity}")
        calls[identity] = (step, call)

    for event in events:
        kind, data = event["type"], event["data"]
        if kind not in known_events and event.get("ignorable") is not True:
            raise TrajectoryError(f"Unknown required event {kind!r} at seq {event['seq']}")
        if kind == "turn/start":
            if data["turn"] in opened_turns:
                raise TrajectoryError("Duplicate turn/start")
            opened_turns.add(data["turn"])
        elif kind == "turn/end":
            if data["turn"] not in opened_turns:
                raise TrajectoryError("turn/end without turn/start")
            opened_turns.remove(data["turn"])
            endings.append(data)
        elif kind == "step/start":
            key = (data["turn"], data["step"])
            if key in opened_steps:
                raise TrajectoryError("Duplicate step/start")
            opened_steps.add(key)
        elif kind == "step/end":
            key = (data["turn"], data["step"])
            if key not in opened_steps:
                raise TrajectoryError("step/end without step/start")
            opened_steps.remove(key)
        elif kind == "request/header":
            route = data["header"]["config"]
        elif kind in ("system/message", "developer/message", "user/message", "assistant/message"):
            message = data if kind == "user/message" else data["message"]
            text, reasoning, requested, updates = message_content(message)
            source = {
                "user": "user",
                "assistant": "agent",
                "developer": "system",
                "system": "system",
            }[message["role"]]
            if source != "agent" and (reasoning or requested):
                raise TrajectoryError("Reasoning and tool calls require an assistant message")
            step = add_step(event, source, text)
            step["extra"]["dsh"].update(
                {
                    "message_id": message["id"],
                    "source": {k: v for k, v in message["source"].items() if k != "replayState"},
                }
            )
            if updates:
                step["extra"]["dsh"]["tool_updates"] = updates
            if source == "agent":
                model = message["source"].get("model", route.get("model"))
                if model:
                    step["model_name"] = model
                if route.get("reasoningEffort"):
                    step["reasoning_effort"] = route["reasoningEffort"]
                if reasoning:
                    step["reasoning_content"] = reasoning
                step["llm_call_count"] = 1
                if data.get("interrupted"):
                    step["extra"]["dsh"]["interrupted"] = True
                if requested:
                    step["tool_calls"] = requested
                    for call in requested:
                        register(step, call)
                if "usage" in data:
                    usage = data["usage"]
                    for value in usage.values():
                        if type(value) is not int or value < 0:
                            raise TrajectoryError("Invalid token usage")
                    metrics = {
                        "prompt_tokens": usage["inputTokens"]
                        + usage.get("cacheReadTokens", 0)
                        + usage.get("cacheWriteTokens", 0),
                        "completion_tokens": usage["outputTokens"],
                        "extra": {"dsh": usage},
                    }
                    if "cacheReadTokens" in usage:
                        metrics["cached_tokens"] = usage["cacheReadTokens"]
                    step["metrics"] = metrics
        elif kind == "tool/call":
            call = tool_call(
                {"id": data["callId"], "name": data["name"], "arguments": data["arguments"]}
            )
            if data["callId"] in calls:
                if calls[data["callId"]][1] != call:
                    raise TrajectoryError("Dispatched call differs from assistant tool call")
            else:
                # PTC and deterministic dispatch need not have an assistant tool-call block.
                step = add_step(event, "agent", "", llm_call_count=0, tool_calls=[call])
                register(step, call)
        elif kind == "tool/result":
            message = data["message"]
            identity = message["toolCallId"]
            if message["source"].get("callId") != identity or identity not in calls:
                raise TrajectoryError(f"Unmatched tool result {identity!r}")
            text, reasoning, nested, updates = message_content(message)
            if reasoning or nested or updates:
                raise TrajectoryError("Tool result contains non-result blocks")
            result = {
                "source_call_id": identity,
                "content": text,
                "extra": {
                    "dsh": {"event_seq": event["seq"], "is_error": message.get("isError", False)}
                },
            }
            if "error" in data:
                result["extra"]["dsh"]["error"] = data["error"]
            if isinstance(event.get("surfaceOp"), dict):
                add_step(
                    event,
                    "system",
                    "",
                    observation={
                        "results": [
                            {"content": text, "extra": {"dsh": {"replaces_tool_call_id": identity}}}
                        ]
                    },
                )
            else:
                if identity in results:
                    raise TrajectoryError(f"Duplicate tool result {identity!r}")
                calls[identity][0].setdefault("observation", {"results": []})["results"].append(
                    result
                )
                results.add(identity)
        elif kind == "assistant/attempt":
            add_step(
                event,
                "system",
                "",
                extra={"dsh": {"event_seq": event["seq"], "event_type": kind, "attempt": data}},
            )
        else:
            omitted[kind] += 1

    pending = sorted(calls.keys() - results)
    incomplete = bool(opened_turns or opened_steps or pending)
    if incomplete and not allow_incomplete:
        raise TrajectoryError(
            "Unfinished turns, steps or tool calls; use --allow-incomplete "
            "to export a marked partial trajectory"
        )
    if not steps:
        raise TrajectoryError("Session contains no exportable steps")
    metrics = [s["metrics"] for s in steps if "metrics" in s and not s.get("is_copied_context")]
    final = {"total_steps": len(steps)}
    if metrics:
        for target, key in (
            ("total_prompt_tokens", "prompt_tokens"),
            ("total_completion_tokens", "completion_tokens"),
            ("total_cached_tokens", "cached_tokens"),
        ):
            if any(key in m for m in metrics):
                final[target] = sum(m.get(key, 0) for m in metrics)
    metadata = {
        "native_session_id": header["id"],
        "native_format_version": 4,
        "status": "incomplete" if incomplete else "closed",
        "header": header,
        "pending_tool_call_ids": pending,
        "turn_endings": endings,
        "omitted_event_counts": dict(omitted),
    }
    if header.get("parentSession"):
        metadata["parent_trajectory_id"] = trajectory_id(
            trial_id, attempt_id, header["parentSession"]
        )
    return {
        "schema_version": "ATIF-v1.8",
        "session_id": trajectory_id(trial_id, attempt_id, "run"),
        "trajectory_id": trajectory_id(trial_id, attempt_id, header["id"]),
        "agent": {"name": "deepseek-harness", "version": harness_version},
        "steps": steps,
        "final_metrics": final,
        "extra": {"dsh": metadata},
    }
