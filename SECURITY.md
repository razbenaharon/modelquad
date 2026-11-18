# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub's private
vulnerability reporting: open this repository's **Security** tab and choose
**Report a vulnerability**.

Do not open a public issue, pull request or discussion for a vulnerability.
There is no security e-mail address.

Please include:

- what an attacker can do, and what they need first (for example another
  Windows account, a web page you visit, or a process running as you);
- the ModelQuad version (`modelquad --version`) and your Windows version;
- steps to reproduce, or a proof of concept;
- any logs, with private details removed.

This is a small project maintained in spare time. Reports are read and
answered as soon as practical. Please give a reasonable time to fix the
problem before you disclose it.

## Supported versions

Only the latest release, and the `main` branch, receive security fixes.

## In scope

- The MCP server (`modelquad-mcp`) and its tools, including the approval
  marking of the tool that starts Codex.
- The CLI (`modelquad`), including the commands that write Claude Code's
  configuration (`install`, `uninstall`), the global switch (`on`, `off`) and
  the hook command Claude Code runs (`hook-review`).
- How ModelQuad launches the Codex CLI: executable resolution, the child
  environment, the sandbox and feature flags it passes, the event-stream
  check, and the worker guard.
- What ModelQuad reads and sends to Codex: the file limits, the list of
  skipped credential files, the secret screening, and the package and its id.
- The loopback rule for ModelQuad's own processes (`netpolicy.py`).
- Files ModelQuad writes on the machine: Claude Code configuration, backups,
  the state folder and its contents.

## Out of scope

- The provider CLIs themselves (Claude Code, the Codex CLI) and their
  sandboxes. Report those to Anthropic or OpenAI.
- Provider accounts, logins, billing and quotas.
- Attacks that need administrator rights on the machine.
- Hostile software already running under your own Windows account (see below).

## Security design today

This section describes what the code does now, not a promise about the future.

**Local only.** ModelQuad has no server of its own. Its own processes connect
only to loopback addresses: each entry point adds an audit hook that refuses
any other `socket.connect` or `socket.sendto` made through Python's socket
module (`src/modelquad/netpolicy.py`, `tests/test_netpolicy.py`). It does not
intercept connects made by Windows asyncio's Proactor event loop, or host
name lookups; ModelQuad's own code makes neither. The hook does not cover the
programs ModelQuad starts. Provider traffic is made by the provider CLIs. See
[NETWORK.md](NETWORK.md).

**MCP server.** Claude Code starts it as a child process and talks to it over
standard input and output. It does not listen on a port, and ModelQuad has no
local service. It has four tools. `modelquad_delegate` is the only one that
starts Codex. Without a standing grant it is marked
`anthropic/requiresUserInteraction`, so Claude Code asks the user before every
call (`tests/test_oss_export_surface.py`). `modelquad_review_package` and
`modelquad_status` only read, and `modelquad_review_verdict` writes only the
local review ledger; none of the three starts Codex or sends anything.

**The standing grant.** A user who wants Claude to consult Codex without a
prompt for each call turns a standing grant on, for one repository or for
every folder (`modelquad settings auto on --yes`, after a notice;
`src/modelquad/grant.py`). A Claude Code session that starts in a granted
folder then registers `modelquad_delegate` without
`anthropic/requiresUserInteraction`, so Claude Code's own permissions apply:
the user's allow rule, or "don't ask again" on the first prompt, lets the
calls run. Every call re-checks the grant for its own folder and refuses
when it is missing or was turned off, and a call without the prompt must stay
inside the folder the session started in; a repository set to Codex off, or a
`.modelquad.toml` that turns Codex off, outranks it. No MCP tool can turn a
grant on (`tests/test_standing_grant.py`). Codex stays read-only, with the
same tool features switched off, either way.

ModelQuad adds no permission rule for its tools. A folder argument
that is not a local folder (a network share such as `\\host\share`, a device
path, or a link to one) is refused before anything opens it, so no tool makes
Windows connect to another machine (`src/modelquad/localpath.py`).

**The global switch.** `modelquad off` stores `codex_enabled: false` in
`preferences.json` (`src/modelquad/switch.py`). Every Codex call and every
package preview is then refused before any project file is opened, and the
plugin's hooks do nothing. A `preferences.json` that cannot be read counts as
off, not on. No MCP tool can turn the switch on, and Claude is told not to run
`modelquad on` itself. Until the user sets the switch, an earlier build's
"disconnected" or "paused" setting, read once from its old database, still
counts as off, so an upgrade never turns Codex back on by itself.

**The package and its id.** Before a run, ModelQuad packs the named files
with the same rules, screening and limits the run uses, and returns a
manifest: relative paths, sizes as sent, digest prefixes, what was left out
and why, the limits, and a package id (`src/modelquad/review.py`). The id is
a SHA-256 digest over the task kind, the task text and each sent file's path,
size and digest. A run given a `package_id` sends that exact package or
nothing (`package_changed`). Every run also compares each file's digest at
launch with the one taken when the call started, and sends nothing if a file
changed in between. The id is a digest, not a signature: it binds what was
shown to what is sent, and does not prove who made the request.

**The manifest is not the whole model input.** The manifest lists the files
ModelQuad sends and nothing else. Codex builds the request to the model
itself and adds its own parts, which ModelQuad neither chooses nor sees:
its base instructions, the declarations of the built-in tools it still lists
(see **Codex worker launch** below), and your global Codex
instructions file (`AGENTS.md` or `AGENTS.override.md` in your Codex folder)
if you have one. On Codex 0.164.0, `codex debug prompt-input` in an empty
folder showed about 22,000 characters of base instructions, about 25,000 of
tool declarations with ModelQuad's switches applied (about 31,000 without),
and the global instructions file in full; `-c project_doc_max_bytes=0` did
not keep that file out, and `codex features list` names no switch for it.
That command reads your Codex configuration and cannot be told to ignore it,
so it is not the same path as the review run: it also showed a skills
listing of about 50,000 characters, and whether a review run carries one was
not checked. The only supported way found to run Codex without that file is a
separate Codex folder, which would need its own sign-in; ModelQuad does not
copy credentials, so it does not do this. The token counts in the reply and
the ledger are Codex's totals for the whole request, not for the package. In
our runs on 0.165.0-alpha.1.2 and 0.169.0 (2026-11-18), a package of one or
two files of a few hundred bytes was reported as 18,100 to 19,700 input
tokens: nearly all of it is what Codex adds. Those numbers are from one
computer with one global instructions file and will differ on another.

**Completion rounds.** When Codex's reply names files it needed and was not
given, ModelQuad adds nothing by itself. The reply lists the names; a new
call that names those files and the earlier reply (`follows`) is a new
review call, with the same consent as any other, and a review has at most two
such rounds. A `follows` that names no review recorded for that folder is
refused.

**The worker guard.** A process started by a Codex call must never ask
ModelQuad for Codex again (`src/modelquad/mcp/worker_guard.py`,
`tests/test_mcp_worker_guard.py`). Two layers check it on every call:

- the environment marker `MODELQUAD_WORKER=1`, which every Codex process is
  started with, refuses unconditionally;
- while a Codex call runs, its process id and start time are kept in a small
  file in `active-workers\` in the state folder, and a process that has that
  Codex process among its ancestors is refused even when the marker was
  dropped from its environment. This layer is best effort: an observation
  that fails is not a refusal.

**Codex worker launch.** `src/modelquad/execution/`.

- Only review, exploration and architecture tasks are accepted. Other task
  types, and resuming a Codex session, are refused before Codex starts.
- ModelQuad reads the files the request names itself, inside the working
  folder and within fixed limits. It refuses a call that names a credential
  file, replaces
  secret-looking values, and refuses text that holds a private key or a
  recognizable access token (`execution/noshell.py`,
  `tests/test_noshell_workers.py`). See [PRIVACY.md](PRIVACY.md).
- Codex runs `exec` in a new empty temporary folder with
  `--sandbox read-only`, `--ignore-user-config`, `--ignore-rules` and
  `--ask-for-approval never`, with web search off, with agents off
  (`agents.enabled=false`), and with its shell, unified exec, hooks, code
  mode, apps, plugins, browser and computer-use features disabled. ModelQuad
  first reads Codex's own feature listing, and refuses a Codex older than
  0.160.0, one whose listing does not confirm that these features are off,
  or one that rejects the agents setting.
- ModelQuad checks Codex's event stream and withholds the answer if it shows a
  command, a file change, an MCP tool call or a collaboration call, or if the
  worker left processes running.
- The sandbox and the feature switches are Codex's code, not ModelQuad's.
  ModelQuad relies on them and checks the event stream; it cannot prove more
  than that. Codex still lists a few built-in tools to the model that
  ModelQuad cannot remove. On Codex 0.164.0, 0.165.0-alpha.1.2 and 0.169.0
  these were a code runner and its wait (`exec`, `wait`), two requests for
  user input and a sleep. The code runner's host is disabled; every call to
  it failed in our probes of 0.164.0 and 0.165.0-alpha.1.2, and no call was
  tried on 0.169.0. Codex's event stream
  also leaves some tool calls out, so a clean stream is evidence that nothing
  it reports acted, not proof that no tool was called.
- Only native executables receive a prompt: an npm `codex.cmd` launcher is
  replaced by the `codex.exe` its package ships. Executables are resolved
  without the current folder and without unsafe PATH entries. The child
  environment drops other services' credentials and sets
  `NoDefaultCurrentDirectoryInExePath=1`.
- The worker gets `MODELQUAD_WORKER=1`. With it set, ModelQuad's MCP tools
  refuse, so a worker cannot start another worker (see the worker guard
  above).
- On Windows the worker runs in a Job Object that stops it, and every process
  it started, when the MCP server process ends or the call is cancelled.
- At most one attempt per package runs at a time in one server process: a
  second identical request while the first runs is refused
  (`duplicate_in_flight`, `src/modelquad/review.py`). Each Claude Code
  session has its own server process, so the same package asked for in two
  sessions at once runs twice, each with its own approval or grant.

**Hooks ModelQuad installs.** Only the plugin installs hooks, and they do
nothing while ModelQuad is off. They never approve a Claude Code tool call, and
they never open a conversation transcript, even when Claude Code
passes its path (`tests/test_hook_transcript_boundary.py`). All are advisory
except two gates, only in a folder with the standing grant and at most once
per change set each: the consultation gate may deny one edit (the first of a
sensitive file, or the one that makes the change three files) until Codex is
consulted, and the Stop review gate may block Claude once to ask for a Codex
review, never while Claude is already continuing for a stop hook. A
consultation or review that failed or was not done is told to the user and
never stops the change again (`tests/test_review_reminder.py`).

**Credentials.** ModelQuad does not extract or proxy provider credentials. The
Codex CLI and Claude Code use their own logins. Two kinds of files can still
hold secrets you put in your configuration:

- `host-registration-backup.json` in the state folder. When install replaces
  an existing `modelquad` MCP entry, ModelQuad keeps a verbatim copy so
  uninstall can restore it. If that entry had `env` values, such as API keys,
  the copy has them too. The file is private to you and SYSTEM, and no command
  shows its contents.
- The `*.modelquad-backup-*` copies of Claude Code's files, beside the
  originals. They are full copies, so they hold whatever the originals held.

**Files written.** Install and uninstall read every target file first and skip
links and files they cannot read or parse. Each changed file is written once,
atomically, after a full copy of its original bytes is saved beside it. The
copy is created with an exclusive open, so a planted link cannot redirect it.
A file that another program changed during the run is left as it is.
A file that could not be read, written or removed makes the command end with
`Not finished: ...` and exit code 1; a file left alone on purpose (a link, a
file that does not parse, your own entry) is a skipped row and not an error
(`tests/test_install_incomplete.py`).

**Which Codex runs.** ModelQuad runs the Codex it finds for its version and
feature checks (`codex --version`, `codex features list`) without a prompt,
so a `codex.exe` anywhere under the working
folder, which is the project Claude Code was opened in, is never used, even
when a folder there is on PATH (an activated `.venv`, for example). A working
folder that is your home folder or above it is the exception, so that your
own install under AppData is still found.

**PYTHONPATH.** Python reads `PYTHONPATH` before the installed packages. A
relative entry (`.`, `src`, or an empty one) is read against the folder a
process starts in, which for ModelQuad is your project, so a project that
carries a `modelquad` folder could replace ModelQuad's own code. Keep
`PYTHONPATH` absolute or unset.

**Local data.** The review ledger, your settings and the other files in the
state folder stay on the machine. Any process running as you can read them,
change them or run the CLI; ModelQuad does not defend against hostile
software running under your own account. An administrator can read any file.
The state folder inherits the access rights of your user profile; the
registration backup is limited to you and SYSTEM. See
[PRIVACY.md](PRIVACY.md).

**Tests.** The test suite replaces Codex with a stand-in and never starts a
real Codex. A passing suite shows how ModelQuad handles the answers and event
streams it was given; it does not show how a particular Codex release
behaves. The Codex observations above come from probes of the named
releases.

**Codex releases checked by hand.** What was run against a real Codex, with
made-up code in throwaway folders and a separate state folder:

| Codex | When | What was run |
|---|---|---|
| 0.160.0 | 2026-10-06, 2026-10-09 | Feature listing and recorded event streams; the oldest release ModelQuad accepts. Not run with the agents setting. |
| 0.164.0 | 2026-11-16 | `codex debug prompt-input` (no provider call): the tools still listed. No review was run on it. |
| 0.165.0-alpha.1.2 (the desktop app's copy) | 2026-11-16 to 2026-11-18 | Reviews through the MCP server and inside Claude Code 2.1.318: package preview, first-use notice, review, verdicts, completion rounds and their limit, a changed package, withheld and secret files, cancel, timeout, a closed or killed session, a Codex killed from outside, an unknown model, a locked ledger, both review gates. |
| 0.169.0 (npm, in a separate folder) | 2026-11-18 | Feature listing, `codex debug prompt-input`, and three runs through the MCP server: a review, a file that tries to give Codex instructions, and a cancel. |

A release newer than these is admitted when its own feature listing confirms
the switches, and is otherwise unchecked. When several Codex installs are on
one computer ModelQuad runs the newest (`codexcli.py`), which can differ from
the `codex` on PATH; `modelquad doctor` names the one it runs.

**Releases.** ModelQuad is distributed as source only. There are no signed
binaries.
