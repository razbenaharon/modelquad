# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A delegation that outlives its caller may not leave a workspace
looking settled.

Earlier work established the two halves this builds on. The CALLER's
departure is recorded and says nothing about the worker; the worker is
observed and never guessed at, and a probe that cannot answer stays UNKNOWN.
Neither of them terminates anything, and that is deliberate: a worker
keeps running under the budget `modelquad_delegate(timeout=...)` gave it, long
after the host's 900 s transport ceiling has told the caller the call failed.

What was missing is the consequence. The worker is still writing to a
WORKSPACE, and the parties hurt by that -- a validation run, a review, a commit
-- hold a PATH and nothing else. `lookup_delegation_attempts` wants an attempt
id, a decision id, or the exact task_type + working_directory + prompt that was
delegated with, and the prompt left with the result that never arrived. So the
question could not be asked at all, and in one real session it was not: a
`check.py` run straddled an orphaned worker's writes and reported PASS for a
tree that existed at no commit.

Two rules decide every answer here, and most of this file exists to hold them:

- **`may_be_mutating` requires an OBSERVATION.** Only a worker pid the process
  table positively reports alive produces it. Age never does.
- **`no_delegation_in_flight` has to be EARNED.** An open row can never reach
  it, because an open row is an unfinished delegation and a dead pid on one is
  a contradiction rather than reassurance -- on Windows the recorded pid is a
  `.cmd` shim whose node child can outlive it. A resolved row reaches it only
  if its ending accounts for the worker. Everything else is `unknown`, and
  `unknown` is never a "no".

The workspace is stored as HASHES, not as a path: `PRIVACY.md` promises the
local history keeps a project directory's name and never its full path, and a
safety feature does not get a quiet exemption from a published claim.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modelquad import procutil
from modelquad.mcp.livelifecycle import derive_recovery_outlook, facts_from_attempt
from modelquad.service import db as service_db
from modelquad.service import history as service_history


REPO_ROOT = Path(__file__).resolve().parents[1]


def _tool(module, name):
    """The undecorated tool body, the way the recovery tests reach one."""

    tool = getattr(module, name)
    return getattr(tool, "fn", tool)


@pytest.fixture()
def db_path(tmp_path) -> Path:
    path = tmp_path / "history.sqlite"
    service_db.connect_and_init(path).close()
    return path


def open_attempt(db_path: Path, workspace, *, pid: int | None = None, **kwargs) -> int:
    attempt_id = service_history.record_delegation_attempt(
        db_path,
        task_type="implementation",
        working_directory=workspace,
        **kwargs,
    )
    assert attempt_id is not None
    if pid is not None:
        service_history.record_worker_pid(db_path, attempt_id, pid)
    return attempt_id


def verdict(db_path: Path, workspace, **kwargs) -> str:
    return service_history.workspace_delegation_liveness(db_path, workspace, **kwargs)[
        "verdict"
    ]


@pytest.fixture()
def live_worker():
    """A real child process, so the pid probe is a probe and not a stub.

    The defect is about what the operating system is actually doing, and a
    monkeypatched `pid_state` would let this file pass on a tree where the
    probe itself had regressed.
    """

    procs: list[subprocess.Popen] = []

    def spawn(script: str = "import time; time.sleep(120)") -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **procutil.no_window_kwargs(),
        )
        procs.append(proc)
        return proc

    yield spawn
    for proc in procs:
        try:
            proc.kill()
            proc.wait(timeout=60)
        except Exception:  # noqa: BLE001 -- teardown is best effort
            pass


@pytest.fixture()
def dead_pid() -> int:
    """A pid proven dead at test time, and kept dead for the test's duration.

    The file used to hard-code one fixed number as "a pid that never
    existed". It is an ordinary Windows process id: under ``pytest -n auto``
    the suite itself spawns dozens of processes, and on 2026-09-14 the
    development machine held a zombie process object at exactly that id for
    hours, so two tests that expect the verdict a dead pid must produce
    failed on every run. A real child that has exited is dead
    by construction, and holding its handle (the ``Popen`` object, until the
    fixture tears down) keeps Windows from recycling the id while the test
    still asserts about it.
    """

    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **procutil.no_window_kwargs(),
    )
    proc.wait(timeout=60)
    assert procutil.pid_state(proc.pid) == "dead", "the exited child must probe as dead before the test relies on it"
    yield proc.pid
    del proc


def await_death(pid: int, *, timeout: float = 10.0) -> None:
    """Wait until the process table positively reports this pid gone."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if procutil.pid_state(pid) == "dead":
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} was still not reported dead after {timeout}s")


# --------------------------------------------------------------------------
# The required ordering
# --------------------------------------------------------------------------


def test_caller_abandoned_worker_still_alive_makes_the_workspace_unsafe(
    db_path, tmp_path, live_worker
):
    """delegation starts -> transport gives up -> worker lives -> the workspace says so.

    This is the real incident's timeline with the fix in place. Every step is the real
    one: a real child process, the real caller-abandonment write, the real pid
    probe. Only the transport ceiling is simulated, and it is simulated by
    doing exactly what the ceiling does -- recording that the caller left.
    """

    workspace = tmp_path / "worktrees" / "issue-111-c3"
    workspace.mkdir(parents=True)
    worker = live_worker()
    attempt_id = open_attempt(db_path, workspace, pid=worker.pid)

    # Before the ceiling: someone is waiting, and the worker is running.
    assert verdict(db_path, workspace) == "may_be_mutating"

    # The 900 s ceiling. The caller is told the call failed; the abandonment
    # write records only that the caller left, and nothing about the worker.
    assert service_history.record_caller_abandonment(
        db_path, attempt_id, reason="transport_cancelled"
    )

    # The row is deliberately still open, and the worker is deliberately still
    # alive. That is the settled behaviour, not the defect.
    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "may_be_mutating"
    assert "may change under you" in answer["statement"]
    assert f"pid {worker.pid} is alive" in answer["reason"]
    [entry] = answer["attempts"]
    assert entry["id"] == attempt_id
    assert entry["caller_abandoned"] is True
    assert entry["workspace_relation"] == "same"
    assert entry["workspace_hazard"] == "may_be_mutating"

    # The answer may not contradict the recovery outlook's answer for the
    # same row: one of them saying "nothing is running" while the other says
    # "do not start it again" is the ambiguity this check exists to remove.
    outlook = derive_recovery_outlook(facts_from_attempt(entry, worker_alive=True))
    assert outlook.state == "unattended"
    assert outlook.worker_may_be_running is True
    assert outlook.safe_to_retry is not True

    # And when the worker dies without the row ever resolving, the answer does
    # NOT become clean. An open row with a dead pid is a contradiction, and on
    # Windows it is the exact shape of a `.cmd` shim whose child outlived it.
    worker.kill()
    await_death(worker.pid)
    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    assert "the row and the process table disagree" in answer["reason"]

    # Only the delegation actually ending clears it.
    service_history.resolve_delegation_attempt(
        db_path, attempt_id, status="completed", outcome="completed"
    )
    assert verdict(db_path, workspace) == "no_delegation_in_flight"


def test_a_second_workspace_is_not_implicated_by_the_first(db_path, tmp_path, live_worker):
    """Scoping: a live worker makes ITS workspace unsafe, not every workspace."""

    mine = tmp_path / "worktrees" / "mine"
    theirs = tmp_path / "worktrees" / "theirs"
    mine.mkdir(parents=True)
    theirs.mkdir(parents=True)
    worker = live_worker()
    open_attempt(db_path, mine, pid=worker.pid)

    assert verdict(db_path, mine) == "may_be_mutating"
    assert verdict(db_path, theirs) == "no_delegation_in_flight"


def test_the_relation_is_symmetric_between_a_tree_and_what_is_inside_it(
    db_path, tmp_path, live_worker
):
    """A worker assigned a parent can write into a child, and the reverse."""

    root = tmp_path / "repo"
    nested = root / "src" / "modelquad"
    nested.mkdir(parents=True)
    worker = live_worker()
    open_attempt(db_path, nested, pid=worker.pid)

    # Asking about the tree that CONTAINS the worker's workspace.
    answer = service_history.workspace_delegation_liveness(db_path, root)
    assert answer["verdict"] == "may_be_mutating"
    assert answer["attempts"][0]["workspace_relation"] == "inside"
    # Asking about the workspace itself.
    assert verdict(db_path, nested) == "may_be_mutating"
    # And a sibling of the worker's workspace is not implicated.
    sibling = root / "dashboard"
    sibling.mkdir()
    assert verdict(db_path, sibling) == "no_delegation_in_flight"


def test_a_worker_assigned_the_repo_root_implicates_a_directory_inside_it(
    db_path, tmp_path, live_worker
):
    root = tmp_path / "repo"
    inside = root / "tests"
    inside.mkdir(parents=True)
    worker = live_worker()
    open_attempt(db_path, root, pid=worker.pid)

    answer = service_history.workspace_delegation_liveness(db_path, inside)
    assert answer["verdict"] == "may_be_mutating"
    assert answer["attempts"][0]["workspace_relation"] == "contains"


def test_the_path_is_matched_the_way_the_filesystem_matches_it(db_path, tmp_path, live_worker):
    """Case is folded with `os.path.normcase`, not assumed either way."""

    workspace = tmp_path / "Worktrees" / "Issue-147"
    workspace.mkdir(parents=True)
    worker = live_worker()
    open_attempt(db_path, workspace, pid=worker.pid)

    spelled_differently = Path(str(workspace).upper())
    expected = "may_be_mutating" if os.name == "nt" else "no_delegation_in_flight"
    assert verdict(db_path, spelled_differently) == expected


def test_a_sibling_is_not_matched_by_a_shared_prefix(db_path, tmp_path, live_worker):
    """`.../repo` must not match `.../repo-two`.

    Hashing whole path COMPONENTS is what makes this true by construction; a
    string prefix comparison gets it wrong the moment it forgets a separator.
    """

    repo = tmp_path / "repo"
    repo_two = tmp_path / "repo-two"
    repo.mkdir()
    repo_two.mkdir()
    worker = live_worker()
    open_attempt(db_path, repo_two, pid=worker.pid)

    assert verdict(db_path, repo) == "no_delegation_in_flight"
    assert verdict(db_path, repo_two) == "may_be_mutating"


def test_a_drive_or_share_root_still_contains_what_is_under_it(db_path, tmp_path, live_worker):
    """The root is where separator arithmetic goes wrong, so it is pinned.

    Asking about `C:\\` (or `/`) must find a worker under it. A prefix check
    that appends a separator to an already-terminated root produces a doubled
    separator, matches nothing, and answers a safety question with a false
    "no".
    """

    workspace = tmp_path / "repo"
    workspace.mkdir()
    worker = live_worker()
    open_attempt(db_path, workspace, pid=worker.pid)

    root = Path(workspace.anchor)
    assert str(root)  # a real anchor on both platforms
    answer = service_history.workspace_delegation_liveness(db_path, root)
    assert answer["verdict"] == "may_be_mutating"
    assert answer["attempts"][0]["workspace_relation"] == "inside"


def test_the_stored_identity_is_not_the_path(db_path, tmp_path):
    """`PRIVACY.md` promises the history never keeps a full path. Hold it to that.

    The whole reason the workspace is hashed rather than stored is that a
    safety feature may not quietly buy an exemption from a published claim.
    """

    workspace = tmp_path / "very-distinctive-directory-name"
    workspace.mkdir()
    open_attempt(db_path, workspace)

    [row] = service_history.fetch_delegation_attempts(db_path, limit=1)
    assert row["workspace_key"] and len(row["workspace_key"]) == 32
    ancestors = json.loads(row["workspace_ancestors"])
    assert ancestors and all(len(item) == 32 for item in ancestors)

    blob = json.dumps(row, default=str)
    assert "very-distinctive-directory-name" not in blob
    assert str(workspace) not in blob
    assert str(workspace.parent) not in blob


def test_the_identity_is_canonical_so_a_relative_or_dotted_path_still_matches(
    db_path, tmp_path, live_worker
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    worker = live_worker()
    open_attempt(db_path, str(workspace / "sub" / ".."), pid=worker.pid)

    assert verdict(db_path, workspace) == "may_be_mutating"


def test_workspace_identity_never_raises_on_a_path_it_cannot_read():
    assert service_history.workspace_identity(None) == (None, None)
    assert service_history.workspace_identity("\x00bad") == (None, None)


# --------------------------------------------------------------------------
# Nothing that merely failed to observe may produce a clean answer
# --------------------------------------------------------------------------


def test_a_pid_that_cannot_be_probed_is_unknown_and_unknown_is_not_a_no(
    db_path, tmp_path, monkeypatch, dead_pid):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    open_attempt(db_path, workspace, pid=dead_pid)
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "unknown")

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    assert "not a 'no'" in answer["statement"]
    assert "could not be probed" in answer["reason"]


def test_an_open_attempt_with_no_worker_pid_is_unknown_at_any_age(db_path, tmp_path):
    """Nothing observed is not the same as nothing running.

    `record_worker_pid` is best effort and tolerates a persistence failure, and
    before this Issue the plan-execution paths passed no `on_spawn` at all. A
    pid-less open row therefore really happens, and the age fallback
    `annotate_attempt_liveness` uses for one is a label, not an observation: a
    901-second-old row used to read clean while its worker had 2400 seconds of
    budget left.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(db_path, workspace)

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    assert "no worker process was ever recorded" in answer["reason"]

    # And age does not change it, in either direction.
    much_later = datetime.now(timezone.utc) + timedelta(days=1)
    assert verdict(db_path, workspace, now=much_later) == "unknown"

    service_history.resolve_delegation_attempt(
        db_path, attempt_id, status="completed", outcome="completed"
    )
    assert verdict(db_path, workspace) == "no_delegation_in_flight"


def test_an_open_row_whose_pid_is_dead_is_a_contradiction_not_reassurance(db_path, tmp_path, dead_pid):
    """The window the timeout path itself opens.

    `run_with_tree_kill` kills the direct child, drains its pipes, and only
    then does the caller resolve the row. A workspace question landing inside
    that window sees an open row with a dead pid -- and if a descendant
    survived the tree walk it is still writing.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    open_attempt(db_path, workspace, pid=dead_pid)  # never existed: positively dead

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    assert "may have outlived it" in answer["reason"]


def test_a_history_that_cannot_be_read_is_unknown_rather_than_silent(tmp_path):
    """An unopenable store has not said "nothing is running here"."""

    unreadable = tmp_path / "not-a-database"
    unreadable.mkdir()

    answer = service_history.workspace_delegation_liveness(unreadable, tmp_path)
    assert answer["verdict"] == "unknown"
    assert "could not be read" in answer["reason"]


def test_a_history_that_does_not_exist_is_unknown_and_is_not_created(tmp_path):
    """A store this question brought into existence cannot then certify itself.

    `connect_and_init` creates the database. Reading a workspace answer out of
    a file that did not exist a line earlier is an empty result dressed up as
    evidence of absence.
    """

    missing = tmp_path / "nothing-here.sqlite"
    answer = service_history.workspace_delegation_liveness(missing, tmp_path)
    assert answer["verdict"] == "unknown"
    assert "no delegation history exists" in answer["reason"]
    assert not missing.exists(), "a read must not create the state store"


def test_a_scan_that_did_not_finish_never_reports_a_quiet_workspace(
    db_path, tmp_path, monkeypatch, dead_pid):
    """"Nothing in the part I looked at" is not "nothing"."""

    monkeypatch.setattr(service_history, "_WORKSPACE_SCAN_LIMIT", 3)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Enough rows FOR THIS WORKSPACE to fill the open-row scan, each settled so
    # the only thing under test is the unfinished scan itself.
    for _ in range(4):
        attempt = open_attempt(db_path, workspace)
        service_history.record_worker_pid(db_path, attempt, dead_pid)

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    assert answer["scan_truncated"] is True


def test_unrelated_history_cannot_hold_a_workspace_at_unknown(
    db_path, tmp_path, monkeypatch, dead_pid):
    """The scans are narrowed to this workspace in SQL.

    A global scan meant a couple of hundred unconfirmed timeouts anywhere --
    they are never cleaned up, by design -- filled the bound and held EVERY
    directory at `unknown` for ever, including one no delegation had ever
    touched.
    """

    monkeypatch.setattr(service_history, "_WORKSPACE_SCAN_LIMIT", 3)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for _ in range(10):
        attempt = open_attempt(db_path, elsewhere, pid=dead_pid)
        service_history.resolve_delegation_attempt(
            db_path, attempt, status="failed", failure_kind="timeout", detail="unaccounted"
        )

    asked_about = tmp_path / "asked-about"
    asked_about.mkdir()
    answer = service_history.workspace_delegation_liveness(db_path, asked_about)
    assert answer["verdict"] == "no_delegation_in_flight"
    assert answer["scan_truncated"] is False
    # And the workspace those rows really are about is still held in doubt.
    assert verdict(db_path, elsewhere) == "unknown"


def test_closed_history_cannot_crowd_the_open_rows_out_of_the_scan(
    db_path, tmp_path, monkeypatch, live_worker
):
    """The two populations are bounded separately, and that is load-bearing.

    Closed rows accumulate forever. Sharing one allowance with them would let
    old history push the live worker off the end of the scan, and the answer
    would be `unknown` at best and clean at worst -- for a workspace with a
    process writing to it right now.
    """

    monkeypatch.setattr(service_history, "_WORKSPACE_SCAN_LIMIT", 3)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for _ in range(10):
        attempt = open_attempt(db_path, elsewhere)
        service_history.resolve_delegation_attempt(
            db_path, attempt, status="failed", failure_kind="timeout", detail="unaccounted"
        )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    worker = live_worker()
    open_attempt(db_path, workspace, pid=worker.pid)

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "may_be_mutating"


def test_an_unresolvable_workspace_path_is_unknown(db_path):
    answer = service_history.workspace_delegation_liveness(db_path, "\x00not-a-path")
    assert answer["verdict"] == "unknown"


def test_an_attempt_this_store_cannot_place_is_never_ruled_out(db_path, tmp_path, dead_pid):
    """A legacy NULL workspace is an absence, and an absence rules nothing out.

    A v17 row can survive migration open, with a live worker, and no workspace
    on it. It must not be reported as being about this directory -- and it must
    not let this directory be called clear either.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    legacy = service_history.record_delegation_attempt(db_path, task_type="implementation")
    service_history.record_worker_pid(db_path, legacy, dead_pid)

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    assert "no workspace recorded in this store" in answer["reason"]
    # Not ATTRIBUTED to this workspace, merely not excluded from it.
    assert answer["attempts"] == []

    service_history.resolve_delegation_attempt(
        db_path, legacy, status="completed", outcome="completed"
    )
    assert verdict(db_path, workspace) == "no_delegation_in_flight"


def test_resolving_an_unplaceable_row_badly_does_not_erase_its_doubt(db_path, tmp_path, dead_pid):
    """The hazard must not be discardable by closing the row.

    An unplaceable row used to be read only while it was OPEN, so resolving it
    as an unconfirmed timeout -- the very ending that means "a descendant may
    still be writing" -- dropped it out of the population and turned the
    workspace clean. Resolution clears a row only when its ending accounts for
    the worker; that is the same rule everywhere else, and having a workspace
    on the row is not what earns it.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    legacy = service_history.record_delegation_attempt(db_path, task_type="implementation")
    service_history.record_worker_pid(db_path, legacy, dead_pid)
    assert verdict(db_path, workspace) == "unknown"

    service_history.resolve_delegation_attempt(
        db_path, legacy, status="failed", failure_kind="timeout", detail="unaccounted"
    )
    assert verdict(db_path, workspace) == "unknown"

    # Reconciliation reaches the same non-answer, and must not clear it either.
    other = service_history.record_delegation_attempt(db_path, task_type="implementation")
    service_history.resolve_delegation_attempt(
        db_path, other, status="failed", failure_kind="orphaned", detail="reconciled"
    )
    assert verdict(db_path, workspace) == "unknown"


def test_an_unplaceable_row_never_claims_a_workspace_is_being_written_to(
    db_path, tmp_path, live_worker
):
    """It contributes doubt, never a positive claim about THIS directory."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    legacy = service_history.record_delegation_attempt(db_path, task_type="implementation")
    service_history.record_worker_pid(db_path, legacy, live_worker().pid)

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"


def test_an_accounted_unplaceable_row_is_simply_history(db_path, tmp_path):
    """An ending that accounts for the worker clears it, workspace or not."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    legacy = service_history.record_delegation_attempt(db_path, task_type="implementation")
    service_history.resolve_delegation_attempt(
        db_path, legacy, status="failed", failure_kind="policy_refused", detail="refused"
    )

    assert verdict(db_path, workspace) == "no_delegation_in_flight"


def test_an_extended_length_spelling_is_the_same_directory(db_path, tmp_path, live_worker):
    r"""The Win32 extended-length spelling names the same directory.

    `\\?\E:\x` and `E:\x` are one directory -- `Path.samefile` says so -- and
    `Path.resolve` keeps the prefix. Hashing them differently meant a live
    worker recorded under the ordinary spelling read as NOTHING AT ALL when
    asked about under the extended one: a false clean answer produced purely
    by how the path was typed.
    """

    prefix = "\\\\?\\" if os.name == "nt" else ""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    worker = live_worker()
    open_attempt(db_path, workspace, pid=worker.pid)

    assert verdict(db_path, prefix + str(workspace)) == "may_be_mutating"
    # And in the other direction: recorded extended, asked ordinary.
    other = tmp_path / "other"
    other.mkdir()
    open_attempt(db_path, prefix + str(other), pid=worker.pid)
    assert verdict(db_path, other) == "may_be_mutating"


@pytest.mark.skipif(os.name != "nt", reason="Win32 extended-length prefixes")
def test_the_unc_extended_prefix_folds_to_the_plain_unc_spelling():
    from modelquad.service.history import _workspace_hash

    extended = "\\\\?\\UNC\\server\\share\\repo"
    plain = "\\\\server\\share\\repo"
    assert _workspace_hash(Path(extended)) == _workspace_hash(Path(plain))


@pytest.mark.parametrize("corrupt", ['{"not": "a list"}', "not json at all", "null", "0", "[1, 2]"])
def test_unreadable_ancestry_is_uncertainty_and_never_unrelated(
    db_path, tmp_path, live_worker, corrupt
):
    """Damage must not read as "that row is about somewhere else".

    A row whose ancestry cannot be parsed is a row whose position is unknown.
    Treating an empty parse as "no ancestors" turned a live worker in a
    subdirectory into a clean answer for its parent -- and `null` and `0` are
    valid JSON that used to raise straight out of the query.
    """

    root = tmp_path / "repo"
    nested = root / "src"
    nested.mkdir(parents=True)
    worker = live_worker()
    attempt_id = open_attempt(db_path, nested, pid=worker.pid)
    assert verdict(db_path, root) == "may_be_mutating"

    conn = service_db.connect_and_init(db_path)
    try:
        conn.execute(
            "UPDATE delegation_attempts SET workspace_ancestors = ? WHERE id = ?",
            (corrupt, attempt_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Still a hazard, and it did not raise.
    assert verdict(db_path, root) in {"may_be_mutating", "unknown"}
    assert verdict(db_path, root) != "no_delegation_in_flight"


def test_the_query_neither_creates_nor_migrates_the_state_store(tmp_path):
    """It is advertised read-only; hold it to that at the sqlite level.

    An existing but uninitialised store used to come back schema 18 with two
    dozen objects in it, created by the read, and then be declared clean.
    """

    store = tmp_path / "empty.sqlite"
    store.touch()
    before = store.stat().st_size

    answer = service_history.workspace_delegation_liveness(store, tmp_path)
    assert answer["verdict"] == "unknown"
    assert store.stat().st_size == before, "a read must not initialise the store"

    import sqlite3

    conn = sqlite3.connect(store)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        tables = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
        assert tables == 0, "a read must not create tables"
    finally:
        conn.close()


def test_a_workspace_whose_own_ancestry_cannot_be_read_is_unknown(db_path, monkeypatch):
    monkeypatch.setattr(
        service_history, "_workspace_ancestor_set", lambda value: None
    )
    answer = service_history.workspace_delegation_liveness(db_path, ".")
    assert answer["verdict"] == "unknown"


def test_a_resolution_loop_does_not_take_the_query_down(monkeypatch):
    """`Path.resolve` raises RuntimeError on a symlink loop; a probe may not."""

    def loop(self, *args, **kwargs):
        raise RuntimeError("Symlink loop from '...'")

    monkeypatch.setattr(Path, "resolve", loop)
    assert service_history.workspace_identity("anywhere") == (None, None)

# --------------------------------------------------------------------------
# Process exit is not proof of workspace stability
# --------------------------------------------------------------------------


def test_a_timeout_whose_tree_was_never_confirmed_dead_keeps_the_workspace_in_doubt(
    db_path, tmp_path, dead_pid):
    """`failure_kind='timeout'` means the kill was attempted and not confirmed.

    On Windows the pid on the row is a `.cmd` shim; its death is not the node
    process's death. Reading the closed row's dead pid as "nothing is writing
    here" would be the same false clean answer in a new place.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(db_path, workspace, pid=dead_pid)
    service_history.resolve_delegation_attempt(
        db_path,
        attempt_id,
        status="failed",
        failure_kind="timeout",
        detail=f"worker pid {dead_pid}: kill attempted (tree kill exit 128) but NOT confirmed dead",
    )

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    assert "descendant of it may still be writing here" in answer["reason"]
    [entry] = answer["attempts"]
    assert entry["workspace_hazard"] == "unknown"


def test_that_doubt_does_not_expire_with_time(db_path, tmp_path, dead_pid):
    """Elapsed time is the absence of evidence, not evidence.

    A descendant that survived a failed tree walk has no remaining lifetime
    anything here can bound, so nothing here may decide on its behalf that it
    has stopped. UNKNOWN stays UNKNOWN.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(db_path, workspace, pid=dead_pid)
    service_history.resolve_delegation_attempt(
        db_path, attempt_id, status="failed", failure_kind="timeout", detail="unaccounted for"
    )

    much_later = datetime.now(timezone.utc) + timedelta(days=30)
    assert verdict(db_path, workspace, now=much_later) == "unknown"


def test_a_reconciled_orphan_also_leaves_doubt(db_path, tmp_path, dead_pid):
    """`orphaned` closes a row because the recorded PID was found dead.

    That is a fact about that pid. Reconciliation never killed anything and
    never looked at a descendant, so it cannot speak for one.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(db_path, workspace, pid=dead_pid)
    service_history.resolve_delegation_attempt(
        db_path, attempt_id, status="failed", failure_kind="orphaned", detail="reconciled"
    )

    assert verdict(db_path, workspace) == "unknown"


def test_a_confirmed_tree_termination_leaves_no_doubt_behind(db_path, tmp_path, dead_pid):
    """`terminated` is written only when the whole tree's death was READ."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(db_path, workspace, pid=dead_pid)
    service_history.resolve_delegation_attempt(
        db_path,
        attempt_id,
        status="failed",
        failure_kind="terminated",
        detail=f"worker pid {dead_pid} terminated (tree kill ok; exit confirmed)",
    )

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "no_delegation_in_flight"
    assert answer["attempts"] == []


@pytest.mark.parametrize(
    "failure_kind",
    ["policy_refused", "no_viable_worker", "provider_unavailable", "failed_to_start"],
)
def test_an_ending_that_accounts_for_the_worker_clears_the_workspace(
    db_path, tmp_path, failure_kind, dead_pid):
    """A worker that never started is accounted for -- on a row with no pid.

    Each of these endings asserts nothing ran, and that is believable exactly
    where nothing was observed to run. The same label on a row that DID record
    a pid is a contradiction, not an accounting, and is covered separately.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(db_path, workspace)
    service_history.resolve_delegation_attempt(
        db_path, attempt_id, status="failed", failure_kind=failure_kind, detail="d"
    )

    assert verdict(db_path, workspace) == "no_delegation_in_flight"

    contradicted = open_attempt(db_path, workspace, pid=dead_pid)
    service_history.resolve_delegation_attempt(
        db_path, contradicted, status="failed", failure_kind=failure_kind, detail="d"
    )
    assert verdict(db_path, workspace) == "unknown"


@pytest.mark.parametrize("failure_kind", ["exception", "internal_error"])
def test_an_uncharacterised_failure_does_not_account_for_the_worker(
    db_path, tmp_path, failure_kind, dead_pid):
    """`exception` is written by a wrapper that catches everything.

    Including something raised AFTER the worker was spawned -- a delegation
    that blew up mid-flight can leave a process behind exactly as a timeout
    can. "We do not know what happened" is the opposite of an accounting.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(db_path, workspace, pid=dead_pid)
    service_history.resolve_delegation_attempt(
        db_path, attempt_id, status="failed", failure_kind=failure_kind, detail="boom"
    )

    assert verdict(db_path, workspace) == "unknown"


def test_the_accounted_endings_are_a_subset_of_the_recorded_vocabulary():
    """A typo here would silently widen what counts as settled."""

    known = set(service_history.DELEGATION_FAILURE_KINDS) | {None}
    assert service_history._ENDINGS_THAT_ACCOUNT_FOR_THE_WORKER <= known
    # The ones that must never be in it, and the reason this feature exists.
    # `timeout` and `orphaned` leave descendants unaccounted for; `exception`
    # and `internal_error` are "something went wrong and nobody characterised
    # it", written by a wrapper that catches anything raised after a worker may
    # already have been spawned.
    for kind in ("timeout", "orphaned", "exception", "internal_error"):
        assert kind not in service_history._ENDINGS_THAT_ACCOUNT_FOR_THE_WORKER


def test_a_completed_delegation_leaves_the_workspace_clear(db_path, tmp_path, live_worker):
    """A resolved, accounted-for attempt is not a writer, even with a live pid.

    The pid is recorded at spawn and never cleared; what closes the question is
    the row resolving with an ending that accounts for the worker.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    worker = live_worker()
    attempt_id = open_attempt(db_path, workspace, pid=worker.pid)
    service_history.resolve_delegation_attempt(
        db_path, attempt_id, status="completed", outcome="completed"
    )

    assert verdict(db_path, workspace) == "no_delegation_in_flight"


# --------------------------------------------------------------------------
# Windows process trees: the direct child is not the worker
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree behaviour")
def test_a_grandchild_survives_a_direct_child_kill_and_the_answer_says_so(
    db_path, tmp_path, live_worker
):
    """The premise the whole "a dead pid is not clean" rule rests on.

    Killing only the direct child leaves its child running. That is the exact
    shape of a provider CLI on Windows (`.cmd` shim -> node.exe). The workspace
    question is asked here, not merely the process probe, because the point is
    that the ANSWER refuses to call this settled.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    script = textwrap.dedent(
        """
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        print(child.pid, flush=True)
        time.sleep(120)
        """
    )
    parent = live_worker(script)
    grandchild_pid = int(parent.stdout.readline().decode().strip())
    attempt_id = open_attempt(db_path, workspace, pid=parent.pid)
    assert procutil.pid_state(grandchild_pid) == "alive"

    parent.kill()
    await_death(parent.pid)
    try:
        assert procutil.pid_state(grandchild_pid) == "alive"
        # The recorded pid is gone and a writer is not. Neither the open row
        # nor the row closed as an unconfirmed timeout may read clean.
        assert verdict(db_path, workspace) == "unknown"
        service_history.resolve_delegation_attempt(
            db_path,
            attempt_id,
            status="failed",
            failure_kind="timeout",
            detail="kill attempted but NOT confirmed dead",
        )
        assert verdict(db_path, workspace) == "unknown"
    finally:
        procutil.terminate_pid(grandchild_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree behaviour")
@pytest.mark.serial_timing
def test_the_tree_kill_a_worker_timeout_uses_reaches_the_grandchild(tmp_path):
    """And when the tree walk DOES succeed, the doubt is genuinely gone.

    `run_with_tree_kill` is what a worker's own budget expiry runs, and it is
    the only thing entitled to produce `terminated`. This pins that its facts
    are earned: the grandchild is dead, and `confirmed_dead`/`tree_kill` say so.
    """

    from modelquad.execution.base import worker_confirmed_dead

    marker = tmp_path / "grandchild.pid"
    script = textwrap.dedent(
        f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open(r"{marker}", "w").write(str(child.pid))
        time.sleep(120)
        """
    )
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        procutil.run_with_tree_kill(
            [sys.executable, "-c", script], capture_output=True, timeout=5.0
        )
    facts = getattr(raised.value, "worker_termination", None)
    assert isinstance(facts, dict)
    assert facts["tree_kill"] == "ok"
    assert facts["confirmed_dead"] is True
    assert worker_confirmed_dead({"worker_termination": facts}) is True

    grandchild_pid = int(marker.read_text().strip())
    await_death(grandchild_pid)


# --------------------------------------------------------------------------
# The answer is only as good as the rows behind it
# --------------------------------------------------------------------------


def _started_attempt_calls(path: Path) -> list[ast.Call]:
    """Every `record_delegation_attempt(...)` that may OPEN a row, in one file.

    A `status` that is not a literal is treated as opening one: this is a
    safety guard, and it may not be talked out of a finding by a name it
    cannot evaluate.

    Known residual: a call reached through a local alias
    (``f = record_delegation_attempt``) is invisible to any AST match on the
    callee's name. Nothing in this repository does that, and the runtime cannot
    detect it either, so it is recorded here rather than papered over.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "record_delegation_attempt":
            continue
        status = next((kw for kw in node.keywords if kw.arg == "status"), None)
        if status is None:
            calls.append(node)  # defaults to 'started'
        elif not isinstance(status.value, ast.Constant):
            calls.append(node)  # not statically a refusal; assume it opens
        elif status.value.value == "started":
            calls.append(node)
    return calls


def test_every_path_that_opens_an_attempt_records_its_workspace():
    """A delegation path that forgets this makes the workspace answer worse.

    An open row with no workspace cannot be attributed to any directory, so it
    holds every workspace at `unknown` until it resolves -- fail-safe, but it
    turns the answer into noise. The whole of `src/` is scanned, not a list of
    files, so a new module cannot quietly join the delegation paths.
    """

    offenders: list[str] = []
    scanned = 0
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        for node in _started_attempt_calls(path):
            scanned += 1
            keywords = {kw.arg for kw in node.keywords}
            # `**kwargs` forwarding carries an unknowable set; a call that
            # forwards is accepted only if it also names the workspace, which
            # is the same bar every explicit call has to clear.
            if "working_directory" not in keywords:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert scanned, "no opening attempt calls found anywhere in src/ -- have they moved?"
    assert not offenders, (
        "these open a delegation attempt without recording its workspace: "
        + ", ".join(offenders)
        + " -- that row can then be attributed to no directory"
    )


def test_every_delegation_that_spawns_a_worker_reports_its_pid():
    """An attempt with no pid is an attempt nothing has been observed about.

    The plan-execution paths used to pass no `on_spawn`, so every
    `modelquad_submit_plan` delegation recorded a worker pid of NULL and every
    workspace it touched was permanently `unknown`.
    """

    # PERF-1010-1 moved the bound launch out of the MCP server; scan both.
    delegating = []
    for module in (("mcp", "server.py"), ("execution", "bound_launch.py")):
        source = (REPO_ROOT / "src" / "modelquad" / module[0] / module[1]).read_text(
            encoding="utf-8"
        )
        delegating += [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None))
            in {"delegate"}
        ]
    assert delegating, "no delegating calls found in the MCP server or bound launch -- have they moved?"
    missing = [
        node.lineno
        for node in delegating
        if not any(kw.arg == "on_spawn" for kw in node.keywords)
    ]
    assert not missing, (
        f"delegating calls at line(s) {missing} do not report the worker's pid; "
        "the attempt row then records no worker at all"
    )


# --------------------------------------------------------------------------
# The surfaces that carry the answer
# --------------------------------------------------------------------------


def test_delegation_status_answers_a_bare_workspace(db_path, tmp_path, monkeypatch, live_worker):
    mcp_server = importlib.import_module("modelquad.mcp.server")
    monkeypatch.setattr(mcp_server.service_db, "DEFAULT_DB_PATH", db_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    worker = live_worker()
    attempt_id = open_attempt(db_path, workspace, pid=worker.pid)
    service_history.record_caller_abandonment(
        db_path, attempt_id, reason="transport_cancelled"
    )

    answer = _tool(mcp_server, "modelquad_delegation_status")(
        working_directory=str(workspace)
    )
    assert answer["query"]["identified_by"] == ["workspace"]
    assert answer["workspace"]["verdict"] == "may_be_mutating"
    [entry] = answer["workspace"]["attempts"]
    assert entry["attempt_id"] == attempt_id
    assert entry["state"] == "unattended"
    assert entry["safe_to_retry"] is not True
    assert entry["workspace_relation"] == "same"
    assert entry["workspace_hazard"] == "may_be_mutating"
    # The path is the caller's own argument; it is never read back off a row.
    assert "working_directory" not in entry


def test_delegation_status_still_requires_the_whole_triple_or_nothing(db_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    mcp_server = importlib.import_module("modelquad.mcp.server")
    monkeypatch.setattr(mcp_server.service_db, "DEFAULT_DB_PATH", db_path)

    status = _tool(mcp_server, "modelquad_delegation_status")
    # The tool boundary now returns one stable public message: domain
    # validation text can carry paths or input, so it never reaches the model.
    for kwargs in ({"task_type": "implementation"},
                   {"task_type": "implementation", "working_directory": "."},
                   {}):
        with pytest.raises(ToolError, match="could not be accepted"):
            status(**kwargs)


def test_delegation_status_by_request_still_works_and_now_carries_the_workspace(
    db_path, tmp_path, monkeypatch, live_worker
):
    """The recovery path is unchanged, and gains the workspace beside it."""

    mcp_server = importlib.import_module("modelquad.mcp.server")
    monkeypatch.setattr(mcp_server.service_db, "DEFAULT_DB_PATH", db_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    worker = live_worker()
    request_key = service_history.request_fingerprint(
        "implementation", str(workspace), "do the thing"
    )
    attempt_id = open_attempt(db_path, workspace, pid=worker.pid, request_key=request_key)

    answer = _tool(mcp_server, "modelquad_delegation_status")(
        task_type="implementation",
        working_directory=str(workspace),
        prompt="do the thing",
    )
    assert answer["found"] is True
    [entry] = answer["attempts"]
    assert entry["attempt_id"] == attempt_id
    assert entry["state"] == "running"
    # And the workspace question was answered too, because the caller is
    # standing in that directory either way.
    assert answer["workspace"]["verdict"] == "may_be_mutating"


def test_the_cli_fails_closed_on_a_workspace_that_may_be_moving(db_path, tmp_path, live_worker):
    """`modelquad workspace . && pytest` must not run the gate on a live tree."""

    from modelquad.cli import main

    workspace = tmp_path / "ws"
    workspace.mkdir()
    worker = live_worker()
    attempt_id = open_attempt(db_path, workspace, pid=worker.pid)

    args = ["--history-db", str(db_path), "workspace", str(workspace)]
    assert main(args) == 2

    worker.kill()
    await_death(worker.pid)
    # Still not clean: the row never resolved.
    assert main(args) == 3
    service_history.resolve_delegation_attempt(
        db_path, attempt_id, status="completed", outcome="completed"
    )
    assert main(args) == 0


def test_the_cli_reports_unknown_distinguishably(db_path, tmp_path, monkeypatch, dead_pid):
    from modelquad.cli import main

    workspace = tmp_path / "ws"
    workspace.mkdir()
    open_attempt(db_path, workspace, pid=dead_pid)
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "unknown")

    assert main(["--history-db", str(db_path), "workspace", str(workspace)]) == 3


def test_a_timed_out_delegation_result_says_its_worker_may_still_be_writing(dead_pid):
    """The caller reads the RESULT, not the attempt row.

    `describe_worker_termination` has always put "unaccounted for" on the row.
    The party about to test, review or commit the tree is the caller, and it
    was never told.
    """

    from modelquad.execution.base import workspace_stability_note

    note = workspace_stability_note(
        {
            "timed_out": True,
            "worker_termination": {
                "pid": dead_pid,
                "tree_kill": "exit 128",
                "confirmed_dead": False,
            },
        },
        r"C:\work\repo",
    )
    assert note["this_worker_may_still_be_writing"] is True
    assert "unaccounted for" in note["worker_fate"]
    assert r"C:\work\repo" in note["warning"]
    assert "modelquad_delegation_status(working_directory=...)" in note["warning"]


def test_one_worker_confirmed_dead_is_never_a_claim_about_the_workspace(dead_pid):
    """Two different requests can run against one directory at the same time.

    They have different fingerprints, so neither is a duplicate and neither is
    refused. A note that said "the workspace is settled" because THIS worker
    died would contradict the workspace answer, which would still -- correctly
    -- say `may_be_mutating`.
    """

    from modelquad.execution.base import workspace_stability_note

    note = workspace_stability_note(
        {
            "timed_out": True,
            "worker_termination": {"pid": dead_pid, "tree_kill": "ok", "confirmed_dead": True},
        },
        r"C:\work\repo",
    )
    assert note["this_worker_may_still_be_writing"] is False
    assert "warning" not in note
    # Never a workspace-wide claim; always a pointer to the one answer that is.
    assert not any(key.startswith("workspace_may") for key in note)
    assert "modelquad_delegation_status(working_directory=...)" in note["workspace_answer"]


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"timed_out": False},
        {"worker_termination": {"pid": 1, "tree_kill": "ok", "confirmed_dead": True}},
        None,
    ],
)
def test_a_delegation_that_did_not_time_out_makes_no_claim_about_processes(metadata):
    """Inventing a reassurance is as wrong as withholding a warning."""

    from modelquad.execution.base import workspace_stability_note

    assert workspace_stability_note(metadata, r"C:\work\repo") == {}


def test_a_timeout_with_no_termination_facts_at_all_still_warns():
    """A runner that recorded nothing has not proved the worker stopped."""

    from modelquad.execution.base import workspace_stability_note

    note = workspace_stability_note({"timed_out": True}, r"C:\work\repo")
    assert note["this_worker_may_still_be_writing"] is True
    assert note["worker_fate"] == "the runner recorded no termination facts"


@pytest.mark.parametrize('source_path', [
    REPO_ROOT / "src" / "modelquad" / "execution" / "bound_launch.py",
], ids=['bound'])
def test_every_result_boundary_that_closes_an_attempt_carries_the_note(source_path):
    """The direct delegate path is not the only one that returns a worker's outcome.

    `modelquad_delegate` can be executed by the reasoning-primary path, and
    `modelquad_submit_plan` has two result boundaries of its own. Each resolves
    a delegation attempt from an execution result, and each used to return that
    result without the timeout warning -- so the same tool call lost its warning
    depending on which internal path ran it.

    The structural invariant: a function that closes an attempt from a worker's
    result must also annotate that result for the caller.
    """

    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    def calls(node) -> set[str]:
        return {
            getattr(inner.func, "id", getattr(inner.func, "attr", None))
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
        }

    resolving = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "attempt_resolution" in calls(node)
    ]
    assert resolving, f"{source_path.name}: nothing resolves an attempt from a result -- has it moved?"
    missing = [
        node.name for node in resolving if "workspace_stability_note" not in calls(node)
    ]
    assert not missing, (
        f"{source_path.name}: {missing} close a delegation attempt from a worker's result without "
        "telling the caller whether that worker may still be writing"
    )


def test_recovery_and_the_workspace_answer_agree_about_an_unconfirmed_timeout(
    db_path, tmp_path, dead_pid):
    """One row may not be "a descendant may be writing" and "not running" at once.

    `worker_may_be_running` asks MAY, and for a fate nobody established the
    honest answer is yes. It used to read False -- asserting the worker had
    stopped on the strength of not knowing -- beside a workspace answer saying
    the opposite about the same attempt.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(db_path, workspace, pid=dead_pid)
    service_history.resolve_delegation_attempt(
        db_path,
        attempt_id,
        status="failed",
        failure_kind="timeout",
        detail="kill attempted but NOT confirmed dead",
    )

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    [entry] = answer["attempts"]
    outlook = derive_recovery_outlook(facts_from_attempt(entry, worker_alive=None))
    assert outlook.state == "unknown"
    assert outlook.worker_may_be_running is True
    # The delegation-status tri-state is untouched: unestablished is still not "unsafe".
    assert outlook.safe_to_retry is None


def test_a_terminal_worker_report_still_says_nothing_is_running():
    """The change above may not leak into states the worker DID report."""

    from modelquad.mcp.livelifecycle import LiveFacts

    for task_status in ("completed", "failed", "blocked", "partial"):
        outlook = derive_recovery_outlook(
            LiveFacts(
                attempt_exists=True,
                attempt_status="failed",
                attempt_resolved=True,
                task_status=task_status,
            )
        )
        assert outlook.worker_may_be_running is False, task_status
        assert outlook.safe_to_retry is True, task_status


@pytest.mark.parametrize(
    "store_name",
    ["plain.sqlite", "with space.sqlite", "hash#tag.sqlite", "pct%20.sqlite", "amp&and.sqlite"],
)
def test_the_store_reads_from_a_path_with_uri_metacharacters(tmp_path, store_name):
    """SQLite parses the read-only URI, so the path has to survive being one.

    `?` starts URI parameters, `#` starts a fragment and `%` starts an escape.
    A history under a directory containing one of those failed to open, which
    failed SAFE -- an unopenable store answers `unknown` -- but a question that
    cannot read a perfectly good history is one nobody keeps asking.
    """

    store = tmp_path / store_name
    service_db.connect_and_init(store).close()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(store, workspace)
    service_history.resolve_delegation_attempt(
        store, attempt_id, status="completed", outcome="completed"
    )

    answer = service_history.workspace_delegation_liveness(store, workspace)
    assert answer["verdict"] == "no_delegation_in_flight", answer.get("reason")


def test_a_store_whose_schema_predates_the_column_answers_unknown(tmp_path):
    """A read-only query cannot migrate, and must not pretend it read one.

    Selecting `workspace_key` from a table that has no such column raises, and
    the honest answer to "may anything be writing here?" from a store this code
    cannot read in the shape it needs is `unknown` -- never a clean verdict.
    """

    store = tmp_path / "old.sqlite"
    conn = service_db.connect_and_init(store)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_delegation_attempts_workspace")
        conn.execute("ALTER TABLE delegation_attempts DROP COLUMN workspace_key")
        conn.commit()
    finally:
        conn.close()

    answer = service_history.workspace_delegation_liveness(store, tmp_path)
    assert answer["verdict"] == "unknown"
    assert "could not be read" in answer["reason"]


def test_an_ending_claiming_nothing_ran_is_not_believed_over_a_recorded_pid(
    db_path, tmp_path
):
    """`failed_to_start` on a row that HAS a worker pid is a contradiction.

    An `OSError` on the pipe after `Popen` already succeeded is classified
    `launch_failed` and resolves `failed_to_start` -- with the pid published
    through `on_spawn` a moment earlier. Believing the label there clears a
    workspace whose worker is running.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(db_path, workspace, pid=9999)
    service_history.resolve_delegation_attempt(
        db_path, attempt_id, status="failed", failure_kind="failed_to_start", detail="launch_failed"
    )

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    assert "disagree about whether anything ran" in answer["reason"]


def test_the_same_ending_is_believed_when_nothing_was_ever_observed_to_run(
    db_path, tmp_path
):
    """A genuine pre-spawn refusal has no pid, and is trusted."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(db_path, workspace)
    service_history.resolve_delegation_attempt(
        db_path, attempt_id, status="refused", failure_kind="policy_refused", detail="no"
    )

    assert verdict(db_path, workspace) == "no_delegation_in_flight"


def test_a_store_that_cannot_even_be_stat_ed_is_unknown_not_an_exception(
    db_path, tmp_path, monkeypatch
):
    """The query is documented never to raise; `Path.exists` can."""

    def denied(self):
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "exists", denied)
    answer = service_history.workspace_delegation_liveness(db_path, tmp_path)
    assert answer["verdict"] == "unknown"
    assert "could not be checked" in answer["reason"]


def test_the_accounting_covers_the_worker_process_and_says_so(db_path, tmp_path, dead_pid):
    """The clean statement must not be read as more than it can support.

    A descendant is enumerated on exactly one path -- the tree walk a timeout
    performs. On a normal exit the direct child is already gone and its
    children are reparented, so a process the worker left behind is outside
    what any of this observes. The statement says that rather than implying a
    guarantee the code does not make.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt_id = open_attempt(db_path, workspace, pid=dead_pid)
    service_history.resolve_delegation_attempt(
        db_path, attempt_id, status="completed", outcome="completed"
    )

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "no_delegation_in_flight"
    statement = answer["statement"]
    assert "worker ModelQuad spawned" in statement
    assert "left running" in statement



# --------------------------------------------------------------------------
# What the worker left running
# --------------------------------------------------------------------------
#
# The workspace answer first accounted for the worker PROCESS ModelQuad
# spawned and said, in as many words, that it did not account for a process
# the worker left behind. The runner now enumerates those at a normal exit and
# the row carries them; the workspace question reads them the way it reads
# the worker's own pid.


def _left_running(pid: int, *, created: float | None = "probe", image: str | None = "probe") -> dict:
    """The facts a runner would attach for one live descendant, from the real process table."""

    if created == "probe":
        created = procutil.process_creation_epoch(pid)
    if image == "probe":
        image = procutil.process_image_name(pid)
    return {
        "enumerated": True,
        "method": "test",
        "observed_at": time.time(),
        "descendants": [{"pid": pid, "image": image, "created": created}],
    }


def _resolve_clean(db_path: Path, attempt: int, **kwargs) -> None:
    service_history.resolve_delegation_attempt(
        db_path, attempt, status="completed", routed_provider="codex", outcome="completed", **kwargs
    )


def test_a_child_the_worker_left_running_keeps_the_workspace_may_be_mutating(db_path, tmp_path, live_worker):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    child = live_worker()
    attempt = open_attempt(db_path, workspace, pid=os.getpid())
    _resolve_clean(db_path, attempt, worker_descendants=_left_running(child.pid))

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "may_be_mutating"
    assert f"pid {child.pid}" in answer["reason"]
    assert "left running by attempt" in answer["reason"]

    child.kill()
    child.wait(timeout=10)
    await_death(child.pid)
    assert verdict(db_path, workspace) == "no_delegation_in_flight"


def test_a_worker_whose_leftovers_could_not_be_enumerated_is_doubt_not_a_no(db_path, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt = open_attempt(db_path, workspace, pid=os.getpid())
    _resolve_clean(
        db_path,
        attempt,
        worker_descendants={"enumerated": False, "method": "toolhelp32", "observed_at": time.time(), "descendants": []},
    )

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    assert "could not be enumerated" in answer["reason"]


def test_a_row_written_before_the_runner_looked_keeps_its_old_verdict(db_path, tmp_path):
    """NULL is not evidence either way: the earlier accounting stands for such rows."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt = open_attempt(db_path, workspace, pid=os.getpid())
    _resolve_clean(db_path, attempt)  # no worker_descendants: the column stays NULL

    with service_db.connect(db_path) as conn:
        assert conn.execute(
            "SELECT worker_descendants FROM delegation_attempts WHERE id = ?", (attempt,)
        ).fetchone()[0] is None
    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "no_delegation_in_flight"
    assert "before ModelQuad looked" in answer["statement"]


def test_a_recycled_descendant_pid_is_not_the_process_the_worker_left(db_path, tmp_path):
    """Alive is not enough: the pid must be the recorded process (image and creation time)."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt = open_attempt(db_path, workspace, pid=os.getpid())
    # This very process is alive with the recorded image, but it was created
    # an hour after the record claims -- so the record's process is gone and
    # the number was recycled.
    created = procutil.process_creation_epoch(os.getpid())
    assert created is not None
    _resolve_clean(db_path, attempt, worker_descendants=_left_running(os.getpid(), created=created - 3600))

    assert verdict(db_path, workspace) == "no_delegation_in_flight"


def test_a_live_descendant_whose_identity_was_not_recorded_is_doubt(db_path, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt = open_attempt(db_path, workspace, pid=os.getpid())
    _resolve_clean(db_path, attempt, worker_descendants=_left_running(os.getpid(), created=None, image=None))

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    assert "recycled pid" in answer["reason"]


def test_a_dead_descendant_settles_the_row(db_path, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    child = subprocess.Popen([sys.executable, "-c", "pass"], **procutil.no_window_kwargs())
    child.wait(timeout=60)
    try:
        attempt = open_attempt(db_path, workspace, pid=os.getpid())
        facts = _left_running(child.pid, created=time.time() - 1, image=Path(sys.executable).name)
        _resolve_clean(db_path, attempt, worker_descendants=facts)

        assert verdict(db_path, workspace) == "no_delegation_in_flight"
    finally:
        del child


def test_the_row_records_only_pid_image_basename_and_creation_time(db_path, tmp_path):
    """No path, no command line, no unbounded list (PRIVACY.md)."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt = open_attempt(db_path, workspace, pid=os.getpid())
    listed = [
        {"pid": 1000 + i, "image": r"C:\Users\someone\secret project\node.exe", "created": 1.0, "argv": ["leak"]}
        for i in range(40)
    ]
    _resolve_clean(
        db_path,
        attempt,
        worker_descendants={"enumerated": True, "method": "toolhelp32", "observed_at": 2.0, "descendants": listed},
    )

    with service_db.connect(db_path) as conn:
        raw = conn.execute("SELECT worker_descendants FROM delegation_attempts WHERE id = ?", (attempt,)).fetchone()[0]
    stored = json.loads(raw)
    assert stored["truncated"] is True
    assert len(stored["descendants"]) == service_history._DESCENDANTS_RECORDED
    assert stored["descendants"][0] == {"pid": 1000, "image": "node.exe", "created": 1.0}
    assert "someone" not in raw and "argv" not in raw and "leak" not in raw
    # More left running than recorded is not an accounting.
    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "unknown"
    assert "more processes running than the row records" in answer["reason"]


def test_resolving_without_facts_never_erases_facts_already_on_the_row(db_path, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt = open_attempt(db_path, workspace, pid=os.getpid())
    _resolve_clean(db_path, attempt, worker_descendants={"enumerated": False, "descendants": []})
    _resolve_clean(db_path, attempt)  # a later writer with nothing to say

    with service_db.connect(db_path) as conn:
        raw = conn.execute("SELECT worker_descendants FROM delegation_attempts WHERE id = ?", (attempt,)).fetchone()[0]
    assert json.loads(raw)["enumerated"] is False


def test_no_kill_was_added_outside_the_timeout_path():
    """Option 2 by construction: enumeration never terminates anything."""

    import inspect

    from modelquad import procutil as pu

    for function in (pu.list_descendants, pu._list_descendants_toolhelp, pu._descendants_at_exit):
        source = inspect.getsource(function)
        for forbidden in ("terminate", "kill", "taskkill", "TerminateProcess", "os.kill"):
            assert forbidden not in source, (function.__name__, forbidden)
    assert "terminate" not in inspect.getsource(service_history._descendant_hazard)
    assert "kill" not in inspect.getsource(service_history._descendant_hazard)


def test_merge_descendant_facts_semantics():
    from modelquad.execution.base import merge_descendant_facts

    a = {"enumerated": True, "observed_at": 1.0, "descendants": [{"pid": 1, "image": "a", "created": 1.0}]}
    b = {"enumerated": True, "observed_at": 2.0, "descendants": [{"pid": 1, "image": "dup", "created": 9.0}, {"pid": 2, "image": "b", "created": 2.0}]}
    failed = {"enumerated": False, "reason": "snapshot_failed", "observed_at": 3.0, "descendants": []}

    assert merge_descendant_facts(None, None) is None
    merged = merge_descendant_facts(a, b)
    assert merged["enumerated"] is True and merged["workers"] == 2 and merged["observed_at"] == 2.0
    # Identity is pid + image + creation time: a different sighting of pid 1 is kept, not folded.
    assert [d["pid"] for d in merged["descendants"]] == [1, 1, 2]
    assert merged["truncated"] is False
    assert merge_descendant_facts(a, None)["reason"] == "worker_not_observed"
    assert merge_descendant_facts(a, failed)["reason"] == "snapshot_failed"
    assert merge_descendant_facts(a, failed)["enumerated"] is False


def test_a_platform_that_does_not_look_keeps_the_147_accounting(db_path, tmp_path):
    """`unsupported_platform` is 'not observed', the same standing as a NULL row."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    attempt = open_attempt(db_path, workspace, pid=os.getpid())
    _resolve_clean(
        db_path,
        attempt,
        worker_descendants={"enumerated": False, "reason": "unsupported_platform", "method": "none", "observed_at": time.time(), "descendants": []},
    )

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "no_delegation_in_flight"
    # A failed observation on a platform that does look is doubt.
    other = open_attempt(db_path, workspace, pid=os.getpid())
    _resolve_clean(
        db_path,
        other,
        worker_descendants={"enumerated": False, "reason": "snapshot_failed", "method": "toolhelp32", "observed_at": time.time(), "descendants": []},
    )
    assert verdict(db_path, workspace) == "unknown"


def test_merge_keeps_both_sightings_of_a_reused_pid():
    """A pid an earlier child gave up and a later child took: both identities
    stay, so the live one is what the workspace question matches (Round 3)."""

    from modelquad.execution.base import merge_descendant_facts

    earlier = {"enumerated": True, "observed_at": 1.0, "descendants": [{"pid": 7, "image": "node.exe", "created": 1.0}]}
    later = {"enumerated": True, "observed_at": 2.0, "descendants": [{"pid": 7, "image": "node.exe", "created": 50.0}]}
    merged = merge_descendant_facts(earlier, later)
    assert merged["descendants"] == [
        {"pid": 7, "image": "node.exe", "created": 1.0},
        {"pid": 7, "image": "node.exe", "created": 50.0},
    ]
    # An identical sighting is one process, once.
    assert len(merge_descendant_facts(earlier, earlier)["descendants"]) == 1


def test_a_reused_pid_whose_live_identity_was_recorded_is_still_a_hazard(db_path, tmp_path, live_worker, monkeypatch):
    """Two sightings of one pid on the row: the stale one reads as recycled,
    the live one matches, and the workspace is may_be_mutating."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    # This checks identity matching, not tasklist/ps startup speed under load.
    # Keep the owned child alive until fixture teardown, rather than for 120 s.
    child = live_worker("import threading; threading.Event().wait()")
    image_probe = procutil.process_image_name
    creation_probe = procutil.process_creation_epoch

    def loaded_run(*args, **kwargs):
        kwargs['timeout'] = max(60, kwargs.get('timeout', 0))
        for key in procutil.no_window_kwargs():
            kwargs.pop(key, None)  # The probe's own suppression is re-applied below.
        return subprocess.run(*args, **procutil.no_window_kwargs(), **kwargs)

    monkeypatch.setattr(procutil, 'process_image_name',
                        lambda pid: image_probe(pid, run=loaded_run))
    monkeypatch.setattr(procutil, 'process_creation_epoch',
                        lambda pid: creation_probe(pid, run=loaded_run))
    attempt = open_attempt(db_path, workspace, pid=os.getpid())
    created = procutil.process_creation_epoch(child.pid)
    image = procutil.process_image_name(child.pid)
    assert created is not None and image is not None, (child.pid, created, image, child.poll())
    facts = {
        "enumerated": True, "method": "merged", "observed_at": time.time(),
        "descendants": [
            {"pid": child.pid, "image": image, "created": created - 3600},  # an earlier child, gone
            {"pid": child.pid, "image": image, "created": created},  # the live one
        ],
    }
    _resolve_clean(db_path, attempt, worker_descendants=facts)

    answer = service_history.workspace_delegation_liveness(db_path, workspace)
    assert answer["verdict"] == "may_be_mutating"
    assert f"pid {child.pid}" in answer["reason"]


def test_the_bound_is_applied_where_the_facts_enter_a_result():
    """execution_history.metadata_json carries the same shape as the row."""

    import subprocess as sp

    from modelquad.execution.base import DESCENDANTS_RECORDED, descendant_facts

    completed = sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    completed.worker_descendants = {
        "enumerated": True, "method": "toolhelp32", "observed_at": 2.0,
        "descendants": [{"pid": 1000 + i, "image": r"C:\\x\\node.exe", "created": 1.0, "argv": ["leak"]} for i in range(40)],
    }
    facts = descendant_facts(completed)["worker_descendants"]
    assert facts["truncated"] is True
    assert len(facts["descendants"]) == DESCENDANTS_RECORDED
    assert facts["descendants"][0] == {"pid": 1000, "image": "node.exe", "created": 1.0}
    assert "argv" not in json.dumps(facts)
