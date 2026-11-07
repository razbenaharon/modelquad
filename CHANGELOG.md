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
  task; Codex takes only read-only steps. It starts nothing.
- **Settings page.** `modelquad settings open` (or `/modelquad:settings`)
  opens a one-time page on this computer to change the settings.
- Codex stays read-only and tool-less: it reads only the files ModelQuad
  sends, with credentials redacted.
