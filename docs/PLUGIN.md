# Claude Code plugin

The Claude Code plugin is how ModelQuad is installed. It lives in `plugin/`,
with a marketplace file in `.claude-plugin/marketplace.json`, so a checkout of
this repository is a local plugin marketplace. The plugin is not listed in any
plugin directory. `modelquad install claude` without `--plugin` remains
available for a setup without it (README, "Without the plugin").

## What the plugin contains

- **The ModelQuad MCP server** (`plugin/.mcp.json`), a copy of the entry
  `modelquad install claude` writes: it launches `modelquad-mcp`, the command
  pip puts on PATH. A plugin's server tools are named
  `mcp__plugin_modelquad_modelquad__<tool>` instead of
  `mcp__modelquad__<tool>`; the tools and their annotations are the same, so
  Claude Code asks the user before every call that would start Codex, unless
  the session started in a folder with the standing grant
  (`modelquad settings auto on`).
- **Seven skills**, copied from the package's own templates
  (`src/modelquad/adapters/templates/claude/skills/`). A plugin namespaces its
  skills, so they appear as `/modelquad:guide`, `/modelquad:review`,
  `/modelquad:handoff`, `/modelquad:quota`, `/modelquad:setup`,
  `/modelquad:split` and `/modelquad:settings`. All but the
  guide are commands only you start.
- **The SessionStart reminder hook** (`modelquad hook-pending claude`), the
  same one the installer adds. It reports retained answers and, when your
  setting for the repository differs from your global one, one line about
  Codex there ([REPO-POLICY.md](REPO-POLICY.md)); it never blocks or approves
  anything.
- **The review reminder and review gate** (`modelquad hook-review claude`, on
  PostToolUse, PreToolUse and Stop). It counts the files Claude changes under
  the session folder (as digests), reminds Claude of the review moment when
  it runs the tests, asks for a consultation before a sensitive file changes,
  and, only in a folder with the standing grant, stops Claude once to ask for
  a Codex review before a multi-file change is handed over. It never approves
  or denies a tool call ([PRIVACY.md](../PRIVACY.md)).

- **A quota band** (`plugin/hooks/quota-band.tsx`, a Claude Code hooks
  module). Where no status line is drawn (the desktop app's
  Code tab, an IDE, a phone), it shows Codex's 5-hour and weekly windows
  above the prompt, worded as the status line words them. It shows Codex
  only; Claude's own usage is in Claude's own settings. The figures are the
  reading ModelQuad's server keeps in `~/.claude/usage-codex.json`, re-read
  every minute and marked "(as of ...)" when older than 15 minutes. It names
  the plan Codex itself reports, from a fixed list ("Codex (Plus) 5h ..."),
  and says "Codex is off in this repository (.modelquad.toml)" when the
  project's repository file sets `allowed = false`. It draws nothing in the
  terminal, which has the status line. It only displays: it
  hooks no tool call, prompt or approval and starts nothing
  (`tests/test_plugin_prototype.py`); `claude plugin test plugin` runs its
  own tests.

`python scripts/sync_plugin.py` copies the skills and the server entry from
the templates; `tests/test_plugin_prototype.py` fails while a copy differs.

## What it deliberately does not contain

- **No status line.** A plugin cannot set Claude Code's status line. Run
  `modelquad install claude --plugin` for it (below).
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
    pip install ".[service]"
    claude plugin marketplace add .
    claude plugin install modelquad@modelquad
    modelquad install claude --plugin
    modelquad doctor

`modelquad install claude --plugin` is needed for the status line in a
terminal, and to take back the copies an earlier `modelquad install claude`
made. With `--plugin`, the
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

## Tested

- Live round 5 (2026-10-27): one server and one copy of each command, the
  approval prompt in the terminal and in the desktop app, a real Codex
  answer, both deny rules, and `modelquad install claude --plugin` leaving one
  copy of each.
- Live check (2026-11-04) on the installed 0.4.0 plugin: the terminal status
  line showed both tools' quotas; the SessionStart reminder stayed silent with
  no answers waiting; in auto mode Claude Code showed its approval prompt
  before `modelquad_delegate`, and after approval a read-only Codex review
  came back in about 40 s with the personal-account note. The first-call
  notice did not appear there because the regular install had already
  printed it; the plugin-only first call is covered by the tests.

## Maintenance

- Keep `plugin/.claude-plugin/plugin.json`'s `version` equal to the package
  version (`tests/test_plugin_prototype.py` checks it).
- Validate with `claude plugin validate plugin --strict` and
  `claude plugin validate .`.
