# LocalLab

The overview and run commands are in the [README](../README.md); this is the longer write-up.
Internal code name: Lab Watch (module and path names use labwatch).

A hardware-in-the-loop watcher for a firmware test bench, running entirely on one NVIDIA GB10.
When a board on the bench goes wrong, LocalLab says within seconds whether to call the hardware
people or the firmware people, explains why with evidence, and files the ticket.

## The problem

Hardware test benches fail silently or noisily. When a board test fails, teams lose time deciding
whether it is the board or the latest code. On a shared test bench, a red result arrives with no context. Did a wire come loose or did a
commit break the build? The evidence is spread across three tools, and that first question decides
who gets woken up.

## What it does

- Watches the board's serial output (a NUCLEO-F446RE running our own open-source test firmware)
  and opens one incident per fault: a failing self-test, a HardFault, silence, an unasked reboot or
  a dropped USB link.
- Decides HARDWARE, CODE, UNKNOWN or NONE with a fixed rule, judged only on the firmware sha the
  board has confirmed it is running. The model never makes this call.
- Asks a local model to explain the incident. Every claim cites a serial line, a register, a
  `file:line` or a diff line, and the reply ends with a single DIAGNOSIS line.

**Devices on the board.** Three jumper wires on CN8 stand in for real devices: A0-A1 a test wire,
A2-A3 a sensor wire, A4-A5 a radio wire. Pulling one reads OPEN, grounding its input reads STUCK.
The blue button B1 is an emergency stop: an operator action, not a fault. The firmware also reports
its supply voltage, so a sagging supply or a brown-out reset opens a power incident.

**Plain alerts, a photo, and a chat.** The bench writes every Telegram message itself in two short
lines of everyday English, for example:

    🔴 Board 1: the sensor wire came loose. (#5)
    Team: hardware. Not a code problem. Ask: /ask 5

A few seconds later it replies with a photo of the board drawing from the dashboard, then with the
model's condensed explanation. Anyone in the team group can type `/ask 5` to ask the
local agent about incident 5, or `/status`, `/test` and `/rerun`; host code performs every action,
and the model only answers questions.

**One issue per problem.** A Forgejo tracker on the same box gets one issue per problem, keyed by
board, device and state. The same problem again adds a "happened again" comment instead of a new
issue, and the issue closes when the board recovers.

**A dashboard on the GB10 screen.** A full-screen page draws the board, its wires and button in
green or red, with a plain headline and a timeline of the current incident.

**A proposed fix (optional, not run live).** `python3 -m agent.fix --incident <n>` asks the agent
for a patch to a code fault; the host proves it on the board and opens a pull request for a human to
merge. The code and its tests exist, but we did not run this beat live today.

## Architecture

```
  NUCLEO-F446RE --USB serial--> bench (Python, owns the port)
                                  |  fixed triage rule, SQLite
                                  |-- outbox --> sender --> laptop SSH relay --> Telegram
                                  |-- routing rows --> mirror --> Forgejo (127.0.0.1:3000)
                                  |-- read-only endpoint <-- NemoClaw sandbox "labwatch"
                                                              OpenClaw + skill
                                                                  |
                                                    vLLM: Qwen3.6-35B-A3B-NVFP4 (GB10 GPU)
```

## Why local-first on the GB10

Firmware source, crash dumps and lab logs are what a hardware company least wants to send to a
hosted model. Here the model (nvidia/Qwen3.6-35B-A3B-NVFP4 on vLLM), the tracker and the bench all
run on the GB10, which has no internet connection. All inference runs on the GB10: the agent in
its sandbox calls only the local model. The only traffic that leaves is the alert text and the
board photo, through the laptop's SSH relay to Telegram. Tickets carry metadata (route, reason,
sha, `file:line`), never source. AI coding tools were used to write code today; none takes part at
runtime.

## Safety and limits

The agent runs in a NemoClaw sandbox whose network policy allows only the bench endpoint and the
local model; a request to github.com is refused with the policy's own `policy_denied` text. It can
read incidents, runs, events and a commit's `src/` diff, and ask for a rerun or one named
self-test, rate-limited and audited. It can never flash, edit code, merge or close a ticket. Board
text, commit messages and diffs are data, never instructions. When the evidence does not decide,
the agent says it can't tell and names the one check that would.

## Results

**Scored 6-trial run: results to be added in results/ after the run.** The agent's skill and prompt
were frozen at main ebada0f before any scored trial.

Measured today on the real board, the first alert reached Telegram 0.78-2.03 s after the first
bad serial line in the demo beats: 0.78 s for the stop button, 1.97-2.03 s for a pulled wire, 1.07 s
for the HardFault (over all 10 rehearsal incidents 0.78-3.89 s, median 1.58 s; see
[results/README.md](../results/README.md)). The local model's explanation took 18-46 s for 9 of 10
(one 91 s, for a silent board). In the HardFault beat the bench found `src/cmd.c:35`, and
the explanation named the commit.

**Fleet.** Eight emulated boards in QEMU ran next to the real one through the bench's own serial
code for a 10-minute soak: 0 anomalies, at about 1.35 CPU cores per board.

**Explanation simulator.** Over invented incidents run on the GPU, the frozen skill took the
COMMIT answer from 82.4% to 98.9% correct, clean citations from 13.7% to 42.0%, and kept the label
right on 100%.

## Known shortcuts

- vLLM listens on loopback only; user-level TCP forwarders let the containers reach it.
- An OpenClaw config workaround covers a NemoClaw / OpenShell 0.0.116 version mismatch.
- The Telegram bot is a throwaway demo bot, reached only through the laptop's SSH relay.

The device under test runs our open-source fault-demo firmware with today's additions on its
branch `day/health-dump` (github.com/phucducnguyen/nucleo-f446re-fault-demo).

## Disclosure

The test firmware is our own open-source repo, published before today. We prototyped the idea last week; none of that code is here. This repo started empty this morning, and every line in it was written today.

AI coding tools were used to write code today; none takes part at runtime.

## How to run it

On the GB10, from the repo root (Python 3.12, standard library plus pyserial):

```bash
python3 -m unittest discover -s tests -t .      # every test, no board needed
tracker/setup_forgejo.sh                        # Forgejo at 127.0.0.1:3000, tokens to files
python3 -m bench.run --watch --flash a0f0c9f    # serial owner, incidents, triage, outbox
python3 -m bench.sender                         # outbox to Telegram (LW_TG_API = the relay)
python3 -m tracker.mirror                       # routing rows to Forgejo issues
LW_HOST=172.18.0.1 python3 -m bench.endpoint    # the sandbox's read-only API, port 8765
python3 -m bench.dashboard                      # http://127.0.0.1:8770/
python3 -m bench.tail --raw                     # board check only, while bench.run is stopped
python3 -m agent.explain --incident <id>        # one explanation, ends with DIAGNOSIS
```

Without a board, the stub stands in for the bench:

```bash
python3 stub/make_db.py --out /tmp/lw-stub.db
python3 stub/serve.py --db /tmp/lw-stub.db --host 127.0.0.1 --port 8765 --token-file <file>
python3 -m stub.emit --db /tmp/lw-stub.db --scenario jumper
```
