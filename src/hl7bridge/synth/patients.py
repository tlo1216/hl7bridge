"""Obviously-fake synthetic patients. These pools are also imported by the
PHI-in-logs test to assert that none of these values ever reach log output."""

from __future__ import annotations

from dataclasses import dataclass

FAMILY_NAMES = (
    "ZZZTEST",
    "ZZZSAMPLE",
    "XXSYNTH",
    "QQFAKE",
    "ZZPHANTOM",
    "XXDUMMY",
    "QQSPECIMEN",
    "ZZMOCK",
)

GIVEN_NAMES = (
    "ALPHA",
    "BRAVO",
    "CHARLIE",
    "DELTA",
    "ECHO",
    "FOXTROT",
    "GOLF",
    "HOTEL",
)


@dataclass(frozen=True, slots=True)
class PatientSpec:
    mrn: str
    family: str
    given: str
    birth_date: str  # YYYYMMDD
    sex: str


def make_pool(hospital_id: str, size: int) -> list[PatientSpec]:
    """Deterministic per-hospital pool. The last two entries are the same
    fake person under two MRNs (the seeded unmerged-duplicate defect)."""
    if size < 3 or size > len(FAMILY_NAMES):
        raise ValueError(f"pool size {size} out of range 3..{len(FAMILY_NAMES)}")
    tag = hospital_id[-1]
    pool = [
        PatientSpec(
            mrn=f"M{tag}{1000 + i}",
            family=FAMILY_NAMES[i],
            given=GIVEN_NAMES[i],
            birth_date=f"{1950 + 4 * i}0{1 + i % 9}15",
            sex="F" if i % 2 == 0 else "M",
        )
        for i in range(size)
    ]
    dup_source = pool[size - 2]
    pool[size - 1] = PatientSpec(
        mrn=f"M{tag}{1000 + size - 1}",
        family=dup_source.family,
        given=dup_source.given,
        birth_date=dup_source.birth_date,
        sex=dup_source.sex,
    )
    return pool
