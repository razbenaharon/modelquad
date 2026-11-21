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
  is named `review` inside the plugin, so its tools are named
  `mcp__plugin_modelquad_review__<tool>` (for example
  `mcp__plugin_modelquad_review__delegate`) instead of
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

    pip install modelquad
    claude plugin marketplace add razbenaharon/modelquad#stable
    claude plugin install modelquad@modelquad
    modelquad doctor

`stable` is a branch of this repository that moves to each release once it
is on PyPI, so the plugin Claude Code fetches matches the package pip
installs. To work from a clone instead, run `pip install .` and
`claude plugin marketplace add ./` in it; a clone's `main` can be ahead of
the last release.

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

## Upgrading

With every Claude Code window and the Claude app closed, run
`python -m modelquad update` (not `modelquad update`: pip cannot replace the
running `modelquad.exe`; started that way it refuses and prints the exact
command). It refuses while `claude.exe`, `Claude.exe`, `modelquad-mcp.exe`
or another `modelquad.exe` is running. It upgrades the package the way it
was installed (pip from PyPI, or `git pull --ff-only` on a clean `main` and
`pip install .` in the clone), then reads `claude plugin list --json`. For
`modelquad@modelquad` installed for the user and enabled it runs
`claude plugin marketplace update modelquad` and
`claude plugin update modelquad@modelquad --scope user`, falling back to
`claude plugin uninstall --keep-data` and `claude plugin install` with the
same scope. A disabled plugin stays disabled and is not refreshed; a plugin
installed for a project is named with the command to run there. Either
makes the update exit with an error. Then it runs the checks of
`modelquad doctor`. `--dry-run` prints the steps only. pip, Git and
`claude plugin` go online themselves; ModelQuad does not
([NETWORK.md](../NETWORK.md)). The steps by hand are in the README.

Since 0.5.0 the server is named `review` inside the plugin and the tools
have no `modelquad_` prefix: `mcp__plugin_modelquad_review__delegate` where
it was `mcp__plugin_modelquad_modelquad__modelquad_delegate`. Claude Code
permission rules with the old names stop matching; `modelquad doctor` names
them. Until the plugin is refreshed, the new package can run under the
cached old plugin (`mcp__plugin_modelquad_modelquad__delegate`) or the new
plugin under an old package (`mcp__plugin_modelquad_review__modelquad_delegate`).
The hook and the review reminder recognise all of these; a deny rule holds
in every case only as the wildcards in the next section.

## The notice before first use

`modelquad install` prints what ModelQuad sends where before it writes
anything. Someone who installs only the plugin does not run it, so their
first `delegate` call returns that notice instead, and Codex does
not start; calling again, after Claude Code asks again, runs normally. An
install that printed the notice, including any 0.3.0 install, counts as
shown.

## Keeping ModelQuad out of a project

The plugin's tool names differ, so a deny rule needs both forms, and
the plugin's names before 0.5.0 as well:
`"deny": ["mcp__modelquad", "mcp__plugin_modelquad_review__*", "mcp__plugin_modelquad_modelquad__*"]`.
`modelquad doctor` names any Claude Code permission rule that still uses a
name from before 0.5.0. See
[RECOMMENDED-SETTINGS.md](RECOMMENDED-SETTINGS.md).

## Tested

- Live round 5 (2026-10-27), on an earlier build of the plugin: one server
  and one copy of each command, the approval prompt in the terminal and in
  the desktop app, a real Codex answer, both deny rules, and
  `modelquad install claude --plugin` leaving one copy of each.
- Live check (2026-11-04), on the earlier 0.4.0 plugin build with quota,
  handoff and the SessionStart reminder: in auto mode Claude Code showed its
  approval prompt before `delegate`, and after approval a read-only
  Codex review came back in about 40 s.
- Live check (2026-11-18), the reduced plugin described here, in headless
  Claude Code 2.1.318 (`claude -p`) with Codex 0.165.0-alpha.1.2, a throwaway
  project and a separate state folder: the four tools loaded; without a
  grant the Codex call was denied even with the tool allowed, because nobody
  could be asked; with the grant on for that folder, the preview, the
  first-call notice, a review and a verdict worked, the consultation gate
  stopped the first edit of a sensitive file once and the Stop gate blocked
  once, each followed by a real Codex call. Not checked: a click or Esc in
  the interactive window.
- Live check (2026-11-18), the same build plus the versioned notice, in
  headless Claude Code 2.1.318 started with `--permission-mode
  bypassPermissions` and `delegate` in `--allowedTools`, a
  throwaway project and a separate state folder. Without a grant, and again
  after the grant was turned off: Claude Code denied the call ("requires
  permission"); ModelQuad was not reached, nothing was recorded, Codex did
  not start. With the grant: the first call returned the notice and started
  nothing, the next ran a review (recorded as answered). With the grant and
  `modelquad off`: ModelQuad refused (`modelquad_off`). With the grant and a
  `.modelquad.toml` that turns Codex off: the grant did not apply, so the
  tool kept its approval mark and Claude Code denied the call; ModelQuad
  itself refuses such a call too (`repo_policy_codex_off`, checked through a
  protocol client). With a notice record from before the notice had a
  version: the updated notice, once, saying it changed; Codex did not start.
  Not checked: an interactive bypass-mode session (it stops at the folder
  trust question, which an automated run cannot answer), so whether Claude
  Code shows a prompt there or only holds the call back is not known.

## Maintenance

- Keep `plugin/.claude-plugin/plugin.json`'s `version` equal to the package
  version (`tests/test_plugin_prototype.py` checks it).
- Validate with `claude plugin validate plugin --strict` and
  `claude plugin validate .`.
