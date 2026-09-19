from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, JsonValue

from aifn.terms import FunctionName

type Schema = dict[str, JsonValue]
"""One property of a tool's arguments, as the harness parameter DSL spells it.

A mutable `dict` rather than the `Mapping` the rest of the library passes JSON around
as: a schema nests inside another schema, and `JsonValue`'s own object is a `dict`, so
a `Mapping` does not fit into one.
"""

type Tools = dict[str, JsonValue]
"""What one AI function needs on the harness side: its two tools, named and shaped.

Named apart from `Schema` although the two are the same shape, because this is what a
patch layer's row carries and a schema is one field inside it. One name for both would
make `patch.Row` read as holding schemas.
"""

SUBMIT_PREFIX: Final = "submit_"
"""The prefix of a submit tool's name; the name is this plus the function's name.

One tool name per function rather than one shared `submit`: with the function's
name in it, calling the wrong tool is "this call was never answered", while under
a shared name it becomes "the answer had the wrong shape", and those two are
indistinguishable in the transcript.
"""

REFUSE_PREFIX: Final = "refuse_"
"""The prefix of a refusal tool's name.

A refusal has to be a tool call too, because it is an answer: let the agent say
"I cannot answer this" in prose and the transcript holds a session with no tool
call in it, which is exactly what a session that ran off the rails looks like.
"""

_SCALARS: Final = frozenset({"string", "number", "integer", "boolean", "null"})
"""The DSL types whose only further constraint is a fixed set of values."""

_ANNOTATIONS: Final = ("description", "default", "examples")
"""The annotation keys the DSL carries through to the agent.

`title` is not among them although the DSL accepts it: Pydantic invents one for
every field out of the field's own name, so carrying them would spend the agent's
context restating the property names it is already reading.
"""

_IGNORED: Final = frozenset({"title", "$ref"})
"""Keys that reach a node and mean nothing here, so they are neither carried nor refused."""


class Refusal(BaseModel):
    """The refusal tool's arguments, the same shape for every AI function.

    One field, holding the reason: a refusal is not a special case of this
    function's output type, it is "this input has no output", so its shape has
    nothing to do with what the function is.
    """

    reason: str


class Untranslatable(Exception):
    """An output type the harness parameter DSL has no shape for.

    Raised where the field is found, naming it and what it carries. The
    alternative is a schema that quietly permits more than the output type does:
    the agent answers what it was shown, the answer fails validation on this side,
    and the call is spent discovering that the two sides were never describing the
    same type.
    """


def submit_tool(function: FunctionName) -> str:
    """What one function's submit tool is called."""
    return f"{SUBMIT_PREFIX}{function}"


def refuse_tool(function: FunctionName) -> str:
    """What one function's refusal tool is called."""
    return f"{REFUSE_PREFIX}{function}"


def parameters(returns: type[BaseModel]) -> Schema:
    """Translate an output type into the submit tool's parameter schema.

    The harness parameter DSL is a subset of JSON Schema: it has types, nesting,
    arrays and enums, it has no pattern, no bounds and no cross-field rules, and
    it marks a `required` flag on each property in place of the array beside them.
    So this gets the shape right and the caller's model enforces the rest, and an
    answer that passes the tool's own check but fails the model is `Fault.INVALID`.

    Anything the DSL cannot express is refused here naming the field, rather than
    dropped. One looseness is not a field's own and so cannot be refused: the
    arguments object is open at its root whatever is written there, so a model
    declaring `extra="forbid"` has that enforced on this side only.
    """
    schema = returns.model_json_schema()
    definitions = schema.get("$defs", {})
    origin = schema.get("$ref")
    seen = (origin,) if isinstance(origin, str) else ()
    root = _resolve(schema, definitions, returns.__name__, ())
    members = root.get("properties")
    if root.get("type") != "object" or not isinstance(members, Mapping):
        raise Untranslatable(
            f"{returns.__name__} cannot be a tool's arguments: a submit tool's parameters are the "
            f"fields of a model, and this type is {root.get('type', 'of no single type')} at its "
            "root"
        )
    _reject_unknown(
        root,
        returns.__name__,
        ("type", "properties", "required", "additionalProperties", "$defs"),
    )
    required = _required_names(root, returns.__name__)
    return {
        name: _translate(
            member, definitions, f"{returns.__name__}.{name}", seen, required=name in required
        )
        for name, member in members.items()
    }


def bundle(returns: type[BaseModel], function: FunctionName) -> Tools:
    """Everything one AI function needs on the harness side: its two tools.

    Data rather than JavaScript: the bundle is an installed dsh plugin holding the
    mechanism for answering one call, and what a call is reaches it as the
    configuration of its row. So what this side has to produce is the two names,
    the schema derived from `returns`, and what the agent reads about each tool.
    """
    return {
        "function": function,
        "submit": submit_tool(function),
        "refuse": refuse_tool(function),
        "parameters": parameters(returns),
        "submitDescription": _submit_description(function),
        "refuseDescription": _refuse_description(function),
    }


def _submit_description(function: FunctionName) -> str:
    """What the agent reads about the submit tool."""
    return (
        f"Answer the {function} call with this, once. The arguments are the answer itself, and "
        "this call is the only thing recorded: prose outside it reaches nobody, and a session "
        "that ends without it counts as having produced no answer at all."
    )


def _refuse_description(function: FunctionName) -> str:
    """What the agent reads about the refusal tool."""
    return (
        f"Record that this {function} input has no answer, and why. For an input you have "
        "established cannot be answered, not for one that is taking a while: this is final, the "
        "caller never retries it, and the reason is all they receive."
    )


def _translate(
    node: Mapping[str, Any],
    definitions: Mapping[str, Any],
    path: str,
    seen: tuple[str, ...],
    *,
    required: bool,
) -> Schema:
    """One JSON Schema node as a DSL property, with everything under it.

    `required` is a flag on the property rather than a list beside it, which is
    the DSL's one structural difference from JSON Schema. It is passed in rather
    than read here, because whether a property is required is stated by the object
    that holds it, and array items and union branches are positions where the DSL
    refuses the flag outright.
    """
    reference = node.get("$ref")
    resolved = _resolve(node, definitions, path, seen)
    if isinstance(reference, str):
        seen = (*seen, reference)

    translated: Schema = {"required": True} if required else {}
    for key in _ANNOTATIONS:
        if key in resolved:
            translated[key] = resolved[key]

    if "anyOf" in resolved or "oneOf" in resolved:
        return translated | _union(resolved, definitions, path, seen)

    declared = resolved.get("type")
    if declared is None:
        _reject_unknown(resolved, path, ())
        return translated | {"type": "json"}
    if not isinstance(declared, str):
        raise Untranslatable(
            f"{path} has type {declared!r}: the DSL carries one type per property, so a property "
            "that is several at once has to be written as a union of them"
        )
    if declared in _SCALARS:
        _reject_unknown(resolved, path, ("type", "enum", "const"))
        return translated | _scalar(resolved, declared, path)
    if declared == "array":
        _reject_unknown(resolved, path, ("type", "items"))
        return translated | _array(resolved, definitions, path, seen)
    if declared == "object":
        _reject_unknown(resolved, path, ("type", "properties", "required", "additionalProperties"))
        return translated | _object(resolved, definitions, path, seen)
    raise Untranslatable(f"{path} has type {declared!r}, which the DSL has no counterpart for")


def _scalar(node: Mapping[str, Any], declared: str, path: str) -> Schema:
    """A leaf, with the fixed set of values it is restricted to when it has one."""
    scalar: Schema = {"type": declared}
    if "const" in node:
        scalar["const"] = node["const"]
    if "enum" in node:
        values = node["enum"]
        if not isinstance(values, Sequence) or isinstance(values, str) or len(values) == 0:
            raise Untranslatable(f"{path} has enum {values!r}, which is empty or not a list")
        scalar["enum"] = list(values)
    return scalar


def _array(
    node: Mapping[str, Any],
    definitions: Mapping[str, Any],
    path: str,
    seen: tuple[str, ...],
) -> Schema:
    """An array, and what one of its elements is.

    Length and uniqueness have already been refused by the caller's key check: the
    DSL states what an element is and nothing about how many there are.
    """
    array: Schema = {"type": "array"}
    if "items" in node:
        array["items"] = _translate(node["items"], definitions, f"{path}[]", seen, required=False)
    return array


def _object(
    node: Mapping[str, Any],
    definitions: Mapping[str, Any],
    path: str,
    seen: tuple[str, ...],
) -> Schema:
    """A nested object, its properties, and which of them are required.

    A mapping whose keys are not known in advance survives this only when its
    values are any JSON at all: the DSL's object holds named properties, so the
    value type of an open mapping has nowhere to go, and a schema that dropped it
    would take a mapping of the wrong value type without the agent ever seeing
    that it had been allowed to.

    A model's own object is where Pydantic writes no `additionalProperties` at all,
    and false is what goes in its place. Refusing an unknown key is the useful
    direction: the agent reads the refusal and can correct a mistyped field name in
    the same turn, while ignoring the key turns the answer into one missing a
    required field and the agent is told about the wrong field.
    """
    extra = node.get("additionalProperties", False)
    if not isinstance(extra, bool):
        raise Untranslatable(
            f"{path} is a mapping of {extra!r}: the DSL's object carries named properties, so the "
            "value type of a mapping with arbitrary keys has nowhere to go"
        )
    nested: Schema = {"type": "object", "additionalProperties": extra}
    members = node.get("properties")
    if members is None:
        return nested
    if not isinstance(members, Mapping):
        raise Untranslatable(f"{path} has properties {members!r}, which is not a mapping")
    required = _required_names(node, path)
    nested["properties"] = {
        name: _translate(member, definitions, f"{path}.{name}", seen, required=name in required)
        for name, member in members.items()
    }
    return nested


def _union(
    node: Mapping[str, Any],
    definitions: Mapping[str, Any],
    path: str,
    seen: tuple[str, ...],
) -> Schema:
    """A union, translatable only when no value could satisfy two of its branches.

    The DSL's `oneOf` means exactly one branch matches while the `anyOf` Pydantic
    writes means at least one, so the two say the same thing only for branches
    nothing satisfies at once. That covers what unions are mostly used for: `T |
    None`, and a choice between two different JSON types. It does not cover a
    discriminated union, whose branches are all objects, and refusing that is the
    point rather than a gap: as `oneOf`, an answer matching two branches is
    rejected by the tool for a reason the agent has no way to read.
    """
    if "discriminator" in node:
        raise Untranslatable(
            f"{path} is a discriminated union: the DSL has no discriminator, and its branches are "
            "all objects, so nothing in it tells one branch from another"
        )
    _reject_unknown(node, path, ("anyOf", "oneOf"))
    branches = node.get("anyOf", node.get("oneOf"))
    if not isinstance(branches, Sequence) or len(branches) < 2:
        raise Untranslatable(f"{path} is a union of {branches!r}, which has fewer than two members")
    tags = [
        _tag(_resolve(branch, definitions, f"{path}|{index}", seen), f"{path}|{index}")
        for index, branch in enumerate(branches)
    ]
    if len(set(tags)) != len(tags) or {"integer", "number"} <= set(tags):
        raise Untranslatable(
            f"{path} is a union over {', '.join(tags)}: the DSL's branches have to be types no "
            "single value belongs to two of, and these overlap"
        )
    return {
        "oneOf": [
            _translate(branch, definitions, f"{path}|{index}", seen, required=False)
            for index, branch in enumerate(branches)
        ]
    }


def _tag(node: Mapping[str, Any], path: str) -> str:
    """Which JSON type one union branch is, for deciding whether two branches overlap."""
    declared = node.get("type")
    if isinstance(declared, str):
        return declared
    raise Untranslatable(
        f"{path} is a union branch of no single type: whether two branches overlap is decided by "
        "their types, and a branch holding any JSON value overlaps with every other"
    )


def _resolve(
    node: Mapping[str, Any],
    definitions: Mapping[str, Any],
    path: str,
    seen: tuple[str, ...],
) -> Mapping[str, Any]:
    """One node with its `$ref` replaced by what it points at.

    A reference already being resolved is a recursive type, and the DSL has no
    reference of its own to state one with, so inlining would not terminate. The
    node's own keys win over the definition's, because Pydantic writes a field's
    description beside the reference rather than into the model it names.
    """
    reference = node.get("$ref")
    if not isinstance(reference, str):
        return node
    if reference in seen:
        raise Untranslatable(
            f"{path} refers to {reference} while that is still being resolved: the type is "
            "recursive, and the DSL states every property in place with no reference to name one"
        )
    name = reference.removeprefix("#/$defs/")
    if name == reference or name not in definitions:
        raise Untranslatable(f"{path} refers to {reference}, which is not one of this type's $defs")
    target = definitions[name]
    if not isinstance(target, Mapping):
        raise Untranslatable(f"{path} refers to {reference}, which is not a schema")
    return {**target, **{key: value for key, value in node.items() if key != "$ref"}}


def _required_names(node: Mapping[str, Any], path: str) -> frozenset[str]:
    """The names an object's `required` array holds."""
    names = node.get("required", ())
    if not isinstance(names, Sequence) or isinstance(names, str):
        raise Untranslatable(f"{path} has required {names!r}, which is not a list of names")
    return frozenset(str(name) for name in names)


def _reject_unknown(node: Mapping[str, Any], path: str, allowed: tuple[str, ...]) -> None:
    """Refuse the keys this position has no place for, naming the first one found.

    The gap between JSON Schema and the DSL is exactly a set of keys: `pattern`,
    `minimum`, `minItems`, `uniqueItems`, `prefixItems`, `format`. Each of them is
    a constraint the agent would never be shown while the caller's model still
    enforces it, which reaches the agent as a rejected answer with nothing said
    about what to change.
    """
    for key in node:
        if key in allowed or key in _ANNOTATIONS or key in _IGNORED:
            continue
        raise Untranslatable(f"{path} carries {key}, which the DSL has no place for")
