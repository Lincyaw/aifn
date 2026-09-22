from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

type CallId = str
"""One call's identity, minted by the caller.

It is the request's file name, the claim's file name and the conclusion's file name, so
it has to be safe to use as one. The caller mints it rather than the worker, because the
caller returns the moment the request is written and it needs a name to wait on by then.
"""

type FunctionName = str
"""One AI function's name.

What a worker reads out of a request is a name, which it looks a declaration up by,
so a name plus a registry both sides share is the only way an AI function crosses a
process boundary.
"""

type WorkerId = str
"""One worker process's identity.

A claim records it, which is what tells the claim a killed worker left behind from the
claim of one that is still running.
"""

type SessionId = str
"""One agent session's identity.

The same id continues the same durable conversation, so every attempt needs a new one:
reusing the last attempt's id has the model answering with the answer that was just
rejected still in its history.
"""

type Event = Mapping[str, JsonValue]
"""One line of a transcript.

What the harness hands over is JSON, and this does not model it: reading an answer only
cares about `tool/call` and `tool/result`, and typing the several dozen other kinds
would only become something to maintain the next time the harness adds one.
"""

type JsonObject = Mapping[str, JsonValue]
"""A JSON object.

A request body and a submit tool's arguments are JSON between processes, validated into
declared models on each side, with no other representation in between.
"""
