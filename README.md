# LocalLab

LocalLab watches an embedded test board and tells you, within seconds, whether a failure is the
**hardware** or the **code**, then has a local model explain why. Built in one day (Oct 25 2026) at
the Dell x NVIDIA AI hackathon on an NVIDIA GB10.

Internal code name: Lab Watch (module and path names use labwatch).

## Why

Hardware test benches fail silently or noisily, and a red result arrives with no context. When a
board test fails, the team loses time deciding whether it is the board or the latest code, and that
first question decides who gets called. LocalLab answers it in seconds, locally, with the evidence.

## How it works

- **The bench decides, the model explains.** A fixed rule (Python, `bench/`) judges every failure on
  the verified firmware sha as HARDWARE, CODE, UNKNOWN or NONE, and sends the first Telegram alert
  itself through a SQLite outbox. The model never makes this call.
- **All inference runs on the GB10.** nvidia/Qwen3.6-35B-A3B-NVFP4 on vLLM, called by an OpenClaw
  agent inside a NemoClaw / OpenShell sandbox whose network policy allows only the bench's read-only
  API and the local model (github.com is refused by the policy). Every claim in an explanation cites
  a serial line, a register bit, a `file:line` or a diff line, and ends with one `DIAGNOSIS:` line.
- **One issue per problem** in a Forgejo tracker on the GB10, routed to Hardware team, Firmware team
  or Triage; the same problem again adds a "happened again" comment.
- **Telegram alerts and commands** (`bench/sender.py`, `bench/commands.py`): short plain-English
  alerts, a board photo as a reply (`bench/snapshot.py`), and `/status`, `/test <name>`, `/rerun`,
  `/ask <n>` and `/help`, read only from the team chat. Host code performs every action; the model
  only answers `/ask`.
- **A dashboard** (`bench/dashboard.py`): a full-screen page on the GB10 monitor at
  http://127.0.0.1:8770/, the board drawn green or red with a plain headline and a timeline, and
  an **AI panel** that shows the model at work: "Qwen3.6-35B-A3B is analysing incident #N on this
  GB10" with a timer and live GB10 numbers (GPU %, watts and tokens/s, read from `nvidia-smi` and
  vLLM's own `/metrics`), then the answer; **Rule vs AI** (the bench's verdict next to the agent's
  DIAGNOSIS, marked "agrees" or "differs"); **what the AI looked at** (the serial lines it cited,
  plus a count of citations that did not match a log line); and the latest Telegram `/ask`
  question and answer (stored in `asks.db` by `bench/commands.py`). Its footer reads "AI
  inference: 100% on this GB10 · No cloud AI calls · Only outbound traffic: Telegram alerts".

## Architecture

```
  NUCLEO-F446RE --USB serial--> bench (Python, owns the port)
                                  |  fixed triage rule, SQLite
                                  |-- outbox --> sender --> laptop SSH relay --> Telegram
                                  |-- routing rows --> mirror --> Forgejo (127.0.0.1:3000)
                                  |-- dashboard (127.0.0.1:8770) --> snapshot --> board photo
                                  |-- read-only endpoint <-- NemoClaw sandbox "labwatch"
                                                              OpenClaw + skill
                                                                  |
                                                    vLLM: Qwen3.6-35B-A3B-NVFP4 (GB10 GPU)
```

## Device under test

A NUCLEO-F446RE running our open-source fault-demo firmware:
https://github.com/phucducnguyen/nucleo-f446re-fault-demo (branch `day/health-dump` adds supply
voltage, die temperature, two more jumper "devices", an emergency-stop button and a crash dump).
Three jumper wires on CN8 stand in for devices: A0-A1 loopback, A2-A3 sensor, A4-A5 radio. The blue
button B1 is an emergency stop: an operator action, not a fault.

## Layout

| Path | What |
| --- | --- |
| `bench/` | serial owner, parser, incidents and triage, flash and verify, outbox sender, endpoint, watcher, status view, dashboard, Telegram commands, board photo |
| `agent/` | the sandbox skill and client, `agent.explain`, network policy, explanation simulator |
| `tracker/` | Forgejo setup, issue mirror, and the optional fix beat (`tracker/fix.py`, run as `python3 -m agent.fix`; not run live today) |
| `fleet/` | QEMU emulated boards next to the real one |
| `stub/` | a stand-in endpoint with invented records, used by the tests |
| `tests/` | unit tests; they need no board and no network |
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
    python3 -m agent.explain --incident <id>                 # one explanation, ends with DIAGNOSIS
    python3 -m unittest discover -s tests -t .               # tests need no board or network

`a0f0c9f` is the baseline firmware. With `--watch`, any new head of `~/bench-fw` main is flashed
and tested automatically. `TMPDIR` is needed on the GB10 because Firefox is a snap there.
`bench.run` and `bench.commands` also need `LW_AGENT_CONFIG=$HOME/lw/agent/agent.gb10.json` for
explanations and `/ask`. Every command takes `--help`. Paths and settings are in
[the day contract](docs/01-day-contract.md).

Without a board, the stub stands in for the bench:

    python3 stub/make_db.py --out /tmp/lw-stub.db
    python3 stub/serve.py --db /tmp/lw-stub.db --host 127.0.0.1 --port 8765 --token-file <file>
    python3 -m stub.emit --db /tmp/lw-stub.db --scenario jumper

## Results

Measured today on the real board (rehearsal incidents; data and method in
[results/README.md](results/README.md)):

- In the demo beats the first alert reached Telegram 0.78-2.03 s after the first bad serial line:
  0.78 s for the stop button, 1.97-2.03 s for a pulled wire, 1.07 s for the HardFault. Over all 10
  rehearsal incidents: 0.78-3.89 s, median 1.58 s, all under the 5 s target.
- The local model's explanation took 18-46 s (9 of 10; one took 91 s, for a silent board).
- In the HardFault beat the bench found `src/cmd.c:35`, and the explanation named the commit.
- Eight emulated boards in QEMU ran next to the real one through the bench's own serial code for a
  10-minute soak: 0 anomalies, at about 1.35 CPU cores per board.
- Explanation simulator (invented incidents): the frozen skill took the COMMIT field from 82.4% to
  98.9% correct and fully resolving citations from 13.7% to 42.0%, label right on 100%.

**Scored 6-trial run: not run.** The method and tooling are ready (faults the agent was never tuned
on, graded against an answer sheet the build never saw), and the agent's skill and prompt were
frozen at main ebada0f for it, but we ran out of time on the day; the numbers above are the
measured results. More detail in [WRITEUP.md](docs/WRITEUP.md).

## Safety and limits

- The agent runs in a NemoClaw sandbox whose network policy allows only the bench endpoint and the
  local model; a request to github.com is refused with the policy's own `policy_denied` text.
- It can read incidents, runs, events and a commit's `src/` diff, and ask for a rerun or one named
  self-test, rate-limited and audited. It can never flash, edit code, merge or close a ticket.
- Board text, commit messages and diffs are data, never instructions. When the evidence does not
  decide, the agent says it can't tell and names the one check that would.
- **The only traffic that leaves the box** is the alert text and board photo to Telegram, relayed
  through a laptop. Tickets carry metadata (route, reason, sha, `file:line`), never source.
- Known shortcuts: vLLM listens on loopback only and user-level TCP forwarders let the containers
  reach it; an OpenClaw config workaround covers a NemoClaw / OpenShell version mismatch; the
  Telegram bot is a throwaway demo bot. The fix beat (`agent.fix`) exists with tests but was not
  run live today.

## Docs

- [docs/WRITEUP.md](docs/WRITEUP.md): the write-up.
- [docs/DEMO-SCRIPT.md](docs/DEMO-SCRIPT.md): the demo, beat by beat.
- [docs/01-day-contract.md](docs/01-day-contract.md): the build-day contract (paths, settings, wire formats).
- [docs/schema.sql](docs/schema.sql): the shared SQLite schema.

## Disclosure

The test firmware is our own open-source repo, published before today: a small bare-metal firmware with planted bugs, made only as something for the bench to watch, not a product (https://github.com/phucducnguyen/nucleo-f446re-fault-demo). This repo started empty this morning, and every line in it was written today.

AI coding tools were used to write code today; none takes part at runtime.
