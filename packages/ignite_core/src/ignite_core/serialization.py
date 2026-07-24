from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass
from enum import Enum
from typing import Any, Union, get_args, get_origin


def from_dict(cls, data):
    if data is None:
        return None
    if not is_dataclass(cls):
        return data

    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        typ = f.type
        origin = get_origin(typ)

        if is_dataclass(typ):
            kwargs[f.name] = from_dict(typ, value)
        elif origin is list:
            inner = get_args(typ)[0]
            kwargs[f.name] = (
                [from_dict(inner, v) for v in value] if is_dataclass(inner) else value
            )
        elif origin is dict:
            kwargs[f.name] = value
        elif origin is Union:
            args = [a for a in get_args(typ) if a is not type(None)]
            if len(args) == 1 and is_dataclass(args[0]):
                kwargs[f.name] = None if value is None else from_dict(args[0], value)
            else:
                kwargs[f.name] = value
        else:
            kwargs[f.name] = value

    return cls(**kwargs)


def to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return to_jsonable(asdict(obj))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
