# Privacy

ModelQuad runs on your computer. It has no server of its own, no account
system, no upload and no telemetry. This document says what it reads, what it
sends where and through which program, what it stores, for how long, and how
to look at it and delete it. It is the complete inventory: every file,
folder and database table ModelQuad writes is listed here.

Everything here describes the code in this repository (`src/modelquad/`).
Where a statement and the code disagree, the code is right and this document
has a bug; please report it. Several statements are checked by tests, named
next to them.

ModelQuad is not affiliated with Anthropic or OpenAI. What those companies do
with data you send them is governed by their terms and your settings with
them, not by this document.

## Summary

- ModelQuad's own processes connect only to loopback addresses
  ([NETWORK.md](NETWORK.md)). It has no telemetry.
- For a Codex call, ModelQuad reads only the files listed in the call's
  `files` argument, which Claude Code shows you in its approval prompt. It
  skips files your Claude Code `Read` deny rules, Git or a `.modelquadignore`
  exclude, screens the rest for secrets, and your Codex CLI sends the task and
  that text to OpenAI. Codex itself runs in an empty temporary folder without
  file, command, web or MCP tools.
- Everything ModelQuad returns to Claude Code becomes part of your
  conversation, which Claude Code sends to Anthropic.
- ModelQuad does not open conversation transcripts.
- Answers waiting to be collected expire after 7 days by default. History is
  kept until you delete it.
- Backups ModelQuad makes are full copies of your files, and can contain
  secrets you put in your configuration.

## What leaves your computer

ModelQuad sends nothing to a ModelQuad service; none exists. Data leaves your
computer only through the two provider programs below, under your accounts
and their terms.

### To OpenAI, through the Codex CLI

#### A Codex call

When Claude Code calls `modelquad_delegate` or `modelquad_handoff` and you
approve the call, ModelQuad prepares the input in its own process and then
starts the Codex CLI.

1. **The task text** is the text Claude Code wrote. If it holds a private key,
   a recognizable access token (for example an `sk-`, `ghp_`, `AKIA` or `xox`
   token), or a long random-looking quoted value under a name such as
   `password` or `api_key`, the call is refused with a message that says so.
   Other values under secret-like names (`api_key`, `token`, `password`,
   `secret`, `credential`, `authorization`, `connection_string` and similar),
   bearer and basic authorization values, JSON web tokens and passwords inside
   URLs are replaced with `<credential>`.
2. **Only the files in `files`.** Both tools take an explicit `files`
   argument: at most 32 paths relative to the working folder. Claude Code
   shows the tool's arguments, this list included, in the approval prompt it
   shows before every call, and `modelquad_handoff` also lists the files and
   their total size in its own form. A file named only in the task text, a
   `Relevant files:` list, a traceback or a patch header is never read or
   sent; the reply may suggest such names, without sending them. If a listed
   file cannot be sent (missing, over the limits, holds a credential, or
   cannot be read safely), the call is refused with the reason. ModelQuad does
   not search folders, follow imports or expand a folder name into its files.
3. **Your read restrictions are honoured.** ModelQuad reads files itself,
   outside Claude Code's file tools, so before reading it applies the same
   restrictions Claude Code would. A listed file is skipped, never read, and
   reported with its reason when:
   - a `Read(...)` deny rule covers it in any Claude Code settings file that
     could apply: managed settings
     (`C:\Program Files\ClaudeCode\managed-settings.json`), your user
     `settings.json` (in `%USERPROFILE%\.claude`, or `CLAUDE_CONFIG_DIR`), and
     the `.claude\settings.json` and `.claude\settings.local.json` of the
     working folder, of every folder between it and the listed file, and of
     every folder above them, up to (not including) your home folder, or to
     the top of the drive for a project outside your home folder
     (`denied_by_claude_settings`). Claude may name a subfolder of the project
     as the working folder, or a folder above it, so the project's own
     settings are found either way; each rule is applied relative to each of
     these folders. The documented path forms are supported:
     `//absolute`, `~/home`, `/project-root`, relative paths, and `*`/`**`
     globs; a rule is read the wider way when in doubt. If one of these files
     exists but cannot be read or parsed, no file is sent at all
     (`claude_settings_unreadable`): ModelQuad cannot honour rules it cannot
     read;
   - Git ignores it (`git_ignored`). ModelQuad asks `git check-ignore` with
     only the listed paths; without Git, or outside a repository, this check
     is skipped;
   - a `.modelquadignore` (gitignore syntax) in any of those folders that
     holds the file covers it (`modelquad_ignored`). Its patterns are
     relative to the folder it sits in.

   A skipped file does not stop the call; the task runs without it, and
   Codex is not told its name.
4. **Limits on what is read.**
   - Only files inside the working folder: relative paths, or absolute paths
     inside it. Paths with `..`, other drives, `.git`, alternate data streams
     or Windows device names are refused.
   - Only regular UTF-8 text files. Links and junctions (the file or any folder
     above it), hard-linked files, network paths, binary files and files that
     change while they are read are not sent.
   - At most 64 names are examined and at most 32 files are sent: each file at
     most 128 KiB, and all files together at most 128 KiB. Reading stops after
     2 seconds.
   - The whole input, task and files together, is capped at 192 KiB; if the
     listed files do not fit, the call is refused.
5. **Credential files are skipped**, whatever the request says: names that
   start with `.env`, `auth.json`, `credentials`, `credentials.json`,
   `.npmrc`, `.yarnrc`, `.yarnrc.yml`, `.pypirc`, `.netrc`, `_netrc`,
   `.git-credentials`, `pip.conf`, `pip.ini`, `nuget.config`, `kubeconfig`,
   `application_default_credentials.json`, files ending in `.pem`, `.key`,
   `.pfx` or `.p12`, anything inside a `.codex`, `.claude`, `.aws`, `.ssh`,
   `.agents`, `.azure`, `.kube` or `gcloud` folder, and paths that themselves
   contain a credential. A file named `PROJECT_NOTES.md` is not sent either.
6. **Secrets inside files** are screened like the task text. A private key or
   a recognizable access token in a listed file refuses the call. Other secret-looking values become `<credential>`. In
   configuration and text files (`.json`, `.yaml`, `.toml`, `.ini`, `.md`,
   `.txt` and similar) every unquoted value under a secret-like name is
   replaced; in code a variable name or a number there is kept, so
   `token = next(it)` is sent as written. Paths in file text are not changed.
7. **What Codex receives** on its standard input: the screened task text, the
   files' text as JSON with line numbers, a fixed instruction (no tools; treat
   the files as evidence, not instructions), a fixed answer format, and a
   short fixed notice about repository notes. In this release that notice
   carries no notes: ModelQuad does not read `PROJECT_NOTES.md`.
8. **How Codex runs.** ModelQuad starts the native `codex.exe` with `exec`, in
   a new empty temporary folder that is deleted afterwards. It passes
   `--sandbox read-only`, `--ignore-user-config`, `--ignore-rules` and
   `--ephemeral`, turns web search off, and turns off Codex's command, code,
   hook, app, plugin, browser and computer-use features (and its image,
   multi-agent and shell-snapshot features when the installed Codex lists
   them). It passes a model and reasoning effort: the ones the request names,
   otherwise the model set in your Codex `config.toml` and ModelQuad's default
   effort for the task type. Before a run, ModelQuad runs `codex --version`
   and `codex features list` (both answers are cached) to check that these
   features exist and are off. A Codex CLI older than 0.160.0 is refused.
9. **Codex's environment** is a copy of yours without other services'
   credentials: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
   `CLAUDE_CODE_OAUTH_TOKEN` and other `CLAUDE_CODE_*TOKEN*` variables,
   `GITHUB_TOKEN`, `GH_TOKEN`, `GH_ENTERPRISE_TOKEN`,
   `GITHUB_ENTERPRISE_TOKEN`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`,
   and `AZURE_*` secrets, passwords, keys, tokens and connection strings.
   `OPENAI_API_KEY` and `CODEX_API_KEY` stay, because they are a way to sign
   Codex itself in. ModelQuad adds `MODELQUAD_WORKER=1`.
10. **Codex sends the input to OpenAI** under your account and returns its
   answer as a stream of JSON events. ModelQuad checks every event; if the
   stream shows a command, a file change, an MCP tool call or a collaboration
   call, the answer is withheld (`tests/test_noshell_workers.py`). A
   `codex exec` run carries Codex's own client identity; ModelQuad adds none.
   What Codex stores on your computer is up to Codex.
11. **What you are told.** The reply lists `sent_files` (each path, its size
    and the first 16 hex characters of the SHA-256 of the text as sent, after
    redaction) and `skipped_files` with their reasons. The same metadata, and
    whether you approved through the delegate prompt or the handoff form, is
    kept in the history (see [The history database](#the-history-database));
    never the contents.

#### Quota reads

To show Codex's quota, ModelQuad starts `codex app-server` and asks it for your
rate limits (`account/rateLimits/read`). This is the only Codex call in which
ModelQuad names itself: it identifies as `modelquad` with its version. Codex
then asks OpenAI. No prompt is sent and no model turn runs. The answer
includes your plan type. ModelQuad stores it in `usage-codex.json` and in the
history database. Neither status replies nor history entries returned to
Claude Code include it (see below). Only its kind reaches Claude Code: the
handoff form and the delegate reply carry one short line, "Codex account:
personal ChatGPT plan", "workspace plan" or "unknown", and for a personal plan
the reminder "If this is work code, check you may send it to a personal
account."

- The MCP server reads once when it starts and then every 240 seconds (about
  every 4 minutes) while it runs. Claude Code sessions share one reading: a
  session skips its read when another one read less than 230 seconds earlier.
- `modelquad usage-refresh` and the `modelquad_usage_refresh` tool read on
  demand, at most once a minute unless forced.
- To stop the background reads, set the environment variable
  `MODELQUAD_CODEX_QUOTA_REFRESH` to `0` (or `false`, `no`, `off`) where
  Claude Code starts ModelQuad, for example with
  `setx MODELQUAD_CODEX_QUOTA_REFRESH 0`, then restart Claude Code. On-demand
  reads still work.
- Starting a Codex task does not ask Codex for the quota.

### To Anthropic, through Claude Code

ModelQuad does not start Claude. Claude Code starts ModelQuad, and everything
ModelQuad returns to it becomes part of your conversation, which Claude Code
sends to Anthropic:

- Codex's answers, and facts about each run: status, timings, counts, which
  files were sent (path, size, digest prefix) and which were skipped and why,
  and the Codex account kind (personal, workspace or unknown), never the plan
  name.
- Quota, from `modelquad_status`: used and remaining percentages, reset times
  and freshness for each window, and the handoff suggestion. Not your plan
  type. When a reading is missing, the reply says why; a reason that mentions
  the quota file names it by its file name only, such as `usage-codex.json`,
  never its folder.
- History, when Claude asks for it: task types, workers, statuses, times,
  short failure details and project folder names. Given a project folder, the
  `modelquad_activity` tools return only the entries of Codex requests started
  from that folder; without one, they return entries from every project. They
  leave out the quota snapshot stored with each request, so not your plan
  type. `modelquad_delegation_status` answers about the request or folder
  Claude names.
- Job status and retained answers. The `modelquad_job_*` tools need the
  project folder and answer only for jobs started from that folder.
- The report of the `modelquad_doctor` tool, which includes local paths, such
  as where Claude Code and Codex are installed.
- The startup reminder: job ids, statuses, task types, whether files may have
  changed, and expiry times. Not answer text.
- The `/modelquad` skill and the MCP server instructions that provide Claude
  with ModelQuad's guidance.

### Model training

Anthropic's and OpenAI's terms each forbid using their output to train the
other's models. With ModelQuad, text Claude wrote goes to OpenAI and Codex's
answers go to Anthropic. We recommend turning off model training with both
providers while you use ModelQuad:

- ChatGPT data controls: <https://chatgpt.com/#settings/DataControls>
- Claude privacy settings: <https://claude.ai/settings/data-privacy-controls>

Opting out does not cover feedback you submit or conversations flagged for
safety review. How long each provider keeps your data depends on its terms
and your settings.

## What ModelQuad reads on your computer

### Hooks and the status line

`modelquad install claude` adds three commands to Claude Code's settings. Each
receives a JSON payload from Claude Code that includes the path of the
session transcript. **ModelQuad never opens, reads or inspects that file**
(`tests/test_hook_transcript_boundary.py`).

- **Status line** (`modelquad usage-hook claude`): the 5-hour and weekly
  rate-limit figures and their reset times. Also the Claude Code version, the
  session id, the model id and display name, and the flag that says whether
  the context is above 200k tokens. The session id is replaced by a
  fingerprint before it is stored (see [The installation id](#the-installation-id)).
  The payload's workspace path, transcript path and cost figures are not read.
- **Startup reminder** (`modelquad hook-pending claude`, at session start):
  the event name and the working directory. The directory is hashed and used
  only to ask the local service which answers are waiting for that project. It
  writes nothing.

### Other reads

- **Six environment variables** Claude Code may set: `CLAUDECODE`,
  `CLAUDE_CODE_ENTRYPOINT`, `AI_AGENT`, `CLAUDE_AGENT_SDK_VERSION`,
  `CLAUDE_CODE_SESSION_ID` and `CLAUDE_EFFORT`. No other variable is read for
  this purpose; variables that carry tokens, paths or endpoints are on a named
  refusal list (`hostobs.CLAUDE_ENV_REFUSED`).
- **The MCP client name and version** Claude Code sends when it connects.
- **Two keys of Claude Code's user settings** (`settings.json`): `model` and
  `effortLevel`.
- **The model settings in Codex's `config.toml`** (in `CODEX_HOME`, or
  `%USERPROFILE%\.codex`): `model`, `model_reasoning_effort`,
  `model_provider`, `profile`, and those keys of the active profile. Nothing
  else in the file is used.
- **Whether you are signed in**: whether Claude Code's
  `.claude\.credentials.json` and Codex's `.codex\auth.json` exist, without
  opening them, and whether `OPENAI_API_KEY` is set.
- **The files a Codex call lists**, as described above, and the read
  restrictions applied first: the `permissions.deny` lists of the Claude Code
  settings files named there, the `.modelquadignore` files named there, and Git's
  answer to `git check-ignore` for the listed paths.

ModelQuad does not read Claude Code or Codex conversation files, session
folders or prompt history, or the contents of credential files. In this
release it does not read `PROJECT_NOTES.md`.

## What is stored, and where

### Outside the state folder

Paths are relative to `%USERPROFILE%`.

| Location | What it holds | Written by |
|---|---|---|
| `.claude\usage-claude.json` | Claude's 5-hour and weekly used percentages, reset times, and when they were observed | The status line command |
| `.claude\usage-codex.json` | Codex's 5-hour and weekly used percentages, reset times, plan type, when they were read, and why a read failed | The MCP server's background read, `modelquad usage-refresh` |
| `.claude\usage-*.json.lock`, `.claude\usage-refresh-*.lock` | Empty lock files | The same writers |
| `.claude.json`, `.claude\settings.json`, `.claude\skills\modelquad*\SKILL.md` (five skills) | The Claude Code integration listed in [README.md](README.md) | `modelquad install claude` |
| `<file>.modelquad-backup-<UTC time>` beside a changed file | A full copy of the file before ModelQuad changed or removed it. It holds whatever the file held, including secrets you put there, such as `env` values of MCP servers in `.claude.json` | Install and uninstall (`adapters/backup.py`) |
| Windows Task Scheduler, task `ModelQuad_Service` | Starts the local service when you sign in | `modelquad service install` |
| Your user `Path` variable (`HKCU\Environment`) | The folder of an installed but unreachable tool | `modelquad tools --fix-path` |

`modelquad uninstall claude` removes the current integration and also takes
back the `CLAUDE.md` block, `PreToolUse` gate hook, and global timeout setting
that older builds may have installed. It does not remove quota files, their
lock files or backups; delete those by hand.

ModelQuad replaces files through a temporary sibling named
`<name>.<random>.tmp`. A process stopped in the middle of such a write can
leave that file behind.

### The state folder

`statecontext.py` picks the folder once per process, in this order:

| How ModelQuad runs | State folder |
|---|---|
| `MODELQUAD_STATE_ROOT` is set | That folder |
| Plain (non-editable) `pip install` | `%USERPROFILE%\.local\share\modelquad\` |
| Editable install (`pip install -e`) from a checkout | `%USERPROFILE%\.modelquad\` |

Before 2026-10-25 a plain install used `%LOCALAPPDATA%\ModelQuad\`. Windows
redirects what the Claude desktop app and the programs it starts write under
`AppData` to a private copy, so ModelQuad started there and ModelQuad started
from a terminal kept two separate folders. ModelQuad no longer reads or writes
the old folder; delete it once you no longer need its history.

`modelquad privacy` and `modelquad state` print the folder in use. Stop the
service (`modelquad service stop`) before you delete anything here.

| File or folder | What it holds | How long | Sensitive? |
|---|---|---|---|
| `modelquad.db`, `modelquad.db-wal`, `modelquad.db-shm` | The history and settings database (below) | No automatic expiry | In a limited way: folder names, hashes of folder paths and of prompts, short failure details, and quota readings with the Codex plan type |
| `modelquad.db.fate.lock` | Empty lock file | Kept | No |
| `job-<random>.json` | A background job's launch data: the full task text, the working folder and the task settings. The named files are read later, when the job runs | The runner deletes it when it reads it; a leftover is deleted at the next job start once it is older than 10 minutes | Yes: your task text. Private to you and SYSTEM |
| `pending-output\` | Answers of background jobs waiting to be collected (below), plus `outcomes.jsonl`, `removal.intent` and `store.lock` | 7 days by default | Yes: an answer can quote your code. Private to you and SYSTEM |
| `identity.json` | The installation id (below) | Until reset or deleted | A resettable pseudonym |
| `service-secret` | The random secret every request to the local service must carry, except its listener check | Kept; created once and not overwritten | Yes: it authorizes the service. Private to you and SYSTEM |
| `host-registration-backup.json` | Your earlier `modelquad` MCP entry in Claude Code, if install replaced one, stored verbatim so uninstall can put it back. Also ownership records for the timeout setting and the `SessionStart` hook, keyed by an unsalted SHA-256 of the settings file path. This file may contain credentials: an MCP entry can carry `args` and `env`, and `env` is where API keys live | Deleted when nothing is owed any more | Yes: it may contain credentials. Private to you and SYSTEM, and not shown by the CLI, `--json`, `doctor` or logs (`tests/test_registration_backup_is_product_state.py`) |
| `host-observation-claude.json` | The last status-line facts: Claude Code version, model id and display name, large-context flag, a 16-character session fingerprint, time | Overwritten on each update | Low; the raw session id is not written |
| `host-integrations.json` | Whether global host integrations are on or off | Kept | No |
| `gate-state\` | One empty marker per Claude Code session, named by the session fingerprint | Markers older than 7 days are removed on the next hook run | No |
| `service.pid`, `service*.lock`, `service.launch-unconfirmed` | The service's process id, start time and launch command line, which includes the Python interpreter's path; lock and marker files | Replaced on each service start | Low: a local path |
| `logs\job-runner.log`, `logs\job-runner.log.1` | Runner events (UTC time, job id, a fixed label or an exception class name) and the runner's error output | Rotated above 1 MiB when opened, one older file kept | Possibly: a Python traceback can contain paths and lines of code |
| `logs\service-diagnostics.log`, `logs\cli-diagnostics.log` (with `.1` and `.lock`) | Fixed-vocabulary start and exit events of windowless processes | About 1 MiB per file, one older file kept | No |

The local service runs with access logging off and its standard streams
discarded, so there is no general service log.

#### Pending output

When a background job finishes, its answer and status are written to
`pending-output\`, separate from the history database. The store sends
nothing anywhere and does not apply an answer to your files.

- Claude Code can collect an answer as often as it needs until it
  acknowledges it. Acknowledging deletes the content.
- Unacknowledged content expires 7 days after it was stored. Expiry is
  enforced on every read, and every 10 minutes while the local service runs.
  Reads warn when expiry is less than a day away.
- Answer text is capped at 1 MiB; longer text is cut and the cut is reported.
- After content is removed, a content-free record (job, attempt and decision
  ids, a random receipt, event times) stays for up to 30 days. After that, only
  a SHA-256 hash of the job id stays for 30 more days, to refuse a replayed
  job. Neither holds answer text or file paths.

### The history database

Schema version 24, defined in `service/db.py`; the writers are in
`service/history.py`. Every table is local; nothing in the database is sent
anywhere. Free text and stored metadata pass through `privacy.py` before they
are written, so absolute paths become `<path>`, including folder names with
spaces (`tests/test_privacy_paths.py`, `tests/test_provider_rows_carry_no_paths.py`).
Columns not described below hold fixed labels, numbers, times or ids.

| Table | What a row records | Notable columns |
|---|---|---|
| `delegation_attempts` | One request to start Codex, written before anything can fail | `detail`: path-redacted, at most 500 characters. `request_key`: the first 32 hex characters of an unsalted SHA-256 over the task type, the normalized working folder and the full prompt; it detects a retry of the same request. `workspace_key`, `workspace_ancestors`: the same kind of truncated, unsalted SHA-256 of the working folder and of each folder above it. `worker_descendants`: pid, start time and executable name of processes the worker left, at most 32. The repository columns (`repo_key`, `policy_version`, `capability`, `target_key`) have no writer in this release |
| `execution_history` | One finished Codex run | `project`: the working folder's name only, not its full path. `result`: a coarse bucket, not the answer. `error`, `task_status_reason`: path-redacted, at most 2,000 characters. `metadata_json`: run facts, such as the size and category of the error output, event counts, the command as the executable's name and its flags with no path, the model and effort, and what the worker left running. `file_audit`: for each launch, the relative path, byte count and SHA-256 prefix of every file sent, the skipped paths with their reasons, and whether you approved through the delegate prompt or the handoff form; never contents. `modelquad_activity_detail` returns it, with the time and the project's workspace key, for the project folder Claude names `outcome_evidence_json`: see below |
| `routing_decisions` | Which worker a request used, and the scores behind that | `providers_json`: per-provider scores and reasons. `evidence_json`: the quota readings and a snapshot of the host (Claude Code version, model id, session fingerprint, an uptime bucket, a size bucket for the handoff, a large-context flag; not the raw session id). `override_reason`: a fixed label |
| `jobs` | One background job's process lifecycle | Runner and worker pids and start times, status, heartbeat and end times, recovery fields, and the Windows boot id and uptime when the job was reserved |
| `job_fate_releases` | Your explicit release of a job whose outcome is unknown (`modelquad jobs release-fate`) | An actor label, the surface, a fixed reason, pid and boot evidence. Append-only: triggers refuse updates and deletes |
| `provider_usage_history` | Quota readings over time: on each Codex call, on `modelquad status` and `modelquad route`, and every minute while the local service runs | `metadata_json`: the raw quota fields, including the Codex plan type, and `source_file`, the quota file it came from, with the path redacted |
| `provider_health` | Checks of the provider programs, every minute while the local service runs | `detail`: the doctor text with paths redacted |
| `settings` | Your choices | Background work on or off, and when you consented; which tools are disconnected; internal defaults |
| `plan_decisions`, `outcome_revisions`, `repo_registrations`, `repo_policy_versions` | Part of schema 24 | Nothing writes them in this release |

`outcome_evidence_json` is a serialized `OutcomeEvidence`
(`execution/base.py`). Most fields are booleans, counts or fixed values.
`blocked_reason` repeats the task-status reason with the same redaction and
cap. The details of its evidence items are written by `outcome.py` only:
`exit_code=<n>`, `task_status=<value>` and, when the request named tests, an
item of kind `requested_tests_not_verified` whose detail is a fixed sentence
with the number of test targets, not their names. ModelQuad does not run
tests.

For read-only tasks, which are the only tasks in this release, stored reasons
and run metadata are reduced to fixed labels and numbers before they are
written (`project_notes.private_result`, `closed_reason`).

The hashes above are pseudonyms: they hide a value from casual reading, not
from a targeted guess. Someone with a copy of the database can confirm a
guessed folder path (unsalted hashes), and with `identity.json` too, a
guessed session id. Deleting rows with SQLite does not guarantee that the
bytes are erased from disk.

### What is not stored

| Claim | Where it is enforced |
|---|---|
| Prompts are not stored in the history. A prompt is on disk only in a short-lived `job-<random>.json`; `request_key` is a truncated hash, not the text | `service/db.py` schema, `jobs/handle.py` |
| Codex's answers are not stored in the history. A background job's answer waits in `pending-output\` until it is acknowledged or expires | `service/history.record_execution`, `jobs/store.py` |
| The contents of files sent to Codex are not stored. They are read into memory, sent, and dropped | `execution/noshell.py` |
| The text of Codex's error output is not stored, in any mode; only its size and a category | `execution/base.result_error`, `workerevidence.py` |
| Conversation transcripts are never opened | `cli_entry.dispatch_gate` (a no-op), `adapters/pending_reminder.py`, `hostobs.record_host_push`; `tests/test_hook_transcript_boundary.py` |
| Absolute paths do not reach the history | `privacy.py`, `service/history.py`; `tests/test_privacy_paths.py` |
| The raw Claude Code session id is not stored. It becomes a 16-character HMAC keyed by the installation id | `hostobs.session_fingerprint` |
| Provider credential files are not opened, copied or sent to Codex. Sign-in checks test only whether they exist. Two kinds of copies can still hold secrets you put in configuration: `host-registration-backup.json` and the `*.modelquad-backup-*` files | `provider_connection.py`, `execution/noshell.py`, `adapters/registration.py` |
| Only six named environment variables of the Claude Code process are read | `hostobs.CLAUDE_ENV_ALLOWLIST`; `tests/test_host_observability.py` |
| Nothing is collected for telemetry | ModelQuad has no telemetry code |

## The installation id

`identity.json` holds a random identifier (a version 4 UUID), created the
first time something needs it. It is derived from nothing: no hardware ids,
user names or machine names. It is a pseudonym, not anonymous: it stays the
same until you reset it. It stays on this computer.

It is the key for the fingerprints of Claude Code session ids, so ModelQuad
can group one session's records without storing the raw id.

`modelquad privacy --reset-identity` replaces it. After a reset, earlier
session fingerprints can no longer be linked to new ones. A reset does not
delete history.

## How to see what is stored

- `modelquad privacy` (or `--json`): the state folder, the installation id,
  the network rule, retention, and how to delete everything.
- `modelquad activity` (or `--json`, or `--detail execution:<id>`): the
  history.
- `modelquad state`: which state folder each part of ModelQuad uses.
- `modelquad network`: the network rule.
- From Claude Code, the `modelquad_job_list_pending` tool lists the answers
  waiting for a project.
- The files themselves: the database is plain SQLite, the rest is JSON or
  text.

## How to delete it

- **One answer**: acknowledge it from Claude Code, or let it expire.
- **Everything**:
  1. `modelquad service stop`, then `modelquad service uninstall`, to stop the
     local service and remove its sign-in task.
  2. `modelquad uninstall --global`, to remove ModelQuad's wiring from Claude
     Code. (`modelquad integrations off` does the same and keeps it off.)
  3. Close the Claude Code sessions that use ModelQuad, then delete the state
     folder that `modelquad privacy` names.
  4. Delete `usage-claude.json`, `usage-codex.json`, `quota-pace.json`,
     `usage-claude.json.lock`, `usage-codex.json.lock`,
     `usage-refresh-claude.lock` and `usage-refresh-codex.lock` in
     `%USERPROFILE%\.claude`, by those exact names: other `.lock` files there,
     such as `daemon.lock`, belong to Claude Code. Then delete the
     `*.modelquad-backup-*` files. Check the backups first: they can contain
     secrets you put in your configuration.

Deleting files is ordinary file deletion. ModelQuad does not securely erase
disk blocks.

## Limits

- Protection is between you and other people, not between you and software
  you run. Any program running as your Windows user can read the state
  folder, the service secret and the database, and run the CLI. An
  administrator can read every file. The state folder inherits your profile's
  access list, which on some computers lets other accounts read it; the files
  marked private above are limited to you and SYSTEM. See
  [SECURITY.md](SECURITY.md).
- The secret and path screening is pattern-based. It catches recognizable
  credentials and paths; it cannot prove that no sensitive fact appears in
  ordinary text. Choose the files you ask Codex to read.
- What Codex and Claude Code send to their providers, and what those
  providers keep, is outside ModelQuad's control.
