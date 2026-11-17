# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The DCO check CI runs (CONTRIBUTING.md, "Sign your commits").

An outside contribution needs its author's ``Signed-off-by``. The
maintainer's own commits need none. Which is which is decided from what
GitHub itself reports, never from the name or e-mail written in a commit
alone, and never from who opened the pull request:

* where the commits came in: a branch of this repository (only accounts with
  write access can push there), not a fork;
* who pushed or started the run: ``github.actor_id``;
* which GitHub account GitHub attributes the commit's author and committer
  to (``/repos/{repo}/commits/{sha}``).

A commit is the maintainer's own only when all three name the maintainer.
Anything else needs the sign-off: every commit of a pull request from a fork
(whatever its author field says), a commit by someone else inside the
maintainer's own pull request, and any commit GitHub could not be asked
about. A ``Co-authored-by`` person who is not an AI tool needs a sign-off of
their own, on the maintainer's commits too.

What this cannot see: text copied into a commit the maintainer authored. The
maintainer answers for that, as CONTRIBUTING.md says.

Stdlib only; runs on the hosted Linux runner.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable

#: GitHub account ids, not names or e-mails: an id cannot be re-registered.
MAINTAINER_IDS = frozenset({181751257})  # razbenaharon
#: The account GitHub commits as for merges and edits made on github.com.
GITHUB_WEB_ID = 19864447

DEPENDABOT_EMAIL = "49699333+dependabot[bot]@users.noreply.github.com"
GITHUB_COMMITTER_EMAIL = "noreply@github.com"

#: Co-authored-by addresses of AI coding tools. A tool cannot certify the DCO
#: and is not asked to.
AI_TOOL_EMAILS = frozenset({"noreply@anthropic.com"})

ZERO_SHA = "0" * 40
_FIELD = "\x1f"
_RECORD = "\x1e"
_EMAIL = re.compile(r"<([^<>\s]+)>\s*$")


@dataclass(frozen=True)
class Commit:
    sha: str
    author_email: str
    committer_email: str
    signoffs: tuple[str, ...]
    coauthors: tuple[str, ...]


@dataclass(frozen=True)
class Facts:
    """What GitHub reports for one commit."""

    author_id: int | None
    committer_id: int | None
    verified: bool


@dataclass(frozen=True)
class Source:
    """Where the commits came in, as GitHub's event reports it."""

    in_repository: bool
    actor_id: int | None

    @property
    def maintainer_pushed_here(self) -> bool:
        return self.in_repository and self.actor_id in MAINTAINER_IDS


def _emails(values: Iterable[str]) -> tuple[str, ...]:
    found = []
    for value in values:
        match = _EMAIL.search(value.strip())
        if match:
            found.append(match.group(1).lower())
    return tuple(found)


def is_maintainers_own(commit: Commit, facts: Facts | None, source: Source) -> bool:
    if facts is None or not source.maintainer_pushed_here:
        return False
    if facts.author_id not in MAINTAINER_IDS:
        return False
    if facts.committer_id in MAINTAINER_IDS:
        return True
    # A squash or edit made on github.com: GitHub commits and signs it.
    return facts.committer_id == GITHUB_WEB_ID and facts.verified


def is_dependabot(commit: Commit, facts: Facts | None) -> bool:
    """Dependabot cannot sign off; a person reviews and merges its bumps.
    Anyone can set an author e-mail, so GitHub's verified signature is needed."""
    return (
        commit.author_email == DEPENDABOT_EMAIL
        and commit.committer_email == GITHUB_COMMITTER_EMAIL
        and facts is not None
        and facts.verified
    )


def problems(commit: Commit, facts: Facts | None, source: Source) -> list[str]:
    """Why this commit fails the check; empty when it passes."""
    found = []
    if is_dependabot(commit, facts):
        return found
    signed = set(commit.signoffs)
    author = commit.author_email.lower()
    if not is_maintainers_own(commit, facts, source) and author not in signed:
        found.append(f"no Signed-off-by trailer for its author <{commit.author_email}>")
    for coauthor in commit.coauthors:
        if coauthor in AI_TOOL_EMAILS or coauthor == author or coauthor in signed:
            continue
        found.append(f"no Signed-off-by trailer for its co-author <{coauthor}>")
    return found


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout


def read_commits(revision_range: str) -> list[Commit]:
    """The non-merge commits of a range. Only real trailers count, so a
    "Signed-off-by:" quoted in a message body does not."""
    fmt = _FIELD.join([
        "%H", "%ae", "%ce",
        "%(trailers:key=Signed-off-by,valueonly,separator=%x1d)",
        "%(trailers:key=Co-authored-by,valueonly,separator=%x1d)",
    ]) + _RECORD
    commits = []
    for record in _git("log", "--no-merges", f"--format={fmt}", revision_range).split(_RECORD):
        record = record.strip("\n")
        if not record:
            continue
        sha, author, committer, signoffs, coauthors = record.split(_FIELD)
        commits.append(Commit(sha, author, committer,
                              _emails(signoffs.split("\x1d")), _emails(coauthors.split("\x1d"))))
    return commits


def github_facts(repo: str, token: str) -> Callable[[str], Facts | None]:
    """A lookup of one commit on GitHub. A failed lookup is ``None``, which
    reads as "not known to be the maintainer's": the sign-off is required."""

    def lookup(sha: str) -> Facts | None:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/commits/{sha}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dco-check"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
            if data.get("sha") != sha:
                return None
            return Facts(
                author_id=(data.get("author") or {}).get("id"),
                committer_id=(data.get("committer") or {}).get("id"),
                verified=(data.get("commit") or {}).get("verification", {}).get("verified") is True,
            )
        except (urllib.error.URLError, OSError, ValueError, AttributeError):
            return None

    return lookup


def source_from_event(env: dict[str, str]) -> Source:
    actor = env.get("ACTOR_ID", "")
    actor_id = int(actor) if actor.isdigit() else None
    if env.get("EVENT_NAME") == "pull_request":
        # A fork's head repository has another name; a deleted fork has none.
        in_repository = bool(env.get("REPO")) and env.get("PR_HEAD_REPO") == env.get("REPO")
    else:
        # A push to this repository, or a manual run on one of its branches.
        in_repository = env.get("EVENT_NAME") in {"push", "workflow_dispatch"}
    return Source(in_repository=in_repository, actor_id=actor_id)


def range_from_event(env: dict[str, str]) -> str:
    """The pull request's commits, the commits a push added, or, for a manual
    run, what the branch adds to main."""
    if env.get("PR_BASE") and env.get("PR_HEAD"):
        return f"{env['PR_BASE']}..{env['PR_HEAD']}"
    before, after = env.get("PUSH_BEFORE", ""), env.get("PUSH_AFTER", "") or "HEAD"
    if before and before != ZERO_SHA:
        return f"{before}..{after}"
    if env.get("EVENT_NAME") == "workflow_dispatch":
        base = _git("merge-base", "refs/remotes/origin/main", after).strip()
        if base != _git("rev-parse", after).strip():
            return f"{base}..{after}"
    parent = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{after}~1"],
                            capture_output=True, text=True)
    return f"{after}~1..{after}" if parent.returncode == 0 else after  # a root commit: alone


def check(commits: Iterable[Commit], source: Source,
          lookup: Callable[[str], Facts | None]) -> list[str]:
    failures = []
    for commit in commits:
        for problem in problems(commit, lookup(commit.sha), source):
            failures.append(f"{commit.sha} has {problem}")
    return failures


def main(env: dict[str, str] | None = None) -> int:
    env = dict(os.environ if env is None else env)
    try:
        # A range that cannot be read must fail the job, not pass as empty.
        commits = read_commits(range_from_event(env))
    except subprocess.CalledProcessError as error:
        print(f"::error::git failed reading the commit range: {error.stderr.strip()}")
        return 1
    source = source_from_event(env)
    failures = check(commits, source, github_facts(env.get("REPO", ""), env.get("GH_TOKEN", "")))
    for failure in failures:
        print(f"::error::{failure}")
    where = "a branch of this repository" if source.in_repository else "outside this repository"
    print(f"{len(commits)} commit(s) checked, from {where}; {len(failures)} problem(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
