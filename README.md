# LocalLab

LocalLab watches an embedded test board and tells you, within seconds, whether a failure is the
**hardware** or the **code**, then has a local model explain why. Built in one day (Oct 25 2026) at
the Dell x NVIDIA AI hackathon on an NVIDIA GB10.

- **The bench decides, the model explains.** A fixed rule (Python, `bench/`) judges every failure on
  the verified firmware sha as HARDWARE, CODE, UNKNOWN or NONE, and sends the first Telegram alert
  itself through a SQLite outbox, typically under 2 s after the first bad serial line.
- **All inference runs on the GB10.** Qwen3.6-35B-A3B (NVFP4) on vLLM, called by an OpenClaw agent
  inside a NemoClaw / OpenShell sandbox whose network policy allows only the bench's read-only API and
  the local model (github.com is refused by the policy). Every claim in an explanation cites a serial
  line, a register bit, a `file:line` or a diff line, and ends with one `DIAGNOSIS:` line.
- **The only traffic that leaves the box** is the alert text to Telegram, relayed through a laptop.
  AI coding tools were used to write code today; none takes part at runtime.
- **One issue per problem** in a Forgejo tracker on the GB10, routed to Hardware team, Firmware team
  or Triage.

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
| `tracker/` | Forgejo setup, issue mirror, `agent.fix` (proposes a patch, proves it on the board, opens a PR) |
| `fleet/` | QEMU emulated boards next to the real one |
| `stub/` | a stand-in endpoint with invented records, used by every lane's tests |
| `docs/` | the day contract, schema, write-up, demo script |

## Run (on the GB10)

    python3 -m bench.run --board nucleo-1 --flash <sha>      # owns the serial port
    LW_TG_API=https://api.telegram.org:8443 python3 -m bench.sender
    LW_HOST=172.18.0.1 python3 -m bench.endpoint             # the sandbox's read-only API
    python3 -m tracker.mirror
    python3 -m bench.status_view
    python3 -m unittest discover -s tests -t .               # tests need no board or network

Paths and settings are in `docs/01-day-contract.md`.

## Disclosure

The test firmware is our own open-source repo, published before today. We prototyped the idea last
week; none of that code is here. This repo started empty this morning, and every line in it was
written today.
