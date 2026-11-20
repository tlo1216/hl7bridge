"""``hl7bridge init``: copy the bundled profiles and sample messages into a
folder of your own.

It never deletes anything and never overwrites a file unless ``--force``
is given. Sample messages are copied byte for byte, so their bare CR
segment ends survive on every OS.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .errors import UsageError
from .profiles import BUNDLED_PROFILES, SAMPLES_DIR

DEFAULT_FOLDER = Path("hl7bridge")


@dataclass(slots=True)
class InitResult:
    profiles: list[Path] = field(default_factory=list)
    samples: list[Path] = field(default_factory=list)


def bundled_files() -> tuple[list[Path], list[Path]]:
    return sorted(BUNDLED_PROFILES.glob("*.yaml")), sorted(SAMPLES_DIR.glob("*.hl7"))


def init_folder(dest: Path, *, force: bool = False) -> InitResult:
    profiles, samples = bundled_files()
    plan = [(src, dest / "profiles" / src.name) for src in profiles]
    plan += [(src, dest / "samples" / src.name) for src in samples]
    if dest.exists() and not dest.is_dir():
        raise UsageError(f"{dest} exists and is not a folder.")
    for folder in (dest / "profiles", dest / "samples"):
        if folder.exists() and not folder.is_dir():
            raise UsageError(f"{folder} exists and is not a folder. Nothing was written.")
    wrong_kind = [target for _, target in plan if target.exists() and not target.is_file()]
    if wrong_kind:
        raise UsageError(
            f"{wrong_kind[0]} exists and is not a file. Nothing was written.",
            hint="Move it, or pick another folder.",
        )
    existing = [target for _, target in plan if target.exists()]
    if existing and not force:
        raise UsageError(
            f"{len(existing)} file(s) already exist under {dest}, for example {existing[0]}."
            " Nothing was written.",
            hint="Pass --force to overwrite them, or pick another folder.",
        )
    result = InitResult()
    for src, target in plan:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        (result.profiles if target.suffix == ".yaml" else result.samples).append(target)
    return result
