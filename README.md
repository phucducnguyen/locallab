# LocalLab

LocalLab watches an embedded test board and tells you, within seconds, whether a failure is the
**hardware** or the **code**, then has a local model explain why. Built in one day (Oct 25 2026) at
the Dell x NVIDIA AI hackathon on an NVIDIA GB10.

- **The bench decides, the model explains.** A fixed rule (Python, `bench/`) judges every failure on
  the verified firmware sha as HARDWARE, CODE, UNKNOWN or NONE, and sends the first Telegram alert
  itself through a SQLite outbox. Measured today on the real board: 0.78-2.03 s after the first bad
  serial line.
- **All inference runs on the GB10.** nvidia/Qwen3.6-35B-A3B-NVFP4 on vLLM, called by an OpenClaw agent
  inside a NemoClaw / OpenShell sandbox whose network policy allows only the bench's read-only API and
  the local model (github.com is refused by the policy). Every claim in an explanation cites a serial
  line, a register bit, a `file:line` or a diff line, and ends with one `DIAGNOSIS:` line.
- **The only traffic that leaves the box** is the alert text and board photo to Telegram, relayed
  through a laptop.
  AI coding tools were used to write code today; none takes part at runtime.
- **One issue per problem** in a Forgejo tracker on the GB10, routed to Hardware team, Firmware team
  or Triage; the same problem again adds a "happened again" comment.
- **Telegram commands** (`bench/commands.py`): `/status`, `/test <name>`, `/rerun`, `/ask <n>` and
  `/help`, read only from the team chat. Host code performs every action; the model only answers
  `/ask`.
- **A board photo** (`bench/snapshot.py`): a screenshot of the dashboard posted as a reply to each
  incident's first alert.
- **A dashboard** (`bench/dashboard.py`): a full-screen page on the GB10 monitor at
  http://127.0.0.1:8770/, the board drawn green or red with a plain headline and a timeline.

## Device under test

A NUCLEO-F446RE running our open-source fault-demo firmware:
https://github.com/phucducnguyen/nucleo-f446re-fault-demo (branch `day/health-dump` adds supply
voltage, die temperature, two more jumper "devices", an emergency-stop button and a crash dump).
Three jumper wires on CN8 stand in for devices: A0-A1 loopback, A2-A3 sensor, A4-A5 radio.

## Layout

| Path | What |
| --- | --- |
| `bench/` | serial owner, parser, incidents and triage, flash and verify, outbox sender, endpoint, watcher, status view, dashboard |
| `agent/` | the sandbox skill and client, `agent.explain`, network policy, explanation simulator |
| `tracker/` | Forgejo setup, issue mirror, and the optional fix beat (`tracker/fix.py`, run as `python3 -m agent.fix`; not run live today) |
| `fleet/` | QEMU emulated boards next to the real one |
| `stub/` | a stand-in endpoint with invented records, used by every lane's tests |
| `docs/` | the day contract, schema, write-up, demo script |

## Run (on the GB10)

    python3 -m bench.run --board nucleo-1 --watch --flash a0f0c9f   # owns the serial port
    LW_TG_API=https://api.telegram.org:8443 python3 -m bench.sender
    LW_HOST=172.18.0.1 python3 -m bench.endpoint             # the sandbox's read-only API
    python3 -m tracker.mirror
    python3 -m bench.status_view
    python3 -m bench.dashboard                               # http://127.0.0.1:8770/
    LW_TG_API=https://api.telegram.org:8443 python3 -m bench.commands
    TMPDIR=$HOME/snap/firefox/common/lw-tmp LW_TG_API=https://api.telegram.org:8443 python3 -m bench.snapshot
    python3 -m unittest discover -s tests -t .               # tests need no board or network

`a0f0c9f` is the baseline firmware. With `--watch`, any new head of `~/bench-fw` main is flashed
and tested automatically. `TMPDIR` is needed on the GB10 because Firefox is a snap there. `bench.run` and `bench.commands` also
need `LW_AGENT_CONFIG=$HOME/lw/agent/agent.gb10.json` for explanations and `/ask`. Paths and
settings are in [the day contract](docs/01-day-contract.md); the demo is in
[the demo script](docs/DEMO-SCRIPT.md) and the write-up in [WRITEUP.md](docs/WRITEUP.md).

## Disclosure

The test firmware is our own open-source repo, published before today. We prototyped the idea last
week; none of that code is here. This repo started empty this morning, and every line in it was
written today.
