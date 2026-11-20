"""Bundled hospital profiles, and where each command finds its profiles and
database.

The YAML files in this folder ship inside the wheel, so a ``pip install``
user has working profiles with no clone. ``hl7bridge init`` copies them
into a folder of your own for editing.

Lookup order for the profile folder:

1. ``--config PATH``
2. the ``HL7BRIDGE_CONFIG`` environment variable
3. ``./hl7bridge/profiles`` (what ``hl7bridge init`` writes)
4. the bundled profiles in this folder

Lookup order for the database: ``--db PATH``, then ``HL7BRIDGE_DB``, then
``./hl7bridge.db``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BUNDLED_PROFILES = Path(__file__).resolve().parent
SAMPLES_DIR = BUNDLED_PROFILES.parent / "samples"

CONFIG_ENV = "HL7BRIDGE_CONFIG"
DB_ENV = "HL7BRIDGE_DB"
LOCAL_PROFILES = Path("hl7bridge") / "profiles"
DEFAULT_DB = Path("hl7bridge.db")


@dataclass(frozen=True, slots=True)
class Resolved:
    """A path and the rule that chose it, for the ``using ...`` line."""

    path: Path
    source: str

    def describe(self) -> str:
        return f"{self.path} ({self.source})"


def resolve_profiles(flag: str | None, *, cwd: Path | None = None) -> Resolved:
    """The profile folder a command should load. A flag or environment value
    is used as given, even if it does not exist, so the error names it."""
    if flag:
        return Resolved(Path(flag), "--config")
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Resolved(Path(env), CONFIG_ENV)
    local = (cwd or Path.cwd()) / LOCAL_PROFILES
    if local.is_dir():
        return Resolved(LOCAL_PROFILES if cwd is None else local, "./hl7bridge/profiles")
    return Resolved(BUNDLED_PROFILES, "bundled")


def resolve_db(flag: str | None) -> Resolved:
    """The database file a command should open."""
    if flag:
        return Resolved(Path(flag), "--db")
    env = os.environ.get(DB_ENV)
    if env:
        return Resolved(Path(env), DB_ENV)
    return Resolved(DEFAULT_DB, "default")
