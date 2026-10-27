# Recommended settings

Plain-language advice for using ModelQuad safely, from a single developer to
an organization. Each section says what to do and why. None of it is required
to use ModelQuad.

## Do not use bypass mode on work code

Claude Code's bypass-permissions mode (`--dangerously-skip-permissions`) lets
Claude run commands and edit files without asking you. ModelQuad still asks
before every Codex call, even in that mode, but everything else Claude does
would happen without a prompt. On code that matters (your employer's, a
client's, anything you cannot easily restore) use the default mode, or plan
mode when you only want ideas.

## Keep ModelQuad out of a sensitive repository

To make sure Claude Code never calls ModelQuad in one project, add a deny
rule to that project's `.claude/settings.json`:

```json
{
  "permissions": {
    "deny": ["mcp__modelquad", "mcp__plugin_modelquad_modelquad__*"]
  }
}
```

`mcp__modelquad` covers every ModelQuad tool from `modelquad install
claude`. The ModelQuad plugin's server names its tools
`mcp__plugin_modelquad_modelquad__...`, so the second rule covers those; keep
both, and the rule holds however ModelQuad was installed. Commit the file so the rule
applies to everyone who opens the repository, or put the same rule in
`.claude/settings.local.json` to apply it only on your computer. Other
projects keep ModelQuad.

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

By default Claude offers a Codex opinion and calls Codex only when you agree
or ask. `modelquad settings proactive on` lets Claude decide on its own; Claude
Code still asks you before each call. `modelquad settings proactive off` goes
back to the default. The change applies to new Claude Code sessions.

## Turn off model training in ChatGPT and Claude

ModelQuad passes text between the two providers, and each provider's terms
forbid using its output to train the other's models. Turn off model training
with both while you use ModelQuad:

- ChatGPT data controls: <https://chatgpt.com/#settings/DataControls>
- Claude privacy settings: <https://claude.ai/settings/data-privacy-controls>

Opting out does not cover feedback you submit or conversations flagged for
safety review.

## Code that belongs to an employer or a client

Before you let Claude Code use ModelQuad on such code, check that you may send
it to OpenAI (through your Codex CLI) as well as to Anthropic. If you may not,
deny ModelQuad in that repository (see above).

## Turn off the background quota read

While Claude Code runs ModelQuad, ModelQuad asks your Codex CLI for your Codex
quota about every 4 minutes, with no prompt. To stop that, set the environment
variable `MODELQUAD_CODEX_QUOTA_REFRESH` to `0` and restart Claude Code. In
PowerShell, for your user account:

```powershell
[Environment]::SetEnvironmentVariable('MODELQUAD_CODEX_QUOTA_REFRESH', '0', 'User')
```

Codex's quota then updates only when you run `modelquad usage-refresh`.

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
  `"permissions": {"deny": ["mcp__modelquad", "mcp__plugin_modelquad_modelquad__*"]}`.

Both of these also match by a name, which a user can choose. Claude Code's
documentation warns that a `serverName` entry is a label, not a security
control; use `serverCommand` when the block must hold. To decide exactly
which servers may run at all, use an allowlist with
`allowManagedMcpServersOnly`, or a fixed `managed-mcp.json`.

The guarantee that a person approves each Codex call is Claude Code's own
approval prompt: both tools that start Codex are marked as requiring user
interaction, which no allow rule or mode skips. The extra confirmation form
the handoff tool shows is a second check, not the guarantee.

See Claude Code's documentation on managed settings and on controlling MCP
server access (`managed-mcp`) for where the managed settings file lives on
each system.
