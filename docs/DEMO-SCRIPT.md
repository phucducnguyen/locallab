# LocalLab demo script

Three minutes, five beats, then the additions for a five-minute slot. Lines in quotes are what
the presenter says. Commands run in the GB10 console. Numbers come only from today's measurements
(see the Results section of the [README](../README.md)).

## Screen layout

| Pane | What it shows |
| --- | --- |
| Dashboard | Firefox full screen on the GB10 monitor, http://127.0.0.1:8770/: the board drawing, a plain headline, the incident timeline |
| GB10 console | tmux session `lw-demo`: status view (board, incidents, stopwatch) on top; bench log and Telegram + tracker log below |
| Telegram | group HackathonGB10SF, on the laptop: short alerts, then the board photo, then the 🧠 explanation |
| Tracker | http://127.0.0.1:3000/labwatch/bench-1/issues, in a browser on the GB10 |
| Timer | a stopwatch from fault to first alert, next to Telegram |
| Camera | phone on the board's CN8 header and the blue button |

## Wiring (CN8 and the button)

| Part | Pins | Pulled reads |
| --- | --- | --- |
| Test wire | A0-A1 | loopback OPEN |
| Sensor wire | A2-A3 | sensor OPEN |
| Radio wire | A4-A5 | radio OPEN |
| Stop button | blue B1, held | estop ON (an operator action, not a fault) |

Never ground A0, A2 or A4; ground A1, A3 or A5 only with that pair pulled.

Never say "production". If anything lags past 15 s, play that beat's backup clip and say out loud
that it is a recording.

## The 3-minute version

### Beat 1: the story (0:00-0:20, 20 s)

- **On screen:** the dashboard all green with its plain headline; Telegram and the tracker quiet.
  Board in camera.
- **Say:** "When a board test fails, teams lose time deciding whether it's the board or the latest code.
  LocalLab answers the first question for you: hardware or
  code, and who to call."
- **Type:** nothing.
- **Fallback:** none needed; keep talking.

### Beat 2: pull the jumper (0:20-1:05, 45 s)

- **On screen:** camera on the A2-A3 sensor wire, the dashboard, Telegram, timer.
- **Do:** pull the A2-A3 sensor wire and start the timer.
- **Say, while it lands:** "The dashboard turns the sensor wire red. The bench decides with a
  fixed rule, not the model: this check passed on this same firmware, so it's hardware." When the
  alert lands, stop the timer and read it: "Board 1: the sensor wire came loose. Team: hardware.
  Not a code problem." Read the first-alert time off the timer (today: 1.97-2.03 s for a pulled wire). The board photo follows as a reply, then the 🧠
  explanation: "It ends with a DIAGNOSIS line, and every claim behind it cites a serial line."
  Show the new issue labelled hardware.
- **Live moment:** type `/ask <n>` in the group with the incident number from the alert. "Anyone
  on the team can ask the local agent. Host code does every action; the model only answers."
- **Do:** put the wire back. "✅ the sensor wire is back. Fixed after N seconds." posts; the issue
  closes.
- **Fallback:** at 15 s with no Telegram post, stay on the dashboard and narrate the bench's
  verdict, then play clip 2. If `/ask` takes past 60 s, move on and come back to it.

### Beat 3: a HardFault commit (1:05-1:55, 50 s)

- **On screen:** the shell pane of `lw-demo`, then the dashboard, Telegram, tracker.
- **Type:**
  ```bash
  git -C ~/bench-fw fetch -q ~/fw refs/remotes/origin/bug/hardfault && git -C ~/bench-fw -c user.name="LocalLab bench" -c user.email=lw@example.invalid cherry-pick FETCH_HEAD
  ```
- **Say:** "A teammate lands a commit. The bench's watcher flashes it, waits for the board to confirm the
  new sha, three self-tests pass, and then it crashes." When the alert lands: "Board 1: the new
  code made it crash. Team: firmware, with the file and line in Details." Show the issue labelled firmware:
  "The ticket has the sha and the line, never the source." Point at the explanation naming the
  commit and the line (in today's rehearsal: `src/cmd.c:35`).
- **Type, right after the beat:**
  ```bash
  git -C ~/bench-fw reset --hard a0f0c9f
  ```
  The watcher flashes the baseline a0f0c9f back by itself.
- **Fallback:** if the flash or the alert stalls past 15 s, play clip 3 and reset `~/bench-fw` as
  above.

### Beat 4: local-first and the limit (1:55-2:20, 25 s)

- **On screen:** shell pane.
- **Type:**
  ```bash
  nvidia-smi
  export PATH=$HOME/.local/bin:$PATH NEMOCLAW_GATEWAY_PORT=8090
  nemoclaw labwatch exec -- curl -sS -m 5 http://github.com/
  ```
  Expected: `{"detail":"GET github.com:80/ not permitted by policy","error":"policy_denied"}`.
- **Say:** "The model runs here, on the GB10. The agent lives in a sandbox that can reach only the
  bench and the model; here is the policy refusing GitHub in its own words. It can read and ask for
  a rerun; it can never flash, edit, merge or close."
- **Fallback:** if a command hangs, play clip 4. Never offer a plain connection error as proof:
  the GB10 has no internet, so only the `policy_denied` text counts.

### Beat 5: the number and the disclosure (2:20-2:40, 20 s)

- **On screen:** the results sentence, then `git log --oneline` in the shell pane.
- **Say:** "Measured today on the real board, the first alert came 0.78 to 2.03 seconds after the first bad
  line, and the local explanation took 18 to 46 seconds. The scored six-trial results go in results/
  after the run." Then: "The test firmware is our own open-source
  repo, published before today. We prototyped the idea last week; none of that code is here. This
  repo started empty this morning, and every line in it was written today."
- **Type:** `git -C ~/lw log --oneline | tail -5` (the repo is `~/lw` on the GB10).
- **Fallback:** read the sentence from the slide.

That leaves about 20 s of slack inside the 3 minutes.

## The 5-minute additions, in order

1. **The problem in full (+45 s).** Before beat 2: hardware test benches fail silently or noisily.
   When a board test fails, teams lose time deciding whether it is the board or the latest code, and
   that first question decides who gets called. LocalLab answers it in seconds, locally.
2. **A fault the judge picks (+45 s).** After beat 3. Offer: any of the three wires out, the blue
   stop button held ("someone pressed the stop button. Not a fault."), the RESET button, the USB
   cable pulled for 20 s. Say what the bench should conclude before it does. Grounding A1, A3 or A5
   is allowed only with that wire out.
3. **The trial table (+30 s).** After beat 5's number: the per-trial table (`python3 -m bench.trial_log --since <unix time> --header`),
   rule and agent side by side, "can't tell" counted separately and never as right.

Any one of these can replace addition 2: `/status` in the group, or the QEMU fleet of eight
emulated boards. The fix beat (`python3 -m agent.fix --incident <n>`: the agent proposes a patch,
the host proves it on the board and opens a pull request) exists but was not run live today; use
it only after a rehearsal, and say it is optional.

## 16:30 checklist: record the backup clips

One clip per beat, recorded on the real setup, each a little shorter than its beat.

- [ ] Clip 1: board and quiet Telegram while the presenter says the problem line (20 s).
- [ ] Clip 2: sensor wire pull with timer, dashboard red, first alert, board photo, 🧠
      explanation, `/ask` answer, hardware issue, wire back, issue closed (45 s).
- [ ] Clip 3: the cherry-pick, verified flash, three passes, HardFault, firmware issue with
      `file:line`, then the reset (50 s).
- [ ] Clip 4: `nvidia-smi` and the `policy_denied` refusal (25 s).
- [ ] Clip 5: the scoring sentence and `git log` (20 s).
- [ ] Each clip plays from the laptop with no network; file names say the beat number.

## Pre-demo checklist

- [ ] GPU explanation simulator paused (it must not compete with the live explanation).
- [ ] Laptop SSH relay up (model tunnel and Telegram relay in one ssh).
- [ ] tmux sessions running (`tmux ls`): lw-bench, lw-sender, lw-endpoint, lw-mirror,
      lw-fwd8000, lw-fwd8000b, lw-dash, lw-commands, lw-snapshot, and lw-demo for the console.
      `~/lw-tools/reset.sh <tag>` (a GB10 script outside this repo) restarts them all with a fresh
      DB and cleared issues; use it only before the demo.
- [ ] Firefox full screen on http://127.0.0.1:8770/, headline green.
- [ ] Board: `LW RESULT PASS`, HEALTH with loopback, sensor and radio OK, estop OFF; all three
      wires seated.
- [ ] `~/bench-fw` main is at a0f0c9f, with no HardFault commit left from a rehearsal.
- [ ] Telegram test message reaches HackathonGB10SF; `/help` answers in the group.
- [ ] Tracker open on the issues page; no open issues left from rehearsal.
- [ ] Timer at zero; phone camera on CN8; backup clips queued.
