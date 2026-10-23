# Claude Code adapter

`modelquad install claude` writes the packaged templates from
`src/modelquad/adapters/templates/claude/`; this directory intentionally
does not duplicate them.

- `skills/modelquad/SKILL.md` becomes `~/.claude/skills/modelquad/SKILL.md`.
- `mcp/mcp-servers.json` is merged into `~/.claude.json` (a sibling of
  `~/.claude/`) as the `modelquad` MCP server entry, with its own tool
  `timeout`, without removing other servers.

The model guidance is the MCP server's `instructions`
(`src/modelquad/adapters/server_instructions.py`), so install writes nothing
into `~/.claude/CLAUDE.md`. Install also adds the `SessionStart` reminder hook
and a status line (unless you already have your own) to
`~/.claude/settings.json`, and takes back what earlier versions added: the
`CLAUDE.md` managed block, the `PreToolUse` gate hook and an owned
`env.MCP_TOOL_TIMEOUT`. It reads every file first, keeps a full backup of each
file it changes, and skips links and files it cannot read. It does not follow
`CLAUDE_CONFIG_DIR`.

Install to the default Claude Code directory with:

```sh
modelquad install claude
```

Use a different client configuration directory with
`modelquad install claude --target-dir <directory>`, and preview either with
`--dry-run`. `modelquad uninstall claude` removes what install wrote and puts
back a `modelquad` entry that install replaced. The README at the repository
root lists every file and setting install changes.
