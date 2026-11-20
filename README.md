<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/razbenaharon/modelquad/main/docs/images/modelquad-logo-dark.svg">
    <img src="https://raw.githubusercontent.com/razbenaharon/modelquad/main/docs/images/modelquad-logo-color.svg" alt="ModelQuad" width="320">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/razbenaharon/modelquad/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/platform-Windows-0078D6" alt="Platform: Windows">
</p>

# ModelQuad

A read-only second-opinion review of only the selected project files, with
a local record of every review. On Windows.

Works with Claude Code and your own OpenAI Codex CLI. Not affiliated with
Anthropic or OpenAI.

ModelQuad is a small local MCP server and Claude Code plugin with one job:
when Claude has changed code, it can ask your own OpenAI Codex CLI for an
independent review of a chosen set of files. ModelQuad packs those files,
leaves out what you or your repository exclude, runs Codex read-only in an
empty folder, and keeps a local record of what was sent, what Codex found,
what became of each finding and what Codex reported using. Claude writes
every change; Codex advises.

It works alongside OpenAI's own Codex plugin for Claude Code, which lets
Codex work inside the repository with its tools. ModelQuad is for the other
case: a second opinion on material you chose, with a record.

Status: early, source-only release. Windows only.

## Who it is for

Developers on Windows who already use Claude Code and the OpenAI Codex CLI,
each under their own sign-in, and who want a second model to look over
Claude's work while keeping control of what that model receives: a review
before a commit, a consultation before a design change, a reading of
unfamiliar code.

## How it works

1. Claude reaches a moment where a second opinion helps: before a large,
   architectural or sensitive change, when a change that touched several
   files is done, before a commit or PR, after a bug survived one fix. You can
   also ask with `/modelquad:review`.
2. Claude names the files. ModelQuad can show the **context package** first
   (`modelquad_review_package`, or `modelquad review package` in a terminal):
   each file with its size as sent, what is left out and why, the limits, and
   a package id (in a terminal, with `--json`). Add or remove files and look again. Showing a package starts
   nothing and sends nothing.
3. Claude Code asks you before the call, unless you turned on the standing
   grant for that folder (`modelquad settings auto on`).
4. ModelQuad reads only the named files, refuses a call that names a
   credential file, replaces secret-looking values, and gives the text to
   your Codex CLI. Codex runs read-only in an empty temporary folder with its
   tool features switched off. If a file changed after the package was shown,
   nothing is sent.
5. Codex's findings come back to Claude as claims to check, not as a verified
   result. Claude checks each one against the code, records its verdict and
   the evidence, and only then changes anything. A review that failed or was
   not done is reported to you, never claimed.
6. If Codex says it needed a file it was not given, the reply says so.
   Nothing is added by itself: you see the larger package and agree, for at
   most two more rounds. A request that was not met is a stated limit of the
   review.

## What you get

- **A package you can see.** Only the files named in the request, as a
  manifest: path, bytes as sent, a digest prefix, and what was left out with
  the reason (a Claude Code Read deny rule, Git ignore, `.modelquadignore`, a
  credential file, a size limit). At most 32 files and 128 KiB by default; a
  repository can lower both.
  The manifest covers what ModelQuad sends. Codex adds its own instructions,
  its tool declarations and your global Codex `AGENTS.md` to the request; see
  [SECURITY.md](https://github.com/razbenaharon/modelquad/blob/main/SECURITY.md).
- **A bound between what you saw and what is sent.** The package id covers
  the task text and every file's content. A run given that id sends that
  package or nothing.
- **A restricted Codex run.** Read-only sandbox, an empty working folder,
  your Codex configuration ignored, agents and tool features off, and the
  answer withheld if Codex's event stream shows a tool call. What this does
  and does not establish is in
  [SECURITY.md](https://github.com/razbenaharon/modelquad/blob/main/SECURITY.md).
- **Consent on every call**, or a standing grant you turn on for a repository
  or for every folder, and one switch that turns everything off (`modelquad off`).
- **A local record** (`modelquad review report`): each call, its package, the
  findings, which were confirmed and on what evidence (a test or a
  reproduction counts; Claude's own reading is recorded as that and is not
  counted as proof), the time, and the token counts Codex itself reported.
  `modelquad review usage-report` prints counts only, for you to read and
  pass on yourself if you choose; ModelQuad does not send it
  ([PRIVACY.md](https://github.com/razbenaharon/modelquad/blob/main/PRIVACY.md#the-usage-report)).
- **Repository rules.** A checked-in `.modelquad.toml` can turn Codex off for
  a repository or narrow what may be sent; it can only narrow.

## What it is not

- Not a general agent orchestrator. One flow, one backend (the Codex CLI).
- Not a way to change any usage limit. Each provider's limits apply as they
  are.
- Not a file chooser. ModelQuad sends what Claude names and you approve; it
  does not pick files, rank them or search the repository, and Git is not
  asked which files changed.
- Not a cost estimator. Sizes are bytes. Token counts are the ones Codex
  reports after a run; ModelQuad does not estimate tokens or a price, and a
  run under a ChatGPT plan has no per-call price to show.
- Not a proof that Codex has no tools. Codex has no mode without tools.
  ModelQuad switches off what Codex lets it switch off and checks what Codex
  reports; SECURITY.md lists what remains.

## When Claude asks Codex

By default Claude decides on its own to ask at the moments above, and Claude
Code asks you before each call. Three settings change that:

- `modelquad settings proactive off`: Claude offers a review in one sentence
  and calls only when you agree.
- `modelquad settings auto on` (this repository) or `--global` (every
  folder): the standing grant. Claude may ask Codex without Claude Code's
  prompt for each call. ModelQuad shows what that means first. With the grant
  on, two gates act, each at most once per change: the first edit of a
  sensitive file (or the edit that makes a change three files) waits until
  Codex is consulted, and a multi-file or sensitive change is not handed over
  before one Codex review is asked for. A review that fails is reported and
  does not stop the change again.
- `modelquad settings repo off`: no Codex in this repository, whatever the
  other settings say. `modelquad off` does the same everywhere.

`/modelquad:settings` shows and changes these inside Claude Code.

## Requirements

- Windows, native. WSL and other systems are not supported, and install
  refuses to run there.
- Python 3.11 or newer. CI tests 3.11 and 3.12; 3.13 and 3.14 runs are
  advisory.
- Git for Windows, to clone and update this repository.
- Claude Code, installed and signed in.
- The OpenAI Codex CLI, version 0.160.0 or newer, installed and signed in. An
  npm install works: ModelQuad runs the native `codex.exe` that the npm
  package ships, not its `codex.cmd` launcher.
- If `OPENAI_API_KEY` or `CODEX_API_KEY` is set in your environment, Codex may
  use that key instead of your ChatGPT sign-in, and OpenAI then bills the
  key's API account per use. ModelQuad passes both variables on to Codex,
  because they are a way to sign Codex in; unset them if you want only your
  ChatGPT plan used.

## Install

ModelQuad installs as a Claude Code plugin. From PowerShell:

    git clone https://github.com/razbenaharon/modelquad
    cd modelquad
    pip install .
    claude plugin marketplace add .
    claude plugin install modelquad@modelquad
    modelquad doctor

- The plugin installs from a clone of this repository. The same package is
  on PyPI as `modelquad` (from 0.4.0); the steps above install it from the
  clone, so the package and the plugin are the same version.
- `pip install` puts the `modelquad` and `modelquad-mcp` commands on PATH. The
  plugin has no programs of its own and runs these two.
- `claude plugin marketplace add .` adds this checkout as a local plugin
  marketplace, and `claude plugin install modelquad@modelquad` installs the
  plugin from it. The plugin carries the server, three commands
  (`/modelquad:review`, `/modelquad:setup` and `/modelquad:settings`), the
  guide skill Claude reads (`/modelquad:guide`), and the review reminder with
  its two gates.
- `modelquad doctor` checks five things and says what to do about each:
  Windows, the Codex CLI (found, new enough, and its own listing confirms the
  switched-off tools), the connection to Claude Code, your settings, and the
  current folder's rules. It runs Codex's `--version` and `features list`,
  which contact no provider.
- The first Codex call shows, once, what ModelQuad sends where, instead of
  starting. Ask again to go ahead.

If you used an earlier build with `modelquad install claude`, run
`modelquad install claude --plugin` once: it takes back the server entry and
commands that install added, so Claude Code does not run them twice, and
removes the quota status line and the session-start note earlier builds
wrote.

See [docs/PLUGIN.md](https://github.com/razbenaharon/modelquad/blob/main/docs/PLUGIN.md) for what the plugin contains.

To upgrade, close every Claude Code window first (in a terminal session, type
`/exit`), then run `git pull` and the same `pip install` again, and refresh the
plugin with `claude plugin marketplace update modelquad`, then
`claude plugin uninstall modelquad@modelquad` and
`claude plugin install modelquad@modelquad` (Claude Code keeps a copy of the
plugin per version number, so an update alone can keep old files when the
version did not change). A running Claude Code keeps `modelquad-mcp.exe`
open, and Windows does not let pip replace a running program: the upgrade
then fails halfway and leaves ModelQuad uninstalled until you run it again
with Claude Code closed.

A plain install keeps its state in `%USERPROFILE%\.local\share\modelquad`,
outside `AppData`: Windows gives the Claude desktop app's programs a private
copy of `AppData`, so a folder there would split in two.
An editable install (`pip install -e`) is treated as a development checkout
and keeps its state in `%USERPROFILE%\.modelquad` instead. `modelquad privacy`
shows which folder is in use.

## Quick start

1. Sign in to Claude Code and to the Codex CLI.
2. Install as above, restart Claude Code, and run `modelquad doctor`.
3. In Claude Code, ask for it and name the files, for example: "Ask Codex to
   review src/server.py and src/auth.py." To see the package first: "Show me
   what Codex would get."
4. Later, `modelquad review report` shows what the reviews found and used.

### Without the plugin

`modelquad install claude` writes the server entry and the commands into
Claude Code's user configuration itself. It prints what ModelQuad sends where
before it writes anything; add `--dry-run` to see the changes first. This
install has no hooks, so there is no review reminder and no gate: Claude asks
Codex when its instructions say to, or when you ask. Without the plugin the
commands are named `/modelquad-review`, `/modelquad-setup` and
`/modelquad-settings`, and the guide skill `/modelquad`.
To move from this install to the plugin, install the plugin and run
`modelquad install claude --plugin`; `modelquad install claude --no-plugin`
goes back.

## What it changes on your machine

The plugin changes nothing outside Claude Code's own plugin folder.

`modelquad install claude` changes Claude Code's user configuration. It reads
every target file first, then writes each changed file once. A file that is a
link, is not UTF-8 text, cannot be read or is not valid JSON is skipped with
a reason; the other files still change.

- `%USERPROFILE%\.claude.json`: an MCP server entry named `modelquad`, with
  `"timeout": 900000` (15 minutes) for ModelQuad's tool calls only. If a
  `modelquad` entry already existed, ModelQuad records it in the state folder
  so uninstall can put it back.
- `%USERPROFILE%\.claude\skills\modelquad\SKILL.md`, and `modelquad-review`,
  `modelquad-setup` and `modelquad-settings` beside it: the guide skill and
  the commands only you start.
- Backups: before it changes an existing file, ModelQuad saves a full copy of
  the original bytes beside it, named `<file>.modelquad-backup-<time>`. JSON
  files are written back with your key order.

Install, with or without `--plugin`, and uninstall also take back what
earlier builds added and this one no longer has, each with a backup first and
a line in the report: ModelQuad's own quota status line, its `SessionStart`
and `PreToolUse` hooks, the commands for handoff, quota and division of work,
the block between `<!-- >>> modelquad managed block >>> -->` markers in
`CLAUDE.md`, and an `MCP_TOOL_TIMEOUT` that ModelQuad recorded as its own and
you did not change. A value or an entry you changed yourself stays.

If `CLAUDE_CONFIG_DIR` is set, Claude Code reads its settings from that
folder, but ModelQuad still writes to the places above. Install warns about
this; ModelQuad does not follow the variable.

While it runs, ModelQuad writes only to its state folder (see Install):
`preferences.json` (your settings), the review ledger, small records of the
review reminder per session and of a running Codex call, an installation id,
and the record of any registration that install replaced. That record can
include `env` values from your old entry, such as keys.
[PRIVACY.md](https://github.com/razbenaharon/modelquad/blob/main/PRIVACY.md)
lists each file.

Earlier builds also kept a history database, stored answers, logs and quota
files. This build does not read, write or delete them. `modelquad privacy`
lists them so you can delete them.

ModelQuad does not read or change Codex's `config.toml`: Codex is started with
its user configuration ignored.

## How to undo it

1. If you installed the plugin, `claude plugin uninstall modelquad@modelquad`
   removes it and `claude plugin marketplace remove modelquad` removes the
   local marketplace.
2. `modelquad uninstall claude` removes the MCP entry (with its timeout) and
   the skills, and anything an earlier version added that install had not yet
   taken back. A `modelquad` entry that is not ModelQuad's own is left in
   place. Uninstall puts back a `modelquad` entry that install replaced. Use
   `--dry-run` to preview.
3. Delete the `*.modelquad-backup-*` files, which install left beside each
   file it changed. Check them first: they can contain secrets you put in
   your configuration.
4. Delete the state folder (`modelquad privacy` shows it).
5. `pip uninstall modelquad`.

## Automated runs

ModelQuad can run without a person watching, under controls you set. What
stops it by default, and what you set to allow it:

- **Permission, explicitly.** Without the standing grant, the one tool that
  starts Codex is marked as needing a person's approval, and Claude Code
  asks for it, so a script or a headless `claude -p` run cannot start Codex
  (checked: the call is denied). To allow it, turn the grant on for the one
  folder the automation works in (`modelquad settings auto on --project DIR`,
  not `--global`) and allow only `mcp__plugin_modelquad_modelquad__modelquad_delegate`
  in that run (`--allowedTools`). A `.modelquad.toml` in the repository can
  narrow it further: task kinds, the number and size of files.
- **One defined task per run.** Give each run one task and name its files;
  a package id binds a call to exactly the files that were shown.
- **Limits on time and calls.** Each Codex call stops at its `timeout`
  (840 seconds at most). ModelQuad does not count or cap the number of
  calls: limit the run itself, with Claude Code's `--max-turns` and your
  scheduler's own time limit.
- **A record.** Each call is recorded when Codex starts, and again when it
  ends (`modelquad review report`; `modelquad review usage-report` for
  counts).
- **A way to stop it.** `modelquad off` stops every new Codex start on this
  computer; `modelquad settings auto off --project DIR` takes the grant back;
  ending the Claude Code process ends the Codex run it started.

What was checked and what was not: headless Claude Code 2.1.318 (`claude -p`)
in a granted folder with the tool allowed started Codex, and without the
grant it was denied, also in bypass-permissions mode (2026-11-18). Not
tested: CI runners, Windows Task Scheduler or other schedulers, Codex
signed in with an API key, approvals given by a program (the Agent SDK),
and many automated sessions at once beyond the rule that the same package
runs once.

Your sign-ins stay yours in an automated run too: the account terms
in [Provider terms and your account](#provider-terms-and-your-account)
apply, and OpenAI recommends API-key sign-in for automation such as CI.

## Privacy and network

- ModelQuad has no server of its own, no update check and no telemetry. Its
  own processes connect only to loopback addresses; an audit hook refuses a
  socket connect or send through Python's socket module to any other address
  (asyncio Proactor connects and host name lookups are outside it; ModelQuad's
  own code makes neither, see [NETWORK.md](https://github.com/razbenaharon/modelquad/blob/main/NETWORK.md)).
- For a Codex call, ModelQuad reads the files the request names, inside the
  working folder only. It refuses a call that names a credential file such as
  `.env` or `auth.json` (the check is by file name and cannot be complete),
  replaces secret-looking values with `<credential>`, and refuses, with a
  reason, text that holds a private key or a recognizable access token. Your
  Codex CLI then sends the task and that text to OpenAI, under OpenAI's
  terms.
- What ModelQuad returns to Claude Code becomes part of your conversation,
  which Claude Code sends to Anthropic, or to the cloud provider you set
  Claude Code up to use, under that provider's terms: Codex's
  answer, the package manifest (file names and sizes) and the token counts
  Codex reported.
- The review ledger stays on your computer until you delete it. It holds no
  task text, no file contents and no full answer: file names, sizes and
  digest prefixes of what was sent, each finding's location and a short
  redacted claim, verdicts, times and token counts.
- **Model training.** OpenAI's terms forbid using its output to develop
  competing models, and Anthropic's forbid using Claude to train AI models or
  build competing products. ModelQuad passes text in both directions.
  Turn off model training in your ChatGPT data controls and in your Claude
  privacy settings while you use ModelQuad. If Codex is signed in with an
  API key, your API organization's retention and data-sharing settings
  apply instead; in a ChatGPT workspace, the workspace's settings apply.
  Do not use ModelQuad's answers or its record as training data. Opting out does not cover feedback you submit
  or conversations flagged for safety review.

Details: [PRIVACY.md](https://github.com/razbenaharon/modelquad/blob/main/PRIVACY.md), [NETWORK.md](https://github.com/razbenaharon/modelquad/blob/main/NETWORK.md). For settings that
keep ModelQuad away from sensitive work (a repository it must not touch, secret
files, employer code), see
[docs/RECOMMENDED-SETTINGS.md](https://github.com/razbenaharon/modelquad/blob/main/docs/RECOMMENDED-SETTINGS.md).

## ModelQuad and OpenAI's Codex plugin

OpenAI publishes its own Codex plugin for Claude Code
([openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc)).
ModelQuad works alongside it, and you can use both. The plugin lets Codex
write code and offers its own reviews, with Codex working inside the
repository. ModelQuad does less, on purpose:

- Codex receives only the files Claude names, as a package you can see
  before it is sent, and cannot search the repository or run anything
  through the tools ModelQuad switches off;
- an approval prompt on every call unless you grant it once for a
  repository or for every folder, a per-repository off, and repository rules that can only
  narrow;
- a record of each review: its package, its findings, Claude's verdicts and
  their evidence, and what Codex reported using.

If you want Codex to explore a repository freely or to write code, use
OpenAI's plugin. ModelQuad has not measured whether a review of a closed
package finds as much as a review with repository access; that comparison is
planned, and until it exists this README makes no claim about it.

ModelQuad is an independent project. It is not affiliated with OpenAI or
Anthropic.

## Not affiliated

ModelQuad is an independent project. It is not affiliated with, endorsed by
or sponsored by Anthropic or OpenAI. "Claude" and "Claude Code" are
trademarks of Anthropic. "OpenAI", "ChatGPT" and "Codex" are trademarks of OpenAI.

The code is open source; the ModelQuad name and logo are not part of the
licence. A changed version needs a name of its own. See
[TRADEMARK.md](https://github.com/razbenaharon/modelquad/blob/main/TRADEMARK.md).

<!-- disclaimer: owner review 2026-11-03; provider terms re-read 2026-11-18; re-read before each release -->

## Provider terms and your account

This section describes the tool. It is not legal advice, and neither provider
has approved ModelQuad.

- ModelQuad runs Anthropic's Claude Code and OpenAI's Codex CLI under your own
  accounts. Your use of each is governed by that provider's terms and usage
  policies, and you are responsible for following them.
- ModelQuad does not change any usage limit. Each provider's limits apply as
  they are.
- Every Codex call runs your own Codex CLI, under your own sign-in, after you
  approve it or within a standing grant you turned on. Automated runs are
  possible with the grant; see [Automated runs](#automated-runs) for how to
  keep them under control and what has not been tested.
- OpenAI's and Anthropic's terms forbid sharing your login or making your
  account available to anyone else. Do not let other people, or a service
  you run for them, use ModelQuad through your sign-ins. OpenAI's terms also
  forbid automatically or programmatically extracting data or output; whether
  an automated ModelQuad run falls under that term is not settled. For
  automation such as CI, OpenAI recommends API-key sign-in, and says not to
  use a ChatGPT sign-in for public or open-source repositories.
- Anthropic and OpenAI can limit, suspend or close accounts, and can change
  their terms at any time. That risk is yours. If a provider tells you that a
  use is not allowed, stop it, and please tell us in an issue.
- Text you send through Codex goes to OpenAI, and Codex's answer goes to
  Anthropic, or to the cloud provider you set Claude Code up to use, as part
  of your conversation. If the code belongs to an employer
  or a client, check that you may send it to both.
- OpenAI's terms forbid using its output to develop competing models, and
  Anthropic's forbid using Claude to train AI models or build competing
  products. Turn off model training with both while you use ModelQuad, and do
  not use ModelQuad's answers or its record as training data.
- Codex's answers can be wrong, and code in them may be subject to third-party
  licences. Check them before you rely on them.

## No warranty

ModelQuad is free software, provided "as is", without warranties or
conditions of any kind, under the Apache License 2.0; see sections 7 and 8 of
[LICENSE](https://github.com/razbenaharon/modelquad/blob/main/LICENSE). To the extent the law allows, its author is not liable for
any damage that comes from using it, including lost work, code or data sent to
a provider, a wrong answer from Codex, or action a provider takes on your
account. This does not limit rights that the law where you live gives you and
that cannot be waived.

## How it was made

ModelQuad was developed with substantial help from AI coding agents. The
maintainer directed, reviewed and tested the work and is responsible for it.

## Documentation

- [PRIVACY.md](https://github.com/razbenaharon/modelquad/blob/main/PRIVACY.md): what ModelQuad reads, sends and stores, and how to
  delete it.
- [docs/RECOMMENDED-SETTINGS.md](https://github.com/razbenaharon/modelquad/blob/main/docs/RECOMMENDED-SETTINGS.md): recommended
  Claude Code settings, in plain language.
- [docs/REPO-POLICY.md](https://github.com/razbenaharon/modelquad/blob/main/docs/REPO-POLICY.md): the `.modelquad.toml` file a
  repository can use to narrow what ModelQuad does with Codex, and your own
  setting for one repository.
- [NETWORK.md](https://github.com/razbenaharon/modelquad/blob/main/NETWORK.md): which connections ModelQuad and the programs it
  starts make.
- [docs/PLUGIN.md](https://github.com/razbenaharon/modelquad/blob/main/docs/PLUGIN.md): the Claude Code plugin, how ModelQuad is
  installed.
- [SECURITY.md](https://github.com/razbenaharon/modelquad/blob/main/SECURITY.md): how to report a vulnerability, and the security
  design.
- [CONTRIBUTING.md](https://github.com/razbenaharon/modelquad/blob/main/CONTRIBUTING.md): how to contribute.
- [CODE_OF_CONDUCT.md](https://github.com/razbenaharon/modelquad/blob/main/CODE_OF_CONDUCT.md): community standards.
- [AGENTS.md](https://github.com/razbenaharon/modelquad/blob/main/AGENTS.md): build, test and safety rules for contributors.
- [THIRD_PARTY.md](https://github.com/razbenaharon/modelquad/blob/main/THIRD_PARTY.md): the packages pip installs with ModelQuad.
- [TRADEMARK.md](https://github.com/razbenaharon/modelquad/blob/main/TRADEMARK.md): how you may use the ModelQuad name and logo.
- [LICENSE](https://github.com/razbenaharon/modelquad/blob/main/LICENSE): Apache License 2.0.
