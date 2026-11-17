# Changelog

## 0.4.0 (unreleased)

The first open-source release.

- **Claude Code plugin.** ModelQuad installs as a Claude Code plugin
  (`plugin/`): the MCP server, seven skills, the startup reminder, the review
  hooks and, in the desktop app, a band with Codex's quota. See
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
- **Division of work.** `modelquad_split` proposes who takes each step of a
  task; Codex takes only read-only steps. When Claude is near its limit and
  Codex has room, a medium or large read-only step may be proposed for Codex
  (`claude_near_limit`), and its calls are counted under the `split_quota`
  trigger. The proposal starts nothing. Under the standing grant the reply
  says that each Codex step runs without a prompt, and `asks_user` is false;
  without the grant each step still shows Claude Code's approval prompt.
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
- **Quota readings.** A reading stamped in the future, or one that is not a
  finite number, reads unknown. The status line, `modelquad status`,
  `modelquad_status` and the split reply show the same figure for the same
  reading, and a lower figure never replaces a higher one inside one window.
- **One count of what happened.** The review report, the settings view, the
  split summary and the activity list use one definition of answered,
  withheld, refused and not started. A call that never started says why
  (a missing file, a folder that does not exist, an unknown model) and is
  not counted as a Codex call.
- **Review ledger.** `modelquad review report --findings` lists decisions and
  findings with their ids. Your verdict still outranks Claude's, and a fix
  Claude records later on a finding you confirmed is kept. Answers collected
  from background jobs are recorded too.
- **The grant.** With a standing grant Claude consults Codex on its own even
  where `offer-only` or the repository's `on_request` is set (only `off`
  stops it); a call without a prompt must stay inside the folder the session
  started in; turning one repository off while the grant for every folder is
  on says that the grant still applies.
- **Install and uninstall.** A configuration file holding a broken emoji
  escape is written back unchanged instead of stopping half-way; uninstall
  restores a displaced entry after a plugin switch and leaves a `modelquad`
  entry that is not ModelQuad's own in place.
- **Saved answers.** A clock that once ran ahead no longer expires new
  answers early, one damaged answer no longer blocks the others, and
  `modelquad jobs clear --yes` deletes them all.
- **Release.** Publishing runs only when started by hand with a typed
  confirmation, from a version tag on `main` whose CI run passed.
- **Claude never turns a grant back on.** When a call is refused because the
  grant is off or the folder is excluded, Claude tells you and changes
  nothing.
- Codex stays read-only and tool-less: it reads only the files ModelQuad
  sends, with credentials redacted, and runs with Codex's agents switched
  off (`agents.enabled=false`), so its collaboration tools are not offered
  to the model.
