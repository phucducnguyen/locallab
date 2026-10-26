# Results measured today (Oct 25 2026)

Everything here was produced on the day, on the NVIDIA GB10 and the real NUCLEO-F446RE board,
by the code in this repo. Nothing is copied from elsewhere.

## Real board: rehearsal incidents (`board-round.tsv`)

Made with `python3 -m bench.trial_log --since 0 --header` from the bench's own database
(times are UTC). These are rehearsal incidents, not the scored run: wires pulled, the stop
button pressed, the RESET button, and the HardFault demo commit.

| What | Measured |
| --- | --- |
| First Telegram alert after the first bad serial line | 0.78-3.89 s over 10 incidents, median 1.58 s; all under the 5 s target |
| Stop button (operator action) | 0.78 and 1.01 s |
| Wire pulled (test, sensor, radio) | 1.89-3.15 s |
| HardFault from a new commit | 1.07 s; the decode found `src/cmd.c:35`, and the explanation named the commit |
| Explanation from the local model (Qwen3.6-35B-A3B on vLLM, in the sandbox) | 18-46 s for 9 of 10; one 91 s for a silent board |

The `diagnosis` column is the agent's own last line. The bench's rule, not the model, sets
`triage`. Some HARDWARE answers name the firmware sha in COMMIT where the skill asks for `none`;
we report them as they are.

## Explanation simulator (`sim-old-skill.json`, `sim-frozen-skill.json`)

`agent/sim_eval.py` runs invented incidents (no recordings, no real cases) through the same skill
and model, and scores the replies automatically. Same cases, old skill vs the skill frozen at
`ebada0f` before any scored trial: label right 100% vs 100%; COMMIT field right 82.4% vs 98.9%;
replies whose every citation resolves 13.7% vs 42.0%.

## Emulated boards (`fleet-soak-600.json`)

`fleet/` runs QEMU boards next to the real one. Measured on the GB10: 8 emulated boards for a
10-minute soak, 10.4 health lines per second each, 0 anomalies; about 1.35 CPU cores per board.

## Scored run

The manual 6-trial scored run (faults the agent was never tuned on, graded against an answer
sheet the build never saw) was not run on the day. The tooling for it is ready, so this folder has
no `trials-*.tsv` file.
