# The repository file: `.modelquad.toml`

A repository can carry a small file named `.modelquad.toml` that limits what
ModelQuad does with Codex in that repository. A team commits it once, and it
applies to everyone who opens the repository with ModelQuad.

The file can only make things narrower. It can turn Codex off, remove task
kinds, lower the file limits, require a work Codex account, and make Claude's
offers rarer. It cannot allow anything that ModelQuad does not already allow.

There are two layers per repository:

- this file, checked into the repository, shared by everyone, and treated as
  untrusted: it can only narrow;
- your own setting, `modelquad settings repo <on|off|offer-only|inherit>`, kept
  on your computer only (see "Your own setting for a repository" below).

## Where it goes

Put it in the top folder of the repository, the folder Git calls the top
level. Outside Git, ModelQuad looks only in the working directory itself.
ModelQuad never looks in a parent folder, never follows a link to find the
file, and reads it again on every Codex call; nothing from it is kept between
calls.

## What it can contain

This is the whole format (schema 1). Every line except `schema = 1` is
optional; a missing line keeps ModelQuad's own value.

```toml
schema = 1

[codex]
# false: ModelQuad refuses every Codex call in this repository.
allowed = true
# Which kinds of Codex task are allowed here: any of
# "review", "exploration", "architecture". Leaving a kind out removes it.
task_kinds = ["review", "architecture"]
# How many files one call may send (0 to 32; ModelQuad's own limit is 32).
max_files = 16
# How much file text one call may send, in KiB (0 to 128; ModelQuad's own limit is 128).
max_total_kib = 64
# "workspace": refuse unless Codex itself reports a work (workspace) plan.
account = "any"

[offers]
# "on_request": Claude does not offer Codex here; it calls it only when you ask.
codex = "default"
```

- `account = "workspace"` allows Codex only when Codex itself reports a work
  plan (team, business, enterprise or education) in its quota read. A plan you
  set with `modelquad settings plan codex <plan>` does not count here, because
  a statement is not evidence. The reading must be a successful one from the
  last 5 minutes (the background refresh keeps it that recent; `modelquad
  usage-refresh` reads it again), because an older one may come from another
  login. When Codex reports a personal plan, a plan name ModelQuad does not
  know exactly, or no recent reading, the call is refused. The check runs at
  the MCP tools and again right before the files are packed.
- `[offers] codex = "on_request"` tells Claude, through a one-line reminder
  when a Claude Code session starts in the repository, not to offer Codex on
  its own and to call it only when you ask. It applies even when you turned
  proactive use on, globally or for this repository.

## What it can never do

ModelQuad treats the file as untrusted, because anyone can commit one to any
repository you might clone. So the file cannot:

- approve a call. Claude Code still asks you before every Codex call, with or
  without the file. The file has no setting for approval, permission rules or
  proactive mode, and ModelQuad never writes Claude Code's settings files.
- add files. It cannot name files to send or include folders; you still choose
  the files on each call. To keep files out, use `.modelquadignore` or Claude
  Code's Read deny rules (see [RECOMMENDED-SETTINGS.md](RECOMMENDED-SETTINGS.md)).
- change how Codex runs. The model, reasoning effort, sandbox, tools and the
  command ModelQuad starts Codex with are fixed in ModelQuad's code. A cloned
  repository cannot make Codex spend more of your usage or run with more
  access.
- raise a limit. A value above ModelQuad's own limit is an error, not a wider
  limit.
- talk to Claude. The file has no text field. When it refuses a call, Claude
  sees a fixed reason code and a fixed sentence, never the file's contents.

Other keys, such as `allow`, `auto_approve`, `permissions`, `proactive`,
`trusted`, `include`, `files`, `model`, `effort` or `sandbox`, are unknown to
schema 1. ModelQuad ignores them and acts on none of them. An unknown key
alone does not refuse a call. `modelquad doctor` lists the keys it ignored, so
a misspelt key does not go unnoticed, and shows what the file sets.

## Examples

Turn Codex off in this repository:

```toml
schema = 1

[codex]
allowed = false
```

Reviews only, at most 8 small files, and only on a work account:

```toml
schema = 1

[codex]
task_kinds = ["review"]
max_files = 8
max_total_kib = 32
account = "workspace"
```

## When the file is broken

If ModelQuad cannot use the file, it refuses every Codex call in the
repository until the file is fixed or removed. A typo therefore cannot quietly
switch a team's "Codex off" back on. The file is refused when it:

- is a link, is not a plain file, or sits in a linked folder;
- is larger than 8 KiB, or is not UTF-8 text;
- is not valid TOML;
- has no `schema` line, or a schema other than 1;
- has a value of the wrong type (for example `allowed = "no"`) or out of range
  (for example `max_files = 64` or `task_kinds = ["implementation"]`).

A refused call starts no Codex process. The reply carries one reason code and
one line that names the file and says how to fix it, for example:

    repo_policy_invalid: This repository's .modelquad.toml has a value ModelQuad
    does not accept (codex.max_files), so ModelQuad refuses Codex here until it
    is fixed. Use the schema 1 format in ModelQuad's docs/REPO-POLICY.md, or
    remove the file.

The reason codes are:

| Code | Meaning |
|---|---|
| `repo_policy_codex_off` | `[codex] allowed = false` |
| `repo_policy_kind_removed` | the task kind is not in `task_kinds` |
| `repo_policy_account_workspace_required` | `account = "workspace"` and Codex has not reported a work plan |
| `repo_policy_invalid` | not valid TOML, a missing or unknown schema, or a wrong value |
| `repo_policy_unreadable` | a link, not a plain file, larger than 8 KiB, or unreadable |
| `repo_policy_file_outside_repo` | a file named for Codex sits in another Git repository (a nested clone or a submodule) below the working folder, whose own file and settings that call would skip |

A file applies to its own repository only. A call from a parent folder that
names a file inside a nested repository is refused rather than sent under the
parent's rules: ask from that repository's own folder.

Only the reason code is written to ModelQuad's local history. The file's
contents, its path and its key names are never stored.

## Your own setting for a repository

`modelquad settings repo <choice> [--project DIR]` records your own choice for
one repository (the current folder's when `--project` is left out); with no
choice it shows what applies there.

| Choice | Meaning |
|---|---|
| `off` | No Codex in this repository. Every Codex call is refused with `user_repo_codex_off`, before any process starts. |
| `offer-only` | Claude offers Codex when it would help and calls it when you agree or ask, even if proactive use is on globally. |
| `on` | Claude may decide on its own to consult Codex in this repository. |
| `inherit` | Remove the entry; the global `modelquad settings proactive` applies. |

Only you can choose `on`: the repository file has no setting that turns
proactive use on. Claude Code still asks you before every Codex call whatever
you choose.

The setting is stored in `preferences.json` in ModelQuad's state folder under
`repos`, keyed by a hash of the repository (HMAC keyed by this installation's
pseudonymous id, over the Git common folder, so every worktree of a repository
shares it). No path or folder name is stored. `modelquad privacy --reset-identity`
mints a new id, after which the stored entries no longer match any
repository.

### How the layers combine

1. ModelQuad's own rules always win: read-only, no tools, an empty folder,
   your approval on every call, loopback only, the file rules.
2. Restrictions (Codex allowed, task kinds, file limits, account) are the most
   restrictive of the layers: your `off` and the file each refuse on their own.
3. Who decides to consult Codex: your choice for the repository beats your
   global setting; then the file's `codex = "on_request"` can only lower it.

### What Claude is told

The MCP server's instructions are built once per Claude Code session and do
not know the repository, so the per-repository mode reaches Claude through
ModelQuad's SessionStart reminder as one line, and only when it differs from
your global setting:

- "In this repository, you may ask Codex on your own; Claude Code still asks
  the user each time."
- "In this repository, offer Codex in one short sentence when it would help,
  and call it only when the user agrees or asks."
- "In this repository, do not offer Codex on your own; call it only when the
  user asks for it."
- "In this repository, Codex is off. Do not offer or call it."
- "In this repository, Codex is off until its .modelquad.toml is fixed. Do not
  offer or call it."

The reminder is advisory. It reads only `preferences.json`, the installation
id, the `.git` entry of the repository and its `.modelquad.toml`; it never
opens the conversation transcript, starts no process for this line, and never
blocks or approves anything. The refusals above are enforced at the tools
whatever the reminder said.
