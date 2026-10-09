-- hl7bridge SQLite schema. All *_utc columns hold ISO 8601 strings in UTC
-- (e.g. 2026-10-07T15:04:05+00:00). Synthetic data only; no real PHI.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- Accepted (AA) messages. Rejected/errored messages live in quarantine.
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    hospital_id     TEXT NOT NULL,
    control_id      TEXT NOT NULL,
    message_type    TEXT NOT NULL,
    received_at_utc TEXT NOT NULL,
    raw             TEXT NOT NULL,
    ack_code        TEXT NOT NULL CHECK (ack_code IN ('AA', 'AE', 'AR')),
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (hospital_id, control_id)
);

CREATE TABLE IF NOT EXISTS patients (
    hospital_id     TEXT NOT NULL,
    mrn             TEXT NOT NULL,
    family_name     TEXT NOT NULL,
    given_name      TEXT NOT NULL,
    birth_date      TEXT,
    sex             TEXT,
    merged_into_mrn TEXT,
    PRIMARY KEY (hospital_id, mrn)
);

CREATE TABLE IF NOT EXISTS orders (
    hospital_id           TEXT NOT NULL,
    accession             TEXT NOT NULL,
    placer_order_id       TEXT,
    procedure_code        TEXT,
    procedure_description TEXT,
    code_system           TEXT,
    modality              TEXT,
    patient_mrn           TEXT,
    ordered_at_utc        TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('new', 'cancelled')),
    PRIMARY KEY (hospital_id, accession)
);

-- DICOM study metadata. accession is nullable on purpose: a study missing
-- tag (0008,0050) is a real-world defect we must represent, not reject.
CREATE TABLE IF NOT EXISTS studies (
    study_uid    TEXT PRIMARY KEY,
    hospital_id  TEXT NOT NULL,
    accession    TEXT,
    modality     TEXT NOT NULL,
    patient_mrn  TEXT,
    study_at_utc TEXT NOT NULL,
    source_file  TEXT
);

-- Latest report per (hospital, accession). A corrected (C) report replaces
-- the final (F); full history stays in messages.raw.
CREATE TABLE IF NOT EXISTS reports (
    hospital_id     TEXT NOT NULL,
    accession       TEXT NOT NULL,
    control_id      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('P', 'F', 'C')),
    report_text     TEXT NOT NULL,
    patient_mrn     TEXT,
    result_at_utc   TEXT,
    received_at_utc TEXT NOT NULL,
    PRIMARY KEY (hospital_id, accession)
);

-- Reports we (the radiology group) sent to hospital EHRs (project 3 input).
CREATE TABLE IF NOT EXISTS outbound_reports (
    hospital_id TEXT NOT NULL,
    control_id  TEXT NOT NULL,
    accession   TEXT NOT NULL,
    sent_at_utc TEXT NOT NULL,
    PRIMARY KEY (hospital_id, control_id)
);

-- ACK messages received from hospitals for our outbound reports.
CREATE TABLE IF NOT EXISTS acks_in (
    id              INTEGER PRIMARY KEY,
    hospital_id     TEXT NOT NULL,
    ack_control_id  TEXT NOT NULL,
    msa_code        TEXT NOT NULL,
    msa_control_id  TEXT NOT NULL,
    reason          TEXT,
    received_at_utc TEXT NOT NULL
);

-- Every message we could not accept, with a stable machine-readable reason.
CREATE TABLE IF NOT EXISTS quarantine (
    id              INTEGER PRIMARY KEY,
    hospital_id     TEXT,
    reason          TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    raw             BLOB NOT NULL,
    ack_code        TEXT NOT NULL CHECK (ack_code IN ('AE', 'AR')),
    received_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merges (
    id            INTEGER PRIMARY KEY,
    hospital_id   TEXT NOT NULL,
    surviving_mrn TEXT NOT NULL,
    merged_mrn    TEXT NOT NULL,
    merged_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_studies_hospital_accession ON studies (hospital_id, accession);
CREATE INDEX IF NOT EXISTS idx_acks_in_control ON acks_in (hospital_id, msa_control_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_reason ON quarantine (reason);
