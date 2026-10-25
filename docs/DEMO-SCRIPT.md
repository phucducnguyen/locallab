# Lab Watch demo script

Three minutes, five beats, then the additions for a five-minute slot. Lines in quotes are what
Patrick says. Commands run in the GB10 console. Numbers come only from today's scoring log; every
`{{...}}` is filled at 16:00.

## Screen layout

| Pane | What it shows |
| --- | --- |
| Dashboard | Firefox full screen on the GB10 monitor, http://127.0.0.1:8770/: the board drawing, a plain headline, the incident timeline |
| GB10 console | tmux session `lw-demo`: bench log on top, a free shell below |
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
- **Say:** "{{PATRICK_STORY_ONE_LINE}} Lab Watch answers the first question for you: hardware or
  code, and who to call."
- **Type:** nothing.
- **Fallback:** none needed; keep talking.

### Beat 2: pull the jumper (0:20-1:05, 45 s)

- **On screen:** camera on the A2-A3 sensor wire, the dashboard, Telegram, timer.
- **Do:** pull the A2-A3 sensor wire and start the timer.
- **Say, while it lands:** "The dashboard turns the sensor wire red. The bench decides with a
  fixed rule, not the model: this check passed on this same firmware, so it's hardware." When the
  alert lands, stop the timer and read it: "Board 1: the sensor wire came loose. Team: hardware.
  First alert in {{THIS_PULL_S}} seconds." The board photo follows as a reply, then the 🧠
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
  git -C ~/bench-fw fetch -q ~/fw bug/hardfault && git -C ~/bench-fw cherry-pick FETCH_HEAD
  ```
- **Say:** "A teammate lands a commit. The bench flashes it, waits for the board to confirm the
  new sha, three self-tests pass, and then it crashes." When the alert lands: "Board 1: the new
  code made it crash. Team: firmware, with the file and line in Details." Show the issue labelled firmware:
  "The ticket has the sha and the line, never the source." Point at the explanation naming the
  commit and its `src/` line.
- **Type, right after the beat:**
  ```bash
  git -C ~/bench-fw reset --hard HEAD~1
  ```
- **Fallback:** if the flash or the alert stalls past 15 s, play clip 3 and reset `~/bench-fw` as
  above.

### Beat 4: local-first and the limit (1:55-2:20, 25 s)

- **On screen:** shell pane.
- **Type:**
  ```bash
  nvidia-smi
  nemoclaw labwatch exec -- curl -sS -m 5 https://github.com
  ```
- **Say:** "The model runs here, on the GB10. The agent lives in a sandbox that can reach only the
  bench and the model; here is the policy refusing GitHub in its own words. It can read and ask for
  a rerun; it can never flash, edit, merge or close."
- **Fallback:** if a command hangs, play clip 4. Never offer a plain connection error as proof:
  the GB10 has no internet, so only the `policy_denied` text counts.

### Beat 5: the number and the disclosure (2:20-2:40, 20 s)

- **On screen:** the scoring sentence, then `git log --oneline` in the shell pane.
- **Say:** "On {{N_TRIALS}} trials today, the rule was right on {{RULE_RIGHT}} and the agent on
  {{AGENT_RIGHT}}; the agent said it couldn't tell on {{CANT_TELL}}; the median time from fault to
  first alert was {{MEDIAN_FAULT_TO_ALERT_S}} s." Then: "The test firmware is our own open-source
  repo, published before today. We prototyped the idea last week; none of that code is here. This
  repo started empty this morning, and every line in it was written today."
- **Type:** `git -C ~/lw-day log --oneline | tail -5` (TO CONFIRM the repo path on the GB10).
- **Fallback:** read the sentence from the slide.

That leaves about 20 s of slack inside the 3 minutes.

## The 5-minute additions, in order

1. **The full story (+45 s).** Before beat 2: {{PATRICK_STORY_FULL}}.
2. **A fault the judge picks (+45 s).** After beat 3. Offer: any of the three wires out, the blue
   stop button held ("someone pressed the stop button. Not a fault."), the RESET button, the USB
   cable pulled for 20 s. Say what the bench should conclude before it does. Grounding A1, A3 or A5
   is allowed only with that wire out.
3. **The trial table (+30 s).** After beat 5's number: the per-trial table from lane D's log,
   rule and agent side by side, "can't tell" counted separately and never as right.

Any one of these can replace addition 2: the agent's proposed fix opened as a pull request on
Forgejo for a human to merge, `/status` in the group, or the QEMU fleet of eight emulated boards.

## 16:30 checklist: record the backup clips

One clip per beat, recorded on the real setup, each a little shorter than its beat.

- [ ] Clip 1: board and quiet Telegram while Patrick says the story line (20 s).
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
- [ ] tmux sessions running: sender, bench, mirror, commands, snapshot, dashboard, and `lw-demo`
      for the console.
- [ ] Firefox full screen on http://127.0.0.1:8770/, headline green.
- [ ] Board: `LW RESULT PASS`, HEALTH with loopback, sensor and radio OK, estop OFF; all three
      wires seated.
- [ ] `~/bench-fw` main has no HardFault commit left from a rehearsal.
- [ ] Telegram test message reaches HackathonGB10SF; `/help` answers in the group.
- [ ] Tracker open on the issues page; no open issues left from rehearsal.
- [ ] Timer at zero; phone camera on CN8; backup clips queued.
