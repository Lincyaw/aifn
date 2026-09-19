from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field, JsonValue, RootModel

from aifn.submit import (
    Schema,
    Untranslatable,
    bundle,
    parameters,
    refuse_tool,
    submit_tool,
)


class Layer(StrEnum):
    CODE = "code"
    CONFIG = "config"


class Locus(BaseModel):
    file: str
    selector: str = "line:1"


class Site(BaseModel):
    identifier: str = Field(description="which place this is")
    layer: Layer
    locus: Locus | None = None
    tags: list[str] = []


class Surface(BaseModel):
    sites: list[Site]
    note: str = ""


def at(schema: JsonValue, *path: str | int) -> Schema:
    """One node of a translated schema, reached by property name and branch index.

    A helper rather than indexing in place, because every step of a translated schema is a
    `JsonValue` as far as the type checker is concerned, and asserting the shape at each step
    is what keeps these tests from needing a cast that would also hide a wrong shape.
    """
    for step in path:
        if isinstance(step, str):
            assert isinstance(schema, dict), f"{step} is not under an object"
            schema = schema[step]
        else:
            assert isinstance(schema, list), f"branch {step} is not under a union"
            schema = schema[step]
    assert isinstance(schema, dict)
    return schema


def test_a_nested_model_is_translated_in_place_and_not_left_as_a_reference() -> None:
    """A nested model becomes an object in place, because the DSL has no `$ref`.

    `model_json_schema` writes every model once under `$defs` and refers to it, and carrying
    the reference through would hand the harness a schema whose nested shape is a string it
    does not resolve. Refusal at registration is the good case; the bad one is a `$ref` whose
    definition is absent, which registers a property the agent reads as accepting anything.
    """
    schema = parameters(Surface)
    site = at(schema, "sites", "items")
    assert site["type"] == "object"
    assert at(site, "properties", "identifier") == {
        "required": True,
        "type": "string",
        "description": "which place this is",
    }
    assert at(site, "properties", "layer")["enum"] == ["code", "config"]
    assert at(site, "properties", "locus", "oneOf", 0, "properties", "file")["required"] is True


def test_required_is_a_flag_on_the_property_and_never_an_array_beside_it() -> None:
    """A required property carries `required: true`, and an optional one carries nothing.

    This is the DSL's one structural difference from JSON Schema, and the difference a
    translation gets wrong silently: leave the array in place and the harness reads a property
    named `required`, so every field becomes optional and an answer missing half of them is
    accepted as the whole answer.
    """
    schema = parameters(Surface)
    assert at(schema, "sites")["required"] is True
    assert "required" not in at(schema, "note")
    assert "required" not in schema
    site = at(schema, "sites", "items")
    assert "required" not in site
    assert "required" not in at(site, "properties", "tags")


def test_an_optional_field_is_a_union_with_null_and_carries_its_default() -> None:
    """`T | None` translates, because nothing is both an object and null.

    The DSL's `oneOf` means exactly one branch matches while Pydantic's `anyOf` means at least
    one, so the two agree only where the branches cannot overlap. An optional nested model is
    the common case of that, and it has to pass.
    """
    locus = at(parameters(Surface), "sites", "items", "properties", "locus")
    assert [at(locus, "oneOf", index)["type"] for index in (0, 1)] == ["object", "null"]
    assert locus["default"] is None


def test_a_closed_object_stays_closed_so_a_mistyped_field_name_is_answerable() -> None:
    """A nested object declares `additionalProperties: false` explicitly.

    The DSL requires the flag on every nested object rather than defaulting it, and false is
    what makes a mistyped field name reach the agent as a rejected call it can correct within
    the same turn. Ignoring the unknown key instead turns the answer into one missing a
    required field, and the agent is then told about the wrong field.
    """
    site = at(parameters(Surface), "sites", "items")
    assert site["additionalProperties"] is False
    assert at(site, "properties", "locus", "oneOf", 0)["additionalProperties"] is False


def test_a_bound_on_a_value_is_refused_by_name() -> None:
    """A constraint the DSL cannot carry fails, naming the field that carries it.

    `Field(ge=...)` is enforced by the model and invisible to the DSL, so dropping it leaves the
    agent answering 0 against a schema that permitted it and the caller rejecting the answer
    for a rule it was never shown. The whole call is spent on that, and the retry repeats it.
    """

    class Bounded(BaseModel):
        count: int = Field(ge=1)

    with pytest.raises(Untranslatable, match="Bounded.count carries minimum"):
        parameters(Bounded)


def test_a_pattern_on_a_string_is_refused_by_name() -> None:
    """A string constrained by a pattern fails rather than becoming a plain string.

    An identifier grammar is the most common thing a pattern holds, and it is exactly what an
    agent gets wrong: as a bare string, every answer passes the tool and the grammar is
    enforced only once the session is over and there is nobody to tell.
    """

    class Identified(BaseModel):
        site: str = Field(pattern=r"^[a-z]+:[a-z]+$")

    with pytest.raises(Untranslatable, match="Identified.site carries pattern"):
        parameters(Identified)


def test_a_fixed_length_tuple_is_refused_because_positions_have_no_place() -> None:
    """A tuple of two different types fails: the DSL's array states one element type.

    Pydantic writes `prefixItems`, saying what each position is, and the DSL has one `items`
    for all of them. Translated as the first position's type, an answer whose second element is
    a string is refused with nothing said about position.
    """

    class Paired(BaseModel):
        span: tuple[int, str]

    with pytest.raises(Untranslatable, match=r"Paired.span carries (prefixItems|maxItems)"):
        parameters(Paired)


def test_a_recursive_model_is_refused_rather_than_inlined_forever() -> None:
    """A type referring to itself fails, because inlining it does not terminate.

    The DSL states every property in place with no reference to name one with, so there is no
    shape for this at all. Failing here names the field; the alternative is a translation that
    recurses until the stack ends.
    """

    class Node(BaseModel):
        name: str
        children: list[Node] = []

    with pytest.raises(Untranslatable, match="refers to #/\\$defs/Node while"):
        parameters(Node)


def test_a_discriminated_union_is_refused_because_nothing_tells_its_branches_apart() -> None:
    """A tagged union fails: its branches are all objects, and the DSL has no discriminator.

    As `oneOf`, exactly one branch has to match, and two object branches both match an answer
    that happens to fill the field names they share. The tool then refuses a valid answer and
    says only that it matched more than one branch, which the agent cannot act on.
    """

    class Circle(BaseModel):
        kind: Literal["circle"] = "circle"
        radius: float

    class Square(BaseModel):
        kind: Literal["square"] = "square"
        side: float

    class Shape(BaseModel):
        shape: Annotated[Circle | Square, Field(discriminator="kind")]

    with pytest.raises(Untranslatable, match="Shape.shape is a discriminated union"):
        parameters(Shape)


def test_a_union_of_two_number_types_is_refused_as_overlapping() -> None:
    """`int | float` fails, because every integer satisfies both branches.

    JSON's integer is a number, so the DSL's "exactly one branch" can never hold for an
    integral value. Translating it anyway produces a tool that refuses 3 and accepts 3.5, which
    is the kind of behaviour that reads as the model having failed.
    """

    class Measured(BaseModel):
        value: int | float

    with pytest.raises(Untranslatable, match="Measured.value is a union over"):
        parameters(Measured)


def test_a_mapping_of_models_is_refused_while_one_of_any_json_translates() -> None:
    """An open mapping survives only when its values are any JSON at all.

    The DSL's object holds named properties, so the value type of a mapping with arbitrary keys
    has nowhere to go. `dict[str, Any]` is the case where there is no value type to lose, and it
    is common enough in an output type to be worth carrying; `dict[str, int]` translated would
    have the agent told nothing about what a value is.
    """

    class Counted(BaseModel):
        values: dict[str, int]

    class Anything(BaseModel):
        payload: dict[str, Any]

    with pytest.raises(Untranslatable, match="Counted.values is a mapping of"):
        parameters(Counted)
    assert at(parameters(Anything), "payload") == {
        "required": True,
        "type": "object",
        "additionalProperties": True,
    }


def test_an_output_type_that_is_not_an_object_is_refused_at_the_root() -> None:
    """An output type that is not an object of fields cannot be a tool's arguments.

    A submit tool's parameters are the fields of one model, so a root model holding a list has
    nothing to name its one value by. Failing at the root says so; the alternative is a tool
    registered with no parameters at all, which accepts an empty call as the answer.
    """

    class Names(RootModel[list[str]]):
        pass

    with pytest.raises(Untranslatable, match="Names cannot be a tool's arguments"):
        parameters(Names)


def test_an_output_type_with_no_fields_is_a_tool_that_takes_no_arguments() -> None:
    """A model with no fields translates to an empty parameter map.

    An output type saying the answer has no content is a real declaration: the call itself is
    then the whole answer, which is what a function reporting only that it finished returns. An
    empty map is the DSL's tool with no arguments, and it has to be distinct from a failure to
    translate.
    """

    class Acknowledged(BaseModel):
        pass

    assert parameters(Acknowledged) == {}


def test_the_bundle_names_both_tools_after_the_function() -> None:
    """A bundle carries both names, and both hold the function's name.

    Every function in one dsh home shares the profile, so tools named `submit` and `refuse`
    would have a session register the names another call is also using. The names are derived
    once, here, and a transcript is read by looking for exactly these strings.
    """
    configured = bundle(Surface, "list_services")
    assert configured["submit"] == submit_tool("list_services") == "submit_list_services"
    assert configured["refuse"] == refuse_tool("list_services") == "refuse_list_services"
    assert configured["parameters"] == parameters(Surface)
    description = configured["submitDescription"]
    assert isinstance(description, str)
    assert "list_services" in description
