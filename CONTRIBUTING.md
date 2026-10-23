# Contributing to ModelQuad

Thank you for your interest. This file covers the rules for contributions.
Build details and the project's safety rules are in [AGENTS.md](AGENTS.md).

## Before you start

- **Scope.** Version 1 supports one host, Claude Code, and one worker, the
  OpenAI Codex CLI. Support for another host or another provider needs an
  issue first, so the scope can be discussed before any code is written.
  Pull requests that add a provider without an agreed issue may be closed.
- **Larger changes.** For anything bigger than a small fix, open an issue
  first and describe the problem.
- **Security problems.** Do not open a public issue. Follow
  [SECURITY.md](SECURITY.md).
- **Conduct.** Everyone taking part follows the
  [Code of Conduct](CODE_OF_CONDUCT.md).

## Requirements

- Windows. ModelQuad is developed and tested on native Windows only, not WSL.
  CI runs on GitHub-hosted Windows.
- Python 3.11 or newer.
- Claude Code and the Codex CLI are not needed to run the test suite.

## Set up and run the tests

From a checkout, in PowerShell:

    python -m venv .venv
    .venv\Scripts\activate
    pip install -e ".[dev,service]"

The pre-flight script sets the import path itself and prints every command
it runs:

    python scripts/check.py --list
    python scripts/check.py fast      # docs + secret scan, seconds
    python scripts/check.py review    # fast + the tests your diff touches
    python scripts/check.py python    # the whole suite

Or run the suite directly:

    python -m pytest -q

The editable install above tests the checkout. CI instead tests the package
as a user installs it. To reproduce that before you push, use a separate
virtual environment with a normal (non-editable) install, and the `pytest`
command rather than `python -m pytest`, which would put the checkout on the
import path:

    python -m venv .venv-ci
    .venv-ci\Scripts\activate
    pip install ".[dev,service]"
    pytest -q -n auto -m "not serial_timing"
    pytest -q -n 0 -m serial_timing

More about the scripts is in [scripts/README.md](scripts/README.md).

Do not use ModelQuad itself while developing this repository: do not run
`modelquad route` and do not call `modelquad_*` MCP tools. The tool under
development is not the tool for developing it.

## Safety invariants

[AGENTS.md](AGENTS.md) lists safety invariants, for example: a worker never
starts another worker, unknown values stay unknown, ModelQuad does not
extract or proxy provider credentials, installed hooks are advisory only, and
every call that starts Codex asks the user. A change must not weaken them. If
you think one is wrong, open an issue.

Privacy rules live in `src/modelquad/privacy.py`. Do not add tool output
or absolute paths to history or metadata; a stored worker command names only
the executable and its flags. ModelQuad has no telemetry.

## Pull requests

- Keep each pull request to one change.
- Add or update tests for behaviour you change.
- Say which checks you ran, and on which Windows version.
- Update the documentation when behaviour changes. Do not describe planned
  work as a feature.
- Fill in the pull request template.

## Disclose AI assistance

If an AI tool helped write the code, tests or text, say so in the pull
request description: which tool, and what it did. This is not a reason to
reject a contribution. It helps reviewers know what to check. You remain
responsible for every line you submit, and you must have the right to submit
it.

## Sign your commits (DCO)

This project uses the Developer Certificate of Origin, version 1.1:
https://developercertificate.org/

By signing off a commit you certify that you wrote the change, or otherwise
have the right to submit it under the project's license, as the DCO
describes. Sign off each commit with:

    git commit -s

This adds a line like the following to the commit message, using your real
name and e-mail:

    Signed-off-by: Your Name <you@example.com>

To sign off commits you already made, use `git commit --amend -s` for the
last one, or `git rebase --signoff` for several.

AI coding tools must not add a `Signed-off-by` line: only a human can certify
the DCO. A commit an AI tool helped with should carry an `Assisted-by:` trailer
naming the tool (for example `Assisted-by: Claude Code`), and the human
submitting it signs off.

## License

ModelQuad is licensed under the Apache License 2.0. By contributing, you
agree that your contribution is licensed under the same terms. See
[LICENSE](LICENSE).
