---
name: settings
description: Show ModelQuad on or off, automatic or approve each call, and the history here; change only what the user picks.
disable-model-invocation: true
metadata:
  managed-by: modelquad
---

# ModelQuad settings

## 1. Show the settings

Run this with the Bash tool, with this session's absolute project directory.
It changes nothing:

    modelquad settings show --project "<this session's absolute project directory>"

Show the user its three numbered items in plain words: whether ModelQuad is on
in Claude Code, the mode (automatic or approve each call, and whether this
folder is excluded), and the history line. Where it says unknown, say unknown;
do not guess.

## 2. Ask what to change

Ask one question with the AskUserQuestion tool, "What do you want to change?",
with these options (it takes at most four):

- **Keep as is**: run nothing.
- **Turn ModelQuad off** when item 1 says on, or **Turn ModelQuad on** when it
  says off. Leave this option out when item 1 is unknown.
- **Change to approve each call** when the mode is automatic, or **Change to
  automatic** when it is approve each call. When the mode is unknown, offer
  both of these in place of Keep as is.
- **Show the history**.

Never pick for the user. If the user dismisses the question or answers with
anything else, change nothing and say so.

## 3. Run only the command for the option the user picked

- Turn ModelQuad off: `modelquad host disconnect claude`
- Turn ModelQuad on: `modelquad host connect claude`
- Change to approve each call: `modelquad settings auto off --global`, and also
  the `modelquad settings auto off --project ...` command when the output of
  step 1 printed one.
- Change to automatic: first run `modelquad settings auto on --global` without
  `--yes`. It changes nothing and prints what automatic means. Show the user
  that text in full, then ask with AskUserQuestion "Turn automatic on for every
  folder?" with the options **Yes, turn it on** and **No, keep approve each
  call**. Only on Yes, run `modelquad settings auto on --global --yes` and show
  the user what it prints, including the allow rule it names. Do not add that
  rule or edit any settings file yourself.
- Show the history: `modelquad review report --project "<this session's
  absolute project directory>"`, and show the user what it prints.

After a change, run the command from step 1 again and show the new state. Tell
the user that a change takes effect in new Claude Code sessions.

## Never

- Never undo an exclusion here. Do not run `modelquad settings repo` to allow
  Codex again and do not edit `.modelquad.toml`. If this folder is excluded,
  say so, and point to the documented command
  `modelquad settings repo off [--project DIR]` (its help,
  `modelquad settings repo --help`, says how to undo it); the user runs that
  in their own terminal.
- Never turn the grant on or reconnect ModelQuad unless the user picked that
  option in this run.
- Never open a browser or a web page for settings.
- Run no other command and call no ModelQuad tool.
