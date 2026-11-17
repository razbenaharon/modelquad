# Changelog

## 0.4.0 (unreleased)

The first open-source release.

### One review flow

ModelQuad is cut down to one flow: Claude Code asks your own Codex CLI for a
read-only review of a named context package, and ModelQuad records what was
sent and what Codex found.

Removed:

- **The local service and background work.** The loopback HTTP service and
  its Task Scheduler task, background jobs and the job runner, answers kept
  for later collection (`pending-output`), and the `SessionStart` hook that
  reported them.
- **Quota.** The status line, `modelquad usage-hook`, the `usage-*.json` and
  `quota-pace.json` files, the Codex app-server quota read and
  `MODELQUAD_CODEX_QUOTA_REFRESH`, the desktop quota band, and the notes when
  Claude is near its limit.
- **Handoff and division of work.** `modelquad_handoff`, `/modelquad:handoff`
  and the handoff window; `modelquad_split`, `/modelquad:split`, declared
  plans and `modelquad settings plan`; the quota command.
- **The history database.** `modelquad.db` and the activity tools.
- **Commands.** `modelquad onboard`, `repair`, `state`, `status`, `advise`,
  `network`, `tools --fix-path`, `integrations on|off` and
  `host connect|disconnect`. The `modelquad_status` tool stays.
- **Other reads.** Codex's `config.toml` and repository notes are no longer
  read.

Added:

- **Package preview.** `modelquad_review_package` and
  `modelquad review package` show the context package: each file with its
  size as sent, what is left out and why, the limits, and a package id.
  Showing it starts nothing and sends nothing.
- **Package id binding.** `modelquad_delegate` takes a `package_id` and sends
  exactly that package or nothing.
- **Completion rounds.** When Codex says it needed files it was not given,
  nothing is added by itself. A call with `follows` sends the larger package
  after you agree, at most two rounds per review.
- **One switch.** `modelquad off` stops every Codex call on this computer and
  quiets the hooks; `modelquad on` turns ModelQuad back on. An earlier build's
  disconnect or pause still counts as off until you set the switch.
- **A short `doctor`.** Five checks, each with what to do next: Windows, the
  Codex CLI, the connection to Claude Code, your settings and the current
  folder's rules.
- **Review ledger schema 3.** A `packages` table keeps each call's package id,
  completion round, counts and manifest (relative paths, sizes, digest
  prefixes, the reasons files were left out). No file contents, no task text,
  no full answer.

Stays: Claude Code's approval prompt or the standing grant on every call, the
review reminder and gates (plugin only), repository rules and
per-repository settings, the secret screening, the read-only Codex run with
its tools switched off, the review ledger and report, and install and
uninstall, which also take back what earlier builds wrote. Files earlier
builds kept are not read, written or deleted; `modelquad privacy` lists them.
The core dependencies are `mcp`, `pydantic` and `anyio`, and `pip install .`
is the install.

### Changes

- **A usage report you pass on yourself.** `modelquad review usage-report`
  prints counts from the review ledger for every project together: calls by
  outcome, trigger and task kind, findings by severity and verdict, the
  median duration and the token totals Codex reported, plus the ModelQuad,
  Windows and Python versions. It holds no text, name, path or id from the
  ledger. `--out FILE` also writes the JSON to a file you name. ModelQuad
  does not send it; PRIVACY.md lists every field.
- **A call is recorded when Codex starts.** The ledger row and the package
  manifest are written at the start, as `unfinished`, and replaced when the
  call ends. A session that closed in the middle of a review used to leave
  no record that a package had gone to Codex; `modelquad review report` now
  counts it as a Codex call with no end recorded.
- **A reply says what went wrong, in its data too.** A reply now says when
  the review ledger could not be written (`ledger_note`), and when Codex
  itself reported an error (for example a model id it does not have). Every
  refusal and every call without an answer carries its plain sentence in the
  reply's data as `message`: Claude Code hands Claude a reply's data, not the
  lines written for a person.
- **The wheel holds the source and nothing else.** `setup.py` empties
  `build/lib` before a build. A checkout that had built an earlier version
  used to put that version's removed modules into the new wheel.
- **The work-account rule names a way out that works.** With
  `[codex] account = "workspace"` every call is refused, because this build
  cannot confirm an account. The message used to suggest switching accounts,
  which would not help; it now names the line to change.
- **DCO for outside contributions only.** The maintainer's own commits need
  no `Signed-off-by`; anyone else's do, wherever they arrive. CI decides
  which is which from what GitHub reports about the branch, the account that
  pushed and the account a commit belongs to, not from a commit's name or
  e-mail ([CONTRIBUTING.md](CONTRIBUTING.md)).
- **Install and uninstall say when they did not finish.** A file ModelQuad
  could not read, write or remove is now an error (`Not finished: ...`, exit
  code 1) and no longer a skipped row under a successful result. A file that
  refuses to open for a moment is read again for about half a second first.
  A file left alone on purpose (a link, your own entry) is still not an error.
- **Claude Code plugin.** ModelQuad installs as a Claude Code plugin
  (`plugin/`): the MCP server, four skills and the review hooks. See
  [docs/PLUGIN.md](docs/PLUGIN.md).
- **Claude decides when to ask Codex.** By default Claude consults Codex at
  named moments: before a large, architectural or sensitive change, when a
  multi-file change is done, before a commit or PR, after a fix that did not
  work, and before a design choice with real trade-offs.
  `modelquad settings proactive off` makes Claude offer instead.
- **The standing grant.** `modelquad settings auto on` lets Claude consult
  Codex in a repository without Claude Code's prompt for each call, after a
  notice and with your own allow rule. A repository set to Codex off
  outranks it, and turning it off refuses the next call.
- **The review gate.** In a granted repository, a Stop hook asks once for a
  Codex review before a change of three or more files, or of one sensitive
  file, is handed over, and a consultation gate stops the first edit of a
  sensitive file, or the edit that makes the change three files, once until
  Codex is consulted on the approach. A review that failed or was not done is reported to
  you, never claimed.
- **Reasoned findings.** Each Codex finding carries its location (line or
  range when one applies), claim, reasoning, severity and Codex's confidence
  with a reason; Claude records its verdict before and after a fix.
- **The review ledger and report.** Each Codex call's findings, Claude's
  verdicts and their evidence, and the time and tokens spent are kept in
  `review-ledger.db`. `modelquad review report` separates findings confirmed
  by a test, a reproduction, CI or your own verdict from those Claude only
  agreed with.
- **Settings inside Claude Code.** `/modelquad:settings` shows whether
  ModelQuad is on, whether Codex calls are automatic or approved one by one,
  and the history, and changes only what you pick; `modelquad settings` shows
  the same in a terminal. No browser page.
- **Stricter secret screening.** More credential files are refused by name
  (`prod.env`, SSH keys, `secrets.json` and similar), more key and token
  shapes refuse a file, and a secret value in a configuration line, a
  connection string or a quoted literal under a secret-like name is replaced
  whatever characters it holds.
- **Exclusions hold.** `modelquad settings repo off` on a folder that is not a
  Git repository covers the folders below it, a junction into a repository
  counts as that repository, and `modelquad privacy --reset-identity` removes
  the per-repository settings and turns every standing grant off instead of
  leaving a grant over folders you had excluded.
- **Fail-closed stream check.** An answer is delivered only when every item
  in Codex's event stream is of a known inert kind; an item of any other
  kind, on any event, withholds it. Reading the completion report takes
  linear time on any input.
- **One count of what happened.** The review report and the settings view
  use one definition of answered, withheld, refused and not started. A call
  that never started says why (a missing file, a folder that does not exist,
  an unknown model) and is not counted as a Codex call.
- **Review ledger.** `modelquad review report --findings` lists decisions and
  findings with their ids. Your verdict still outranks Claude's, and a fix
  Claude records later on a finding you confirmed is kept.
- **The grant.** With a standing grant Claude consults Codex on its own even
  where `offer-only` or the repository's `on_request` is set (only `off`
  stops it); a call without a prompt must stay inside the folder the session
  started in; turning one repository off while the grant for every folder is
  on says that the grant still applies.
- **Install and uninstall.** A configuration file holding a broken emoji
  escape is written back unchanged instead of stopping half-way; uninstall
  restores a displaced entry after a plugin switch and leaves a `modelquad`
  entry that is not ModelQuad's own in place.
- **Release.** Publishing runs only when started by hand with a typed
  confirmation, from a version tag on `main` whose CI run passed.
- **Claude never turns a grant back on.** When a call is refused because the
  grant is off or the folder is excluded, Claude tells you and changes
  nothing.
- Codex stays read-only and tool-less: it reads only the files ModelQuad
  sends, with credentials redacted, and runs with Codex's agents switched
  off (`agents.enabled=false`), so its collaboration tools are not offered
  to the model.

### Removed before release

These were in earlier 0.4.0 builds and are gone from this one (see "One
review flow" above):

- **Division of work.** `modelquad_split` proposed who takes each step of a
  task, including a read-only step for Codex when Claude was near its limit
  (the `split_quota` trigger).
- **Quota readings.** The status line, `modelquad status`, `modelquad_status`
  and the split reply showed Codex's and Claude's quota windows.
- **The startup reminder and the quota band** in the plugin.
- **Saved answers.** Answers of background jobs waited in the state folder
  for up to 7 days, and `modelquad jobs clear --yes` deleted them; the review
  ledger also recorded answers collected from background jobs.
