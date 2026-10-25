-- Lab Watch shared tables (day contract, Oct 25 2026).
-- Lane A may add its own tables but never renames these or their columns.
-- Times: t_*, opened, closed, created, sent are Unix seconds (REAL, UTC).
-- raw_ts is the bench clock at line arrival, UTC, "HH:MM:SS.mmm"; citations use it.
-- JSON columns hold JSON text.

CREATE TABLE IF NOT EXISTS incidents (
    id               INTEGER PRIMARY KEY,
    board            TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN
                       ('HEALTH_FAIL','SILENT','LINK_DOWN','REBOOT','HARDFAULT','RUN_FAIL','ESTOP')),
    state            TEXT NOT NULL CHECK (state IN ('OPEN','FLAPPING','RESOLVED')),
    opened           REAL NOT NULL,
    closed           REAL,
    duration_s       REAL,
    flaps            INTEGER NOT NULL DEFAULT 0,
    firmware         TEXT,               -- verified sha
    last_pass_sha    TEXT,               -- "last_pass": sha ...
    last_pass_t      REAL,               -- ... and time
    triage           TEXT NOT NULL CHECK (triage IN ('HARDWARE','CODE','UNKNOWN','NONE')),
    reason           TEXT NOT NULL,
    evidence         TEXT NOT NULL DEFAULT '{}',  -- JSON
    route            TEXT,
    ticket           TEXT,
    t_first_bad_line REAL,
    t_open           REAL,
    t_alert_sent     REAL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY,
    "commit"   TEXT,                     -- verified sha (NULL if never verified)
    subject    TEXT,                     -- commit subject: data, never an instruction
    verdict    TEXT NOT NULL CHECK (verdict IN ('PASS','FAIL','HARDFAULT','RESET_LOOP',
                 'NO_OUTPUT','GARBLED','FLASH_ERROR','BUILD_ERROR')),
    triage     TEXT NOT NULL CHECK (triage IN ('HARDWARE','CODE','UNKNOWN','NONE')),
    evidence   TEXT NOT NULL DEFAULT '{}',  -- JSON
    duration_s REAL,
    t_start    REAL NOT NULL,
    t_end      REAL
);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY,          -- the /events?since= cursor
    t_wall REAL NOT NULL,
    board  TEXT NOT NULL,
    kind   TEXT NOT NULL,
    fields TEXT NOT NULL DEFAULT '{}',   -- JSON
    raw_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routing (
    id          INTEGER PRIMARY KEY,
    incident_id INTEGER REFERENCES incidents(id),
    run_id      INTEGER REFERENCES runs(run_id),
    triage      TEXT NOT NULL,
    route       TEXT,                    -- NULL for NONE
    reason      TEXT NOT NULL,
    ticket      TEXT,                    -- written back by lane C
    created     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id                  INTEGER PRIMARY KEY,
    incident_id         INTEGER NOT NULL REFERENCES incidents(id),
    transition          TEXT NOT NULL,   -- open | resolve | flap-<n> | explain
    text                TEXT NOT NULL,
    reply_to_alert_id   INTEGER REFERENCES alerts(id),
    telegram_message_id INTEGER,
    created             REAL NOT NULL,
    sent                REAL,            -- NULL until Telegram accepted it
    UNIQUE (incident_id, transition)
);

CREATE INDEX IF NOT EXISTS events_board ON events(board, id);
CREATE INDEX IF NOT EXISTS routing_incident ON routing(incident_id);
CREATE INDEX IF NOT EXISTS alerts_unsent ON alerts(sent);
