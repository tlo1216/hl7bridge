"""FHIR JSON with decimals kept as sent.

A FHIR decimal keeps its precision in the JSON text: ``12.50`` is not
``12.5`` [R4 json.html]. ``fhir.resources`` turns ``Decimal`` into a float
when it dumps a model, so every Bundle and resource hl7bridge writes, to a
file or to a server, goes through ``resource_dict`` and ``dumps`` here and
nowhere else.

- ``resource_dict(model)`` is the model as a JSON-ready dict with every
  decimal element put back as the ``Decimal`` the model holds.
- ``dumps(value)`` writes such a dict as JSON text. A ``Decimal`` is
  written as its own digits (``12.50``), never through a float.
- ``loads(text)`` reads JSON with every number that has a fraction or an
  exponent kept as a ``Decimal``, so a resource read from a server and
  written back keeps its decimals too.
"""

from __future__ import annotations

import json
import re
import secrets
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

# What a Decimal may print as: plain JSON number syntax, nothing else.
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _restore(model_value: Any, data: Any) -> Any:
    if isinstance(model_value, Decimal):
        return model_value
    if isinstance(model_value, BaseModel) and isinstance(data, dict):
        for name, field in type(model_value).model_fields.items():
            key = field.alias or name
            if key not in data:
                key = name
                if key not in data:
                    continue
            data[key] = _restore(getattr(model_value, name), data[key])
        return data
    if isinstance(model_value, (list, tuple)) and isinstance(data, list):
        return [_restore(m, d) for m, d in zip(model_value, data, strict=False)]
    return data


def resource_dict(model: BaseModel) -> dict[str, Any]:
    """The model as a JSON-ready dict, empty elements left out, with each
    decimal back as its ``Decimal``."""
    data: dict[str, Any] = model.model_dump(mode="json", exclude_none=True)
    restored: dict[str, Any] = _restore(model, data)
    return restored


def dumps(value: Any, *, indent: int | None = None) -> str:
    """JSON text for a dict from ``resource_dict`` or ``loads``. Each
    ``Decimal`` is written with its own digits. The marker that carries
    them through ``json.dumps`` holds a fresh random part on every call,
    so no string in the data can be mistaken for one."""
    marker = f"hl7bridge-decimal-{secrets.token_hex(16)}"

    def default(obj: object) -> str:
        if isinstance(obj, Decimal):
            text = str(obj)
            if not _JSON_NUMBER.fullmatch(text):
                raise ValueError("a decimal that is not a finite JSON number")
            return f"{marker}{text}{marker}"
        raise TypeError(f"{type(obj).__name__} is not JSON serializable")

    text = json.dumps(value, indent=indent, default=default)
    return re.sub(f'"{marker}([^"]*){marker}"', r"\1", text)


def loads(text: str | bytes) -> Any:
    """Parse JSON, keeping numbers with a fraction or exponent as Decimal."""
    return json.loads(text, parse_float=Decimal)


__all__ = ["dumps", "loads", "resource_dict"]
