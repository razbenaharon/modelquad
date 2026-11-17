# Claude Code adapter

`modelquad install claude` writes the packaged templates from
`src/modelquad/adapters/templates/claude/`; this directory intentionally
does not duplicate them.

- The four skills in `skills/` (`modelquad` and the commands `modelquad-review`,
  `modelquad-setup` and `modelquad-settings`) become
  `~/.claude/skills/<name>/SKILL.md`.
- `mcp/mcp-servers.json` is merged into `~/.claude.json` (a sibling of
  `~/.claude/`) as the `modelquad` MCP server entry, with its own tool
  `timeout`, without removing other servers.

The model guidance is the MCP server's `instructions`
(`src/modelquad/adapters/server_instructions.py`), so install writes nothing
into `~/.claude/CLAUDE.md`. This install adds no hooks and no status line.
Install and uninstall also take back what earlier versions added: the quota
status line, the `SessionStart` and `PreToolUse` hooks in
`~/.claude/settings.json`, the handoff, quota and split commands, the
`CLAUDE.md` managed block and an owned `env.MCP_TOOL_TIMEOUT`, each only when
it is ModelQuad's own. Install reads every file first, keeps a full backup of
each file it changes, and skips links and files it cannot read. It does not
follow `CLAUDE_CONFIG_DIR`.

Install to the default Claude Code directory with:

```sh
modelquad install claude
```

Use a different client configuration directory with
`modelquad install claude --target-dir <directory>`, and preview either with
`--dry-run`. With the Claude Code plugin, `modelquad install claude --plugin`
removes the server entry and the commands this install wrote, because the
plugin supplies them, and `--no-plugin` goes back to the regular install
(see `docs/PLUGIN.md`). `modelquad uninstall claude` removes what install
wrote and puts back a `modelquad` entry that install replaced. The README at
the repository root lists every file and setting install changes.
