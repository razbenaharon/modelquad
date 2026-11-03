# The repository file: `.modelquad.toml`

A repository can carry a small file named `.modelquad.toml` that limits what
ModelQuad does with Codex in that repository. A team commits it once, and it
applies to everyone who opens the repository with ModelQuad.

The file can only make things narrower. It can turn Codex off, remove task
kinds, lower the file limits, require a work Codex account, and make Claude's
offers rarer. It cannot allow anything that ModelQuad does not already allow.

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
# "workspace": refuse unless the Codex account is a work account.
account = "any"

[offers]
# "on_request": Claude offers Codex only when you ask.
codex = "default"
```

- `account = "workspace"` allows Codex only when the Codex account is known to
  be a work account: either Codex reports a work plan, or you set one with
  `modelquad settings plan codex <plan>`. When the account kind is personal or
  not known, the call is refused.
- `[offers] codex = "on_request"` is read and checked in this version, but it
  does not yet change what Claude is told. Use `modelquad settings proactive
  off` (the default) for that today.

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
alone does not refuse a call.

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
| `repo_policy_account_workspace_required` | `account = "workspace"` and the Codex account is not known to be a work account |
| `repo_policy_invalid` | not valid TOML, a missing or unknown schema, or a wrong value |
| `repo_policy_unreadable` | a link, not a plain file, larger than 8 KiB, or unreadable |

Only the reason code is written to ModelQuad's local history. The file's
contents, its path and its key names are never stored.
