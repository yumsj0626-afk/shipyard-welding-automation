PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    state_version INTEGER NOT NULL CHECK (state_version >= 0),
    now_tick INTEGER NOT NULL CHECK (now_tick >= 0),
    active_plan_version INTEGER NOT NULL CHECK (active_plan_version >= 0),
    snapshot_json TEXT NOT NULL
);

CREATE TABLE events (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    event_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    PRIMARY KEY (run_id, event_id)
);

CREATE TABLE event_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    event_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    outcome TEXT NOT NULL,
    tick INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE evidence (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    evidence_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, evidence_id)
);

CREATE TABLE plans (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, plan_id)
);

CREATE TABLE checks (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    check_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, check_id),
    FOREIGN KEY (run_id, plan_id) REFERENCES plans(run_id, plan_id)
);

CREATE TABLE instructions (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    instruction_id TEXT NOT NULL,
    plan_id TEXT,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, instruction_id)
);

CREATE TABLE observations (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    observation_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    instruction_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, observation_id)
);

CREATE TABLE audit_log (
    log_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    tick INTEGER NOT NULL,
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
