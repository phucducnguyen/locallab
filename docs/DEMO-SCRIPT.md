# Lab Watch demo script

Three minutes, five beats, then the additions for a five-minute slot. Lines in quotes are what
Patrick says. Commands run in the GB10 console. Numbers come only from today's scoring log; every
`{{...}}` is filled at 16:00.

## Screen layout

| Pane | What it shows |
| --- | --- |
| GB10 console | tmux session `lw-demo`: bench log on top, a free shell below |
| Raw serial | the bench's own console lines (the bench owns the port; nothing else opens it) |
| Telegram | group HackathonGB10SF, on the laptop |
| Tracker | http://127.0.0.1:3000/labwatch/bench-1/issues, in a browser on the GB10 |
| Timer | a stopwatch from fault to first alert, next to Telegram |
| Camera | phone on the board's CN8 header and the A0-A1 jumper |

Never say "production". If anything lags past 15 s, play that beat's backup clip and say out loud
that it is a recording.

## The 3-minute version

### Beat 1: the story (0:00-0:20, 20 s)

- **On screen:** Telegram and the tracker, both quiet. Board in camera, `loopback=OK`.
- **Say:** "{{PATRICK_STORY_ONE_LINE}} Lab Watch answers the first question for you: hardware or
  code, and who to call."
- **Type:** nothing.
- **Fallback:** none needed; keep talking.

### Beat 2: pull the jumper (0:20-1:05, 45 s)

- **On screen:** camera on the jumper, raw serial, Telegram, timer.
- **Do:** pull the A0-A1 jumper and start the timer. Never ground A0.
- **Say, while it lands:** "Serial says loopback OPEN. The bench decides with a fixed rule, not
  the model: this check passed on this same firmware, so it's hardware." When the alert lands,
  stop the timer: "First alert in {{THIS_PULL_S}} seconds, routed to the Hardware team." Point at
  the explanation as it follows: "Every sentence cites a serial line, and it ends with a DIAGNOSIS
  line." Show the new issue labelled hardware.
- **Do:** put the jumper back. "Recovered after N seconds" posts; the issue closes.
- **Type:** nothing.
- **Fallback:** at 15 s with no Telegram post, stay on raw serial and narrate the bench's verdict,
  then play clip 2.

### Beat 3: a HardFault commit (1:05-1:55, 50 s)

- **On screen:** the shell pane of `lw-demo`, then raw serial, Telegram, tracker.
- **Type:**
  ```bash
  git -C ~/bench-fw fetch -q ~/fw bug/hardfault && git -C ~/bench-fw cherry-pick FETCH_HEAD
  ```
- **Say:** "A teammate lands a commit. The bench flashes it, waits for the board to confirm the
  new sha, three self-tests pass, and then it crashes." When the alert lands: "Code, routed to
  Firmware, with the file and line from the crash registers." Show the issue labelled firmware:
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
2. **A fault the judge picks (+45 s).** After beat 3. Offer: jumper out, the RESET button, the USB
   cable pulled for 20 s. Say what the bench should conclude before it does. Grounding A1 is
   allowed only with the jumper out.
3. **The trial table (+30 s).** After beat 5's number: the per-trial table from lane D's log,
   rule and agent side by side, "can't tell" counted separately and never as right.

If it works by 15:00, any one of these can replace addition 2: the agent's proposed fix opened as
a pull request on Forgejo for a human to merge, the full-screen red/green status view, or an
emulated QEMU board.

## 16:30 checklist: record the backup clips

One clip per beat, recorded on the real setup, each a little shorter than its beat.

- [ ] Clip 1: board and quiet Telegram while Patrick says the story line (20 s).
- [ ] Clip 2: jumper pull with timer, first alert, explanation, hardware issue, jumper back,
      issue closed (45 s).
- [ ] Clip 3: the cherry-pick, verified flash, three passes, HardFault, firmware issue with
      `file:line`, then the reset (50 s).
- [ ] Clip 4: `nvidia-smi` and the `policy_denied` refusal (25 s).
- [ ] Clip 5: the scoring sentence and `git log` (20 s).
- [ ] Each clip plays from the laptop with no network; file names say the beat number.

## Pre-demo checklist

- [ ] GPU explanation simulator paused (it must not compete with the live explanation).
- [ ] Laptop SSH relay up (model tunnel and Telegram relay in one ssh).
- [ ] tmux sessions running: sender, bench, mirror, and `lw-demo` for the console.
- [ ] Board: `LW RESULT PASS` and HEALTH with `loopback=OK`; jumper seated.
- [ ] `~/bench-fw` main has no HardFault commit left from a rehearsal.
- [ ] Telegram test message reaches HackathonGB10SF.
- [ ] Tracker open on the issues page; no open issues left from rehearsal.
- [ ] Timer at zero; phone camera on CN8; backup clips queued.
