"""``--json`` output for the report commands.

Reports are dataclasses. This turns them into plain JSON values: nested
dataclasses become objects, tuples become lists, tuple dictionary keys
(the TAT groups) become ``"MOD/PRIORITY"`` strings, ``sqlite3.Row`` becomes
an object and a ``Counter`` a plain object. Properties that callers rely on
are added by each command, not guessed here.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any


def to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, sqlite3.Row):
        return {key: to_jsonable(value[key]) for key in value.keys()}  # noqa: SIM118
    if isinstance(value, Mapping):
        return {_key(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value) if isinstance(value, (set, frozenset)) else value
        return [to_jsonable(v) for v in items]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot render {type(value).__name__} as JSON")


def _key(key: Any) -> str:
    if isinstance(key, tuple):
        return "/".join(str(part) for part in key)
    return str(key)


def dumps(value: Any) -> str:
    """Stable, indented JSON: the same report gives the same text."""
    return json.dumps(to_jsonable(value), indent=2, sort_keys=True)
