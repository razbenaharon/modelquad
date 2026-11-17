# Claude Code plugin

The Claude Code plugin is how ModelQuad is installed. It lives in `plugin/`,
with a marketplace file in `.claude-plugin/marketplace.json`, so a checkout of
this repository is a local plugin marketplace. The plugin is not listed in any
plugin directory. `modelquad install claude` without `--plugin` remains
available for a setup without it (README, "Without the plugin").

## What the plugin contains

- **The ModelQuad MCP server** (`plugin/.mcp.json`), a copy of the entry
  `modelquad install claude` writes: it launches `modelquad-mcp`, the command
  pip puts on PATH, with a tool timeout of 15 minutes. A plugin's server tools
  are named `mcp__plugin_modelquad_modelquad__<tool>` instead of
  `mcp__modelquad__<tool>`; the four tools and their annotations are the
  same, so Claude Code asks the user before every call that would start
  Codex, unless the session started in a folder with the standing grant
  (`modelquad settings auto on`).
- **Four skills**, copied from the package's own templates
  (`src/modelquad/adapters/templates/claude/skills/`). A plugin namespaces its
  skills, so they appear as `/modelquad:guide`, `/modelquad:review`,
  `/modelquad:setup` and `/modelquad:settings`. The guide is the one Claude
  reads; the other three are commands only you start.
- **The review reminder and review gates** (`plugin/hooks/hooks.json`:
  `modelquad hook-review claude`, on PostToolUse, PreToolUse and Stop). It
  counts the files Claude changes under the session folder (as digests) and
  reminds Claude of the review moment when it runs the tests. Only in a
  folder with the standing grant, it stops the first edit of a sensitive file,
  or the edit that makes the change three files, once until Codex is
  consulted, and stops Claude once to ask for a Codex review before a
  multi-file change is handed over. It never approves a tool call, and it
  does nothing while ModelQuad is off (`modelquad off`)
  ([PRIVACY.md](../PRIVACY.md)).

`python scripts/sync_plugin.py` copies the skills and the server entry from
the templates; `tests/test_plugin_prototype.py` fails while a copy differs.

## What it deliberately does not contain

- **No SessionStart hook, status line or quota display.** This build reads no
  quota and keeps no answers to report at session start.
- **No permission rules, agents, output styles or settings.** Every call that
  starts Codex keeps Claude Code's approval prompt, unless you turned the
  standing grant on for that folder (`modelquad settings auto on`) and added
  the allow rule it prints.
- **No executables.** The plugin needs the `modelquad` package installed with
  pip; the server and the hook run its `modelquad-mcp` and `modelquad`
  commands.

## Installing with the plugin

    git clone https://github.com/razbenaharon/modelquad
    cd modelquad
    pip install .
    claude plugin marketplace add .
    claude plugin install modelquad@modelquad
    modelquad doctor

`modelquad install claude --plugin` is needed only if an earlier
`modelquad install claude` wrote ModelQuad into Claude Code's user
configuration. It removes ModelQuad's own server entry and commands, which
the plugin now supplies, and what earlier builds wrote and this one no longer
has (the quota status line, the `SessionStart` and `PreToolUse` hooks, the
handoff, quota and split commands, the `CLAUDE.md` block and the global
timeout setting). Each removal comes with a backup, and only when the item is
provably ModelQuad's: a `modelquad` server entry the install did not write
stays, and the report says so. The choice is saved in `preferences.json`.
`modelquad install claude --no-plugin` goes back to the regular install.

`modelquad doctor` checks whether ModelQuad is connected to Claude Code: the
plugin from Claude Code's own plugin record
(`~/.claude/plugins/installed_plugins.json`, read-only), or the regular
install. Claude Code does not document that file as stable, so an unreadable
record reads as unknown, never as installed.

## The notice before first use

`modelquad install` prints what ModelQuad sends where before it writes
anything. Someone who installs only the plugin does not run it, so their
first `modelquad_delegate` call returns that notice instead, and Codex does
not start; calling again, after Claude Code asks again, runs normally. An
install that printed the notice, including any 0.3.0 install, counts as
shown.

## Keeping ModelQuad out of a project

The plugin's tool names differ, so a deny rule needs both forms:
`"deny": ["mcp__modelquad", "mcp__plugin_modelquad_modelquad__*"]`. See
[RECOMMENDED-SETTINGS.md](RECOMMENDED-SETTINGS.md).

## Tested

- Live round 5 (2026-10-27), on an earlier build of the plugin: one server
  and one copy of each command, the approval prompt in the terminal and in
  the desktop app, a real Codex answer, both deny rules, and
  `modelquad install claude --plugin` leaving one copy of each.
- Live check (2026-11-04), on the earlier 0.4.0 plugin build with quota,
  handoff and the SessionStart reminder: in auto mode Claude Code showed its
  approval prompt before `modelquad_delegate`, and after approval a read-only
  Codex review came back in about 40 s.
- No live run of the reduced plugin described here is recorded yet. Its
  hooks and first-call notice are covered by the test suite, which uses a
  stand-in for Codex.

## Maintenance

- Keep `plugin/.claude-plugin/plugin.json`'s `version` equal to the package
  version (`tests/test_plugin_prototype.py` checks it).
- Validate with `claude plugin validate plugin --strict` and
  `claude plugin validate .`.
