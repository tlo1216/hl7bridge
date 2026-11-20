"""Mapping hooks: replace three small mapping steps with a module of your own.

``listen --mapping-module FILE`` and ``to-fhir --mapping-module FILE`` load
a Python file that defines one or more of these functions:

- ``sex_code(pid8)``: a FHIR gender code, in place of table 0001.
- ``abnormal_flag(obx8)``: a v3 ObservationInterpretation code, in place
  of table 0078. Called once per OBX-8 repetition.
- ``observation_time(obx14, zone)``: an aware datetime or None, in place
  of the OBX-14 time. ``zone`` is the message's default zone.

Each is called with the value as sent.

The file exists for a teaching lab: students fix a seeded bug in a mounted
file without touching the installed package. It runs Python from a path, so
it is for a loopback lab only, never a shared listener.

What a hook returns is checked: a gender or interpretation code outside the
FHIR value set, a naive datetime, or any exception raises a MappingError
with the field position and a fixed reason. The hook's own exception text
is never shown (it can quote the value). A file that defines a name not in
the table is refused, so a typo cannot silently leave the default in place.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from collections.abc import Callable
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from ..errors import ConfigError, MappingError

HOOK_NAMES = ("sex_code", "abnormal_flag", "observation_time")
GENDERS = frozenset({"male", "female", "other", "unknown"})

_active: dict[str, Callable[..., Any]] = {}


def load_mapping_module(path: Path) -> tuple[str, ...]:
    """Load ``path`` and install the hooks it defines. Returns their names.

    Public functions (no leading underscore) defined in the file itself
    must all be hook names, and a hook name must be such a function (a
    lambda, partial or imported callable under a hook name is refused, not
    silently skipped). Raises ConfigError for a file that is missing, is
    not a .py file, fails to import, or breaks either rule. The module is
    in ``sys.modules`` while it runs, so dataclasses and type hints work."""
    if path.suffix != ".py" or not path.is_file():
        raise ConfigError("--mapping-module must name an existing .py file.")
    spec = importlib.util.spec_from_file_location("hl7bridge_mapping_hooks", path)
    if spec is None or spec.loader is None:
        raise ConfigError("--mapping-module could not be loaded as a Python module.")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001  user code, SystemExit included
        raise ConfigError(f"--mapping-module failed to import ({type(exc).__name__}).") from None
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
    misfit = sorted(
        name
        for name in HOOK_NAMES
        if name in vars(module)
        and not (
            inspect.isfunction(vars(module)[name])
            and vars(module)[name].__module__ == module.__name__
        )
    )
    if misfit:
        raise ConfigError(
            f"--mapping-module binds {', '.join(misfit)} to something other than a function"
            " defined in the file; write each hook as a def."
        )
    defined = {
        name: obj
        for name, obj in vars(module).items()
        if inspect.isfunction(obj)
        and not name.startswith("_")
        and obj.__module__ == module.__name__
    }
    unknown = sorted(set(defined) - set(HOOK_NAMES))
    if unknown:
        raise ConfigError(
            f"--mapping-module defines {', '.join(unknown)}, which is not a mapping hook."
            f" Known hooks: {', '.join(HOOK_NAMES)}. Prefix helpers with an underscore."
        )
    reset()
    _active.update(defined)
    return tuple(sorted(defined))


def install(name: str, func: Callable[..., Any]) -> None:
    """Install one hook (tests)."""
    if name not in HOOK_NAMES:
        raise ValueError(name)
    _active[name] = func


def reset() -> None:
    _active.clear()


def active() -> tuple[str, ...]:
    return tuple(sorted(_active))


def _call(
    name: str, field: str, segment: int, *args: Any, check: Callable[[Any], bool] = bool
) -> Any:
    """Run a hook and check its answer, both inside one guard: whatever
    the user's code raises or returns (SystemExit, an unhashable value)
    becomes a MappingError, never an internal error. Only Ctrl+C passes."""
    try:
        value = _active[name](*args)
        ok = check(value)
    except KeyboardInterrupt:
        raise
    except BaseException:  # noqa: BLE001  user code; the text can quote the value
        raise MappingError(field, f"the mapping hook {name} failed", segment=segment) from None
    if not ok:
        raise MappingError(
            field, f"the mapping hook {name} returned no usable value", segment=segment
        )
    return value


def sex_code(raw: str, default: Callable[[str], str]) -> str:
    if "sex_code" not in _active:
        return default(raw)
    code = _call("sex_code", "PID-8", 1, raw, check=lambda v: isinstance(v, str) and v in GENDERS)
    return str(code)


def abnormal_flag(
    raw: str, segment: int, default: Callable[[str], str], allowed: frozenset[str]
) -> str:
    if "abnormal_flag" not in _active:
        return default(raw)
    code = _call(
        "abnormal_flag",
        "OBX-8",
        segment,
        raw,
        check=lambda v: isinstance(v, str) and v in allowed,
    )
    return str(code)


def observation_time(
    raw: str, zone: tzinfo, segment: int, default: Callable[[], str | None]
) -> str | None:
    """The effectiveDateTime for OBX-14: the default unless a hook is in."""
    if "observation_time" not in _active:
        return default()
    if not raw:
        return None
    value = _call(
        "observation_time",
        "OBX-14",
        segment,
        raw,
        zone,
        check=lambda v: v is None or (isinstance(v, datetime) and v.utcoffset() is not None),
    )
    if value is None:
        return None
    from ..store.db import utc_iso

    return utc_iso(value)


__all__ = [
    "HOOK_NAMES",
    "abnormal_flag",
    "active",
    "install",
    "load_mapping_module",
    "observation_time",
    "reset",
    "sex_code",
]
