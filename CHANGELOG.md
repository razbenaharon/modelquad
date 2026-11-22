# Changelog

## Unreleased

- **The review report counts refusals before Codex starts.** The ledger
  records their reason codes, and the report lists how many stops had each
  reason. Worker and unusable-folder refusals leave no row. A locked
  ledger never holds up the refusal.

## 0.5.3 (2026-11-22)

- **The package preview gives its exact size and, when the record allows,
  an input-token range.** `review_package` and `modelquad review package`
  now give the size of the whole payload ModelQuad sends (`payload_bytes`:
  the task text, the files with their labels and line numbers, and
  ModelQuad's own instructions), measured on the same rendering the run
  sends. Input tokens are an estimate, never a count
  (`input_tokens_estimate`): a straight line through the last 50
  successful reviews recorded on this computer (task text and file bytes
  against the input tokens Codex reported), widened by the largest miss
  among them and rounded outward to the thousand. It is shown only from 20
  such reviews and only for a package within the sizes they cover;
  otherwise the preview says the tokens are unknown and why. No tokenizer
  is used and no price is estimated. The ledger is read, never created,
  and the range is not written to it. Only the preview computes the range:
  the call that starts Codex does not read the ledger for it, and a ledger
  another process holds locked reads as unknown at once instead of holding
  up the preview. After a run, the reply puts the
  bytes sent beside the input tokens Codex reported, how many came from
  its cache and how many did not (`usage.payload_bytes`,
  `usage.uncached_input_tokens`), and the ledger's package manifest keeps
  the payload size.
- **A file named like a secrets file no longer ends a review without
  saying why.** When a file in `files` is named like an environment or
  credential file (for example `config/billing.env.tmpl`), the reply now
  says that its name stopped the review, not its contents, and that
  ModelQuad did not open it. The path stays hidden as before; the reply
  gives the file's position in Claude's own `files` list (`left_out`) and
  whether its name, its contents or its path stopped it. It also offers the
  same package without it (`retry_package`, with its own `package_id`) so
  the user can decide whether the review is still useful. Nothing is sent
  until Claude calls again with that package id, which sends that package
  or nothing. `modelquad review package` shows the same. The explanation
  comes from the packing pass that refused: no file is opened again to
  explain it, so a file Git, a Read deny rule or `.modelquadignore`
  excludes is never read for it.
- **The package names tests Codex may also need.** For each source file
  in a package, the preview lists up to three existing test files that the
  package does not include, found by name alone (for `src/pkg/foo.py`,
  `tests/test_foo.py` and then `tests/test_foo_*.py`; for a script file,
  `foo.test.*`, `foo.spec.*` and `__tests__/foo.*`). ModelQuad lists the
  folders and opens none of these files; each passes the same file rules
  as a named file, and a name that looks like a secret is never offered.
  Nothing is added by itself and the package id does not change: the
  reply's `may_also_need` and the line "Codex may also need (not added)"
  are a hint for Claude, which adds a file only by naming it. The hint is
  not written to the review ledger.
- **Claude is told what a sufficient package holds.** The server's
  instructions now ask Claude to include the tests covering a change, one
  caller of each changed public function and the config it reads: Codex
  can ask only for files the sent code names, so it cannot ask for callers
  or tests.
- **Shorter server instructions, with the retry rule first.** Claude Code
  before version 2.1.296 keeps only the first 2,048 characters of a
  server's instructions. Every variant ModelQuad sends (proactive on,
  proactive off, standing grant) now fits within that, with repeated
  sentences merged. The first paragraph is now the `retry_package` rule:
  show the user what was left out, why, and what that package sends, and
  use its package id only after the user explicitly agrees, even with a
  standing grant.

## 0.5.2 (2026-11-21)

- **`modelquad --version` names a release.** A copy pip installed from a
  package index printed `(unknown build)`; it now prints
  `0.5.2 (release from a package index)`, read from pip's own record of
  the install. The commit is still not claimed; a source checkout still
  names its commit, and any other install still says `(unknown build)`.
- **Contact addresses.** support@modelquad.com for questions, and
  security@modelquad.com for a vulnerability when GitHub's private form
  cannot be used. The README says how to follow new releases on GitHub.

## 0.5.1 (2026-11-21)

- **The plugin installs from GitHub.** The install is now
  `pip install modelquad`, `claude plugin marketplace add
  razbenaharon/modelquad#stable` and `claude plugin install
  modelquad@modelquad`: no clone. `stable` is a branch that moves to each
  release once it is on PyPI, so the plugin and the package stay the same
  version, and `python -m modelquad update` refreshes both. The clone
  install still works for reading or changing the code.

### If you upgrade

An install that took the plugin from a clone keeps working. To move it to
PyPI and `stable`, see the README's upgrade section.

## 0.5.0 (2026-11-21)

- **Shorter tool names.** Claude Code showed the review tool as
  `modelquad:modelquad - modelquad_delegate`. The plugin's MCP server is now
  named `review` and the four tools lose their `modelquad_` prefix:
  `review_package`, `delegate`, `review_verdict` and `status`. With the
  plugin their ids are `mcp__plugin_modelquad_review__<tool>`; without it
  (`modelquad install claude`, whose server keeps the name `modelquad`),
  `mcp__modelquad__<tool>`. The CLI commands keep their names. The plugin's
  hook, the review reminder and the tool kind in `hook-timing.jsonl`
  recognise the review tool by all six ids it can have, including the mixed
  ones below, and the hook messages name it by its full id.
- **`modelquad doctor` names permission rules with an old tool name.** A
  sixth check reads `permissions.allow` and `permissions.deny` in Claude
  Code's managed settings, your user settings (`CLAUDE_CONFIG_DIR` or
  `%USERPROFILE%\.claude\settings.json`) and the project's
  `.claude\settings.json` and `.claude\settings.local.json`, read-only, and
  names every rule that still uses an id from before 0.5.0. For a deny rule
  it names the wildcard to add; a deny counts as covered only by a wildcard
  for the new server in the same scope or a wider one (managed covers user
  and project, user covers project). An uncovered deny is a problem; an
  allow rule is a note; an unreadable file reads as unknown. ModelQuad still
  writes no permission rule.
- **The recommended deny rules are wildcards for both names**
  (`mcp__plugin_modelquad_review__*`, `mcp__plugin_modelquad_modelquad__*`
  and `mcp__modelquad`), so a rule holds with an older build and in a mixed
  state. `modelquad settings auto on` prints the allow rule for both
  installs.
- **`python -m modelquad update`.** One user-started step. It refuses unless
  started as `python -m modelquad` (pip cannot replace the running
  `modelquad.exe`; it prints the exact command), in a ModelQuad worker, and
  while Claude Code (`claude.exe`), the Claude app (`Claude.exe`),
  `modelquad-mcp.exe` or another `modelquad.exe` is running. Otherwise it
  upgrades the package the way it was installed
  (`python -P -m pip install --upgrade modelquad` with the same interpreter
  for PyPI; for a clone on a clean `main`, `git pull --ff-only`, then
  `pip install .`, or `pip install -e .` for an editable install), refreshes
  the plugin with `claude plugin update --scope user` (falling back to a
  reinstall with `--keep-data`) when `claude plugin list --json` shows it
  enabled for the user, runs the doctor checks and prints the new version.
  A disabled plugin stays disabled; a skipped or failed plugin refresh, or a
  failed doctor check, ends with an error that says the package upgrade
  itself succeeded. When it cannot tell how ModelQuad was installed it says
  so and prints the commands instead. `--dry-run` runs nothing. pip, Git and
  `claude plugin` go online themselves; ModelQuad's own process still
  connects only to loopback, and nothing updates unless you run the
  command. `python -m modelquad` now runs the `modelquad` command.

### If you upgrade

- The tool ids change: `mcp__plugin_modelquad_modelquad__modelquad_delegate`
  is now `mcp__plugin_modelquad_review__delegate`, and
  `mcp__modelquad__modelquad_delegate` is now `mcp__modelquad__delegate`
  (the same for `review_package`, `review_verdict` and `status`).
- **Mixed state.** Claude Code keeps its own copy of the plugin. Until that
  copy is refreshed, the new package runs under the old plugin and the tool
  is `mcp__plugin_modelquad_modelquad__delegate`; a new plugin with an old
  package gives `mcp__plugin_modelquad_review__modelquad_delegate`. In that
  state a rule naming one exact id, old or new, can miss the live tool.
  Refresh the plugin (the update does it) and restart Claude Code.
- An **allow** rule for an old id stops matching: Claude Code asks again
  before the call. Put the new id in the rule to skip the prompt again.
- A **deny** rule for an old id (or only `mcp__plugin_modelquad_modelquad__*`)
  no longer covers the tool once the plugin is refreshed, so it no longer
  keeps ModelQuad out. Deny the wildcards `mcp__plugin_modelquad_review__*`,
  `mcp__plugin_modelquad_modelquad__*` and `mcp__modelquad` instead of exact
  ids: they hold before, after and in between.
- `modelquad doctor` names every such rule in your Claude Code settings and
  the project's, with what to use. It does not change the files.

## 0.4.2 (2026-11-21)

- **The review hook records how long it took.** Claude Code still showed "A
  hook didn't answer in time" once with the 10-second limit, and the cause
  could not be reproduced. Each hook call now adds two lines to
  `hook-timing.jsonl` in the state folder: how long its process took to
  start, and how long its work took, with the event and a tool kind. A call
  Claude Code stopped leaves a start line with no end line; a notice with no
  start line means ModelQuad's code never ran. Numbers and fixed words only;
  nothing is kept while ModelQuad is off. PRIVACY.md lists the file.

## 0.4.1 (2026-11-20)

- **The install line for the plugin works.** `claude plugin marketplace add .`
  is refused by Claude Code 2.1.318 ("Invalid marketplace source format");
  the README and docs/PLUGIN.md now say `claude plugin marketplace add ./`,
  which adds the clone as before.
- **The review hooks get time to start.** Claude Code gave the plugin's hooks
  3 seconds (5 for the Stop hook) and skipped a hook that had not answered by
  then, with the notice "A hook didn't answer in time". Starting Python on
  Windows alone can take most of that on a busy machine. The limit is now 10
  seconds for each hook. The hook still bounds its own work to 1.5 seconds
  once it is running, so a normal call is no slower.
- **The PyPI page describes the PyPI package.** 0.4.0's page carried the
  README from before the upload, which still called the PyPI name a
  placeholder.

## 0.4.0 (2026-11-20)

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

- **Automated runs, under control, instead of a blanket ban.** The notice
  and the README no longer forbid unattended, scripted, scheduled or CI use
  as if a provider required it. They say what stops a run without a person
  (no grant: Claude Code asks), how to allow one under control (one granted
  folder, one allowed tool, one task, time and turn limits, the record, a
  way to stop it), and what has not been tested (CI, other schedulers, an
  API-key sign-in). The account rule is stated as the providers state it:
  no sharing your login or making your account available to anyone else.
- **A stopped worker is stopped when the stop says so.** After a timeout or
  a cancel, the stop reported the process tree stopped (`tree_kill: ok`)
  once the Windows job listed no process. Windows drops a process from that
  list as soon as its exit begins, so a member could still finish a write
  after the stop had returned (seen once in CI). The stop now also waits,
  within the same bound, for every member it holds to finish exiting, and
  says `members_exiting` when one has not. The cleanup after a normal
  finish waits the same way, and a process the stop could not hold counts
  as finished only when it has finished exiting, not when its exit code is
  set. A first job census that cannot be read leaves the stop
  `census_unknown`.
- **A usage report you pass on yourself.** `modelquad review usage-report`
  prints counts from the review ledger for every project together: calls by
  outcome, trigger and task kind, findings by severity and verdict, the
  median duration and the token totals Codex reported (with the number of
  calls they cover), plus the ModelQuad, Windows and Python versions. It
  holds no text, name, path or id from the ledger, and says what its counts
  cannot show. `--out FILE` also writes the JSON to a file you name and does
  not replace an existing file without `--force`. ModelQuad does not send
  it; PRIVACY.md lists every field. The period is at least 30 days.
- **The notice has a version, and four more lines.** The first-use notice
  now also says that with the standing grant files go to OpenAI with no
  prompt and no list first, that the secret screen can miss a secret under
  an ordinary name, what the local record keeps, and that an API-key or
  workspace sign-in has its own data settings. Someone who saw an earlier
  notice is shown this one once, before their next Codex call, with a line
  that says it changed.
- **`--project .` works.** `modelquad settings auto`, `settings repo`,
  `settings show` and `review verdict` took a relative `--project` as it was
  typed, so `auto on --project .` failed with "could not identify this
  folder's repository" and `status` reported off. The folder is now made
  absolute first.
- **A Git check that gives no answer sends nothing.** When Git timed out,
  could not be started or refused the folder (for example as of dubious
  ownership, or a worktree whose repository moved), the files it might
  ignore used to be sent as if it had said none. Now no file is sent
  (`git_check_failed`), as when a settings file cannot be read. A file in
  a submodule is checked against the submodule's own ignore rules.
- **One run per package across sessions.** The same package asked for in two
  Claude Code sessions at once used to run twice, because each session's
  server only knew its own runs. The second is now refused
  (`duplicate_in_flight`) through an operating-system lock in the state
  folder, which Windows releases when a run ends or its session dies.
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
- Codex stays read-only, with its tool features switched off (a few
  built-in tools stay listed; SECURITY.md names them): it reads only the
  files ModelQuad sends, with credentials redacted, and runs with Codex's agents switched
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
