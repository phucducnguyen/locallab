# Lab Watch: the day contract (Sun Oct 25 2026)

The decisions every lane builds against today. Written in the main checkout at 10:30. Everything
here is PROPOSED until its Sunday check passes on the real board. To change it, a lane proposes the
change in its stage report; main edits this file.

## Repo layout

| Path | Owner |
| --- | --- |
| `bench/` | lane A (serial, incidents, triage, flash, alerts, endpoint) |
| `agent/` | lane B; the OpenClaw skill folder is `agent/skill/labwatch/` |
| `tracker/` | lane C |
| `score/` | lane D |
| `stub/` | main (the stand-in endpoint for B, C and D) |
| `tests/test_<lane>_*.py` | each lane its own (`a`, `b`, `c`, `d`, `stub`) |
| `docs/` | main: this file and `schema.sql` only |

Tests: `python3 -m unittest discover -s tests -t .` from the repo root. Python 3.12, standard
library plus pyserial.

## Paths and environment on the GB10

| What | Default | Override |
| --- | --- | --- |
| Bench DB | `~/.local/state/labwatch/bench.db` | `LW_DB` |
| Hold flag | `~/.local/state/labwatch/hold` | `LW_HOLD` |
| Secrets dir | `~/.config/labwatch/` (files chmod 600, placed by Patrick) | `LW_SECRETS_DIR` |
| Bench firmware repo | `~/bench-fw` (main only; no CASES.md, logs or bugs) | `LW_FW_REPO` |
| Endpoint port | 8765 | `LW_PORT` |
| Endpoint host | the docker bridge IP, found at startup | `LW_HOST` |
| Telegram base URL | `https://api.telegram.org` (GB10: `https://api.telegram.org:8443`, the laptop relay) | `LW_TG_API` |

Secret files in the secrets dir: `bench_token`, `telegram_token`, `telegram_chat`, `tracker_token`.
Code opens them by path and never prints, logs or copies their contents. Tests make fake ones in
temp dirs.

`~/fw` is the full firmware clone. The bench never reads it; only the scorer and the demo-commit
fetch use it. The GB10 has no internet.

Any git command that creates a commit passes `-c user.name="Lab Watch bench"
-c user.email=lw@example.invalid` (a fresh GB10 may have no git identity).

## SQLite

Every connection sets `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`. The shared tables
are in `docs/schema.sql`: `incidents`, `runs`, `events`, `routing`, `alerts`. Lane A may add its own
tables; nobody renames or drops these tables or their columns.

Conventions in those tables:
- Times are Unix seconds (REAL, UTC). `events.raw_ts` is the bench clock at line arrival, UTC,
  `HH:MM:SS.mmm`; the agent cites serial lines as `[serial <raw_ts>]`.
- The incident's `last_pass` is two columns, `last_pass_sha` and `last_pass_t`. The endpoint
  returns them as `"last_pass": {"sha", "t"}`.
- `evidence` and `fields` are JSON text. A quoted line in evidence is
  `{"event_id", "raw_ts", "line"}` so every citation resolves to an `events` row.
- `events` holds one row per parsed `LW` line (kind `BOOT`, `TEST`, `RESULT`, `HARDFAULT`, `ALIVE`,
  `HEALTH`, `REPLY`; the line itself in `fields.line`) and one row per bench happening (`RUN_START`,
  `RUN_END`, `INCIDENT_OPEN`, `INCIDENT_FLAP`, `INCIDENT_RESOLVE`, `ALERT_SENT`, with the ids in
  `fields`). `events.id` is the `/events?since=` cursor.
- `incidents.state` is `OPEN`, `FLAPPING` or `RESOLVED`. "Open" in `/incidents?open=1` means not
  `RESOLVED`.
- `routing.route` is `Hardware team`, `Firmware team`, `Triage`, or NULL for NONE. A failed run
  that opens an incident has one routing row with both `incident_id` and `run_id`. Lane C writes
  its issue number into `routing.ticket` and `incidents.ticket`.
- `alerts.transition` is `open`, `resolve`, `flap-<n>` (n = the flap count when sent) or `explain`.
  Every non-open alert has `reply_to_alert_id` = the incident's `open` alert. `sent` stays NULL
  until Telegram accepts it; the sender sends only rows with `sent IS NULL`.

## Alerts (the bench's fixed wording)

    LAB WATCH incident <id> OPEN            (or: RESOLVED after <n> s / FLAPPING (<n> flaps))
    Board: <board>
    Kind: <kind>
    Triage: <triage>
    Routed to: <route or none>
    Reason: <reason>
    Line: [<raw_ts>] <the quoted LW line>
    File: <file:line>                       (only when there is one)
    Incident: <id>

The stub's `stub/db.py:alert_text` writes exactly this; lane A writes its own.

## Explanation contract

Lane B ships `python3 -m agent.explain --incident <id>` (or `--run <id>`). It asks the sandbox
agent in a fresh context and prints the explanation, ending with the DIAGNOSIS line, on stdout,
exit 0. Exit 2 on timeout or when no DIAGNOSIS line came back. It never posts anything.

Lane A's bench calls it after the first alert and posts the text through its own outbox (an
`explain` alert row, `reply_to_alert_id` = the first alert). Lane D's scorer calls the same
command.

## Endpoint

The routes, auth and limits are the interface's. Shapes the stub fixes for B, C and D:
- Auth: `Authorization: Bearer <bench_token>` on every route but `/health`; 401 otherwise.
- `/health`: `{"ok": true, "stub": true|false, "time"}`. The real bench says `"stub": false`.
- `/incidents` → `{"incidents": [...]}`, newest first; `/incidents/<id>` adds `"routing": [...]`.
- `/runs` → `{"runs": [...]}`; commit text is under `"data": {"subject"}`, never top level.
- `/events?since=<id>` → `{"events": [...], "cursor": <last id>}`, at most 500 per call.
- `/board` → `{"boards": [{"board", "open_incidents", "boot", "health", "result"}]}`.
- `/show?ref=<sha>` → `{"ref", "truncated", "data": {"diff": [lines]}}`; the ref check is a full
  match of `^[0-9a-f]{7,40}$` (a trailing newline fails), else 400.
- Every string in every response is printable ASCII, capped at 120 characters.
- `POST /run`, `POST /board/test` → 202 when accepted; 429 while the hold file exists or (for
  `/run`) within 60 s of the last accepted run; 400 for a bad body. Every other route is 404.

## Hold flag

While the hold file exists (contents: holder and time), the endpoint's side-effect routes return
429. Lane D creates it for the accuracy loop and removes it after.

## Watcher

Any change of the head of `~/bench-fw` main, a new commit or a move back, triggers a run of the new
head.

## The stub

    python3 stub/make_db.py --out /tmp/lw-stub.db
    python3 stub/serve.py --db /tmp/lw-stub.db --host 127.0.0.1 --port 8765 --token-file <file>
    python3 stub/emit.py --db /tmp/lw-stub.db --scenario jumper|recover|flap|hardfault

All stub values are invented: shas 1111111 (last pass) and 2222222 (the new commit), `src/demo.c:42`,
made-up registers. Canned incidents: 1 pulled jumper (HARDWARE, resolved), 2 flap (FLAPPING,
flaps 4), 3 HardFault of 2222222 (CODE, Firmware team). Runs: 1 pass of 1111111, 2 HardFault of
2222222. `/show` answers only for 2222222.
