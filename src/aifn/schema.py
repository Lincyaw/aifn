"""Construct JSON-native Pydantic objects after the Node plugin's schema check."""

from __future__ import annotations

from enum import Enum
from hashlib import sha256
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from aifn.storage import readable_json


def schema_hash(model: type[BaseModel]) -> str:
    return sha256(readable_json(model.model_json_schema()).encode()).hexdigest()


def _field_key(name: str, field: FieldInfo, *, construction: bool = False) -> str:
    keys = (field.alias, field.validation_alias, name) if construction else (
        field.validation_alias, field.alias, name,
    )
    key = next(value for value in keys if value is not None)
    if not isinstance(key, str):
        raise ValueError("JSON Schema mode supports string field aliases only")
    return key


def _ensure_literal(value: Any) -> None:
    scalar = value.value if isinstance(value, Enum) else value
    if type(scalar) not in (str, int, float, bool, type(None)):
        raise ValueError("JSON Schema mode requires JSON scalar Enum/Literal values")


def _annotation(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated and any(
        type(item).__name__ == "Json" for item in get_args(annotation)[1:]
    ):
        raise ValueError("Json[...] fields require pydantic validation mode")
    return get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation


def ensure_constructible(model: type[BaseModel], seen: set[type[BaseModel]] | None = None) -> None:
    """Reject types needing Python validation/coercion before starting a task."""
    visited = set() if seen is None else seen
    if model in visited:
        return
    visited.add(model)
    if model.model_post_init is not BaseModel.model_post_init:
        raise ValueError("model_post_init requires pydantic validation mode")
    aliases: set[str] = set()
    for field in model.model_fields.values():
        if (field.alias is not None and field.validation_alias is not None
                and field.alias != field.validation_alias):
            raise ValueError("Distinct validation/construction aliases require pydantic mode")
        if field.default_factory is not None:
            raise ValueError("default_factory requires pydantic validation mode")
        if field.validation_alias is not None and not isinstance(field.validation_alias, str):
            raise ValueError("JSON Schema mode supports string field aliases only")
        if any(type(item).__name__ == "Json" for item in field.metadata):
            raise ValueError("Json[...] fields require pydantic validation mode")
        _ensure(field.annotation, visited)
    for name, field in model.model_fields.items():
        key = _field_key(name, field, construction=True)
        if key in aliases:
            raise ValueError("JSON Schema mode requires unique field aliases")
        aliases.add(key)


def _ensure(annotation: Any, seen: set[type[BaseModel]]) -> None:
    annotation = _annotation(annotation)
    origin, arguments = get_origin(annotation), get_args(annotation)
    if annotation in (set, frozenset) or origin in (set, frozenset):
        raise ValueError("Use list instead of set/frozenset in JSON Schema mode")
    if annotation in (Any, str, int, float, bool, type(None), list, dict, tuple):
        return
    if origin is Literal:
        for item in arguments:
            _ensure_literal(item)
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        ensure_constructible(annotation, seen)
        return
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        for member in annotation:
            _ensure_literal(member)
        return
    if origin in (Union, UnionType):
        choices = [item for item in arguments if item is not type(None)]
        if len(choices) == 1:
            _ensure(choices[0], seen)
            return
        raise ValueError("JSON Schema mode supports optional unions only; use pydantic mode")
    if origin in (list, set, frozenset, tuple, dict):
        if origin is dict and arguments[0] is not str:
            raise ValueError("JSON Schema mode requires string dictionary keys")
        for item in arguments:
            if item is not Ellipsis:
                _ensure(item, seen)
        return
    raise ValueError(f"JSON Schema mode cannot construct {annotation!r}; use pydantic mode")


def construct_model[O: BaseModel](model: type[O], value: Any) -> O:
    """Deserialize already validated JSON without model_validate or validators."""
    if model.__pydantic_root_model__:
        return model.model_construct(root=_construct(model.model_fields["root"].annotation, value))
    fields: dict[str, Any] = {}
    supplied: set[str] = set()
    consumed: set[str] = set()
    for name, field in model.model_fields.items():
        key = _field_key(name, field)
        if key in value:
            construction_key = _field_key(name, field, construction=True)
            fields[construction_key] = _construct(field.annotation, value[key])
            supplied.add(name)
            consumed.add(key)
    if model.model_config.get("extra") == "allow":
        fields.update({key: item for key, item in value.items() if key not in consumed})
    return model.model_construct(_fields_set=supplied, **fields)


def _construct(annotation: Any, value: Any) -> Any:
    annotation = _annotation(annotation)
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        return next((item for item in arguments if isinstance(item, Enum) and item.value == value),
                    value)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return construct_model(annotation, value)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if value is None:
        return None
    if annotation in (tuple, set, frozenset):
        return annotation(value)
    if origin in (Union, UnionType):
        return _construct(next(item for item in arguments if item is not type(None)), value)
    if origin is dict:
        return {key: _construct(arguments[1], item) for key, item in value.items()}
    if origin in (list, set, frozenset):
        return origin(_construct(arguments[0], item) for item in value)
    if origin is tuple:
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(_construct(arguments[0], item) for item in value)
        return tuple(_construct(kind, item) for kind, item in zip(arguments, value, strict=True))
    return float(value) if annotation is float else value
