# Privacy

ModelQuad runs on your computer. It has no server of its own, no account
system, no upload and no telemetry. This document says what it reads, what it
sends where and through which program, what it stores, for how long, and how
to look at it and delete it. It is meant to be a complete inventory of every file,
folder and database table ModelQuad writes; if you find something missing,
please report it.

Everything here describes the code in this repository (`src/modelquad/`).
Where a statement and the code disagree, the code is right and this document
has a bug; please report it. Several statements are checked by tests, named
next to them.

ModelQuad is not affiliated with Anthropic or OpenAI. What those companies do
with data you send them is governed by their terms and your settings with
them, not by this document.

ModelQuad's author receives no data from the tool. Nothing ModelQuad does
sends anything to him or to any server he runs. If you choose to, you can
print a usage report and pass it on yourself
([below](#the-usage-report)).

## Summary

- ModelQuad's own processes connect only to loopback addresses
  ([NETWORK.md](NETWORK.md)). It has no telemetry.
- For a Codex call, ModelQuad reads only the files listed in the call's
  `files` argument, which Claude Code shows you in its approval prompt
  unless you turned on the standing grant ([below](#the-standing-grant)). It
  refuses a call that names a file your Claude Code `Read` deny rules, Git or
  a `.modelquadignore` exclude, screens the rest for secrets, and your Codex
  CLI sends the task and
  that text to OpenAI. Codex itself runs read-only in an empty temporary
  folder with its file, command, web and MCP tool features switched off.
  Codex still lists a few built-in tools that ModelQuad cannot remove; see
  [SECURITY.md](SECURITY.md).
- Everything ModelQuad returns to Claude Code becomes part of your
  conversation, which Claude Code sends to Anthropic, or to the cloud
  provider you set Claude Code up to use.
- ModelQuad does not open conversation transcripts.
- ModelQuad keeps no answers. The review ledger, which holds no task text, no
  file contents and no full answer, is kept until you delete it.
- Backups ModelQuad makes are full copies of your files, and can contain
  secrets you put in your configuration.

## What leaves your computer

ModelQuad sends nothing to a ModelQuad service; none exists. Data leaves your
computer only through the two provider programs below, under your accounts
and their terms.

### To OpenAI, through the Codex CLI

#### A Codex call

`modelquad_delegate` is the only tool that starts Codex. When Claude Code
calls it and you approve the call (or the standing grant below lets it run),
ModelQuad prepares the input in its own process and then starts the Codex
CLI. `modelquad_review_package` and `modelquad review package` show the same
input as a package (below) and start nothing and send nothing.

1. **The task text** is the text Claude Code wrote. If it holds a private key,
   a recognizable access token (for example an `sk-`, `ghp_`, `AKIA` or `xox`
   token), or a long random-looking quoted value under a name such as
   `password` or `api_key`, the call is refused with a message that says so.
   Other values under secret-like names (`api_key`, `token`, `password`,
   `secret`, `credential`, `authorization`, `connection_string` and similar),
   bearer and basic authorization values, JSON web tokens and passwords inside
   URLs are replaced with `<credential>`.
2. **Only the files in `files`.** The tool takes an explicit `files`
   argument: at most 32 paths relative to the working folder. Claude Code
   shows the tool's arguments, this list included, in the approval prompt it
   shows before every call without a standing grant. A file named only in the
   task text, a `Relevant files:` list, a traceback or a patch header is never
   read or sent; the reply may suggest such names, without sending them. If a
   listed file cannot be sent (missing, over the limits, holds a credential,
   or cannot be read as a plain local file), the call is refused with the
   reason. ModelQuad does not search folders, follow imports or expand a
   folder name into its files.
3. **Your read restrictions are honoured.** ModelQuad reads files itself,
   outside Claude Code's file tools, so before reading it applies the same
   restrictions Claude Code would. A listed file is never read when:
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
     relative to the folder it sits in. If one of these files exists but
     cannot be read, no file is sent (`modelquadignore_unreadable`).

   When a file the request names is excluded this way, the call is refused
   before Codex starts and the reply names the file and the reason, so
   Claude can call again without it. Codex is not told its name.
4. **Limits on what is read.**
   - Only files inside the working folder: relative paths, or absolute paths
     inside it. Paths with `..`, other drives, `.git`, alternate data streams
     or Windows device names are refused.
   - Only regular UTF-8 text files. Links and junctions (the file or any folder
     above it), hard-linked files, network paths, binary files and files that
     change while they are read are not sent.
   - At most 64 names are examined and at most 32 files are sent: each file at
     most 128 KiB, and all files together at most 128 KiB. Reading stops after
     2 seconds. A repository's `.modelquad.toml` can lower the number of files
     and the total ([docs/REPO-POLICY.md](docs/REPO-POLICY.md)).
   - The whole input, task and files together, is capped at 192 KiB; if the
     listed files do not fit, the call is refused.
5. **Files with a credential name are not sent**, whatever the request says.
   Listing one refuses the whole call before Codex starts; nothing is sent.
   The check works by file name and cannot be complete: a secret kept in a
   file with an ordinary name is not recognised by its name. This
   covers environment files by name (`.env`, `.env.local`, `prod.env`,
   `app.env.production`, `env.local`), the exact names `auth.json`, `credentials`, `credentials.json`,
   `.npmrc`, `.yarnrc`, `.yarnrc.yml`, `.pypirc`, `.netrc`, `_netrc`,
   `.git-credentials`, `pip.conf`, `pip.ini`, `nuget.config`, `kubeconfig`,
   `application_default_credentials.json`, SSH key files (`id_rsa`,
   `id_ed25519` and similar, not their `.pub` halves), `secrets.json`,
   `secrets.yaml`, `credentials.yml`, `token.json`, `service-account*.json`,
   `client_secret*.json`, `.dockercfg`, `wp-config.php`,
   `local.settings.json`, files ending in `.pem`, `.key`,
   `.pfx`, `.p12`, `.ppk`, `.jceks`, `.kdbx` or `.tfvars.json`, a backup copy
   of any of these (`auth.json.bak`), anything inside a `.codex`, `.claude`, `.aws`, `.ssh`,
   `.agents`, `.azure`, `.kube` or `gcloud` folder, and paths that themselves
   contain a credential. A file named `PROJECT_NOTES.md` is not sent either;
   it is skipped and the call goes on without it.
6. **Secrets inside files** are screened like the task text. A private key or
   a recognizable access token in a listed file refuses the call. Other secret-looking values become `<credential>`. In
   configuration and text files (`.json`, `.yaml`, `.toml`, `.ini`, `.md`,
   `.txt` and similar) every unquoted value under a secret-like name is
   replaced; in key-value configuration (`.yaml`, `.toml`, `.ini`, `.cfg`,
   `.conf`, `.properties`) that is the whole rest of the line, whatever
   characters it holds. In code a variable name or a number there is kept, so
   `token = next(it)` is sent as written, while a quoted literal assigned to a
   name ending in a secret word (`dbPassword = "..."`) and a `Password=...;`
   part of a connection string are replaced. This screen works on names and
   shapes; it does not understand the code, so a secret under an ordinary
   name, or built from parts, can still be sent. Do not name files that hold
   secrets. Paths in file text are not changed.
7. **What Codex receives** on its standard input: the screened task text, the
   files' text as JSON with line numbers, a fixed instruction (no tools; treat
   the files as evidence, not instructions), and a fixed answer format (one
   line per finding, and a short report that may name files it needed and did
   not get).
8. **How Codex runs.** ModelQuad starts the native `codex.exe` with `exec`, in
   a new empty temporary folder that is deleted afterwards. It passes
   `--sandbox read-only`, `--ignore-user-config`, `--ignore-rules` and
   `--ephemeral`, turns web search and Codex's agents off, and turns off
   Codex's command, code, hook, app, plugin, browser and computer-use features
   (and its image, multi-agent and shell-snapshot features when the installed
   Codex lists them). It passes a model and a reasoning effort only when the
   request names them; otherwise Codex uses its own defaults. ModelQuad does
   not read your Codex `config.toml`, and Codex is started with it ignored.
   Before a run, ModelQuad runs `codex --version` and `codex features list`
   to check that these features exist and are off. A Codex CLI older than
   0.160.0 is refused.
9. **Codex's environment** is a copy of yours without other services'
   credentials: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
   `CLAUDE_CODE_OAUTH_TOKEN` and other `CLAUDE_CODE_*TOKEN*` variables,
   `GITHUB_TOKEN`, `GH_TOKEN`, `GH_ENTERPRISE_TOKEN`,
   `GITHUB_ENTERPRISE_TOKEN`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`,
   `AZURE_*` secrets, passwords, keys, tokens and connection strings,
   `GOOGLE_APPLICATION_CREDENTIALS`, `DATABASE_URL`, and any variable whose
   name looks like a secret (it ends in `TOKEN`, `API_KEY`, `APIKEY`,
   `_DSN` or `_DATABASE_URL`, or contains `TOKEN_`, `SECRET`, `PASSWORD`,
   `PASSWD`, `PRIVATE_KEY`, `ACCESS_KEY`, `CREDENTIALS` or
   `CONNECTION_STRING`). Every `OPENAI_*` and `CODEX_*` variable stays,
   because `OPENAI_API_KEY` and `CODEX_API_KEY` are a way to sign Codex itself
   in. ModelQuad adds `MODELQUAD_WORKER=1`. The `codex --version` and
   `codex features list` checks run with the same environment, in an empty
   temporary folder.
10. **Codex sends the input to OpenAI** under your account and returns its
   answer as a stream of JSON events. ModelQuad checks every event; if the
   stream shows a command, a file change, an MCP tool call or a collaboration
   call, the answer is withheld (`tests/test_noshell_workers.py`). The stream
   does not report every tool call; see [SECURITY.md](SECURITY.md). A
   `codex exec` run carries Codex's own client identity; ModelQuad adds none.
   What Codex stores on your computer is up to Codex. Codex also adds its own
   instructions to the request, including your global instructions file
   (`AGENTS.md` or `AGENTS.override.md` in your Codex folder) if you have one:
   `--ignore-user-config` does not keep it out, and no setting does (tested
   with Codex 0.164.0).
11. **What you are told.** The reply carries the package manifest: each sent
    file's path, its size and the first 16 hex characters of the SHA-256 of
    the text as sent (after redaction), the files left out with their reasons,
    and the package id. The same manifest is kept in the review ledger (see
    [The review ledger](#the-review-ledger)); never the contents.
12. **What was checked is what is sent.** When the call starts, ModelQuad
    takes the same digest of each file it would send. If, when Codex is about
    to start, a file's text differs or a file not sent then would be sent
    now, the call is refused and nothing is sent.

#### The package and its id

`modelquad_review_package` (or `modelquad review package` in a terminal)
packs the files exactly as a call would, with the same rules, screening and
limits, and returns the manifest and a package id. It reads the files and
sends nothing anywhere. The id is a digest over the task kind, the task text
and each sent file's path, size and digest. A `modelquad_delegate` call given
that id sends that exact package or nothing: if anything changed, the call is
refused (`package_changed`).

If Codex's reply says it needed files it was not given, nothing is added by
itself. Claude can show you the larger package, and a new call that names
those files and the earlier reply (`follows`) is a completion round, at most
two per review. Each round is an ordinary Codex call, with the same prompt or
grant as any other.

#### The standing grant

`modelquad settings auto on --yes`, run by you in a terminal or chosen by you
in `/modelquad:settings`, turns on
a standing grant for one repository (or, with `--global`, for every folder).
In a Claude Code session started in a granted folder, `modelquad_delegate`
carries no per-call approval mark, so Claude Code's own permissions decide
whether you are asked. With the grant on and the allow rule added (or "don't
ask again" chosen at the first prompt), files go to OpenAI with no per-call
prompt and no file list shown to you first.

What is sent does not change: only the files in `files`, read under the same
rules and redaction as above. Each call is recorded as before in the review
ledger (`modelquad review report`). `modelquad settings auto off` turns the
grant off; `modelquad off`, `modelquad settings repo off` or a
`.modelquad.toml` that turns Codex off outranks it.

### To Anthropic, through Claude Code

ModelQuad does not start Claude. Claude Code starts ModelQuad, and everything
ModelQuad returns to it becomes part of your conversation, which Claude Code
sends to Anthropic:

- Codex's answers (a reply shows at most 20,000 characters and says when it
  cut one), and facts about each run: its outcome, the package manifest
  (relative paths, sizes, digest prefixes, the files left out and why, the
  package id), the token counts and time Codex reported, each finding's
  reference, severity, file, line, category and Codex's confidence, and the
  names of files Codex said it needed.
- The package from `modelquad_review_package`: the same manifest, the limits
  and the package id.
- From `modelquad_status`: whether ModelQuad is on, whether Codex calls are
  automatic or approved one by one, what the folder's own settings and
  `.modelquad.toml` allow, and one line of history from the review ledger.
- From `modelquad_review_verdict`: how many verdicts were recorded, and which
  were not kept and why.
- With the plugin, the review reminder's and the review gates' fixed lines,
  which carry a count of changed files or the kind of a sensitive file, and
  never a path.
- The `/modelquad` skill and the MCP server instructions that provide Claude
  with ModelQuad's guidance.

### Model training

OpenAI's terms forbid using its output to develop models that compete with
it, and Anthropic's forbid using Claude's output to train any AI model
without its permission. ModelQuad passes text in both directions: text Claude
wrote goes to OpenAI, and Codex's answers go to Anthropic. Turn off model
training with both providers while you use ModelQuad, and do not use its
answers or history
as training data:

- ChatGPT data controls: <https://chatgpt.com/#settings/DataControls>
- Claude privacy settings: <https://claude.ai/settings/data-privacy-controls>

Opting out does not cover feedback you submit or conversations flagged for
safety review. How long each provider keeps your data depends on its terms
and your settings.

## What ModelQuad reads on your computer

### Hooks

Only the plugin installs hooks. `modelquad install claude` without the plugin
installs none, and takes back the status line and the startup reminder that
earlier builds wrote. Each hook receives a JSON payload from Claude Code that
includes the path of the session transcript. **ModelQuad never opens, reads
or inspects that file** (`tests/test_hook_transcript_boundary.py`).

- **Review reminder and review gates** (`modelquad hook-review claude`, in the
  plugin, after Claude edits a file or runs a command, before it edits a
  file, and when it stops): the event and tool names, the session id, the
  working directory and the edited file's path; for a `modelquad_delegate`
  call, its task kind and trigger and whether its reply carries an answer.
  It never reads file contents, even though an edit's payload carries them,
  and never the transcript. To choose its wording it reads
  `preferences.json`, the installation id, the repository's `.git` entry and
  its `.modelquad.toml`. While ModelQuad is off (`modelquad off`), it does
  nothing. It keeps one small file per session in `review-reminder\`:
  SHA-256 digests of the changed paths under the working directory (never the
  paths), whether one looked sensitive, and the gate's state. It records gate
  events in the review ledger with digests of the folder and the session.

### Other reads

- **Environment variables**: `CLAUDE_PROJECT_DIR` (the folder a session
  started in, to check the standing grant), `CLAUDE_CONFIG_DIR` (only to warn
  that ModelQuad does not follow it), ModelQuad's own `MODELQUAD_*` settings,
  the folders it uses to find Codex and Git (`PATH`, `PATHEXT`,
  `LOCALAPPDATA`), the home, `AppData` and `TEMP` folders it removes from
  stored text, and the two variables that tell WSL apart. It passes Codex the
  filtered environment described above.
- **Claude Code's plugin record** (`.claude\plugins\installed_plugins.json`),
  read-only, so `modelquad doctor`, `modelquad settings` and install can tell
  whether the plugin is installed. An unreadable record reads as unknown.
- **The files a Codex call or a package lists**, as described above, and the
  read restrictions applied first: the `permissions.deny` lists of the Claude
  Code settings files named there, the `.modelquadignore` files named there,
  and Git's answer to `git check-ignore` for the listed paths.
- **The repository's `.modelquad.toml`**, if there is one, on each Codex call
  and package, for `modelquad_status`, `modelquad settings` and
  `modelquad doctor`, and in the review hook
  ([docs/REPO-POLICY.md](docs/REPO-POLICY.md)). Its contents, path and key
  names are not stored.
- **The repository's top folder and current commit**: ModelQuad asks
  `git rev-parse --show-toplevel` for the top folder, and reads the commit of
  `HEAD` from the `.git` files, without starting Git, for the review ledger's
  code version.
- **An earlier build's settings**: if `modelquad.db` from an earlier build is
  in the state folder and you have not set `modelquad on` or `modelquad off`
  yet, ModelQuad opens it read-only and reads two settings rows, whether you
  had disconnected Claude Code or paused ModelQuad, so an upgrade never turns
  Codex back on by itself. Nothing else in that file is read.

ModelQuad does not read Claude Code or Codex conversation files, session
folders or prompt history, Codex's `config.toml`, or the contents of
credential files. It does not read `PROJECT_NOTES.md`.

## What is stored, and where

### Outside the state folder

Paths are relative to `%USERPROFILE%`.

| Location | What it holds | Written by |
|---|---|---|
| `.claude.json`, `.claude\skills\modelquad*\SKILL.md` (four skills) | The Claude Code integration listed in [README.md](README.md) | `modelquad install claude` without the plugin |
| `<file>.modelquad-backup-<UTC time>` beside a changed file | A full copy of the file before ModelQuad changed or removed it. It holds whatever the file held, including secrets you put there, such as `env` values of MCP servers in `.claude.json` | Install and uninstall (`adapters/backup.py`) |

The plugin itself is kept by Claude Code in its own plugin folder.

`modelquad uninstall claude` removes the current integration and also takes
back what earlier builds wrote and this one no longer has: the quota status
line, the `SessionStart` and `PreToolUse` hooks, the handoff, quota and split
commands, the `CLAUDE.md` block and the global timeout setting, each only when
it is ModelQuad's own and each with a backup. It does not remove backups;
delete those by hand.

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

`modelquad privacy` prints the folder in use.

| File or folder | What it holds | How long | Sensitive? |
|---|---|---|---|
| `preferences.json` | Your settings: ModelQuad on or off (`modelquad on`/`off`), proactive use on or off, whether the plugin install is in use, your per-repository choices from `modelquad settings repo` (one word each, keyed by a hash of the repository made with your installation id; no path or folder name), and the standing grant from `modelquad settings auto` (on for every folder, or the same repository hashes, each with when it was turned on and the notice version) | Kept until you change or unset them | No |
| `review-ledger.db` | The review ledger (below) | No automatic expiry | In a limited way: a finding's claim describes your code. Claims, reasoning, confidence reasons and notes are redacted for credentials and paths and capped (300, 400, 200 and 200 characters) |
| `review-reminder\` | One file per Claude Code session: SHA-256 digests of the paths Claude changed, a sensitive-file flag and the review gate's state | Deleted after 2 days | No: digests only |
| `active-workers\` | One small file per running Codex call: the process id and start time of the Codex process, so a process Codex starts cannot ask ModelQuad for Codex again | Removed when the call ends; a leftover after 1 hour | No |
| `identity.json` | The installation id (below) | Until reset or deleted | A resettable pseudonym |
| `data-flow-notice.json` | That the notice of what ModelQuad sends where was shown, where (install or the first Codex call) and when | Kept, so the notice is shown once | No |
| `host-registration-backup.json` | Your earlier `modelquad` MCP entry in Claude Code, if install replaced one, stored verbatim so uninstall can put it back. Also ownership records for the timeout setting and the `SessionStart` hook earlier builds wrote, keyed by an unsalted SHA-256 of the settings file path. This file may contain credentials: an MCP entry can carry `args` and `env`, and `env` is where API keys live | Emptied when nothing is owed any more; the empty file is kept as a record that the notice was shown | Yes: it may contain credentials. Private to you and SYSTEM, and not shown by the CLI, `--json`, `doctor` or logs (`tests/test_registration_backup_is_product_state.py`) |

#### Left by earlier builds

Earlier builds kept more. This build does not read, write or delete it,
except the one read-only look at `modelquad.db` described above:

- `modelquad.db`, with `modelquad.db-wal` and `modelquad.db-shm` (the history
  of earlier builds);
- `pending-output\` (answers earlier builds kept);
- `logs\`;
- `usage-claude.json`, `usage-codex.json` and `quota-pace.json` in
  `%USERPROFILE%\.claude`, with their lock files.

Other files earlier builds left in the state folder, such as
`service-secret`, `service.pid`, `job-*.json`, `host-observation-claude.json`
and `host-integrations.json`, are not used either. If an earlier build
registered the `ModelQuad_Service` sign-in task with `modelquad service
install`, this build does not remove it; delete it in Task Scheduler.
`modelquad privacy` lists what earlier builds kept.

### The review ledger

`review-ledger.db`, schema version 3, defined in `review_ledger.py`. Every
table is local; nothing in it is sent anywhere. It holds no task text, no
file contents and no full answer.

| Table | What a row records |
|---|---|
| `runs` | One Codex call: time, a digest of the project folder (not its path), the task kind, why the call was made (the trigger), the code version (the first 12 characters of the `HEAD` commit and a digest of the sent files' digests), the outcome, the duration, Codex's input and output token counts, and the number of findings. The row is written when Codex starts, as `unfinished`, and replaced when the call ends; a call whose session closed in the middle keeps the `unfinished` row |
| `findings` | One finding Codex listed in the agreed one-line form: its reference, the repository-relative file and line or line range (an absolute path is dropped), severity, category, Codex's stated confidence and its short reason, the claim and the reasoning. Free text is redacted for credentials and paths and capped |
| `verdicts` | What Claude or you decided about a finding: the verdict, its evidence, the action, whether a fix was verified, an optional short note (redacted and capped) and who recorded it |
| `gate_events` | What a review gate did: time, the folder digest, a session pseudonym, the event and a file count |
| `packages` | What one call sent: the package id, the completion round and the reply it follows, the counts (files, bytes, files left out, files Codex said it needed), the model calls and Codex's cached input and reasoning token counts, and the manifest: relative paths, sizes, digest prefixes and the reasons files were left out. A manifest longer than 32,000 characters is not kept; its counts and id are |

The folder digest is the first 16 hex characters of an unsalted SHA-256 of
the folder path. The session pseudonym is a 16-character HMAC of the Claude
Code session id, keyed by the installation id; the raw session id is not
stored. These are pseudonyms: they hide a value from casual reading, not
from a targeted guess. Someone with a copy of the ledger can confirm a
guessed folder path, and with `identity.json` too, a guessed session id.
Deleting rows with SQLite does not by itself erase the bytes from disk.

Stored free text passes through `privacy.py`, so absolute paths become
`<path>` (`tests/test_privacy_paths.py`). A path under your home folder,
`AppData`, `TEMP`, OneDrive or the working folder is removed whole, even
when a folder name has a space. Elsewhere, the part of a path after its
first space can remain.

### What is not stored

| Claim | Where it is enforced |
|---|---|
| Prompts are not stored. The ledger keeps the package id, which covers a digest of the task text, not the text | `review_ledger.py`, `review.py` |
| Codex's answers are not stored. The review ledger keeps only the findings Codex lists in the agreed one-line form, redacted and capped, never the whole answer | `review_ledger.py` |
| The contents of files sent to Codex are not stored. They are read into memory, sent, and dropped | `execution/noshell.py` |
| The text of Codex's error output is not stored, in any mode | `execution/base.result_error`, `workerevidence.py` |
| Conversation transcripts are never opened | `adapters/review_reminder.py`, `cli_entry.py`; `tests/test_hook_transcript_boundary.py` |
| Absolute paths do not reach the ledger | `privacy.py`, `review_ledger.py`; `tests/test_privacy_paths.py` |
| The raw Claude Code session id is not stored. It becomes a 16-character HMAC keyed by the installation id | `review_ledger.session_pseudonym` |
| Provider credential files (the names listed above) are not opened, copied or sent to Codex. Two kinds of copies can still hold secrets you put in configuration: `host-registration-backup.json` and the `*.modelquad-backup-*` files | `execution/noshell.py`, `adapters/registration.py` |
| Nothing is collected for telemetry | ModelQuad has no telemetry code. The usage report below is text you print and pass on yourself (`usage_report.py`; `tests/test_usage_report.py`) |

## The usage report

`modelquad review usage-report` reads the review ledger and prints a report
of counts: readable lines first, then the same data as one JSON object.
`--days N` sets the period (30 by default). `--out FILE` also writes the
JSON to the file you name, and to no other place; it does not replace a file
that exists unless you add `--force`. The report covers every project on
this computer together, with no figure per project.

ModelQuad does not send the report. The command opens the ledger read-only,
connects to nothing and turns nothing on. Passing the text on, for example
by e-mail or in a GitHub issue, is your own choice. If you do, the channel
shows who you are: an e-mail carries your address, and a public issue
carries your account.

The fields, and there are no others:

| Field | What it holds |
|---|---|
| `schema`, `kind` | The report format: `1` and `modelquad-usage-report` |
| `modelquad_version` | This build's version |
| `windows_major` | `10` or `11`; null when it cannot be read or is another version |
| `python_minor` | `3.11` to `3.14`, or `other` |
| `window_days` | The period, in days |
| `ledger_found` | Whether a review ledger exists on this computer |
| `calls` | The number of calls recorded in the period |
| `calls_by_outcome` | Calls per outcome: `answered`, `withheld`, `failed`, `refused`, `not_started`, `unfinished`, `other` |
| `calls_by_trigger` | Calls per trigger: `review_gate`, `pre_change`, `user`, `claude`, `other` |
| `calls_by_task_kind` | Calls per task kind: `review`, `exploration`, `architecture`, `other` |
| `completion_rounds` | The number of calls that were a completion round |
| `calls_by_files_sent` | Calls that sent `0`, `1-3`, `4-10` or `more` files, or an `unknown` number |
| `findings_by_severity` | Findings per severity: `high`, `medium`, `low`, `other` |
| `findings_by_verdict` | Findings counted as `modelquad review report` counts them: `confirmed_independent`, `confirmed_claude_only`, `refuted`, `undecidable`, `unjudged`, `fixed_and_verified` |
| `median_call_seconds` | The median call duration, to the whole second |
| `tokens_reported_by_codex` | The number of calls for which Codex reported a token count, and the input and output totals over those calls, each to the nearest thousand (a total under 500 shows as 0; null when no call has a count) |
| `gate_events` | Gate events per kind (the names in `review_ledger.GATE_EVENTS`), and `other` |

Every value is a number, true or false, null, or one of the fixed words
above. A count the ledger does not hold is null, not zero. The report holds
no code, task text, answer, finding text, note or error text, no file,
folder or repository name, no digest, no package, decision or installation
id, and no time finer than the period. The Codex CLI version is not in it:
the ledger does not record it. A small count can still point at a person
when few people use a build.

What the counts cover, and what they cannot show. The report counts only
the calls recorded in the review ledger on this computer: a call whose
record could not be written (the reply then says so) is missing, and so is
a row you removed. A finding counts as unjudged until a verdict is
recorded, and unjudged is not refuted. Reports reach the author only from
people who choose to pass them on, so a set of reports does not stand for
everyone who uses ModelQuad. The printed text says this too.

## The installation id

`identity.json` holds a random identifier (a version 4 UUID), created the
first time something needs it. It is derived from nothing: no hardware ids,
user names or machine names. It is a pseudonym, not anonymous: it stays the
same until you reset it. It stays on this computer.

It is the key for the pseudonyms of Claude Code session ids, so ModelQuad
can group one session's gate events without storing the raw id, and for the
hashes of your per-repository settings and grants.

`modelquad privacy --reset-identity` replaces it. After a reset, earlier
session pseudonyms can no longer be linked to new ones. A reset does not
delete the review ledger. Your per-repository settings and standing grants
are stored under keys made from the id and cannot follow a new one, so a
reset removes them and turns every standing grant off, and says so; Codex
calls then ask you each time until you set them again.

## How to see what is stored

- `modelquad privacy` (or `--json`): the state folder, the installation id,
  the network rule, how long each store keeps its data, what earlier builds
  kept, and the steps under "Everything" below.
- `modelquad review report`: the review ledger.
- `modelquad review usage-report`: the usage report, and nothing else.
- `modelquad settings`: your settings, and one line of history.
- The files themselves: the ledger is plain SQLite, the rest is JSON.

## How to delete it

- **The review ledger**: delete `review-ledger.db` in the state folder;
  ModelQuad creates an empty one on the next Codex call.
- **Everything**:
  1. Run `modelquad uninstall claude`. If you installed the plugin, also run
     `claude plugin uninstall modelquad`.
  2. Close the Claude Code sessions that use ModelQuad, then delete the state
     folder that `modelquad privacy` names.
  3. Delete the `*.modelquad-backup-*` files beside your Claude Code settings.
     Check the backups first: they can contain secrets you put in your
     configuration.

Deleting files is ordinary file deletion. ModelQuad does not overwrite
disk blocks.

Files earlier builds left outside the state folder are not covered by these
steps: delete `usage-claude.json`, `usage-codex.json`, `quota-pace.json`,
`usage-claude.json.lock`, `usage-codex.json.lock`,
`usage-refresh-claude.lock` and `usage-refresh-codex.lock` in
`%USERPROFILE%\.claude`, by those exact names: other `.lock` files there,
such as `daemon.lock`, belong to Claude Code. Delete the old
`%LOCALAPPDATA%\ModelQuad\` folder and the `ModelQuad_Service` task, if you
have them.

## Limits

- Protection is between you and other people, not between you and software
  you run. Any program running as your Windows user can read the state
  folder and the ledger, and run the CLI. An administrator can read every
  file. The state folder inherits your profile's access list, which on some
  computers lets other accounts read it; the file marked private above is
  limited to you and SYSTEM. See [SECURITY.md](SECURITY.md).
- The secret and path screening is pattern-based. It catches recognizable
  credentials and paths; it cannot prove that no sensitive fact appears in
  ordinary text. Choose the files you ask Codex to read.
- What Codex and Claude Code send to their providers, and what those
  providers keep, is outside ModelQuad's control.
