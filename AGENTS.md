# Working on ModelQuad

Build and test instructions for contributors and coding agents. What ModelQuad
does and how to use it is in [README.md](README.md).

**Developing this repository never uses ModelQuad itself.** Do not run
`modelquad advise` (or its old name `route`), and do not call any `modelquad_*` MCP tool while working
here; the tool under development is not the instrument for developing it.

## Current state

- Version `0.4.0`, DB schema `24`, router engine `4`, outcome assessor `3`.
  `python scripts/check.py docs` checks these against the code.
- Native Windows only, not WSL. Install, onboarding, repair and
  `service install` refuse to run elsewhere.
- ModelQuad installs as a Claude Code plugin (`plugin/`, with
  `modelquad install claude --plugin` for the status line);
  `modelquad install claude` alone is the install without it
  ([docs/PLUGIN.md](docs/PLUGIN.md)).
- One MCP host (Claude Code) and one worker (the Codex CLI, under the user's
  own login). Codex runs read-only: review, exploration and architecture
  tasks only.
- By default Claude decides on its own to ask Codex at the moments the
  instructions name (a multi-file change done, before a commit or PR, a bug
  that survived one fix, a design trade-off), and a PostToolUse reminder
  (`adapters/review_reminder.py`) names that moment after several files
  change. `modelquad settings proactive off` makes Claude offer instead and
  call only when the user agrees or asks.
  The guidance is the MCP server's `instructions`
  (`adapters/server_instructions.py`), not a global `CLAUDE.md` block.
  Without the user's standing grant (`grant.py`, `modelquad settings auto`),
  Claude Code asks the user before every call that would start Codex. In a
  granted folder `modelquad_delegate` follows Claude Code's own permissions,
  and a Stop-hook review gate asks once for a Codex review before a
  multi-file or sensitive change is handed over; a PreToolUse consultation
  gate stops, once, the first edit of a sensitive file or the edit that makes
  the change three files until Codex is consulted on the approach. Codex
  stays read-only; it writes no code. Codex's findings and
  Claude's verdicts go to the review ledger (`review_ledger.py`, a SQLite
  file of its own; `modelquad review report`). Quota is
  display-only: when Claude is near its limit, a note says so (wait for the
  reset, or ask Codex for a review) and starts nothing.
- Division of work: `modelquad_split` (and `/modelquad-split`) proposes who
  takes each step and starts nothing; `modelquad split summary` reports
  afterwards from history; `modelquad settings plan` and
  `modelquad settings repo` keep declared plans and per-repository choices
  in `preferences.json` (`plans.py`, `repo_settings.py`, `split.py`,
  `split_summary.py`).
- A checked-in `.modelquad.toml` can only narrow Codex use in its repository
  and fails closed when broken (`repo_policy.py`, `docs/REPO-POLICY.md`).
- Public text passes a banned-phrase check (`python scripts/check.py docs`).
- No telemetry. ModelQuad's own processes connect only to loopback.

## Layout

| Path | What it is |
|---|---|
| `src/modelquad/` | The product |
| `src/modelquad/mcp/` | The MCP server Claude Code talks to (`server.py`, job tools in `jobs_tools.py`) |
| `src/modelquad/cli_entry.py`, `src/modelquad/cli.py` | The `modelquad` command; `cli_entry.py` is the installed entry point and runs the hooks without loading the full CLI |
| `src/modelquad/execution/` | The Codex worker; `noshell.py` packs the files sent to Codex and checks its event stream |
| `src/modelquad/usage/` | Quota readings: Claude's status line, Codex's app-server, the background refresh |
| `src/modelquad/jobs/` | Background jobs and the store of answers waiting to be collected |
| `src/modelquad/service/` | The optional loopback service and its HTTP API |
| `src/modelquad/adapters/` | Install and uninstall for Claude Code; `adapters/templates/claude/` holds the files they write |
| `src/modelquad/netpolicy.py`, `privacy.py`, `disclosure.py` | The loopback rule, redaction of stored text, and the notices shown before install and before background work |
| `adapters/claude/README.md` | Points to the packaged templates |
| `tests/` | The suite |
| `scripts/` | Development tooling; none of it ships |

## Commands

    # Set up (PowerShell or any shell)
    python -m venv .venv
    .venv\Scripts\activate
    pip install -e ".[dev,service]"

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

CI (`.github/workflows/windows-ci.yml`) installs the package non-editable on
GitHub-hosted Windows and runs the suite against the installed distribution.

## Safety invariants: do not weaken these

1. **A worker never re-enters orchestration.** `MODELQUAD_WORKER=1` refuses
   delegation at every entry point (`tests/test_mcp_worker_guard.py`).
2. **At most one active attempt per logical request.** Enforced by a partial
   unique index on `request_key`, not only by an application check.
3. **A capability ModelQuad cannot bound is not granted.** The registry says
   so in data (`supported=True, allowed=False` with a reason), so a
   restriction is never reported as an inability.
4. **Unknown stays unknown.** Do not invent a quota percentage, a freshness
   or a capability. Absent evidence reads unknown, not a confident zero.
5. **Do not extract or proxy provider credentials.** ModelQuad drives the
   provider CLIs as subprocesses under the user's own login. The
   copies install keeps for an exact uninstall (`host-registration-backup.json`
   and the `*.modelquad-backup-*` files) can hold secrets the user put in
   configuration: keep them private and never show their contents
   (`tests/test_registration_backup_is_product_state.py`).
6. **A host hook ModelQuad installs never approves or denies a host tool
   call, and never opens a conversation file** (transcript, rollout or
   conversation store), even when the host hands it the path
   (`tests/test_hook_transcript_boundary.py`). Every hook is advisory except
   two gates, which act only in a folder where the user turned the standing
   grant on, at most once per change set each, and only to ask for Codex:
   the consultation gate may deny one edit (the first of a sensitive file, or
   the one that makes the change three files) until Codex is consulted, and
   the Stop review gate may block once, never while Claude is already
   continuing because of a stop hook. A consultation or review that failed
   or was not done is reported and never stops the change again; no hook
   ever approves a tool call (`tests/test_review_reminder.py`).
7. **Codex starts only with the user's consent: Claude Code's prompt for
   that call, or a standing grant the user turned on, from their own
   terminal, for that folder or globally.** The grant removes the per-call
   mark from `modelquad_delegate` only. ModelQuad shows the grant's notice before turning it on,
   records it, and the user can turn it off at any time; no MCP tool can turn
   it on (`tests/test_standing_grant.py`). Without a grant,
   `modelquad_delegate` and `modelquad_handoff` carry
   `anthropic/requiresUserInteraction`, so Claude Code shows its approval
   prompt for each call (`tests/test_oss_export_surface.py`). Every call
   re-checks the grant and the repository's choices, which outrank it. A
   refusal, a grant turned off or a provider refusal is final for that
   request: ModelQuad never starts Codex by another route. ModelQuad installs
   no permission rule for its tools, and quota never starts work.
   `modelquad_handoff` keeps its prompt either way. `modelquad_handoff_wait` starts
   Codex only after the handoff window's Approve button, whose tool
   (`modelquad_handoff_confirm`) is app-only, so a host keeps it from the
   model, and refuses in a client that renders no window
   (`tests/test_handoff_widget.py`).
8. **Codex stays read-only and tool-less.** Only review, exploration and
   architecture run. Codex runs in an empty temporary folder with
   `--sandbox read-only` and `--ignore-user-config` and its tools disabled;
   ModelQuad supplies the named files itself, and withholds an answer whose
   event stream shows a tool call (`tests/test_codex_read_only.py`,
   `tests/test_noshell_workers.py`).
9. **ModelQuad's own processes connect only to loopback.** Every entry point
   installs the audit hook in `netpolicy.py` (`tests/test_netpolicy.py`).

Privacy rules live in `src/modelquad/privacy.py`: free text and metadata
are path-redacted where history is written (`tests/test_privacy_paths.py`):
a path under home, `AppData`, `TEMP`, OneDrive or the working folder is
removed whole even with spaces; elsewhere the part after its first space can
remain. Tool stdout and the stderr body are
not persisted in any mode; the persisted worker command names only the
executable and its flags, and persisted argv carries no absolute path
(`tests/test_execution.py`). ModelQuad has no telemetry. See
[PRIVACY.md](PRIVACY.md).

## House rules

- Evidence beats assertion: cite a file, a test, or a recorded run.
- A worker's own account is never sufficient: a clean exit code is not
  success without a completion envelope.
- Prefer the smallest change that is honest.
