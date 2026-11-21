# Working on ModelQuad

Build and test instructions for contributors and coding agents. What ModelQuad
does and how to use it is in [README.md](README.md).

**Developing this repository never uses ModelQuad itself.** Do not call any
`modelquad_*` MCP tool while working here; the tool under development is not
the instrument for developing it.

## Current state

- Version `0.5.0`, review ledger schema `3`.
  `python scripts/check.py docs` checks these against the code.
- One product, one flow: Claude Code asks the user's own Codex CLI for a
  read-only review of a named context package, and ModelQuad records what was
  sent, what was found and what Codex reported using.
- Native Windows only, not WSL. Install and the Codex launch refuse to run
  elsewhere.
- ModelQuad installs as a Claude Code plugin (`plugin/`): the plugin supplies
  the MCP server entry, the commands and the hooks.
  `modelquad install claude` is the install without the plugin (server and
  commands only, no hooks); `modelquad install claude --plugin` takes back
  those copies ([docs/PLUGIN.md](docs/PLUGIN.md)).
- One backend: the Codex CLI, under the user's own login, read-only. There
  is no direct API path, no second provider, no background service, no
  background job, no quota reading and no division of work.
- Four MCP tools (`mcp/server.py`): `review_package` (what a review
  would send; starts nothing), `delegate` (the review; the only tool
  that starts Codex), `review_verdict` (what became of a finding)
  and `status`. Inside the plugin the server is named `review`, so Claude
  Code shows `mcp__plugin_modelquad_review__<tool>`; without the plugin,
  `mcp__modelquad__<tool>` (`branding.tool_ids`). Before 0.5.0 they were
  `mcp__plugin_modelquad_modelquad__modelquad_<tool>` and
  `mcp__modelquad__modelquad_<tool>`; `modelquad doctor` names a Claude
  Code permission rule that still uses one (it reads those settings files
  and never writes them).
- `python -m modelquad update` (`updater.py`, `__main__.py`), user-started:
  refuses unless started as `python -m modelquad` (pip cannot replace a
  running `modelquad.exe`), in a worker, and while Claude Code, the Claude
  app, `modelquad-mcp.exe` or another `modelquad.exe` runs; otherwise pip
  (PyPI) or `git pull --ff-only` on a clean `main` and `pip install .`
  (clone) as child processes, then `claude plugin update` for an enabled
  user-scope plugin, the doctor checks and the version. Its children go
  online; ModelQuad's own process does not. Nothing updates by itself.
- After an upgrade the package and Claude Code's cached plugin can differ in
  version, so the review tool's id can be any of six
  (`branding.delegate_ids`); the hook matcher, the reminder and the hook
  timing record accept all of them.
- The context package (`review.py`): only the files Claude names. The manifest
  lists each file as sent (path, size, digest prefix), what was left out and
  why, and a package id. A run given a package id sends that package or
  nothing. When Codex says it needed more files, the reply carries a
  completion request; nothing is added by itself, and a review has at most two
  completion rounds. Git is not asked which files changed (asking can run a
  repository's own filters), and no token count is estimated: the only token
  counts are the ones Codex reports after a run.
- By default Claude decides on its own to ask Codex at the moments the
  server's instructions name (`adapters/server_instructions.py`), and a
  PostToolUse reminder (`adapters/review_reminder.py`) names that moment after
  several files change. `modelquad settings proactive off` makes Claude offer
  instead. Without the user's standing grant (`grant.py`,
  `modelquad settings auto`), Claude Code asks the user before every call that
  would start Codex. In a granted folder `delegate` follows Claude
  Code's own permissions, a Stop-hook review gate asks once for a Codex
  review before a multi-file or sensitive change is handed over, and a
  PreToolUse consultation gate stops, once, the first edit of a sensitive
  file or the edit that makes the change three files until Codex is consulted.
- `modelquad off` turns every Codex start off (`switch.py`, in
  `preferences.json`); the hooks are then silent. An off stored by an earlier
  build still counts until the user sets the switch here.
- Codex's findings and Claude's verdicts go to the review ledger
  (`review_ledger.py`, a SQLite file of its own; `modelquad review report`),
  with the package manifest and the completion round of each call. The call
  and its package are written when Codex starts (`unfinished`) and replaced
  when the call ends, so a session that closes mid-run leaves a record. A
  reply says so when the ledger could not be written (`ledger_note`).
- Settings: `/modelquad:settings` (skill, user-started) and
  `modelquad settings` (`settings_view.py`).
- A checked-in `.modelquad.toml` can only narrow Codex use in its repository
  and fails closed when broken (`repo_policy.py`, `docs/REPO-POLICY.md`). Its
  `account = "workspace"` rule refuses every call in this build, which does
  not read which Codex account is signed in.
- Public text passes a banned-phrase check (`python scripts/check.py docs`).
- No telemetry. ModelQuad's own processes connect only to loopback.

## Layout

| Path | What it is |
|---|---|
| `src/modelquad/review.py` | The flow: the gates, the context package and its manifest, the run, the reply |
| `src/modelquad/mcp/` | The MCP server Claude Code talks to (`server.py`) and the worker guard |
| `src/modelquad/execution/` | The Codex executor; `noshell.py` packs the files sent to Codex and checks its event stream; `closed.py` keeps reasons and metadata to fixed sets |
| `src/modelquad/review_ledger.py` | The local record of calls, packages, findings and verdicts |
| `src/modelquad/usage_report.py` | `modelquad review usage-report`: counts from the ledger, printed for the user to pass on; sends nothing |
| `src/modelquad/repo_policy.py`, `repo_settings.py`, `grant.py`, `switch.py` | The repository's rules, the user's per-repository choice, the standing grant, the global switch |
| `src/modelquad/cli_entry.py`, `src/modelquad/cli.py` | The `modelquad` command; `cli_entry.py` is the installed entry point and runs the hook without loading the full CLI |
| `src/modelquad/setupcheck.py` | `modelquad doctor` |
| `src/modelquad/adapters/` | Install and uninstall for Claude Code, and the review hook; `adapters/templates/claude/` holds the files install writes |
| `src/modelquad/netpolicy.py`, `privacy.py`, `disclosure.py` | The loopback rule, redaction of stored text, and the notice shown before install and before the first Codex call |
| `plugin/` | The Claude Code plugin: MCP entry, commands, hooks |
| `tests/` | The suite |
| `scripts/` | Development tooling; none of it ships |

## Commands

    # Set up (PowerShell or any shell)
    python -m venv .venv
    .venv\Scripts\activate
    pip install -e ".[dev]"

    # Pre-flight: sets the import root itself and echoes every command.
    python scripts/check.py --list
    python scripts/check.py fast      # docs + secret scan, seconds
    python scripts/check.py review    # fast + the tests your diff touches
    python scripts/check.py python    # the whole suite
    python scripts/check.py python --pace gentle   # while you work: half the workers
    python scripts/check.py python --pace full     # unattended: normal priority

    # By default the suite runs on every core at below-normal priority: full
    # speed on an idle machine, and your foreground programs come first.
    # MODELQUAD_TEST_PACE=gentle|normal|full sets the same thing.

    # Tests marked ci_only (wheel build, real venv, repository-wide scans of
    # the tests) are skipped locally; CI runs them. Locally, set
    # MODELQUAD_FULL_SUITE=1 to include them.

    # The suite directly
    python -m pytest -q

Build a wheel for release or a hand check from a clean export
(`git archive HEAD | tar -x -C <empty folder>`, then `pip wheel . --no-deps`
there), so that it holds the commit and not uncommitted edits. A `build/`
folder left by an earlier build no longer adds removed modules to the wheel:
`setup.py` empties `build/lib` first (`tests/test_wheel_packaging.py`).

CI (`.github/workflows/windows-ci.yml`) installs the package non-editable on
GitHub-hosted Windows and runs the suite against the installed distribution.

The suite uses a stand-in for Codex. A passing suite shows that ModelQuad
builds the launch, the package and the record as intended; it does not show
how a real Codex release behaves. That is checked by hand against real
releases and recorded in SECURITY.md.

## Safety invariants: do not weaken these

1. **A worker never re-enters ModelQuad.** `MODELQUAD_WORKER=1` refuses at
   every entry point, and a process with a running worker among its ancestors
   is refused as well (`tests/test_mcp_worker_guard.py`).
2. **At most one active run per package.** A second request for the package
   that is running is refused, not queued, in the same server process or in
   another one that shares the state folder: an operating-system lock on
   `<state root>/running/<package id>.lock`, released by Windows when the
   run ends or its process dies (`tests/test_review_flow.py`).
3. **A boundary ModelQuad cannot check is not claimed.** A Codex whose feature
   listing does not confirm the switched-off tools is refused, and the docs
   say which tools Codex still lists to the model.
4. **Unknown stays unknown.** Do not invent a token count, a size or an
   account kind. Absent evidence reads unknown, not a confident zero.
5. **Do not extract or proxy provider credentials.** ModelQuad drives the
   Codex CLI as a subprocess under the user's own login. The copies install
   keeps for an exact uninstall (`host-registration-backup.json` and the
   `*.modelquad-backup-*` files) can hold secrets the user put in
   configuration: keep them private and never show their contents
   (`tests/test_registration_backup_is_product_state.py`).
6. **The hook ModelQuad installs never approves a host tool call, and never
   opens a conversation file** (transcript, rollout or conversation store),
   even when the host hands it the path
   (`tests/test_hook_transcript_boundary.py`). It is advisory except for two
   gates, which act only in a folder where the user turned the standing grant
   on, at most once per change set each, and only to ask for Codex: the
   consultation gate may deny one edit (the first of a sensitive file, or the
   one that makes the change three files) until Codex is consulted, and the
   Stop review gate may block once, never while Claude is already continuing
   because of a stop hook. A consultation or review that failed or was not
   done is reported and never stops the change again
   (`tests/test_review_reminder.py`).
7. **Codex starts only with the user's consent: Claude Code's prompt for
   that call, or a standing grant the user turned on (in a terminal, or by
   choosing it in `/modelquad:settings`), for that folder or globally.**
   ModelQuad shows the grant's notice before turning it on, records it, and
   the user can turn it off at any time; no MCP tool can turn it on, and when
   a call is refused for lack of a grant ModelQuad tells Claude to tell the
   user and turn nothing back on (`tests/test_standing_grant.py`,
   `tests/test_settings_show.py`). Without a grant, `delegate`
   carries `anthropic/requiresUserInteraction`, so Claude Code shows its
   approval prompt for each call (`tests/test_oss_export_surface.py`). Every
   call re-checks the global switch, the grant and the repository's choices,
   which outrank it. A refusal, a grant turned off or a provider refusal is
   final for that request: ModelQuad never starts Codex by another way.
   ModelQuad installs no permission rule for its tools. Showing a package
   (`review_package`) starts nothing and sends nothing.
8. **Codex stays read-only, and gets only the package.** Only review,
   exploration and architecture run. Codex runs in an empty temporary folder
   with `--sandbox read-only`, `--ignore-user-config`, its agents off and its
   tool features disabled; ModelQuad supplies the named files itself, refuses
   to send a file that changed since the package was shown, never adds a file
   Codex asks for by itself, and withholds an answer whose event stream shows
   a tool call (`tests/test_codex_read_only.py`,
   `tests/test_noshell_workers.py`). Codex still lists a few built-in tools
   to the model that ModelQuad cannot remove; SECURITY.md names them.
9. **ModelQuad's own processes connect only to loopback.** Every entry point
   installs the audit hook in `netpolicy.py` (`tests/test_netpolicy.py`).

Privacy rules live in `src/modelquad/privacy.py`: free text kept in the ledger
is path-redacted where it is written (`tests/test_privacy_paths.py`). The
task text, file contents and Codex's whole answer are never stored; the
ledger keeps relative paths, sizes and digest prefixes of what was sent, and
of the answer only the findings Codex listed (claim and reasoning, redacted
and capped). ModelQuad has no telemetry. See [PRIVACY.md](PRIVACY.md).

## House rules

- Evidence beats assertion: cite a file, a test, or a recorded run.
- A worker's own account is never sufficient: a clean exit code is not
  success without a completion envelope.
- Prefer the smallest change that is honest.
- Never add a `Signed-off-by` line, in anyone's name. The maintainer's own
  commits need none; an outside contributor signs their own
  ([CONTRIBUTING.md](CONTRIBUTING.md), `.github/scripts/dco_check.py`).
- Do not add a second backend, a provider registry or code for a future
  feature. A second backend would implement `review.ReviewExecutor`; until one
  is decided, there is one.
