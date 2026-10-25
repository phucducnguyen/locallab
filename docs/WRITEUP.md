# Lab Watch

A hardware-in-the-loop watcher for a firmware test bench, running entirely on one NVIDIA GB10.
When a board on the bench goes wrong, Lab Watch says within seconds whether to call the hardware
people or the firmware people, explains why with evidence, and files the ticket.

## The problem

{{PATRICK_STORY: one or two sentences, a real night when a bench failure cost the team a day}}
On a shared test bench, a red result arrives with no context. Someone has to work out whether a
wire came loose or a commit broke the build, and the evidence (serial output, fault registers,
the diff) is spread across three tools. That first question, hardware or code, decides who gets
woken up, and today it is answered by whoever looks first.

## What it does

- Watches the board's serial output (a NUCLEO-F446RE running our own open-source test firmware)
  and opens one incident per fault: a self-test that stops passing, a HardFault, silence, a
  reboot nobody asked for, or the USB link dropping.
- Decides HARDWARE, CODE, UNKNOWN or NONE with a fixed rule, judged only on the firmware sha the
  board has confirmed it is running. The model never makes this call.
- Posts the first alert to the team's Telegram group by itself, with the route ("Routed to:
  Hardware team" or "Firmware team"), the quoted serial line and, for a crash, the file and line.
  The target is under 5 s from the first bad line.
- Then asks a local model to explain the incident. Every claim carries a citation to a serial
  line, a register, a `file:line` or a diff line, and the reply ends with a single DIAGNOSIS line.
- Mirrors each incident into exactly one issue on a Forgejo tracker on the same box, comments on
  flaps, and closes it when the board recovers.

If it works by 15:00: a proposed fix (the agent drafts a patch, the host proves it on the board,
then opens a pull request on the local Forgejo for a human to merge), a full-screen red/green
status view, emulated QEMU boards, and a GPU-run explanation simulator over invented incidents.

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
run on the GB10, which has no internet connection. The only traffic that leaves is the alert text,
through the laptop's SSH relay to Telegram. Tickets carry metadata (route, reason, sha,
`file:line`), never source.

## Safety and limits

The agent runs in a NemoClaw sandbox whose network policy allows only the bench endpoint and the
local model. A request to github.com or example.com is refused with the policy's own denial text
(`policy_denied ... endpoint github.com:443 is not allowed by any policy`). Through the endpoint
the agent can read incidents, runs, events and a commit's `src/` diff, and it can ask for a rerun
or one named self-test, rate-limited and audited. It can never flash, edit code, merge or close a
ticket. Board text, commit messages and diffs are treated as data, never as instructions. When the
evidence does not decide, the agent is told to say it can't tell and name the one check that would.

## Results

On {{N_TRIALS}} trials today, the rule was right on {{RULE_RIGHT}} and the agent on {{AGENT_RIGHT}}; the agent said it couldn't tell on {{CANT_TELL}}; the median time from fault to first alert was {{MEDIAN_FAULT_TO_ALERT_S}} s.

## Known shortcuts

- vLLM listens on loopback only; user-level TCP forwarders let the containers reach it.
- An OpenClaw config workaround covers a version mismatch between NemoClaw and OpenShell 0.0.116.
- The Telegram bot is a throwaway demo bot; its token lives only in a file on the GB10.
- The GB10 reaches Telegram only through the laptop's SSH relay.

The device under test runs our open-source fault-demo firmware with today's additions on its
branch `day/health-dump` (github.com/phucducnguyen/nucleo-f446re-fault-demo).

## Disclosure

The test firmware is our own open-source repo, published before today. We prototyped the idea last week; none of that code is here. This repo started empty this morning, and every line in it was written today.

## How to run it

On the GB10, from the repo root (Python 3.12, standard library plus pyserial):

```bash
python3 -m unittest discover -s tests -t .      # every test, no board needed
tracker/setup_forgejo.sh                        # Forgejo at 127.0.0.1:3000, tokens to files
python3 -m bench.run --expect-flash <sha>       # serial owner, incidents, triage, outbox
python3 -m bench.sender                         # outbox to Telegram (LW_TG_API = the relay)
python3 -m tracker.mirror                       # routing rows to Forgejo issues
python3 -m bench.tail --raw                     # board check only, while bench.run is stopped
python3 -m agent.explain --incident <id>        # one explanation, ends with DIAGNOSIS
```

Without a board, the stub stands in for the bench:

```bash
python3 stub/make_db.py --out /tmp/lw-stub.db
python3 stub/serve.py --db /tmp/lw-stub.db --host 127.0.0.1 --port 8765 --token-file <file>
python3 -m stub.emit --db /tmp/lw-stub.db --scenario jumper
```
