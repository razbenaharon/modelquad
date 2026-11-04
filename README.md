<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/modelquad-logo-dark.svg">
    <img src="docs/images/modelquad-logo-color.svg" alt="ModelQuad" width="320">
  </picture>
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
it drives run on your computer, under your own sign-ins. It works alongside
OpenAI's own Codex plugin for Claude Code: the plugin for when you want
Codex to change code, ModelQuad for a read-only second opinion.

Status: early, source-only release. Windows only.

## What you get

- **A read-only second opinion from Codex.** Claude Code can send Codex a
  review, exploration or architecture question. ModelQuad reads the files the
  request names and passes their text to Codex with the task. Codex runs in an
  empty temporary folder, read-only, without file, command, web or MCP tools.
  When Codex says other files would have helped, the reply lists them (only
  existing files that ModelQuad's rules would send), and Claude can offer you
  a follow-up call with them. Nothing is sent until you approve that call.
- **Progress while Codex works.** Claude Code shows its progress (preparing
  the files, started, still working, answered, withheld or not started), never
  the task or file text.
- **Both quotas in the status line.** Claude's and Codex's 5-hour and weekly
  usage, in Claude Code's status line and in ModelQuad's replies, with the
  5-hour reset in your local time ("resets 15:00") and, once there are two
  readings, when the window fills at the current pace. Missing data is said in
  plain words ("shows after your first message", "not signed in (run codex
  login)", "not found", "no 5-hour limit on your plan") or as unknown, never
  as 0%, and an old reading says when it was taken ("as of 14:02").
- **A note when Claude is near its limit.** When Claude has used 80% of its
  5-hour window or 90% of its weekly window, ModelQuad says so, with the reset
  time and Codex's own usage, and that you can wait for the reset or ask Codex
  for a read-only review. You decide. When only the 5-hour window is over and
  it resets within 30 minutes, the note says waiting may be simpler. When
  Codex has itself used 90% of either window, there is no note. The note
  starts nothing and does not change either provider's limits.
- **A proposed division of the work.** For a task of several steps, Claude
  can show who could take each step: Claude builds and runs, Codex reviews,
  explores and advises, read-only. It starts nothing; see
  [Division of work](#division-of-work).
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
  provider traffic. It runs the provider CLIs, which use their own logins.
- Other AI providers, a desktop window, a tray icon, an installer and team
  features are not supported.

## Claude offers; you approve each call

- By default, Claude offers a read-only Codex review when one would help, for
  example after a multi-file change or a risky fix, or before a commit or PR,
  and calls Codex only when you agree or ask. `modelquad settings proactive on`
  lets Claude decide on its own to consult Codex (`off` goes back); it takes
  effect in new Claude Code sessions. Either way, the approval prompt below
  still appears before each call.
- `modelquad settings plan claude <plan>` and `modelquad settings plan codex
  <plan>` record which plan you are on (`unset` clears it; `modelquad settings
  plan` shows both). The values are a fixed list, printed by `modelquad
  settings plan claude` or `codex`. Codex's own report of its plan comes first
  when there is one. ModelQuad uses a plan only to show it to you and to say
  in the notice on each Codex call whether each side is a personal or a work
  account, for example when code would go from a work account to a personal
  one. Claude sees only that kind word, never the plan name. A plan never
  decides which tool works or when.
- `modelquad settings repo <on|off|offer-only|inherit>` sets this for one
  repository (the current folder's, or `--project DIR`): `off` turns Codex off
  there, `offer-only` keeps Claude to offering, `on` lets Claude decide on its
  own there, and `inherit` goes back to the global setting. With no choice it
  shows what applies. A repository's own `.modelquad.toml` can still make
  Claude offer Codex less often, never more. The setting is stored in `preferences.json`
  under a keyed hash of the repository, never its path or name. `off` applies
  from the next call; the other choices reach Claude as one line when a new
  Claude Code session starts in that repository. The approval prompt still
  appears before each call.
- This guidance reaches Claude through ModelQuad's MCP server instructions and
  the guide skill (`/modelquad:guide` in the plugin, `/modelquad` without
  it). ModelQuad writes nothing into your global
  `CLAUDE.md`.
- The two tools that start Codex, `modelquad_delegate` and `modelquad_handoff`,
  are marked as needing your interaction (`anthropic/requiresUserInteraction`).
  Claude Code shows its approval prompt before every call, also in auto mode
  and in bypass-permissions mode. ModelQuad adds no permission rule for its
  tools.
- In a terminal, `modelquad_handoff` then shows the handoff form. Codex starts
  only if you tick the box and accept. In the desktop app's Code tab, which
  shows no forms, Claude Code's approval prompt is your answer, and Codex then
  starts. In any other Claude Code that cannot show the form, nothing starts:
  Claude asks you in the conversation instead and, only if you agree, uses
  `modelquad_delegate`, which Claude Code asks about first.
- To block new Codex calls from Claude Code: `modelquad host disconnect claude`.
  Undo with `modelquad host connect claude`.
- A repository's `.modelquad.toml` and your own `modelquad settings repo off`
  can turn Codex off in one repository (see [Division of work](#division-of-work)).
- Treat Codex's answer as input to check, not as a verified result.

## Division of work

For a task of several steps, Claude proposes a split of the work between the
two tools you already use, and you approve each Codex call. Codex's part is
always read-only: a review, an exploration of the code or an architecture
opinion.

**The proposal.** Claude describes the steps to the `modelquad_split` tool
(or you ask with `/modelquad:split`). For each step the reply says who takes
it and why, in a fixed reason: steps that edit files or run commands are
always Claude's, because Codex through ModelQuad is read-only. A review of
Claude's own change, or another opinion that should not come from Claude, can
be proposed for Codex. The proposal starts nothing. Each Codex step is its own
call, with Claude Code's approval prompt when it runs; there is no way to
approve the whole plan at once. What decides is the kind of step, your
repository setting and the repository's `.modelquad.toml`. Both tools' quota
and the account kind of each plan are shown beside the proposal and never
change it. When Codex reports a limit reached, the reply says a Codex step
would likely be refused. The step titles go back to Claude in the reply only;
ModelQuad does not store or log them.

**Your plans.** `modelquad settings plan claude <plan>` and
`modelquad settings plan codex <plan>` say which plan you are on, from a fixed
list (`modelquad settings plan claude` alone prints it; `unset` removes the
value). A plan Codex itself reports in its quota read comes before the one you
set; otherwise the plan is unknown. `modelquad status` and `modelquad doctor`
show the plans to you. Claude sees only the account kind (personal, workspace
or unknown) and where it came from, in the account line on Codex calls and in
`modelquad_status`; never the plan name. A plan never decides who takes a
step and never starts or stops anything.

**The repository file.** A repository can carry a `.modelquad.toml` that
narrows Codex use there: Codex off, fewer task kinds, lower file limits, a
work account only (one Codex itself reports), or Claude calling Codex only
when you ask. It can never approve a call, add files, turn on Claude's own
initiative or change how Codex runs, and a broken file refuses Codex in that
repository. See [docs/REPO-POLICY.md](docs/REPO-POLICY.md).

**Your own setting for a repository.**
`modelquad settings repo on|off|offer-only|inherit [--project DIR]` records
your choice for one repository: `off` (no Codex there), `offer-only` (Claude
offers and calls only when you agree or ask), `on` (Claude may decide on its
own there) or `inherit` (your global `modelquad settings proactive` applies).
Only you can choose `on`. When the choice differs from your global setting,
ModelQuad's startup reminder tells Claude in one line. Claude Code still asks
you before every Codex call whatever you choose.

**Afterwards.** `modelquad split summary [--project DIR] [--since 1d|7d]` (and
a `split` block for this session in `modelquad_activity`) shows what happened,
from ModelQuad's own history: the Codex requests for that project by kind,
and how many were answered, withheld because Codex tried a tool, refused (by
ModelQuad's rules, the repository's `.modelquad.toml`, your repo setting or
Codex's own limit) or not answered, with the median time to an answer. For
Claude it says only that ModelQuad does not measure Claude's own work;
declines in Claude Code's approval prompt and in the handoff form are not
counted. Quota is each tool's percentage points used in each of its own
windows over the period, one column each and never added together, from the
readings ModelQuad stored (at each Codex request, or on a timer with the
background service). A window with fewer than two readings in the period
reads unknown, and one that reset during the period says so.

**What it never does.**

- Approve a whole plan, or start a Codex step without Claude Code's approval
  prompt for that call.
- Let quota or a plan decide who takes a step, or start, queue or time any
  work because of quota.
- Hand a step to the other tool because one of them reports a limit.
- Let a repository file approve a call, add files, turn on Claude's own
  initiative, or change how Codex runs.
- Keep your plans or repository choices anywhere but your computer: they stay
  in `preferences.json` in ModelQuad's state folder, keyed by a hash of the
  repository, with no path or folder name. Claude sees only each account's
  kind (personal, work or unknown), never the plan name, and the repository's
  Codex setting. See [PRIVACY.md](PRIVACY.md).

## Terminal or desktop app

ModelQuad works in Claude Code in a terminal and in the Claude desktop app's
Code tab, with differences found in testing (Claude Code 2.1.296, October
2026):

- **Status line, and so Claude's quota and Claude Code's version, appear only
  in a terminal session.** The desktop app's Code tab has no status line, so
  there Claude's quota stays unknown or goes stale; Codex's quota still shows,
  for example with `/modelquad:quota`.
- **The approval prompt before each Codex call appears in both,** also in auto
  mode.
- **The handoff form** appears in a terminal session. The desktop app's Code
  tab never shows MCP forms (it answers each one "decline" without showing
  it), so there `/modelquad:handoff` takes Claude Code's approval prompt as
  your answer: that prompt lists the files, and only you can click it. If any
  other Claude Code cannot show the form, the handoff starts nothing and
  Claude falls back to asking you in the conversation. `modelquad_doctor` (the
  MCP tool) says how this session asks.
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

ModelQuad installs as a Claude Code plugin. From PowerShell:

    git clone https://github.com/razbenaharon/modelquad
    cd modelquad
    pip install ".[service]"
    claude plugin marketplace add .
    claude plugin install modelquad@modelquad
    modelquad install claude --plugin
    modelquad doctor

- `pip install` puts the `modelquad` and `modelquad-mcp` commands on PATH. The
  plugin has no programs of its own and runs these two. The `service` extra
  is only needed for the optional local service, which runs background jobs
  and the startup reminder.
- `claude plugin marketplace add .` adds this checkout as a local plugin
  marketplace, and `claude plugin install modelquad@modelquad` installs the
  plugin from it. The plugin carries the server, the five commands
  (`/modelquad:review`, `/modelquad:handoff`, `/modelquad:quota`,
  `/modelquad:setup` and `/modelquad:split`), the guide skill Claude reads
  (`/modelquad:guide`), the startup reminder, and, in the desktop app's Code
  tab, a line above the prompt with Codex's quota.
- `modelquad install claude --plugin` prints what ModelQuad sends where, then
  adds the status line, which a plugin cannot set. It also takes back the
  server, commands and reminder an earlier `modelquad install claude` added,
  so Claude Code does not run them twice. The choice is saved, so `modelquad
  repair` keeps it.
- `modelquad doctor` checks the setup, including that the plugin is installed
  and that the folder pip put the commands in is on the PATH Claude Code sees.

See [docs/PLUGIN.md](docs/PLUGIN.md) for what the plugin contains.

To upgrade, close every Claude Code window first (in a terminal session, type
`/exit`), then run `git pull` and the same `pip install` again, and update the
plugin with `claude plugin marketplace update modelquad` and
`claude plugin update modelquad@modelquad`. A running Claude Code keeps
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
2. Install as above.
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

### Without the plugin

`modelquad install claude` writes the same server, commands and reminder into
Claude Code's user configuration itself, together with the status line
(listed below). Run it after `pip install`, instead of the three plugin
commands. `modelquad onboard` does the same and first finds Claude Code and
Codex. Both print what ModelQuad sends where before they write anything; add
`--dry-run` to see the changes first. Without the plugin the commands are
named `/modelquad-review`, `/modelquad-handoff`, `/modelquad-quota`,
`/modelquad-setup` and `/modelquad-split`, and the guide skill `/modelquad`.
To move from this install to the plugin, install the plugin and run
`modelquad install claude --plugin`; `modelquad install claude --no-plugin`
goes back.

## What it changes on your machine

`modelquad install claude` (and `modelquad onboard`) change Claude Code's user
configuration. They read every target file first, then write each changed
file once. With `--plugin` it writes only the status line below, because
the plugin carries the rest. A file that is a link, is not UTF-8 text, cannot be read or is not
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
  `modelquad-quota`, `modelquad-setup` and `modelquad-split`: commands only
  you start (`/modelquad-review` asks Codex about files you name,
  `/modelquad-handoff` hands the current read-only task to Codex,
  `/modelquad-quota` shows both quotas, `/modelquad-setup` explains any setup
  problem, `/modelquad-split` shows a proposed division of a task). Claude
  Code still asks before every Codex call.
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
ModelQuad has no Claude quota and shows no near-limit note until you add it,
because the status line is its only source of Claude's quota. Install,
`modelquad onboard` and `modelquad doctor` print the exact one-line recipe for
your setup. `modelquad usage-hook claude --segment` records the quota and
prints only ModelQuad's compact part, for example
`Claude 5h 38% left | Codex 5h 70% left`:

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
2. If you installed the plugin, `claude plugin uninstall modelquad@modelquad`
   removes it and `claude plugin marketplace remove modelquad` removes the
   local marketplace.
3. `modelquad uninstall claude` removes the MCP entry (with its timeout), the
   `SessionStart` hook, the status line ModelQuad added and the skill, and
   anything an earlier version added that install had not yet taken back (the
   `CLAUDE.md` block, the `PreToolUse` hook, the global timeout setting if
   ModelQuad set it and you did not change it). When the `statusLine` object
   holds only what ModelQuad wrote, the whole object goes. Uninstall puts back
   a `modelquad` entry that install replaced. Use `--dry-run` to preview.
   `modelquad integrations off` removes the same wiring without putting back
   an earlier entry, and refuses later installs until
   `modelquad integrations on`.
4. Delete what uninstall leaves in `%USERPROFILE%\.claude`, by these exact
   names: `usage-claude.json`, `usage-codex.json`, `quota-pace.json`,
   `usage-claude.json.lock`, `usage-codex.json.lock`,
   `usage-refresh-claude.lock` and `usage-refresh-codex.lock`. Do not delete
   other `.lock` or `usage-*.json` files there: Claude Code and other tools
   keep their own files in the same folder (`daemon.lock` is Claude Code's).
   Also delete the `*.modelquad-backup-*` files, which install left beside
   each file it changed.
5. Delete the state folder (`modelquad state` shows it).
6. If you used `tools --fix-path`, remove the added folder from your user PATH.
   If you added `modelquad usage-hook claude --segment` or `--quiet` to your
   own status line, remove it there.
7. `pip uninstall modelquad`.

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
- **Model training.** Each provider's terms forbid using its output to train
  models that compete with it, and ModelQuad passes text in both directions.
  Turn off model training in your ChatGPT data controls and in your Claude
  privacy settings while you use ModelQuad, and do not use its answers or
  history as training data. Opting out does not cover feedback you submit or
  conversations flagged for safety review.

Details: [PRIVACY.md](PRIVACY.md), [NETWORK.md](NETWORK.md). For settings that
keep ModelQuad away from sensitive work (a repository it must not touch, secret
files, employer code), see
[docs/RECOMMENDED-SETTINGS.md](docs/RECOMMENDED-SETTINGS.md).

## ModelQuad and OpenAI's Codex plugin

OpenAI publishes its own Codex plugin for Claude Code
([openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc)).
ModelQuad works alongside it, and you can use both. The plugin lets Codex
write code and offers its own reviews. ModelQuad adds:

- both quotas in the Claude Code status line;
- a note when Claude is near its limit: wait for the reset, or ask Codex for
  a review;
- read-only Codex reviews, with an approval prompt on every call;
- optional background jobs that keep running after you close the chat.

ModelQuad is an independent project. It is not affiliated with OpenAI or
Anthropic.

## Not affiliated

ModelQuad is an independent project. It is not affiliated with, endorsed by
or sponsored by Anthropic or OpenAI. "Claude" and "Claude Code" are
trademarks of Anthropic. "OpenAI" and "Codex" are trademarks of OpenAI.

The code is open source; the ModelQuad name and logo are not part of the
licence. A changed version needs a name of its own. See
[TRADEMARK.md](TRADEMARK.md).

<!-- disclaimer: owner review 2026-11-03; re-read provider terms before each release -->

## Provider terms and your account

- ModelQuad runs Anthropic's Claude Code and OpenAI's Codex CLI under your own
  accounts. Your use of each is governed by that provider's terms and usage
  policies, and you are responsible for following them.
- ModelQuad does not change any usage limit. Each provider's limits apply as
  they are, and a note that Claude is near its limit starts nothing.
- Every Codex call runs your own Codex CLI, under your own sign-in, after you
  approve it. Do not run ModelQuad unattended, in CI or on behalf of other
  people. OpenAI recommends API-key sign-in for programmatic Codex workflows
  such as CI/CD.
- Anthropic and OpenAI can limit, suspend or close accounts, and can change
  their terms at any time. That risk is yours. If a provider tells you that a
  use is not allowed, stop it, and please tell us in an issue.
- Text you send through Codex goes to OpenAI, and Codex's answer goes to
  Anthropic as part of your conversation. If the code belongs to an employer
  or a client, check that you may send it to both.
- Each provider's terms forbid using its output to train models that compete
  with it. Turn off model training with both while you use ModelQuad, and do
  not use ModelQuad's answers or history as training data.
- Codex's answers can be wrong, and code in them may be subject to third-party
  licences. Check them before you rely on them.

## No warranty

ModelQuad is free software, provided "as is", without warranties or
conditions of any kind, under the Apache License 2.0; see sections 7 and 8 of
[LICENSE](LICENSE). To the extent the law allows, its author is not liable for
any damage that comes from using it, including lost work, code or data sent to
a provider, a wrong answer from Codex, or action a provider takes on your
account. This does not limit rights that the law where you live gives you and
that cannot be waived.

## How it was made

ModelQuad was developed with substantial help from AI coding agents. The
maintainer directed, reviewed and tested the work and is responsible for it.

## Documentation

- [PRIVACY.md](PRIVACY.md): what ModelQuad reads, sends and stores, and how to
  delete it.
- [docs/RECOMMENDED-SETTINGS.md](docs/RECOMMENDED-SETTINGS.md): recommended
  Claude Code settings, in plain language.
- [docs/REPO-POLICY.md](docs/REPO-POLICY.md): the `.modelquad.toml` file a
  repository can use to narrow what ModelQuad does with Codex, and your own
  setting for one repository.
- [NETWORK.md](NETWORK.md): which connections ModelQuad and the programs it
  starts make.
- [docs/PLUGIN.md](docs/PLUGIN.md): the Claude Code plugin, how ModelQuad is
  installed.
- [SECURITY.md](SECURITY.md): how to report a vulnerability, and the security
  design.
- [CONTRIBUTING.md](CONTRIBUTING.md): how to contribute.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md): community standards.
- [AGENTS.md](AGENTS.md): build, test and safety rules for contributors.
- [THIRD_PARTY.md](THIRD_PARTY.md): the packages pip installs with ModelQuad.
- [TRADEMARK.md](TRADEMARK.md): how you may use the ModelQuad name and logo.
- [LICENSE](LICENSE): Apache License 2.0.
