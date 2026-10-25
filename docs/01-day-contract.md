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
| `fleet/` | lane E (QEMU emulated boards) |
| `stub/` | main (the stand-in endpoint for B, C and D) |
| `tests/test_<lane>_*.py` | each lane its own (`a`, `b`, `c`, `d`, `stub`) |
| `docs/` | main: this file, `schema.sql`, the write-up and the demo script |

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
- `alerts.transition` is one of those listed under "Alerts" below. Every non-open alert has `reply_to_alert_id` = the incident's `open` alert. `sent` stays NULL
  until Telegram accepts it; the sender sends only rows with `sent IS NULL`.

## Alerts (the bench's fixed wording, `bench/alerts.py`)

Every Telegram message is two or three short lines in everyday English, written by fixed code
with no model. The first line says what happened, with the board and the incident number; the
second names the team and points to `/ask`. Technical words appear only after "Details:" and only
when needed. The quoted board line, the sha and the timings stay in the DB, the dashboard and the
issue. Messages go out as plain text (no parse mode), so board text can never format or link
anything. Examples:

    🔴 Board 1: the sensor wire came loose. (#5)
    Team: hardware. Not a code problem. Ask: /ask 5

    🔴 Board 1: the new code made it crash. (#6)
    Team: firmware. Details: src/x.c:42. Ask: /ask 6

    ⛔ Board 1: someone pressed the stop button. Not a fault. (#8)

    ✅ Board 1: the sensor wire is back. Fixed after 26 seconds. (#5)

A 🟡 first line ("Checking if it is the board or the code.") marks UNKNOWN triage; 🟠 marks a flap.

### Transitions in use (`alerts.transition`)

| Transition | Written by | What it says |
| --- | --- | --- |
| `open` | `bench.run` | the first alert above; every other row replies to it |
| `resolve` | `bench.run` | ✅ the device is back, with the duration |
| `flap-<n>` | `bench.run` | 🟠 keeps coming and going; n = the flap count when sent |
| `diag` | `bench.run` | 🔧 the board's own self-check passed, failed or got no answer |
| `decode` | `bench.run` | 🧩 where in the code the crash happened (`Details: file:line`) |
| `confirmed` | `bench.confirm` | ✔ / ✖ the result of putting the last good code back |
| `explain` | `bench.run` via `agent.explain` | 🧠 the model's answer, condensed to cause, next step and the DIAGNOSIS line |
| `ticket` | `tracker.mirror` | `Ticket #<n>: <url>`, once per incident |
| `fix` | `tracker.fix` (`python3 -m agent.fix`) | the proposed fix's pull-request link (optional; not run live today) |

### The board photo (`bench/snapshot.py`)

A separate process polls the DB read-only for sent `open` alerts with no photo yet, waits 1.5 s so
the dashboard shows the red state, screenshots the dashboard (http://127.0.0.1:8770/, headless
Firefox by default) and posts it with `sendPhoto` as a reply to that alert, with a one-line plain
caption. Photos sent or skipped are kept in `snapshots.db` in the state dir, so a restart never
resends. LINK_DOWN gets no photo.

### Telegram commands (`bench/commands.py`)

    /status          device states, open incidents and the last run (bench DB, read-only)
    /test <name>     POST /board/test (crc32 ringbuf systick cmd_dispatch loopback)
    /rerun           POST /run with the bench's verified sha
    /ask <n|text>    one sandbox-agent turn, 60 s, one at a time; "/ask 5" asks about incident 5
    /help            the list

Only messages from the team chat (`telegram_chat`, optionally narrowed by `LW_TG_USERS`) that start
with `/` are read; everything else is ignored with a log line that never holds the text. Host code
picks and performs every action; the model only answers `/ask`, and it never flashes, edits, calls
git or talks to the board. At most one command per 3 s per user. The getUpdates offset is saved
before each update is handled, so a restart never replays a command.

### Incident kinds (`incidents.kind`)

| Kind | Opens on |
| --- | --- |
| `HEALTH_FAIL` | a jumper device or the supply; `evidence.device` is `loopback`, `sensor`, `radio` or `power` |
| `ESTOP` | the blue button held (an operator action, triage NONE, nobody to call) |
| `HARDFAULT` | the board's crash line |
| `RUN_FAIL` | a run of a new sha that does not pass |
| `SILENT` | no LW lines for too long |
| `LINK_DOWN` | the USB serial link dropped |
| `REBOOT` | a boot nobody asked for |

### Problem key (issue reuse)

`tracker.mirror` writes the exact line `lw-key: board<board>/<device>/<state>` in each issue body
(CODE incidents use `board<board>/CODE/<file:line>`). An incident whose key matches an open issue
gets a "happened again" comment on it instead of a new issue; one closed under 60 min ago is
reopened with that comment.

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

## Updates, 12:45

### Board protocol additions (firmware 7e056f5 and a0f0c9f; additions only)

    LW HEALTH <n> loopback=OK|OPEN|STUCK sensor=OK|OPEN|STUCK radio=OK|OPEN|STUCK estop=ON|OFF[ vdd=<mV>][ temp=<C>]

- Parse every field by key, never by position. sensor, radio and estop come with a0f0c9f
  (SIM builds read OK, OK, OFF); older firmware and the recordings lack them: absent = unknown.
- vdd: millivolts from VREFINT (about 3300 on USB). temp: signed degrees C, die sensor (+-5 C).
  Both optional; absent = unknown, never 0.
- Pins (CN8): loopback A0 (PA0 out) to A1 (PA1 in); sensor A2 (PA4 out) to A3 (PB0 in); radio A4
  (PC1 out) to A5 (PC0 in); estop = blue user button B1 (PC13), held = ON. OPEN = wire pulled,
  STUCK = input grounded. Never ground A0, A2 or A4; ground A1, A3 or A5 only with that pair pulled.
- On a fault, once, before the first LW HARDFAULT (which still repeats every second):

      LW DUMP reg r0=0x%08x r1=... r2=... r3=... r12=... lr=... pc=... psr=...
      LW DUMP stack 0x<addr> 0x<w0> 0x<w1> 0x<w2> 0x<w3>     (up to 8; or: LW DUMP stack bad sp=0x...)
      LW DUMP cause <NAME>[ <NAME>...]                        (HFSR/CFSR bit names, or NONE)
      LW DUMP end

  A burst belongs to the next HARDFAULT line; it may be missed after a reconnect, and the
  HARDFAULT line stays the authority.

### Alert transitions

Now listed in full under "Alerts" above. Every non-open row replies to the incident's open alert.

### On the GB10 today

| What | Value |
| --- | --- |
| Endpoint | `LW_HOST=172.18.0.1` (the openshell-docker bridge the sandbox reaches as host.openshell.internal), port 8765 |
| Bench DB | `~/.local/state/labwatch/bench.db` |
| Hold file | `~/.local/state/labwatch/hold` |
| bench token | `~/.config/labwatch/bench_token` (copied into the sandbox as `/sandbox/.config/labwatch/bench_token`) |
| Explain | `LW_AGENT_CONFIG=~/lw/agent/agent.gb10.json python3 -m agent.explain --incident <id>` (18-46 s measured today; timeout 55 s) |
| NemoClaw | every `nemoclaw`/`openshell` call needs `NEMOCLAW_GATEWAY_PORT=8090` and `PATH=$HOME/.local/bin:$PATH` |
| Model path | vLLM on 127.0.0.1:8000; user-level forwarders on 172.17.0.1:8000 and 172.18.0.1:8000 (tmux lw-fwd8000, lw-fwd8000b) |
| Bench firmware | `~/bench-fw` main; baseline for the scored run: a0f0c9f |
| Telegram | sender with `LW_TG_API=https://api.telegram.org:8443` through the laptop relay; chat = the team group |
