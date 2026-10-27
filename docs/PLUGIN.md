# Claude Code plugin (experimental, not published)

This repository carries a Claude Code plugin in `plugin/` and a marketplace
file in `.claude-plugin/marketplace.json`. It is **not** the supported install
yet: `modelquad install claude` remains the documented way to set ModelQuad
up, and nothing here is published to any plugin directory.

## What the plugin contains

- **The ModelQuad MCP server** (`plugin/.mcp.json`), a copy of the entry
  `modelquad install claude` writes: it launches `modelquad-mcp`, the command
  pip puts on PATH. A plugin's server tools are named
  `mcp__plugin_modelquad_modelquad__<tool>` instead of
  `mcp__modelquad__<tool>`; the tools and their annotations are the same, so
  Claude Code asks the user before every call that would start Codex.
- **Five skills**, copied from the package's own templates
  (`src/modelquad/adapters/templates/claude/skills/`). A plugin namespaces its
  skills, so they appear as `/modelquad:guide`, `/modelquad:review`,
  `/modelquad:handoff`, `/modelquad:quota` and `/modelquad:setup`. All but the
  guide are commands only you start.
- **The SessionStart reminder hook** (`modelquad hook-pending claude`), the
  same one the installer adds. It only reports retained answers; it never
  blocks or approves anything.

`python scripts/sync_plugin.py` copies the skills and the server entry from
the templates; `tests/test_plugin_prototype.py` fails while a copy differs.

## What it deliberately does not contain

- **No status line.** A plugin cannot set Claude Code's status line. Run
  `modelquad install claude --plugin` for it (below).
- **No permission rules, agents, output styles or settings.** Every call that
  starts Codex keeps Claude Code's approval prompt.
- **No executables.** The plugin needs the `modelquad` package installed with
  pip; the server and the hook run its `modelquad-mcp` and `modelquad`
  commands.

## Installing with the plugin

    pip install <this repository>
    claude plugin install modelquad --marketplace razbenaharon/modelquad
    modelquad install claude --plugin

The last step is needed for the status line in a terminal, and to take back
the copies an earlier `modelquad install claude` made. With `--plugin`, the
install keeps only the status line and removes ModelQuad's own server entry,
commands and reminder, each with a backup, and only when it is provably
ModelQuad's: a `modelquad` server entry the install did not write stays, and
the report says so. The choice is saved, so `modelquad repair` keeps it.
`modelquad install claude --no-plugin` goes back to the regular install.

`modelquad doctor` reports the plugin's registration from Claude Code's own
plugin record (`~/.claude/plugins/installed_plugins.json`, read-only), and
warns when the plugin and the regular install are both present, because
Claude Code would then run the server, the commands and the reminder twice.
Claude Code does not document that file as stable, so an unreadable record
reads as unknown, never as installed.

## The notice before first use

`modelquad onboard` and `modelquad install` print what ModelQuad sends where
before they write anything. Someone who installs only the plugin runs
neither, so their first call that would start Codex (`modelquad_delegate` or
`modelquad_handoff`) returns that notice instead, and Codex does not start;
calling again, after Claude Code asks again, runs normally. The background
Codex quota read the notice describes waits for it too. An install that
printed the notice, including any 0.3.0 install, counts as shown.

## Keeping ModelQuad out of a project

The plugin's tool names differ, so a deny rule needs both forms:
`"deny": ["mcp__modelquad", "mcp__plugin_modelquad_modelquad__*"]`. See
[RECOMMENDED-SETTINGS.md](RECOMMENDED-SETTINGS.md).

## Before this becomes a supported install

- Live test round 5 (2026-10-27) passed: one server and one copy of each
  command, the approval prompt in the terminal and in the desktop app, a
  real Codex answer, both deny rules, and `modelquad install claude --plugin`
  leaving one copy of each. Still to see live: the status line, the
  SessionStart reminder, and the notice on a first call.
- Keep `plugin/.claude-plugin/plugin.json`'s `version` equal to the package
  version (`tests/test_plugin_prototype.py` checks it).
- Validate with `claude plugin validate plugin --strict` and
  `claude plugin validate .`.
