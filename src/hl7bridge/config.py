"""Per-hospital configuration (field locations live here, not in code).

Validation is strict and loud: unknown keys, bad timezones, bad field references,
and duplicate facility codes all fail at load time with the offending file named.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .errors import ConfigError

# Fields a hospital may use for the accession number. "SEG-N" refers to the
# HL7 spec position (OBR-3 is the filler order number, ORC-3 its ORC twin).
ALLOWED_ACCESSION_FIELDS = ("OBR-2", "OBR-3", "ORC-2", "ORC-3")

KNOWN_KEYS = {
    "hospital_id",
    "display_name",
    "facility_codes",
    "accession_field",
    "timezone",
    "hl7_version",
    "timestamps_include_offset",
    "expected_message_types",
    "procedure_code_system",
    "z_segment",
    "reconcile",
    "ack_timeout_minutes",
    "tat",
    "lint",
    "fhir",
}

KNOWN_RECONCILE_KEYS = {
    "order_without_image_hours",
    "order_without_image_hours_by_modality",
    "order_lag_minutes",
}

KNOWN_TAT_KEYS = {
    "exam_to_final_target_hours",
    "exam_to_final_target_hours_by_priority",
}

KNOWN_LINT_KEYS = {"disabled_rules"}

KNOWN_FHIR_KEYS = {"identifier_systems", "patient_policy", "tag", "allowed_processing_ids"}
PATIENT_POLICIES = ("create", "match_only")
# MSH-11 values the direct-to-FHIR listener takes when a profile names none:
# training and debugging traffic only, never production (P).
DEFAULT_PROCESSING_IDS = ("T", "D")
# An HL7 table 0103 processing id, and a FHIR tag code: short plain tokens.
_PROCESSING_ID = re.compile(r"[A-Z]")
_TAG_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# The identifier kinds the v2-to-FHIR mapping writes. Each defaults to
# urn:hl7bridge:<hospital id, lower case>:<kind>.
IDENTIFIER_KINDS = ("mrn", "accession", "placer", "visit", "observation")
# Characters a FHIR token search treats as special. A system holding one
# would break the conditional update URL (identifier=system|value).
_TOKEN_SPECIAL = ("|", ",", "$", "\\", " ")


def _unknown_keys_message(
    source: str, unknown: set[str], allowed: set[str], section: str = ""
) -> str:
    """``hospb.yaml: unknown key 'acession_field'. Did you mean 'accession_field'?``

    One unknown key gets a close-match suggestion when there is one. Several
    unknown keys are listed with the allowed set."""
    where = f" under '{section}'" if section else ""
    names = sorted(str(k) for k in unknown)
    if len(names) == 1:
        name = names[0]
        close = difflib.get_close_matches(name, sorted(allowed), n=1, cutoff=0.6)
        if close:
            return f"{source}: unknown key '{name}'{where}. Did you mean '{close[0]}'?"
        return f"{source}: unknown key '{name}'{where}. Allowed: {', '.join(sorted(allowed))}"
    listed = ", ".join(f"'{n}'" for n in names)
    return f"{source}: unknown keys {listed}{where}. Allowed: {', '.join(sorted(allowed))}"


# Lint rule ids look like HL7-010; the lint runner validates that each
# disabled id actually exists (config stays import-cycle-free of lint).
_LINT_RULE_ID_SHAPE = ("HL7-", 7)


@dataclass(frozen=True, slots=True)
class ReconcileConfig:
    """Thresholds for order-to-image reconciliation."""

    order_without_image_hours: float = 4.0
    order_without_image_hours_by_modality: dict[str, float] = field(default_factory=dict)
    # Grace period before an unmatched study is flagged, to tolerate
    # late-arriving orders.
    order_lag_minutes: float = 60.0

    def threshold_hours(self, modality: str) -> float:
        return self.order_without_image_hours_by_modality.get(
            modality, self.order_without_image_hours
        )


@dataclass(frozen=True, slots=True)
class TatConfig:
    """Turnaround-time targets. The breach comparison is strict:
    a report exactly on target is not a breach."""

    exam_to_final_target_hours: float = 2.0
    exam_to_final_target_hours_by_priority: dict[str, float] = field(default_factory=dict)

    def target_hours(self, priority: str | None) -> float:
        if priority is not None and priority in self.exam_to_final_target_hours_by_priority:
            return self.exam_to_final_target_hours_by_priority[priority]
        return self.exam_to_final_target_hours


@dataclass(frozen=True, slots=True)
class LintConfig:
    """Per-hospital lint profile."""

    disabled_rules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IdentifierSystems:
    """FHIR identifier systems for one hospital, from the profile's
    ``fhir.identifier_systems`` block, defaults filled in."""

    mrn: str
    accession: str
    placer: str
    visit: str
    observation: str

    @classmethod
    def defaults(cls, hospital_id: str) -> IdentifierSystems:
        base = f"urn:hl7bridge:{hospital_id.lower()}"
        return cls(**{kind: f"{base}:{kind}" for kind in IDENTIFIER_KINDS})


@dataclass(frozen=True, slots=True)
class FhirSettings:
    """The rest of the profile's ``fhir`` block, read by the direct-to-FHIR
    listener (``listen --target fhir``).

    ``patient_policy``: ``create`` (the default, what ``to-fhir --post``
    and ``fhir sync`` do) or ``match_only`` (find the Patient by MRN, never
    write it; the direct listener takes only this). ``mrn_by_authority``
    maps a PID-3.4 assigning authority to the MRN identifier system. ``tag``
    is the lab tag (system, code) every direct write carries.
    ``allowed_processing_ids`` are the MSH-11 values direct mode accepts
    (default T and D).
    """

    patient_policy: str = "create"
    mrn_by_authority: tuple[tuple[str, str], ...] = ()
    tag: tuple[str, str] | None = None
    allowed_processing_ids: tuple[str, ...] = DEFAULT_PROCESSING_IDS

    def mrn_system(self, authority: str, fallback: str) -> str | None:
        """The MRN system for a PID-3.4 authority: the mapped one, the
        fallback when PID-3.4 is empty, and None for a valued authority the
        profile does not map (another authority's MRN under the default
        system could be another patient)."""
        if not authority:
            return fallback
        return dict(self.mrn_by_authority).get(authority)


@dataclass(frozen=True, slots=True)
class HospitalConfig:
    hospital_id: str
    display_name: str
    facility_codes: tuple[str, ...]
    accession_field: str
    timezone: str
    expected_message_types: tuple[str, ...]
    procedure_code_system: str = "local"
    hl7_version: str = "2.3"
    timestamps_include_offset: bool = False
    z_segment: str | None = None
    reconcile: ReconcileConfig = field(default_factory=ReconcileConfig)
    ack_timeout_minutes: float = 30.0
    tat: TatConfig = field(default_factory=TatConfig)
    lint: LintConfig = field(default_factory=LintConfig)
    identifier_systems: IdentifierSystems | None = None
    fhir: FhirSettings = field(default_factory=FhirSettings)

    @property
    def systems(self) -> IdentifierSystems:
        """The identifier systems, with the defaults when the profile has none."""
        if self.identifier_systems is not None:
            return self.identifier_systems
        return IdentifierSystems.defaults(self.hospital_id)

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _require_str(data: dict[str, object], key: str, source: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{source}: '{key}' must be a non-empty string, got {value!r}")
    return value.strip()


def _str_tuple(data: dict[str, object], key: str, source: str) -> tuple[str, ...]:
    value = data.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and v.strip() for v in value)
    ):
        raise ConfigError(f"{source}: '{key}' must be a non-empty list of strings, got {value!r}")
    return tuple(v.strip() for v in value)


def load_hospital_config(path: Path) -> HospitalConfig:
    source = str(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at top level, got {type(data).__name__}")

    unknown = set(data) - KNOWN_KEYS
    if unknown:
        raise ConfigError(_unknown_keys_message(source, unknown, KNOWN_KEYS))

    accession_field = _require_str(data, "accession_field", source)
    if accession_field not in ALLOWED_ACCESSION_FIELDS:
        raise ConfigError(
            f"{source}: accession_field {accession_field!r} not in {ALLOWED_ACCESSION_FIELDS}"
        )

    tz_name = _require_str(data, "timezone", source)
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"{source}: unknown IANA timezone {tz_name!r}") from exc

    reconcile_raw = data.get("reconcile", {})
    if not isinstance(reconcile_raw, dict):
        raise ConfigError(f"{source}: 'reconcile' must be a mapping")
    unknown_rec = set(reconcile_raw) - KNOWN_RECONCILE_KEYS
    if unknown_rec:
        raise ConfigError(
            _unknown_keys_message(source, unknown_rec, KNOWN_RECONCILE_KEYS, "reconcile")
        )
    by_modality_raw = reconcile_raw.get("order_without_image_hours_by_modality", {})
    if not isinstance(by_modality_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, (int, float)) for k, v in by_modality_raw.items()
    ):
        raise ConfigError(
            f"{source}: order_without_image_hours_by_modality must map modality to hours"
        )
    reconcile = ReconcileConfig(
        order_without_image_hours=float(reconcile_raw.get("order_without_image_hours", 4.0)),
        order_without_image_hours_by_modality={k: float(v) for k, v in by_modality_raw.items()},
        order_lag_minutes=float(reconcile_raw.get("order_lag_minutes", 60.0)),
    )

    tat_raw = data.get("tat", {})
    if not isinstance(tat_raw, dict):
        raise ConfigError(f"{source}: 'tat' must be a mapping")
    unknown_tat = set(tat_raw) - KNOWN_TAT_KEYS
    if unknown_tat:
        raise ConfigError(_unknown_keys_message(source, unknown_tat, KNOWN_TAT_KEYS, "tat"))
    by_priority_raw = tat_raw.get("exam_to_final_target_hours_by_priority", {})
    if not isinstance(by_priority_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, (int, float)) for k, v in by_priority_raw.items()
    ):
        raise ConfigError(
            f"{source}: exam_to_final_target_hours_by_priority must map priority to hours"
        )
    tat = TatConfig(
        exam_to_final_target_hours=float(tat_raw.get("exam_to_final_target_hours", 2.0)),
        exam_to_final_target_hours_by_priority={k: float(v) for k, v in by_priority_raw.items()},
    )

    lint_raw = data.get("lint", {})
    if not isinstance(lint_raw, dict):
        raise ConfigError(f"{source}: 'lint' must be a mapping")
    unknown_lint = set(lint_raw) - KNOWN_LINT_KEYS
    if unknown_lint:
        raise ConfigError(_unknown_keys_message(source, unknown_lint, KNOWN_LINT_KEYS, "lint"))
    disabled_raw = lint_raw.get("disabled_rules", [])
    prefix, id_len = _LINT_RULE_ID_SHAPE
    if not isinstance(disabled_raw, list) or not all(
        isinstance(r, str) and r.startswith(prefix) and len(r) == id_len for r in disabled_raw
    ):
        raise ConfigError(
            f"{source}: lint.disabled_rules must be a list of rule ids like 'HL7-010'"
        )
    lint = LintConfig(disabled_rules=tuple(disabled_raw))

    hospital_id = _require_str(data, "hospital_id", source)
    systems, fhir_settings = _load_fhir_block(data.get("fhir", {}), hospital_id, source)

    z_segment_raw = data.get("z_segment")
    if z_segment_raw is not None and not isinstance(z_segment_raw, str):
        raise ConfigError(f"{source}: 'z_segment' must be a string if present")

    ack_timeout_raw = data.get("ack_timeout_minutes", 30.0)
    if not isinstance(ack_timeout_raw, (int, float)) or ack_timeout_raw <= 0:
        raise ConfigError(f"{source}: 'ack_timeout_minutes' must be a positive number")

    include_offset = data.get("timestamps_include_offset", False)
    if not isinstance(include_offset, bool):
        raise ConfigError(f"{source}: 'timestamps_include_offset' must be a boolean")

    return HospitalConfig(
        hospital_id=hospital_id,
        display_name=_require_str(data, "display_name", source),
        facility_codes=_str_tuple(data, "facility_codes", source),
        accession_field=accession_field,
        timezone=tz_name,
        expected_message_types=_str_tuple(data, "expected_message_types", source),
        procedure_code_system=str(data.get("procedure_code_system", "local")),
        hl7_version=str(data.get("hl7_version", "2.3")),
        timestamps_include_offset=include_offset,
        z_segment=z_segment_raw,
        reconcile=reconcile,
        ack_timeout_minutes=float(ack_timeout_raw),
        tat=tat,
        lint=lint,
        identifier_systems=systems,
        fhir=fhir_settings,
    )


def _is_system(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(("urn:", "http://", "https://"))
        and not any(ch in value for ch in _TOKEN_SPECIAL)
    )


_SYSTEM_RULE = "must be a urn: or http(s) URI with no '|', ',', '$', backslash or space"


def _load_fhir_block(
    raw: object, hospital_id: str, source: str
) -> tuple[IdentifierSystems, FhirSettings]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: 'fhir' must be a mapping")
    unknown = set(raw) - KNOWN_FHIR_KEYS
    if unknown:
        raise ConfigError(_unknown_keys_message(source, unknown, KNOWN_FHIR_KEYS, "fhir"))
    given = raw.get("identifier_systems", {})
    if not isinstance(given, dict):
        raise ConfigError(f"{source}: 'fhir.identifier_systems' must be a mapping")
    allowed_keys = {*IDENTIFIER_KINDS, "mrn_by_authority"}
    unknown = set(given) - allowed_keys
    if unknown:
        raise ConfigError(
            _unknown_keys_message(source, unknown, allowed_keys, "fhir.identifier_systems")
        )
    values = {
        kind: getattr(IdentifierSystems.defaults(hospital_id), kind) for kind in IDENTIFIER_KINDS
    }
    for kind, value in given.items():
        if kind == "mrn_by_authority":
            continue
        if not _is_system(value):
            raise ConfigError(f"{source}: fhir.identifier_systems.{kind} {_SYSTEM_RULE}")
        values[kind] = value
    by_authority = given.get("mrn_by_authority", {})
    if not isinstance(by_authority, dict) or not all(
        isinstance(k, str) and k and _is_system(v) for k, v in by_authority.items()
    ):
        raise ConfigError(
            f"{source}: fhir.identifier_systems.mrn_by_authority must map an assigning"
            f" authority (PID-3.4) to a system that {_SYSTEM_RULE}"
        )

    policy = raw.get("patient_policy", "create")
    if policy not in PATIENT_POLICIES:
        raise ConfigError(
            f"{source}: fhir.patient_policy must be one of {', '.join(PATIENT_POLICIES)}"
        )

    tag_raw = raw.get("tag")
    tag: tuple[str, str] | None = None
    if tag_raw is not None:
        if (
            not isinstance(tag_raw, dict)
            or set(tag_raw) != {"system", "code"}
            or not _is_system(tag_raw["system"])
            or not isinstance(tag_raw["code"], str)
            or not _TAG_CODE.fullmatch(tag_raw["code"])
        ):
            raise ConfigError(
                f"{source}: fhir.tag must be a mapping with a system and a code; the"
                f" system {_SYSTEM_RULE}, the code is letters, digits, '.', '_' or '-'"
            )
        tag = (str(tag_raw["system"]).rstrip("/"), str(tag_raw["code"]))

    ids_raw = raw.get("allowed_processing_ids")
    processing = DEFAULT_PROCESSING_IDS
    if ids_raw is not None:
        if (
            not isinstance(ids_raw, list)
            or not ids_raw
            or not all(isinstance(i, str) and _PROCESSING_ID.fullmatch(i) for i in ids_raw)
        ):
            raise ConfigError(
                f"{source}: fhir.allowed_processing_ids must be a list of MSH-11 codes"
                " such as [T, D]"
            )
        processing = tuple(ids_raw)

    settings = FhirSettings(
        patient_policy=str(policy),
        mrn_by_authority=tuple(sorted((str(k), str(v)) for k, v in by_authority.items())),
        tag=tag,
        allowed_processing_ids=processing,
    )
    return IdentifierSystems(**values), settings


class HospitalRegistry:
    """All configured hospitals, indexed by id and by MSH facility code."""

    def __init__(self, hospitals: list[HospitalConfig]) -> None:
        if not hospitals:
            raise ConfigError("no hospital configurations loaded")
        self.by_id: dict[str, HospitalConfig] = {}
        self.by_facility: dict[str, HospitalConfig] = {}
        for h in hospitals:
            if h.hospital_id in self.by_id:
                raise ConfigError(f"duplicate hospital_id {h.hospital_id!r}")
            self.by_id[h.hospital_id] = h
            for code in h.facility_codes:
                if code in self.by_facility:
                    raise ConfigError(
                        f"facility code {code!r} claimed by both "
                        f"{self.by_facility[code].hospital_id!r} and {h.hospital_id!r}"
                    )
                self.by_facility[code] = h

    @classmethod
    def load_dir(cls, directory: Path) -> HospitalRegistry:
        return cls([load_hospital_config(p) for p in profile_paths(directory)])

    def match_facility(self, *codes: str) -> HospitalConfig | None:
        """Match MSH-4 / MSH-6 values against configured facility codes."""
        for code in codes:
            hospital = self.by_facility.get(code.strip())
            if hospital is not None:
                return hospital
        return None


def profile_paths(directory: Path) -> list[Path]:
    """The ``*.yaml`` profiles in ``directory``, sorted. Raises ConfigError
    with a hint when the folder is missing or holds none."""
    if not directory.is_dir():
        raise ConfigError(
            f"profile folder {directory} does not exist",
            hint="Try: hl7bridge init, or pass --config with a folder of profiles.",
        )
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise ConfigError(
            f"no *.yaml hospital profiles in {directory}",
            hint="Try: hl7bridge init",
        )
    return paths


def profile_as_dict(cfg: HospitalConfig) -> dict[str, object]:
    """Every key of a loaded profile with defaults filled in, in file order,
    shaped like the YAML so ``profile show`` output can be pasted back."""
    return {
        "hospital_id": cfg.hospital_id,
        "display_name": cfg.display_name,
        "facility_codes": list(cfg.facility_codes),
        "accession_field": cfg.accession_field,
        "timezone": cfg.timezone,
        "hl7_version": cfg.hl7_version,
        "timestamps_include_offset": cfg.timestamps_include_offset,
        "expected_message_types": list(cfg.expected_message_types),
        "procedure_code_system": cfg.procedure_code_system,
        "z_segment": cfg.z_segment,
        "reconcile": {
            "order_without_image_hours": cfg.reconcile.order_without_image_hours,
            "order_without_image_hours_by_modality": dict(
                cfg.reconcile.order_without_image_hours_by_modality
            ),
            "order_lag_minutes": cfg.reconcile.order_lag_minutes,
        },
        "ack_timeout_minutes": cfg.ack_timeout_minutes,
        "tat": {
            "exam_to_final_target_hours": cfg.tat.exam_to_final_target_hours,
            "exam_to_final_target_hours_by_priority": dict(
                cfg.tat.exam_to_final_target_hours_by_priority
            ),
        },
        "lint": {"disabled_rules": list(cfg.lint.disabled_rules)},
        "fhir": {
            "identifier_systems": {
                **{kind: getattr(cfg.systems, kind) for kind in IDENTIFIER_KINDS},
                "mrn_by_authority": dict(cfg.fhir.mrn_by_authority),
            },
            "patient_policy": cfg.fhir.patient_policy,
            "tag": {"system": cfg.fhir.tag[0], "code": cfg.fhir.tag[1]} if cfg.fhir.tag else None,
            "allowed_processing_ids": list(cfg.fhir.allowed_processing_ids),
        },
    }


@dataclass(frozen=True, slots=True)
class ProfileCheck:
    """One line of ``profile check``: a file and its problem, or None."""

    name: str
    problem: str | None
    hospital_id: str | None = None


def check_profile_dir(directory: Path) -> list[ProfileCheck]:
    """Validate every profile in ``directory`` on its own, then the set
    together (duplicate ids and facility codes), so one bad file does not
    hide the others. A missing or empty folder raises ConfigError."""
    results: list[ProfileCheck] = []
    loaded: list[HospitalConfig] = []
    for path in profile_paths(directory):
        try:
            cfg = load_hospital_config(path)
        except ConfigError as exc:
            results.append(ProfileCheck(path.name, str(exc)))
            continue
        loaded.append(cfg)
        results.append(ProfileCheck(path.name, None, cfg.hospital_id))
    if loaded:
        try:
            HospitalRegistry(loaded)
        except ConfigError as exc:
            results.append(ProfileCheck("(all profiles)", str(exc)))
    return results
