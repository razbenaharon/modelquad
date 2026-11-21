# The repository file: `.modelquad.toml`

A repository can carry a small file named `.modelquad.toml` that limits what
ModelQuad does with Codex in that repository. A team commits it once, and it
applies to everyone who opens the repository with ModelQuad.

The file can only make things narrower. It can turn Codex off, remove task
kinds, lower the file limits, require a work Codex account (which this build
cannot confirm, so it refuses every call; see below), and make Claude's
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
# "workspace": in this build, refuse every call (see below).
account = "any"

[offers]
# "on_request": Claude does not offer Codex here; it calls it only when you ask.
codex = "default"
```

- `account = "workspace"` was meant to allow Codex only when Codex itself
  reports a work plan. This build does not read which Codex account is
  signed in, so it can never confirm one, and the rule refuses every Codex
  call in the repository (`repo_policy_account_workspace_required`).
  `modelquad doctor` says so when it finds the line. Leave `account` out, or
  set it to `"any"`, unless you want Codex off there.
- `[offers] codex = "on_request"` asks Claude not to offer Codex on its own
  in the repository and to call it only when you ask. In this build it acts
  through the plugin's review reminder, which then neither prompts Claude to
  ask Codex nor to offer it there; the MCP server's instructions, which
  Claude reads when a session starts, do not know the repository. It applies
  even when you turned proactive use on, globally or for this repository. It
  does not apply in a folder your standing grant covers: there you chose that
  Claude consults Codex on its own, and the file's other limits (Codex off,
  task kinds, file limits, account) still hold.

## What it can never do

ModelQuad treats the file as untrusted, because anyone can commit one to any
repository you might clone. So the file cannot:

- approve a call. Claude Code still asks you before every Codex call, with or
  without the file, unless you turned on the standing grant yourself
  (`modelquad settings auto on`). The file has no setting for approval, the
  standing grant, permission rules or proactive mode, and ModelQuad never
  writes Claude Code's settings files.
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
alone does not refuse a call. `modelquad doctor` shows what the file sets,
and `status` reports how many keys were ignored, so a misspelt key
can be noticed.

## Examples

Turn Codex off in this repository:

```toml
schema = 1

[codex]
allowed = false
```

Reviews only, at most 8 small files:

```toml
schema = 1

[codex]
task_kinds = ["review"]
max_files = 8
max_total_kib = 32
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
| `repo_policy_account_workspace_required` | `account = "workspace"`; this build cannot confirm a work account, so every call is refused |
| `repo_policy_invalid` | not UTF-8 text, not valid TOML, a missing or unknown schema, or a wrong value |
| `repo_policy_unreadable` | a link, not a plain file, larger than 8 KiB, or unreadable |
| `repo_policy_file_outside_repo` | a file named for Codex sits in another Git repository (a nested clone or a submodule) below the working folder, whose own file and settings that call would skip |

A file applies to its own repository only. A call from a parent folder that
names a file inside a nested repository is refused rather than sent under the
parent's rules: ask from that repository's own folder.

The file's contents, its path and its key names are never stored.

## Your own setting for a repository

`modelquad settings repo <choice> [--project DIR]` records your own choice for
one repository (the current folder's when `--project` is left out); with no
choice it shows what applies there.

| Choice | Meaning |
|---|---|
| `off` | No Codex in this repository. Every Codex call is refused with `user_repo_codex_off`, before any process starts. |
| `offer-only` | Claude offers Codex when it would help and calls it when you agree or ask, even if proactive use is on globally. A standing grant that covers the folder outranks this: Claude then consults Codex on its own. |
| `on` | Claude may decide on its own to consult Codex in this repository. |
| `inherit` | Remove the entry; the global `modelquad settings proactive` applies. |

Only you can choose `on`: the repository file has no setting that turns
proactive use on. Claude Code still asks you before every Codex call whatever
you choose, unless you turned on the standing grant for the repository.
`off` outranks the grant.

The setting is stored in `preferences.json` in ModelQuad's state folder under
`repos`, keyed by a hash of the repository (HMAC keyed by this installation's
pseudonymous id, over the Git common folder, so every worktree of a repository
shares it). No path or folder name is stored. `modelquad privacy --reset-identity`
mints a new id, under which the stored entries would no longer match any
repository; the reset therefore removes them, turns every standing grant off
and tells you, so no grant goes on covering a folder you had turned off.

In a folder that is not a Git repository, `off` and `offer-only` also cover
the folders below it, unless you set one of those folders yourself; `on`
applies to that one folder only. A junction or link into a repository counts
as that repository, for this setting and for its `.modelquad.toml`.

### How the layers combine

1. ModelQuad's own rules always win: read-only, Codex's tool features
   switched off, an empty folder,
   your approval on every call (or the standing grant you turned on),
   loopback only, the file rules.
2. Restrictions (Codex allowed, task kinds, file limits, account) are the most
   restrictive of the layers: your `off` and the file each refuse on their own.
3. Who decides to consult Codex: your choice for the repository beats your
   global setting; then the file's `codex = "on_request"` can only lower it.
   Your standing grant, where it covers the folder, comes first: Claude
   consults Codex on its own there. Only `off` (yours or the file's) stops it.

### What Claude is told

The MCP server's instructions are built once per Claude Code session and do
not know the repository. The per-repository mode reaches Claude in two ways:

- a refused call: when your `off` or the file refuses Codex, the reply
  carries a fixed reason code and a fixed sentence;
- with the plugin, the review reminder: after a multi-file change it tells
  Claude to call Codex, to offer Codex in one sentence, or nothing, according
  to the mode that applies in the folder.

`status` and `modelquad settings` show what applies in a folder.
Earlier builds also added one line at session start through a `SessionStart`
hook; this build has no such hook.

The reminder is advisory. To choose its wording it reads only
`preferences.json`, the installation id, the `.git` entry of the repository
and its `.modelquad.toml`; it never opens the conversation transcript,
starts no process and never approves anything. The refusals above are
enforced at the tools whatever the reminder said.
