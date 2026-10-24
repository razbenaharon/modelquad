# Claude Code plugin (experimental, not published)

This repository carries a prototype Claude Code plugin in `plugin/` and a
marketplace file in `.claude-plugin/marketplace.json`. It is **not** part of
the supported install yet: `modelquad install claude` remains the way to set
ModelQuad up, and nothing here is published to any plugin directory.

## What the plugin contains

- Five skills, copied from the package's own templates
  (`src/modelquad/adapters/templates/claude/skills/`) by
  `python scripts/sync_plugin.py`. A plugin namespaces its skills, so they
  appear as `/modelquad:guide`, `/modelquad:review`, `/modelquad:handoff`,
  `/modelquad:quota` and `/modelquad:setup`. All but the guide are commands
  only you start.
- The SessionStart reminder hook (`modelquad hook-pending claude`), the same
  one the installer adds. It only reports retained answers; it never blocks
  or approves anything.

## What it deliberately does not contain

- **No MCP server.** The `modelquad` MCP server and the status line still come
  from `modelquad install claude`. A server bundled in a plugin gets new tool
  names (`mcp__plugin_modelquad_modelquad__*`), which would silently stop
  existing deny rules such as `mcp__modelquad` from matching, and a plugin
  cannot set Claude Code's main status line.
- **No permission rules, agents, output styles or settings.** Every call that
  starts Codex keeps Claude Code's approval prompt.
- **No executables.** The plugin needs the `modelquad` package installed with
  pip; the hook calls its `modelquad` command.

## Before this becomes a supported install

- A live test that the plugin's hook and commands behave the same as the
  installed ones.
- **Known issue:** enabling the plugin and also running
  `modelquad install claude` (still needed for the MCP server) gives two
  copies of the commands and runs the SessionStart reminder twice, because
  plugin hooks and settings hooks are separate. Do not enable the plugin until
  the installer can leave the skills and the hook to it.
- Decide how `modelquad install claude` and the plugin share the skills and
  the hook, so a user has one copy of each.
- Keep `plugin/.claude-plugin/plugin.json`'s `version` equal to the package
  version (`tests/test_plugin_prototype.py` checks it).
- Validate with `claude plugin validate plugin --strict` and
  `claude plugin validate .`.
