# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The DCO check: the maintainer's own commits need no sign-off, and nothing
else is let through.

The exemption rests on what GitHub reports (the branch's repository, the
account that pushed, the accounts a commit is attributed to). These tests
give the check those reports directly and, for the trailer parsing, run it
on real commits in a throwaway repository.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from modelquad.procutil import no_window_kwargs

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "dco_check.py"
_spec = importlib.util.spec_from_file_location("dco_check", SCRIPT)
dco = importlib.util.module_from_spec(_spec)
import sys  # noqa: E402

sys.modules["dco_check"] = dco
_spec.loader.exec_module(dco)

MAINTAINER = 181751257
STRANGER = 4242
MAINTAINER_EMAIL = "181751257+razbenaharon@users.noreply.github.com"
STRANGER_EMAIL = "someone@example.com"

OWN_BRANCH = dco.Source(in_repository=True, actor_id=MAINTAINER)
FORK = dco.Source(in_repository=False, actor_id=MAINTAINER)
OWN_FACTS = dco.Facts(author_id=MAINTAINER, committer_id=MAINTAINER, verified=False)


def _commit(author=MAINTAINER_EMAIL, committer=None, signoffs=(), coauthors=()):
    return dco.Commit("a" * 40, author, committer or author, tuple(signoffs), tuple(coauthors))


def test_the_maintainers_own_unsigned_commit_passes():
    assert dco.problems(_commit(), OWN_FACTS, OWN_BRANCH) == []


def test_an_ai_tool_co_author_needs_no_sign_off():
    commit = _commit(coauthors=["noreply@anthropic.com"])
    assert dco.problems(commit, OWN_FACTS, OWN_BRANCH) == []


@pytest.mark.parametrize("facts, source, why", [
    # A fork can write the maintainer's name and e-mail into a commit, and
    # GitHub then attributes it to the maintainer's account. The fork is what
    # decides.
    (OWN_FACTS, FORK, "a pull request from a fork"),
    (OWN_FACTS, dco.Source(True, STRANGER), "pushed by another account"),
    (OWN_FACTS, dco.Source(True, None), "no actor reported"),
    (None, OWN_BRANCH, "GitHub could not be asked"),
    (dco.Facts(STRANGER, MAINTAINER, False), OWN_BRANCH, "someone else's commit, applied by the maintainer"),
    (dco.Facts(None, MAINTAINER, False), OWN_BRANCH, "an author GitHub attributes to no account"),
    (dco.Facts(MAINTAINER, STRANGER, False), OWN_BRANCH, "committed by another account"),
    (dco.Facts(MAINTAINER, dco.GITHUB_WEB_ID, False), OWN_BRANCH, "GitHub as committer, unverified"),
])
def test_the_maintainers_e_mail_alone_exempts_nothing(facts, source, why):
    found = dco.problems(_commit(), facts, source)
    assert found == [f"no Signed-off-by trailer for its author <{MAINTAINER_EMAIL}>"], why
    # The sign-off still satisfies the check.
    assert dco.problems(_commit(signoffs=[MAINTAINER_EMAIL]), facts, source) == []


def test_a_commit_made_on_github_by_the_maintainer_passes_when_github_signed_it():
    facts = dco.Facts(MAINTAINER, dco.GITHUB_WEB_ID, True)
    assert dco.problems(_commit(committer="noreply@github.com"), facts, OWN_BRANCH) == []


def test_an_outside_commit_in_the_maintainers_pull_request_needs_its_authors_sign_off():
    facts = dco.Facts(STRANGER, STRANGER, False)
    unsigned = _commit(author=STRANGER_EMAIL)
    assert dco.problems(unsigned, facts, OWN_BRANCH) == [
        f"no Signed-off-by trailer for its author <{STRANGER_EMAIL}>"]
    # The maintainer's sign-off does not stand in for the author's.
    assert dco.problems(_commit(author=STRANGER_EMAIL, signoffs=[MAINTAINER_EMAIL]),
                        facts, OWN_BRANCH)
    assert dco.problems(_commit(author=STRANGER_EMAIL, signoffs=[STRANGER_EMAIL]),
                        facts, OWN_BRANCH) == []


def test_a_human_co_author_on_the_maintainers_commit_needs_a_sign_off():
    commit = _commit(coauthors=[STRANGER_EMAIL, "noreply@anthropic.com"])
    assert dco.problems(commit, OWN_FACTS, OWN_BRANCH) == [
        f"no Signed-off-by trailer for its co-author <{STRANGER_EMAIL}>"]
    signed = _commit(coauthors=[STRANGER_EMAIL], signoffs=[STRANGER_EMAIL])
    assert dco.problems(signed, OWN_FACTS, OWN_BRANCH) == []


def test_dependabot_is_skipped_only_with_githubs_verified_signature():
    bump = _commit(author=dco.DEPENDABOT_EMAIL, committer="noreply@github.com")
    assert dco.problems(bump, dco.Facts(49699333, dco.GITHUB_WEB_ID, True), FORK) == []
    assert dco.problems(bump, dco.Facts(49699333, dco.GITHUB_WEB_ID, False), FORK)
    assert dco.problems(bump, None, FORK)
    forged = _commit(author=dco.DEPENDABOT_EMAIL, committer=STRANGER_EMAIL)
    assert dco.problems(forged, dco.Facts(49699333, STRANGER, True), FORK)


@pytest.mark.parametrize("env, expected", [
    ({"EVENT_NAME": "pull_request", "REPO": "o/r", "PR_HEAD_REPO": "o/r", "ACTOR_ID": "181751257"},
     dco.Source(True, MAINTAINER)),
    ({"EVENT_NAME": "pull_request", "REPO": "o/r", "PR_HEAD_REPO": "fork/r", "ACTOR_ID": "181751257"},
     dco.Source(False, MAINTAINER)),
    # A deleted fork reports no head repository.
    ({"EVENT_NAME": "pull_request", "REPO": "o/r", "PR_HEAD_REPO": "", "ACTOR_ID": "181751257"},
     dco.Source(False, MAINTAINER)),
    ({"EVENT_NAME": "pull_request", "REPO": "", "PR_HEAD_REPO": "", "ACTOR_ID": "181751257"},
     dco.Source(False, MAINTAINER)),
    ({"EVENT_NAME": "push", "REPO": "o/r", "ACTOR_ID": "4242"}, dco.Source(True, STRANGER)),
    ({"EVENT_NAME": "workflow_dispatch", "REPO": "o/r", "ACTOR_ID": ""}, dco.Source(True, None)),
    ({"EVENT_NAME": "pull_request_target", "REPO": "o/r", "PR_HEAD_REPO": "o/r", "ACTOR_ID": "181751257"},
     dco.Source(False, MAINTAINER)),
])
def test_the_source_is_read_from_the_event(env, expected):
    assert dco.source_from_event(env) == expected


def test_a_failed_github_lookup_is_unknown_not_a_pass(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise dco.urllib.error.URLError("no network")

    monkeypatch.setattr(dco.urllib.request, "urlopen", refuse)
    lookup = dco.github_facts("o/r", "token")
    assert lookup("a" * 40) is None
    assert dco.check([_commit()], OWN_BRANCH, lookup) == [
        f"{'a' * 40} has no Signed-off-by trailer for its author <{MAINTAINER_EMAIL}>"]


# -- against real commits ---------------------------------------------------


def _git(repo: Path, *args: str, author: str = MAINTAINER_EMAIL) -> str:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "A Person", "GIT_AUTHOR_EMAIL": author,
        "GIT_COMMITTER_NAME": "A Person", "GIT_COMMITTER_EMAIL": author,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    })
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
        cwd=repo, env=env, capture_output=True, text=True, check=True, **no_window_kwargs(),
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "commit", "-q", "--allow-empty", "-m", "root")
    monkeypatch.chdir(root)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return root


def test_trailers_are_read_from_real_commits(repo: Path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "commit", "-q", "--allow-empty", "-m", "mine",
         "-m", "Co-Authored-By: Claude <noreply@anthropic.com>")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "theirs", "-m", "A body line.",
         "-m", f"Signed-off-by: Some One <{STRANGER_EMAIL.upper()}>", author=STRANGER_EMAIL)
    # A sign-off quoted in the body is not a trailer.
    _git(repo, "commit", "-q", "--allow-empty", "-m", "quoted",
         "-m", f"Signed-off-by: Some One <{STRANGER_EMAIL}>\n\nmore text after it, so no trailer",
         author=STRANGER_EMAIL)

    quoted, theirs, mine = dco.read_commits(f"{base}..HEAD")
    assert mine.author_email == MAINTAINER_EMAIL
    assert mine.coauthors == ("noreply@anthropic.com",) and mine.signoffs == ()
    assert theirs.signoffs == (STRANGER_EMAIL,)
    assert quoted.signoffs == ()

    accounts = {MAINTAINER_EMAIL: MAINTAINER, STRANGER_EMAIL: STRANGER}
    by_sha = {c.sha: c for c in (quoted, theirs, mine)}

    def lookup(sha: str):
        account = accounts[by_sha[sha].author_email]
        return dco.Facts(account, account, False)

    failures = dco.check([quoted, theirs, mine], OWN_BRANCH, lookup)
    assert failures == [f"{quoted.sha} has no Signed-off-by trailer for its author <{STRANGER_EMAIL}>"]
    # The same three commits from a fork: the maintainer's unsigned one fails too.
    assert len(dco.check([quoted, theirs, mine], FORK, lookup)) == 2


def test_merge_commits_are_not_checked_but_what_they_bring_in_is(repo: Path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "theirs")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "outside work", author=STRANGER_EMAIL)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "theirs", "-m", "Merge theirs")
    commits = dco.read_commits(f"{base}..HEAD")
    assert [c.author_email for c in commits] == [STRANGER_EMAIL]


def test_the_range_follows_the_event(repo: Path):
    root = _git(repo, "rev-parse", "HEAD").strip()
    assert dco.range_from_event({"EVENT_NAME": "push", "PUSH_AFTER": root}) == root
    _git(repo, "update-ref", "refs/remotes/origin/main", root)
    _git(repo, "checkout", "-q", "-b", "work")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "one")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "two")
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert dco.range_from_event({"EVENT_NAME": "pull_request", "PR_BASE": root, "PR_HEAD": head,
                                 "PUSH_AFTER": "merge"}) == f"{root}..{head}"
    assert dco.range_from_event({"EVENT_NAME": "push", "PUSH_BEFORE": root,
                                 "PUSH_AFTER": head}) == f"{root}..{head}"
    assert dco.range_from_event({"EVENT_NAME": "push", "PUSH_BEFORE": dco.ZERO_SHA,
                                 "PUSH_AFTER": head}) == f"{head}~1..{head}"
    # A manual run checks everything the branch adds to main, not its tip alone.
    assert dco.range_from_event({"EVENT_NAME": "workflow_dispatch",
                                 "PUSH_AFTER": head}) == f"{root}..{head}"


def test_an_unreadable_range_fails_the_job(repo: Path, capsys):
    code = dco.main({"EVENT_NAME": "pull_request", "PR_BASE": "0" * 39 + "1", "PR_HEAD": "0" * 39 + "2",
                     "REPO": "o/r", "PR_HEAD_REPO": "o/r", "ACTOR_ID": str(MAINTAINER)})
    assert code == 1
    assert "::error::git failed reading the commit range" in capsys.readouterr().out
