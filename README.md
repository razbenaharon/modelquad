<p align="center">
  <img src="docs/images/modelquad-logo.png" alt="ModelQuad" width="180">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/platform-Windows-0078D6" alt="Platform: Windows">
</p>

# ModelQuad

A second opinion from Codex inside Claude Code, on Windows.

ModelQuad is a local MCP server for Claude Code. It lets Claude ask your own
OpenAI Codex CLI for a read-only review, exploration or architecture opinion,
and it shows Claude's and Codex's usage side by side. ModelQuad and the CLIs
it drives run on your computer, under your own logins. It works alongside
OpenAI's official Codex plugin for Claude Code: the plugin for when you want
Codex to change code, ModelQuad for a locked-down second opinion.

Status: early, source-only release. Windows only.

## What you get

- **A read-only second opinion from Codex.** Claude Code can send Codex a
  review, exploration or architecture question. ModelQuad reads the files the
  request names and passes their text to Codex with the task. Codex runs in an
  empty temporary folder, read-only, without file, command, web or MCP tools.
  When Codex says other files would have helped, the reply lists them (only
  existing files that ModelQuad's rules would send), and Claude can offer you
  a follow-up call with them. Nothing is sent until you approve that call.
- **Progress phases while Codex works.** Claude Code shows packing, start,
  periodic working-time and answer/withheld phases without task or file content.
- **Both quotas in the status line.** Claude's and Codex's 5-hour and weekly
  usage, in Claude Code's status line and in ModelQuad's replies, with the
  5-hour reset in your local time ("resets 15:00") and, once there are two
  readings, when the window fills at the current pace. Missing data is said in
  plain words ("shows after your first message", "sign in to Codex", "Codex
  not found", "no 5-hour limit on your plan") or as unknown, never as 0%.
- **A low-quota handoff suggestion.** When Claude has used 80% of its 5-hour
  window or 90% of its weekly window, ModelQuad suggests handing the task to
  Codex, unless Codex has itself used 90% of either window. When only the
  5-hour window is over and it resets within 30 minutes, it suggests waiting
  instead. The suggestion only asks. It starts nothing and does not change
  either provider's limits.
- **An approval prompt on every Codex call.** Claude Code asks you before each
  call that would start Codex.
- **Optional background jobs.** With the local service running and background
  work turned on (off by default), a delegated task keeps running after you
  close the chat. Answers are kept for 7 days by default.

## What it is not

- Not a product of Anthropic or OpenAI (see [Not affiliated](#not-affiliated)).
- Not a way to have Codex change your code. ModelQuad's Codex runs are
  read-only (see
  [ModelQuad and OpenAI's Codex plugin](#modelquad-and-openais-codex-plugin)).
- Not a proxy. ModelQuad does not extract your provider credentials or relay
  provider traffic. It runs the official CLIs, which use their own logins.
- Other AI providers, a desktop window, a tray icon, an installer and team
  features are not supported.

## Claude offers; you approve each call

- By default, Claude offers a read-only Codex review when one would help, for
  example after a multi-file change or a risky fix, or before a commit or PR,
  and calls Codex only when you agree or ask. `modelquad settings proactive on`
  lets Claude decide on its own to consult Codex (`off` goes back); it takes
  effect in new Claude Code sessions. Either way, the approval prompt below
  still appears before each call.
- This guidance reaches Claude through ModelQuad's MCP server instructions and
  the `/modelquad` skill. ModelQuad writes nothing into your global
  `CLAUDE.md`.
- The two tools that start Codex, `modelquad_delegate` and `modelquad_handoff`,
  are marked as needing your interaction (`anthropic/requiresUserInteraction`).
  Claude Code shows its approval prompt before every call, also in auto mode
  and in bypass-permissions mode. ModelQuad adds no permission rule for its
  tools.
- `modelquad_handoff` then shows a prompt of its own. Codex starts only if you
  tick the box and accept. If your Claude Code cannot show that form, nothing
  starts, and Claude asks you in the conversation instead and, only if you
  agree, uses `modelquad_delegate`, which Claude Code asks about first.
- To block new Codex calls from Claude Code: `modelquad host disconnect claude`.
  Undo with `modelquad host connect claude`.
- Treat Codex's answer as input to check, not as a verified result.

## Terminal or desktop app

ModelQuad works in Claude Code in a terminal and in the Claude desktop app's
Code tab, with differences found in testing (Claude Code 2.1.296, October
2026):

- **Status line, and so Claude's quota and Claude Code's version, appear only
  in a terminal session.** The desktop app's Code tab has no status line, so
  there Claude's quota stays unknown or goes stale; Codex's quota still shows,
  for example with `/modelquad-quota`.
- **The approval prompt before each Codex call appears in both,** also in auto
  mode.
- **The handoff form** appears in a terminal session. The desktop app's Code
  tab never shows MCP forms (it answers each one "decline" without showing
  it), so there `/modelquad-handoff` takes Claude Code's approval prompt as
  your answer: that prompt lists the files, and only you can click it. If any
  other Claude Code cannot show the form, the handoff starts nothing and
  Claude falls back to asking you in the conversation.
- **A handoff window** (the files and Approve / Decline buttons, for a Claude
  Code that renders MCP Apps) is built but switched off: in testing (October
  2026) the desktop app drew it empty and its Approve press never reached
  ModelQuad. `modelquad_doctor` (the MCP tool) says how this session asks.
- **Progress while Codex works** shows where Claude Code displays MCP progress;
  it was not visible in the desktop app's Code tab.

## Requirements

- Windows, native. WSL and other systems are not supported, and install
  refuses to run there.
- Python 3.11 or newer.
- Claude Code, installed and signed in. Claude's quota comes from the status
  line data Claude Code provides with a Claude Pro or Max sign-in. With a Team,
  Enterprise or API-key sign-in, Claude's quota shows as unknown.
- The OpenAI Codex CLI, version 0.160.0 or newer, installed and signed in with
  your ChatGPT account. Codex's quota is read through that sign-in. An npm
  install works: ModelQuad runs the native `codex.exe` that the npm package
  ships, not its `codex.cmd` launcher.
- If `OPENAI_API_KEY` or `CODEX_API_KEY` is set in your environment, Codex may
  use that key instead of your ChatGPT sign-in, and OpenAI then bills the
  key's API account per use. ModelQuad passes both variables on to Codex,
  because they are a way to sign Codex in; unset them if you want only your
  ChatGPT plan used.

## Install

From a checkout of this repository, in PowerShell:

    pip install ".[service]"

The `service` extra is only needed for the optional local service, which runs
background jobs and the startup reminder.

Claude Code starts `modelquad` and `modelquad-mcp` by name, so the folder pip
put them in must be on the PATH that Claude Code sees. `modelquad doctor`
checks this.

To upgrade, close every Claude Code window first (in a terminal session, type
`/exit`), then run the same `pip install` again. A running Claude Code keeps
`modelquad-mcp.exe` open, and Windows does not let pip replace a running
program: the upgrade then fails halfway and leaves ModelQuad uninstalled until
you run it again with Claude Code closed.

A plain install keeps its state in `%USERPROFILE%\.local\share\modelquad`,
outside `AppData`: Windows gives the Claude desktop app's programs a private
copy of `AppData`, so a folder there would split in two.
An editable install (`pip install -e`) is treated as a development checkout
and keeps its state in `%USERPROFILE%\.modelquad` instead. `modelquad state`
shows which folder is in use.

## Quick start

1. Sign in to Claude Code and to the Codex CLI.
2. Run `modelquad onboard`. It finds Claude Code and Codex, prints what
   ModelQuad sends where, and writes the Claude Code integration listed below.
   Add `--dry-run` to see the changes first. `modelquad install claude` writes
   the same integration on its own and prints the same notice.
3. Restart Claude Code, then run `modelquad doctor --first-run`. It ends with
   a plain checklist of what is set up and what is left, including the
   Windows traps: `python` opening the Microsoft Store, the `Scripts` folder
   missing from PATH, and Git for Windows missing.
4. In Claude Code, ask for it and name the files, for example: "Ask Codex to
   review src/server.py and src/auth.py." Codex receives the task text and the
   text of the files the request names. It has no tool to open other files.

Optional: `modelquad service install` registers a sign-in task for the local
service, and `modelquad service start` starts it now. With the service
running, `modelquad settings job-runner on` keeps delegated work running after
you close the chat. It explains what that means and turns on only after you
type `yes` (or pass `--yes` where there is no prompt).

### Or as a Claude Code plugin (experimental)

The ModelQuad plugin carries the server, the five commands (as
`/modelquad:review` and the like), the startup reminder, and, in the desktop
app's Code tab, a line above the prompt with Codex's quota. It still needs the
package from the step above:

    claude plugin install modelquad --marketplace razbenaharon/modelquad
    modelquad install claude --plugin

The second command adds the status line, which a plugin cannot set, and takes
back the server, commands and reminder an earlier `modelquad install claude`
added, so Claude Code does not run them twice. Someone who installs only the
plugin sees the notice of what ModelQuad sends where on their first Codex
call, which starts nothing. See [docs/PLUGIN.md](docs/PLUGIN.md).

## What it changes on your machine

`modelquad install claude` (and `modelquad onboard`) change Claude Code's user
configuration. They read every target file first, then write each changed
file once. A file that is a link, is not UTF-8 text, cannot be read or is not
valid JSON is skipped with a reason; the other files still change.

- `%USERPROFILE%\.claude.json`: an MCP server entry named `modelquad`, with
  `"timeout": 900000` (15 minutes) for ModelQuad's tool calls only. If a
  `modelquad` entry already existed, ModelQuad records it in the state folder
  so uninstall can put it back.
- `%USERPROFILE%\.claude\settings.json`:
  - a status line that runs `modelquad usage-hook claude`. If you already have
    a status line, it is left alone (see below);
  - a `SessionStart` hook that runs `modelquad hook-pending claude`. When the
    local service runs, it tells Claude which answers are waiting for the
    current project.
- `%USERPROFILE%\.claude\skills\modelquad\SKILL.md`: the `/modelquad` skill.
- `%USERPROFILE%\.claude\skills\modelquad-review`, `modelquad-handoff`,
  `modelquad-quota` and `modelquad-setup`: commands only you start
  (`/modelquad-review` asks Codex about files you name, `/modelquad-handoff`
  hands the current read-only task to Codex, `/modelquad-quota` shows both
  quotas, `/modelquad-setup` explains any setup problem). Claude Code still
  asks before every Codex call.
- Backups: before it changes an existing file, ModelQuad saves a full copy of
  the original bytes beside it, named `<file>.modelquad-backup-<time>`. JSON
  files are written back with your key order.

ModelQuad does not write your global `CLAUDE.md`, add a `PreToolUse` hook, or
set `env.MCP_TOOL_TIMEOUT` (which Claude Code applies to every MCP server).
Earlier versions did all three. Install and uninstall take back each one an
earlier version added, with a backup first, and say so in their report: the
block between `<!-- >>> modelquad managed block >>> -->` markers in
`CLAUDE.md` (the rest of the file is not changed), a `PreToolUse` hook that
runs ModelQuad's own `hook-gate`, and an `MCP_TOOL_TIMEOUT` that ModelQuad
recorded as its own and you did not change. A value you set yourself stays.

If `CLAUDE_CONFIG_DIR` is set, Claude Code reads its settings from that
folder, but ModelQuad still writes to the places above. Install and
`modelquad doctor` warn about this; ModelQuad does not follow the variable.

**If you already have a status line.** ModelQuad leaves it alone. Then
ModelQuad has no Claude quota and makes no handoff suggestion until you add it,
because the status line is its only source of Claude's quota. Install,
`modelquad onboard` and `modelquad doctor` print the exact one-line recipe for
your setup. `modelquad usage-hook claude --segment` records the quota and
prints only ModelQuad's compact part, for example
`Codex 5h 70% left | Claude 5h 38% left`:

- ccstatusline: add a Custom Command widget whose command is
  `modelquad usage-hook claude --segment`.
- Any other status line command: run yours and then ModelQuad's on the same
  input. In `settings.json` (Claude Code runs status line commands through
  bash):

      "statusLine": {
        "type": "command",
        "command": "input=$(cat); printf '%s' \"$input\" | your-status-line-command; printf '%s' \"$input\" | modelquad usage-hook claude --segment"
      }

  Use `--quiet` instead of `--segment` to record the quota and print nothing.

While it runs, ModelQuad also writes:

- `%USERPROFILE%\.claude\usage-claude.json`, `usage-codex.json` and small
  `*.lock` files: the latest quota readings; `quota-pace.json`: the first
  reading of each current 5-hour window, for the pace hint.
- The state folder (see Install): the history database, answers waiting to be
  collected, logs, the service secret, an installation id, the record of any
  registration that install replaced, and the other small files listed in
  [PRIVACY.md](PRIVACY.md). The registration record can include `env` values
  from your old entry, such as keys.

Only when you ask for them:

- `modelquad service install`: a Task Scheduler task named
  `ModelQuad_Service` that starts the local service when you
  sign in. It runs as you, with limited rights.
- `modelquad tools --fix-path`: adds the folder of an installed Claude Code or
  Codex CLI that is not on PATH to your user PATH.

ModelQuad reads the model settings in Codex's `config.toml` and does not change
the file.

## How to undo it

1. `modelquad service stop`, then `modelquad service uninstall`, if you
   installed the service.
2. `modelquad uninstall claude` removes the MCP entry (with its timeout), the
   `SessionStart` hook, the status line ModelQuad added and the skill, and
   anything an earlier version added that install had not yet taken back (the
   `CLAUDE.md` block, the `PreToolUse` hook, the global timeout setting if
   ModelQuad set it and you did not change it). When the `statusLine` object
   holds only what ModelQuad wrote, the whole object goes. Uninstall puts back
   a `modelquad` entry that install replaced. Use `--dry-run` to preview.
   `modelquad integrations off` removes the same wiring without putting back
   an earlier entry, and refuses later installs until
   `modelquad integrations on`.
3. Delete what uninstall leaves in `%USERPROFILE%\.claude`, by these exact
   names: `usage-claude.json`, `usage-codex.json`, `quota-pace.json`,
   `usage-claude.json.lock`, `usage-codex.json.lock`,
   `usage-refresh-claude.lock` and `usage-refresh-codex.lock`. Do not delete
   other `.lock` or `usage-*.json` files there: Claude Code and other tools
   keep their own files in the same folder (`daemon.lock` is Claude Code's).
   Also delete the `*.modelquad-backup-*` files, which install left beside
   each file it changed.
4. Delete the state folder (`modelquad state` shows it).
5. If you used `tools --fix-path`, remove the added folder from your user PATH.
   If you added `modelquad usage-hook claude --segment` or `--quiet` to your
   own status line, remove it there.
6. `pip uninstall modelquad`.

## Privacy and network

- ModelQuad has no server of its own, no update check and no telemetry. Its
  own processes connect only to loopback addresses; an audit hook refuses any
  other connection from them. `modelquad network` shows the rule.
- For a Codex call, ModelQuad reads the files the request names, inside the
  working folder only. It skips credential files such as `.env` and
  `auth.json`, replaces secret-looking values with `<credential>`, and refuses,
  with a reason, text that holds a private key or a recognizable access token.
  Your Codex CLI then sends the task and that text to OpenAI, under OpenAI's
  terms.
- What ModelQuad returns to Claude Code becomes part of your conversation,
  which Claude Code sends to Anthropic under Anthropic's terms: Codex's
  answers, quota percentages and reset times, and history entries.
- The MCP server asks your Codex CLI for your Codex quota when it starts and
  then about every 4 minutes. No prompt is sent. Set the environment variable
  `MODELQUAD_CODEX_QUOTA_REFRESH` to `0` to stop this. Starting a task does not
  wait for a quota read.
- The optional local service listens on `127.0.0.1` only, and every request
  except its listener check needs the installation secret.
- Answers waiting to be collected are kept for 7 days by default. History
  stays until you delete it.
- **Model training.** Anthropic's and OpenAI's terms each forbid using their
  output to train the other's models, and ModelQuad passes text in both
  directions. We recommend turning off model training in your ChatGPT data
  controls and in your Claude privacy settings while you use ModelQuad.
  Opting out does not cover feedback you submit or conversations flagged for
  safety review.

Details: [PRIVACY.md](PRIVACY.md), [NETWORK.md](NETWORK.md). For settings that
keep ModelQuad away from sensitive work (a repository it must not touch, secret
files, employer code), see
[docs/RECOMMENDED-SETTINGS.md](docs/RECOMMENDED-SETTINGS.md).

## ModelQuad and OpenAI's Codex plugin

OpenAI publishes an official Codex plugin for Claude Code
([openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc)).
ModelQuad works alongside it, and you can use both. The plugin lets Codex
write code and offers its own reviews. ModelQuad adds:

- both quotas in the Claude Code status line;
- a low-quota handoff suggestion;
- locked-down, read-only Codex reviews, with an approval prompt on every call;
- optional background jobs that keep running after you close the chat.

ModelQuad is an independent project. It is not affiliated with or endorsed by
OpenAI or Anthropic.

## Not affiliated

ModelQuad is an independent project. It is not affiliated with, endorsed by
or sponsored by Anthropic or OpenAI. "Claude" and "Claude Code" are
trademarks of Anthropic. "OpenAI" and "Codex" are trademarks of OpenAI.

The code is open source; the ModelQuad name and logo are not part of the
licence. A changed version needs a name of its own. See
[TRADEMARK.md](TRADEMARK.md).

<!-- disclaimer: pending legal review -->
You are responsible for following the terms of each provider whose tools you
use, including Anthropic's and OpenAI's terms and usage policies. ModelQuad
runs the official CLIs under your own accounts and does not change what those
terms allow or any usage limit. Anthropic and OpenAI can limit or suspend
accounts. That risk is yours. ModelQuad is provided "as is", without
warranties or conditions of any kind, under the Apache License 2.0; see
sections 7 and 8 of [LICENSE](LICENSE).

## How it was made

ModelQuad was developed with substantial help from AI coding agents.

## Documentation

- [PRIVACY.md](PRIVACY.md): what ModelQuad reads, sends and stores, and how to
  delete it.
- [docs/RECOMMENDED-SETTINGS.md](docs/RECOMMENDED-SETTINGS.md): recommended
  Claude Code settings, in plain language.
- [NETWORK.md](NETWORK.md): which connections ModelQuad and the programs it
  starts make.
- [docs/PLUGIN.md](docs/PLUGIN.md): the experimental Claude Code plugin in this
  repository (not yet a supported install).
- [SECURITY.md](SECURITY.md): how to report a vulnerability, and the security
  design.
- [CONTRIBUTING.md](CONTRIBUTING.md): how to contribute.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md): community standards.
- [AGENTS.md](AGENTS.md): build, test and safety rules for contributors.
- [THIRD_PARTY.md](THIRD_PARTY.md): the packages pip installs with ModelQuad.
- [TRADEMARK.md](TRADEMARK.md): how you may use the ModelQuad name and logo.
- [LICENSE](LICENSE): Apache License 2.0.
