# Recommended settings

Plain-language advice for choosing what ModelQuad may do, from a single
developer to an organization. Each section says what to do and why. None of it is required
to use ModelQuad.

## Do not use bypass mode on work code

Claude Code's bypass-permissions mode (`--dangerously-skip-permissions`) lets
Claude run commands and edit files without asking you. Without the standing
grant, ModelQuad marks its Codex call as needing your approval, and in that
mode Claude Code still held the call back in our tests (Claude Code 2.1.318,
non-interactive runs, 2026-11-18; whether the interactive window shows a
prompt there was not checked). With the grant on (`modelquad settings auto
on`), the call runs without one. Everything else Claude does would happen
without a prompt. On code that matters (your employer's, a
client's, anything you cannot easily restore) use the default mode, or plan
mode when you only want ideas.

## Keep ModelQuad out of a sensitive repository

To make sure Claude Code never calls ModelQuad in one project, add a deny
rule to that project's `.claude/settings.json`:

```json
{
  "permissions": {
    "deny": [
      "mcp__modelquad", "mcp__plugin_modelquad_review__*",
      "mcp__plugin_modelquad_modelquad__*"
    ]
  }
}
```

`mcp__modelquad` covers every ModelQuad tool from `modelquad install
claude`. The ModelQuad plugin names its tools
`mcp__plugin_modelquad_review__...` (since 0.5.0), so the second rule covers
those; the third covers the plugin's names before 0.5.0
(`mcp__plugin_modelquad_modelquad__...`), so the rule also holds on a
computer that still runs an older build. Keep all three, and the rule holds
however ModelQuad was installed. A deny rule written for an older build that
names only the old plugin names no longer covers the plugin's tools:
`modelquad doctor` names such a rule and the name to use instead. Commit the file so the rule
applies to everyone who opens the repository, or put the same rule in
`.claude/settings.local.json` to apply it only on your computer. Other
projects keep ModelQuad.

These rules block ModelQuad, not Codex. Once ModelQuad's tools are gone,
Claude may offer to run the Codex CLI itself from a shell (it asks first),
and that run has none of ModelQuad's limits: Codex's own tools and sandbox
settings apply. To keep Claude from starting Codex in that project too, add
shell rules next to the others:

```json
{
  "permissions": {
    "deny": [
      "mcp__modelquad", "mcp__plugin_modelquad_review__*",
      "mcp__plugin_modelquad_modelquad__*",
      "Bash(codex *)", "PowerShell(codex *)"
    ]
  }
}
```

Claude Code matches a shell rule against the command as Claude writes it,
so it stops `codex ...` but not Codex started by a full path or from inside
another shell; Claude Code's documentation says such a rule is not a
security boundary. If the code must never reach OpenAI, do not run Codex
in that folder yourself either.

## Narrow Codex use in one repository

To keep ModelQuad's tools but limit what Codex does in one repository, commit
a `.modelquad.toml` file at the repository's top folder. It can turn Codex off
there, allow only some task kinds (for example reviews), lower how many files
and how much text one call sends. It
can only narrow: it never approves a call or adds files, and if the file is
broken ModelQuad refuses Codex in that repository until it is fixed. The format
and examples are in [REPO-POLICY.md](REPO-POLICY.md).

## Block secret files with Read deny rules

Tell Claude Code which files it must not read:

```json
{
  "permissions": {
    "deny": ["Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)"]
  }
}
```

Claude Code then refuses to read those files, and ModelQuad honours the same
Read deny rules when it collects the files a request names for Codex: a file
Claude Code may not read is not sent to Codex either. ModelQuad also skips
credential files such as `.env` and `auth.json` on its own and replaces
secret-looking values, but a deny rule is the rule you control.

## CLAUDE.md is guidance, not enforcement

A sentence in `CLAUDE.md` such as "never send this folder to Codex" is an
instruction Claude usually follows. It does not stop anything. For something
that must not happen, use a deny rule (above): Claude Code enforces those.

## Choose who decides to ask Codex

By default Claude decides on its own to ask Codex at the moments its
instructions name, such as a multi-file change that is done or a bug that
survived one fix. Claude Code still asks you before each call, unless you
turned on the standing grant for that folder. `modelquad settings proactive
off` makes Claude offer instead and call Codex only when you agree or ask;
`modelquad settings proactive on` goes back to the default. The change applies
to new Claude Code sessions.

The standing grant (`modelquad settings auto on --yes`, for one repository or
with `--global`) lets `delegate` run without Claude Code's prompt
for each call, once you add the allow rule it prints or choose "don't ask
again". You turn it on yourself, in a terminal or by choosing automatic in
`/modelquad:settings`; Claude never turns it on by itself.
`modelquad settings auto off` turns it off.

## Turn ModelQuad off everywhere

`modelquad off` stops every Codex call on this computer, whatever the other
settings say, and the plugin's hooks go quiet. `modelquad on` turns it back
on. Only you turn it on again; Claude is told not to.

## Turn off model training in ChatGPT and Claude

ModelQuad passes text between the two providers. OpenAI's terms forbid using
its output to develop models that compete with it, and Anthropic's forbid
using Claude's output to train any AI model without its permission. Turn off
model training with both while you use ModelQuad:

- ChatGPT data controls: <https://chatgpt.com/#settings/DataControls>
- Claude privacy settings: <https://claude.ai/settings/data-privacy-controls>

Opting out does not cover feedback you submit or conversations flagged for
safety review.

## Code that belongs to an employer or a client

Before you let Claude Code use ModelQuad on such code, check that you may send
it to OpenAI (through your Codex CLI) as well as to Anthropic. If you may not,
deny ModelQuad in that repository (see above).

## For administrators

Claude Code's managed settings, which users cannot override, can keep
ModelQuad off a whole fleet:

- block the server by the command that starts it, with a `deniedMcpServers`
  entry: `"deniedMcpServers": [{"serverCommand": ["modelquad-mcp"]}]`. This
  is the command both `modelquad install claude` and the ModelQuad plugin
  register. Claude Code compares
  the command and its arguments exactly, so a server started another way (a
  full path, or `python -m`) needs its own entry, or
- deny its tools with
  `"permissions": {"deny": ["mcp__modelquad", "mcp__plugin_modelquad_review__*", "mcp__plugin_modelquad_modelquad__*"]}`
  (the last one covers the plugin's tool names before 0.5.0).

Both of these also match by a name, which a user can choose. Claude Code's
documentation warns that a `serverName` entry is a label, not a security
control; use `serverCommand` when the block must hold. To decide exactly
which servers may run at all, use an allowlist with
`allowManagedMcpServersOnly`, or a fixed `managed-mcp.json`.

What makes a person approve each Codex call is Claude Code's own
approval prompt: `delegate`, the one tool that starts Codex, is
marked as requiring user interaction, which no allow rule or mode skips,
unless you turn the standing grant on for a folder
(`modelquad settings auto on`): sessions there register
`delegate` without that mark, and your allow rule applies.

See Claude Code's documentation on managed settings and on controlling MCP
server access (`managed-mcp`) for where the managed settings file lives on
each system.
