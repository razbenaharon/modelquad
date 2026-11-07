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
  marking of the tools that start Codex.
- The CLI (`modelquad`), including the commands that write Claude Code's
  configuration (`install`, `uninstall`, `onboard`, `repair`, `integrations`)
  and the hook commands Claude Code runs.
- The optional loopback service (`modelquad serve`, `modelquad service ...`):
  its Host and Origin checks, the installation secret and token check, the
  listener proof, and the Task Scheduler task it registers.
- How ModelQuad launches the Codex CLI: executable resolution, the child
  environment, the sandbox and feature flags it passes, the event-stream
  check, and the worker guard.
- What ModelQuad reads and sends to Codex: the file limits, the list of
  skipped credential files, and the secret screening.
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
any other `socket.connect` or `socket.sendto` (`src/modelquad/netpolicy.py`,
`tests/test_netpolicy.py`). The hook does not cover the programs ModelQuad
starts. Provider traffic is made by the provider CLIs. See
[NETWORK.md](NETWORK.md).

**MCP server.** Claude Code starts it as a child process and talks to it over
standard input and output. It does not listen on a port. The two tools that
start Codex, `modelquad_delegate` and `modelquad_handoff`, are marked
`anthropic/requiresUserInteraction`, so Claude Code asks the user before every
call (`tests/test_oss_export_surface.py`). Two more tools can start Codex, and
both belong to a handoff window that is built but switched off in this
version (`handoff_widget.ENABLED`): `modelquad_handoff_confirm` is the
window's Approve / Decline button, marked app-only so a host keeps it from
the model, and `modelquad_handoff_wait` starts Codex only after that button
was pressed. It refuses in a client that renders no window
(`tests/test_handoff_widget.py`).
**The standing grant.** A user who wants Claude to consult Codex without a
prompt for each call turns a standing grant on, for one repository or for
every folder (`modelquad settings auto on --yes`, after a notice;
`src/modelquad/grant.py`). A Claude Code session that starts in a granted
folder then registers `modelquad_delegate` without
`anthropic/requiresUserInteraction`, so Claude Code's own permissions apply:
the user's allow rule, or "don't ask again" on the first prompt, lets the
calls run. Every call re-checks the grant for its own folder and refuses
when it is missing or was turned off; a repository set to Codex off, or a
`.modelquad.toml` that turns Codex off, outranks it. No MCP tool can turn a
grant on (`tests/test_standing_grant.py`). Codex stays read-only and
tool-less either way.

ModelQuad adds no permission rule for its tools. The job tools answer only
for jobs started from the project folder they are given. A folder argument
that is not a local folder (a network share such as `\\host\share`, a device
path, or a link to one) is refused before anything opens it, so no tool makes
Windows connect to another machine (`src/modelquad/localpath.py`).

**Loopback service (optional).** `src/modelquad/service/`.

- It binds to `127.0.0.1` only (port 8765 by default) and takes no bind
  address.
- It answers only requests whose `Host` is a loopback name (`127.0.0.1`,
  `localhost`, `::1`); anything else gets `421`. This defeats DNS rebinding.
- A request whose `Origin` is not the service's own origin (the same host and
  port), or that says `Sec-Fetch-Site: cross-site`, gets `403`. There is no
  CORS exception.
- Every request except `POST /api/listener-proof`, reads included, needs the
  installation secret in the `X-ModelQuad-Token` header. It is compared in
  constant time. The browser checks run first, so the secret never admits a
  cross-site page. A service started without a verified secret refuses every
  request except the proof (`service/trust.py`, `tests/test_service_trust.py`).
- FastAPI's documentation and schema pages are turned off.
- The endpoints are the listener proof, health, the background-work setting, the
  summary of waiting answers, and the job launch, diagnostics, fate and
  release-fate endpoints. The launch endpoint refuses every browser request, even
  one with the secret, and starts only a job ModelQuad has already prepared in
  the state folder.
- Turning background work on through the API needs the consent that
  `modelquad settings job-runner on` records after you type `yes` (or pass
  `--yes`). Without it the service refuses with `409`.
- The secret lives in `service-secret` in the state folder. On Windows the
  file is created new, with access for only the current user and SYSTEM, and
  is checked from its open handle on every start. A file that fails the check
  is not repaired; the service then refuses every request.
- ModelQuad's own clients send the secret through one transport
  (`service/listener.py`). On Windows it first checks that the listening
  socket belongs to a process of the current user, started from this
  installation, and verifies a proof derived from the secret.
- `modelquad service uninstall` deletes the scheduled task only after the same
  ownership check that install applies.

What this protects: other Windows accounts, web pages and network peers cannot
read from or change ModelQuad through the service.

What it does not protect:

- Any process running as you can read the secret, write the database or run
  the CLI. ModelQuad does not defend against hostile software running under
  your own account.
- An administrator can read any file.
- The state folder inherits the access rights of your user profile. The
  secret, the job launch files, the waiting answers and the registration
  backup are limited to you and SYSTEM.

**Codex worker launch.** `src/modelquad/execution/`.

- Only review, exploration and architecture tasks are accepted. Other task
  types, and resuming a Codex session, are refused before Codex starts.
- ModelQuad reads the files the request names itself, inside the working
  folder and within fixed limits. It skips credential files, replaces
  secret-looking values, and refuses text that holds a private key or a
  recognizable access token (`execution/noshell.py`,
  `tests/test_noshell_workers.py`). See [PRIVACY.md](PRIVACY.md).
- Codex runs `exec` in a new empty temporary folder with
  `--sandbox read-only`, `--ignore-user-config`, `--ignore-rules` and
  `--ask-for-approval never`, with web search off, and with its shell,
  unified exec, hooks, code mode, apps, plugins, browser and computer-use
  features disabled. ModelQuad first reads Codex's own feature listing, and
  refuses a Codex older than 0.160.0 or one whose listing does not confirm
  that these features are off.
- ModelQuad checks Codex's event stream and withholds the answer if it shows a
  command, a file change, an MCP tool call or a collaboration call, or if the
  worker left processes running.
- The sandbox and the feature switches are Codex's code, not ModelQuad's.
  ModelQuad relies on them and checks the event stream; it cannot prove more
  than that.
- Only native executables receive a prompt: an npm `codex.cmd` launcher is
  replaced by the `codex.exe` its package ships. Executables are resolved
  without the current folder and without unsafe PATH entries. The child
  environment drops other services' credentials and sets
  `NoDefaultCurrentDirectoryInExePath=1`.
- The worker gets `MODELQUAD_WORKER=1`. With it set, ModelQuad's MCP tools
  refuse, so a worker cannot start another worker
  (`tests/test_mcp_worker_guard.py`).
- On Windows a worker started inside a Claude Code session runs in a Job
  Object that stops it when the MCP server process ends. A background job is
  started by the local service instead and keeps running.
- At most one active attempt per request is allowed. A unique index in the
  database enforces it.

**Hooks ModelQuad installs.** They never approve a Claude Code tool call, and
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

**Which Codex runs.** ModelQuad runs the Codex it finds for its version and
its quota without a prompt, so a `codex.exe` anywhere under the working
folder, which is the project Claude Code was opened in, is never used, even
when a folder there is on PATH (an activated `.venv`, for example). A working
folder that is your home folder or above it is the exception, so that your
own install under AppData is still found.

**PYTHONPATH.** Python reads `PYTHONPATH` before the installed packages. A
relative entry (`.`, `src`, or an empty one) is read against the folder a
process starts in, which for ModelQuad is your project, so a project that
carries a `modelquad` folder could replace ModelQuad's own code. Keep
`PYTHONPATH` absolute or unset; `modelquad doctor` warns about a relative
entry.

**Local data.** History, retained answers, the review ledger and logs stay on
the machine. See
[PRIVACY.md](PRIVACY.md).

**Releases.** ModelQuad is distributed as source only. There are no signed
binaries.
