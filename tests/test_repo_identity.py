# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Repository identity contracts; Git writes only in disposable temp repos."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys

import pytest

from modelquad import identity, procutil, repo_identity as ri
from modelquad.jobs import changes

KEY = "0123456789abcdef0123456789abcdef"  # gitleaks:allow (synthetic test key)
OTHER_KEY = "abcdef0123456789abcdef0123456789"  # gitleaks:allow (synthetic test key)


def git(directory, *args):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, *args], cwd=directory,
        env=env, check=True, capture_output=True, timeout=300,  # fixture setup; preflight headroom
        **procutil.no_window_kwargs(),
    ).stdout.decode("utf-8").strip()


@pytest.fixture(autouse=True)
def _loaded_runner_headroom(monkeypatch):
    """Headroom for loaded runners (train 21 local run: git_timeout in
    test_symlink_alias under the 20 s budget). The claims are identities, not
    speed; tests that exercise the budgets themselves set their own values,
    which override these."""
    monkeypatch.setattr(ri, "GIT_SECONDS", 60)
    monkeypatch.setattr(changes, "GIT_SECONDS", 60)  # _git clamps to its own module value
    monkeypatch.setattr(ri, "RESOLVE_SECONDS", 90)


@pytest.fixture
def repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git unavailable")
    directory = tmp_path / "checkout"
    directory.mkdir()
    git(directory, "init", "--initial-branch=main")
    git(directory, "config", "user.name", "Fixture")
    git(directory, "config", "user.email", "fixture@example.invalid")
    (directory / "file.txt").write_text("initial\n", encoding="utf-8", newline="\n")
    git(directory, "add", ".")
    git(directory, "commit", "-m", "initial")
    return directory


def resolve(directory, key=KEY):
    result = ri.resolve_repo_identity(directory, installation_key=key)
    assert isinstance(result, ri.RepoIdentity), result
    return result


def test_root_and_nested_directory_share_identity(repo):
    nested = repo / "nested" / "deep"
    nested.mkdir(parents=True)
    assert resolve(nested) == resolve(repo)


def test_linked_worktrees_share_repo_but_not_checkout(repo, tmp_path):
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "other", str(linked))
    main, other = resolve(repo), resolve(linked)
    assert main.repo_key == other.repo_key
    assert main.root_fingerprint == other.root_fingerprint
    assert main.worktree_key != other.worktree_key
    assert (main.branch, other.branch) == ("main", "other")
    assert (main.display_name, other.display_name) == ("checkout", "linked")
    assert resolve(linked / ".").repo_key == main.repo_key


def test_orphan_linked_worktree_shares_repo_key_and_all_ref_roots(repo, tmp_path):
    linked = tmp_path / "orphan-linked"
    git(repo, "worktree", "add", "-b", "other", str(linked))
    before = resolve(repo)
    git(linked, "checkout", "--orphan", "orphan")
    unborn = resolve(linked)
    assert unborn.repo_key == before.repo_key
    assert unborn.root_fingerprint == before.root_fingerprint
    assert unborn.head_kind == "unborn" and unborn.branch == "orphan"
    git(linked, "commit", "-m", "orphan root")
    main, orphan = resolve(repo), resolve(linked)
    assert main.repo_key == orphan.repo_key == before.repo_key
    assert main.root_fingerprint == orphan.root_fingerprint != before.root_fingerprint
    assert orphan.worktree_key == unborn.worktree_key != main.worktree_key


def test_orphan_branch_checkout_preserves_keys(repo):
    before = resolve(repo)
    git(repo, "checkout", "--orphan", "orphan")
    unborn = resolve(repo)
    assert (unborn.repo_key, unborn.worktree_key) == (before.repo_key, before.worktree_key)
    assert unborn.root_fingerprint == before.root_fingerprint
    git(repo, "commit", "-m", "orphan root")
    orphan = resolve(repo)
    assert (orphan.repo_key, orphan.worktree_key) == (before.repo_key, before.worktree_key)
    assert orphan.root_fingerprint != before.root_fingerprint
    git(repo, "checkout", "main")
    assert resolve(repo).root_fingerprint == orphan.root_fingerprint


def test_same_remote_clones_are_distinct(repo, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(repo), str(remote))
    clones = [tmp_path / name for name in ("clone-one", "clone-two")]
    for clone in clones:
        git(tmp_path, "clone", str(remote), str(clone))
    a, b = map(resolve, clones)
    assert a.repo_key != b.repo_key
    assert a.root_fingerprint == b.root_fingerprint
    assert a.remote_fingerprint == b.remote_fingerprint
    assert a.remote_fingerprint is not None


def test_changed_and_removed_remote_preserve_keys(repo):
    git(repo, "remote", "add", "origin", "https://host.invalid/team/a.git")
    a = resolve(repo)
    git(repo, "remote", "set-url", "origin", "https://host.invalid/team/b.git")
    b = resolve(repo)
    git(repo, "remote", "remove", "origin")
    c = resolve(repo)
    assert a.repo_key == b.repo_key == c.repo_key
    assert a.worktree_key == b.worktree_key == c.worktree_key
    assert a.remote_fingerprint != b.remote_fingerprint
    assert c.remote_fingerprint is None


def test_installation_scope_changes_every_key(repo):
    git(repo, "remote", "add", "origin", "https://host.invalid/repo.git")
    a, b = resolve(repo), resolve(repo, OTHER_KEY)
    for field in ("repo_key", "worktree_key", "root_fingerprint", "remote_fingerprint"):
        assert getattr(a, field) != getattr(b, field)
        assert len(getattr(a, field)) == 32
    assert resolve(repo, KEY.upper()) == a


def test_detached_head_has_no_branch(repo):
    before = resolve(repo)
    git(repo, "checkout", "--detach", "HEAD")
    detached = resolve(repo)
    assert detached.branch is None and detached.head_kind == "detached"
    assert detached.repo_key == before.repo_key
    assert detached.worktree_key == before.worktree_key
    assert git(repo, "rev-parse", "HEAD") not in repr(detached)


@pytest.mark.parametrize("failed_query", [
    ["symbolic-ref", "-q", "--short", "HEAD"],
    ["rev-parse", "-q", "--verify", "HEAD"],
])
def test_head_query_failure_does_not_guess_detached_or_unborn(repo, monkeypatch, failed_query):
    original = ri._git
    def unavailable(directory, args, deadline):
        if args == failed_query:
            raise changes._Unavailable
        return original(directory, args, deadline)
    monkeypatch.setattr(ri, "_git", unavailable)
    assert ri.resolve_repo_identity(repo, installation_key=KEY) == ri.Unknown("git_unavailable")


def test_unborn_branch(tmp_path):
    git(tmp_path, "init", "--initial-branch=main")
    unborn = resolve(tmp_path)
    assert unborn.branch == "main" and unborn.head_kind == "unborn"
    assert unborn.remote_fingerprint is None
    assert unborn.root_fingerprint == ri._hash(bytes.fromhex(KEY), "root", "unborn")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "first root")
    born = resolve(tmp_path)
    assert born.repo_key == unborn.repo_key
    assert born.worktree_key == unborn.worktree_key
    assert born.root_fingerprint != unborn.root_fingerprint


def test_normal_commit_and_branch_change_do_not_change_keys(repo):
    before = resolve(repo)
    git(repo, "checkout", "-b", "new-branch")
    git(repo, "commit", "--allow-empty", "-m", "second")
    after = resolve(repo)
    assert after.branch == "new-branch"
    assert (after.repo_key, after.worktree_key) == (before.repo_key, before.worktree_key)


def test_different_project_recloned_at_same_path_changes_root_fingerprint(repo, tmp_path):
    before = resolve(repo)
    repo.rename(tmp_path / "old-project")
    source = tmp_path / "other-project"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "Other fixture")
    git(source, "config", "user.email", "other@example.invalid")
    git(source, "commit", "--allow-empty", "-m", "other project")
    git(tmp_path, "clone", str(source), str(repo))
    after = resolve(repo)
    assert after.repo_key == before.repo_key
    assert after.worktree_key == before.worktree_key
    assert after.root_fingerprint != before.root_fingerprint


def test_shallow_clone_boundary_is_a_root_and_deepening_changes_fingerprint(repo, tmp_path):
    git(repo, "commit", "--allow-empty", "-m", "second")
    clone = tmp_path / "shallow"
    source = repo.as_uri()
    git(tmp_path, "-c", "protocol.file.allow=always", "clone", "--depth=1", source, str(clone))
    shallow = resolve(clone)
    boundary = git(clone, "rev-parse", "HEAD")
    assert shallow.root_fingerprint == ri._hash(bytes.fromhex(KEY), "root", boundary)
    git(clone, "-c", "protocol.file.allow=always", "fetch", "--unshallow")
    deep = resolve(clone)
    assert (deep.repo_key, deep.worktree_key) == (shallow.repo_key, shallow.worktree_key)
    assert deep.root_fingerprint != shallow.root_fingerprint
    assert deep.root_fingerprint == resolve(repo).root_fingerprint


def test_moved_repository_requires_registration(repo, tmp_path):
    before = resolve(repo)
    moved = tmp_path / "moved"
    repo.rename(moved)
    assert resolve(moved).repo_key != before.repo_key


def test_not_git_is_unknown(tmp_path):
    assert ri.resolve_repo_identity(tmp_path, installation_key=KEY) == ri.Unknown("git_unavailable")


def test_missing_git(repo, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert ri.resolve_repo_identity(repo, installation_key=KEY) == ri.Unknown("git_missing")


def test_timeout(repo, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("private command", 15, stderr="private path")
    monkeypatch.setattr(changes.subprocess, "Popen", timeout)
    assert ri.resolve_repo_identity(repo, installation_key=KEY) == ri.Unknown("git_timeout")


def test_shared_runner_deadline_timeout(repo, monkeypatch):
    monkeypatch.setattr(ri, "RESOLVE_SECONDS", 0)
    assert ri.resolve_repo_identity(repo, installation_key=KEY) == ri.Unknown("git_timeout")


def test_bare_repo_is_unknown(repo, tmp_path):
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "--bare", str(repo), str(bare))
    assert ri.resolve_repo_identity(bare, installation_key=KEY) == ri.Unknown("bare_repository")


@pytest.mark.parametrize("key", [None, "", "invalid", "a" * 31, "a" * 33, "x" * 32, " " * 32])
def test_invalid_installation_key_does_not_observe_or_write(tmp_path, monkeypatch, key):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid installation key must not access state or git")
    monkeypatch.setattr(ri, "_git", forbidden)
    monkeypatch.setattr(identity, "installation_id", forbidden)
    assert ri.resolve_repo_identity(tmp_path, installation_key=key) == ri.Unknown("installation_key_unavailable")


def test_resolver_never_mints_installation_state(repo, monkeypatch, isolate_home_directory):
    def forbidden(*args, **kwargs):
        pytest.fail("resolver must not mint identity")
    monkeypatch.setattr(identity, "installation_id", forbidden)
    resolve(repo)
    assert not list(isolate_home_directory.rglob("identity.json"))


@pytest.mark.parametrize("value", [None, "\0bad-path", "missing"])
def test_unavailable_path(tmp_path, value):
    path = tmp_path / value if value == "missing" else value
    assert ri.resolve_repo_identity(path, installation_key=KEY) == ri.Unknown("path_unavailable")


def test_unreadable_common_dir_is_unknown(repo, monkeypatch):
    def denied(*args):
        raise PermissionError("private directory canary")
    monkeypatch.setattr(ri.os, "scandir", denied)
    assert ri.resolve_repo_identity(repo, installation_key=KEY) == ri.Unknown("repository_unavailable")


def test_privacy_and_credential_rotation(repo):
    raw = "https://user-canary:password-canary@HOST.invalid:443/team/repo.git?token=query-canary#fragment-canary"  # trufflehog:ignore (synthetic canary URL)
    git(repo, "remote", "add", "origin", raw)
    result = resolve(repo)
    rendered = repr(result) + json.dumps(asdict(result)) + str(result)
    for private in (str(repo), str(repo.parent), raw, KEY, "user-canary", "password-canary",
                    "query-canary", "fragment-canary", "host.invalid"):
        assert private not in rendered
    # Assembled from parts so the source holds no literal user:password@host
    # (a secret scanner flagged the literal as a Basic Auth string).
    rotated = "https://" + "other-canary" + ":" + "rotated-canary" + "@host.invalid/team/repo/"
    git(repo, "remote", "set-url", "origin", rotated)
    assert resolve(repo).remote_fingerprint == result.remote_fingerprint


@pytest.mark.parametrize("raw,expected", [
    ("git@HOST.invalid:team/repo.git", "ssh://host.invalid/team/repo"),
    ("ssh://user:pass@HOST.invalid:22/team/repo.git", "ssh://host.invalid/team/repo"),
    ("https://host.invalid:8443/team/repo.git", "https://host.invalid:8443/team/repo"),
    ("https://user:pass@[::1]:443/team/repo.git", "https://[::1]/team/repo"),
    ("C:\\temp\\remote.git", "C:/temp/remote.git"),
])
def test_remote_normalization(raw, expected):
    assert ri._normalize_remote(raw) == expected


@pytest.mark.parametrize("remote", [
    "https://user:password@host.invalid:bad/repo.git",  # trufflehog:ignore (synthetic canary URL)
    "https://host.invalid:65536/repo.git",
    "https://[broken/repo.git",
])
def test_unparseable_remote_preserves_repository_identity(repo, remote):
    before = resolve(repo)
    git(repo, "remote", "add", "origin", remote)
    after = resolve(repo)
    assert after == before
    assert after.remote_fingerprint is None


def test_hash_domains_and_framing():
    key = bytes.fromhex(KEY)
    assert len({ri._hash(key, label, "same") for label in ("repo", "worktree", "root", "remote")}) == 4
    assert ri._hash(key, "repo", "ab", "c") != ri._hash(key, "repo", "a", "bc")


def test_index_bytes_and_mtime_unchanged(repo):
    (repo / "file.txt").write_text("dirty", encoding="utf-8")
    index = repo / ".git/index"
    before = index.read_bytes(), index.stat().st_mtime_ns
    resolve(repo)
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    assert not list((repo / ".git").rglob("*.lock"))


def test_resolver_does_not_run_clean_filters_or_walk_working_tree(repo, monkeypatch):
    (repo / ".gitattributes").write_text("file.txt filter=canary\n", encoding="utf-8", newline="\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-m", "attributes")
    script = repo / "filter.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        "Path('clean-filter-ran').touch()\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8", newline="\n",
    )
    git(repo, "config", "filter.canary.clean", f'"{sys.executable}" "{script}"')
    git(repo, "config", "filter.canary.required", "true")
    (repo / "file.txt").write_text("changed content for filter\n", encoding="utf-8", newline="\n")
    original = ri._git
    commands = []
    def metadata_only(directory, args, deadline):
        commands.append(args)
        assert args[0] in {"rev-parse", "symbolic-ref", "rev-list", "remote"}
        return original(directory, args, deadline)
    monkeypatch.setattr(ri, "_git", metadata_only)
    resolve(repo)
    assert ["symbolic-ref", "-q", "--short", "HEAD"] in commands
    assert ["rev-parse", "-q", "--verify", "HEAD"] in commands
    assert not (repo / "clean-filter-ran").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-root display names")
@pytest.mark.parametrize("root", ["C:\\", "\\\\?\\C:\\"])
def test_drive_root_display_name_is_drive(root):
    # Do not initialize a repository on any real drive root.
    assert ri._display_name(Path(root)) == "C:"


def test_hardening_including_ceiling_and_no_window(repo, monkeypatch):
    calls = []
    original = changes.subprocess.Popen
    def record(command, **kwargs):
        calls.append((command, kwargs))
        return original(command, **kwargs)
    for name in ("GIT_CEILING_DIRECTORIES", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_TRACE", "GIT_CONFIG_COUNT"):
        monkeypatch.setenv(name, "private-invalid-value")
    monkeypatch.setattr(changes.subprocess, "Popen", record)
    resolve(repo)
    assert ri._git is changes._git
    assert len(calls) >= 5
    for command, kwargs in calls:
        assert {k: v for k, v in kwargs["env"].items() if k.upper().startswith("GIT_")} == {
            "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
        for config in ("core.fsmonitor=false", "core.untrackedCache=false", "diff.external=",
                       "core.hooksPath=" + os.devnull, "filter.lfs.required=false"):
            assert config in command
        assert kwargs["stdin"] == subprocess.DEVNULL
        if os.name == "nt":
            assert kwargs["creationflags"] & 0x08000000
        assert not any("safe.directory=" in arg for arg in command)


def test_output_bound(repo, monkeypatch):
    monkeypatch.setattr(changes, "MAX_GIT_BYTES", 1)
    assert ri.resolve_repo_identity(repo, installation_key=KEY) == ri.Unknown("git_unavailable")


def test_shared_runner_refusal_is_unknown_without_guessing_or_bypassing(repo, monkeypatch, caplog):
    calls = []
    def refused(directory, args, deadline):
        calls.append(args)
        raise changes._Unavailable("private path, URL and credential canary")
    monkeypatch.setattr(ri, "_git", refused)
    result = ri.resolve_repo_identity(repo, installation_key=KEY)
    assert result == ri.Unknown("git_unavailable")
    assert len(calls) == 1
    assert "canary" not in repr(result) + json.dumps(asdict(result))
    assert not caplog.records


def test_submodule_is_its_own_repository(repo, tmp_path):
    source = tmp_path / "source"
    git(tmp_path, "clone", str(repo), str(source))
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(source), "module")
    git(repo, "commit", "-am", "submodule")
    assert resolve(repo / "module").repo_key != resolve(repo).repo_key


def test_symlink_alias(repo, tmp_path):
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(repo, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink creation requires unavailable privilege")
    assert resolve(alias) == resolve(repo)


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions")
def test_junction_alias(repo, tmp_path):
    alias = tmp_path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(repo)], check=True,
                   capture_output=True, timeout=15, **procutil.no_window_kwargs())
    try:
        assert resolve(alias) == resolve(repo)
    finally:
        alias.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows canonical letter case and extended paths")
def test_windows_case_and_extended_aliases(repo):
    expected = resolve(repo)
    assert resolve(str(repo).swapcase()) == expected
    assert resolve("\\\\?\\" + str(repo)) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows cross-drive common-dir resolution")
def test_cross_drive_worktree(repo, monkeypatch):
    # Headroom for a loaded runner (train 17 preflight: git_timeout under the
    # 15 s per-call budget); the claim is the identity, not the speed.
    from modelquad.jobs import changes
    monkeypatch.setattr(ri, "GIT_SECONDS", 60)
    monkeypatch.setattr(changes, "GIT_SECONDS", 60)  # _git clamps to its own module value
    monkeypatch.setattr(ri, "RESOLVE_SECONDS", 90)
    # The suite's temp root and source worktree may be on different drives.
    # Only this fresh temporary directory is used; the source Git is untouched.
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
        other = Path(temporary) / "linked"
        if other.drive.lower() == repo.drive.lower():
            pytest.skip("A writable temporary directory on a second drive is unavailable")
        git(repo, "worktree", "add", "-b", "cross-drive", str(other))
        try:
            assert resolve(other).repo_key == resolve(repo).repo_key
        finally:
            git(repo, "worktree", "remove", str(other))


def test_relative_common_dir_is_resolved_against_git_cwd(repo, monkeypatch):
    nested = repo / "nested"
    nested.mkdir()
    original = ri._git
    def relative(directory, args, deadline):
        if args == ["rev-parse", "--git-common-dir"]:
            return b"../.git\n"
        return original(directory, args, deadline)
    expected = resolve(repo)
    monkeypatch.setattr(ri, "_git", relative)
    assert resolve(nested) == expected


def test_case_sensitive_directories_do_not_fold(repo, tmp_path):
    upper, lower = tmp_path / "Distinct", tmp_path / "distinct"
    upper.mkdir()
    if lower.exists():
        pytest.skip("Fixture filesystem is case insensitive")
    lower.mkdir()
    assert str(ri._canonical_directory(upper)) != str(ri._canonical_directory(lower))
    for directory in (upper, lower):
        git(tmp_path, "clone", str(repo), str(directory))
    assert resolve(upper).repo_key != resolve(lower).repo_key


def test_root_commit_order_is_canonical(repo, monkeypatch):
    original = ri._git
    commits = ["1" * 40, "2" * 40]
    def roots(directory, args, deadline):
        if args == ["rev-list", "--max-parents=0", "--all"]:
            return ("\n".join(commits) + "\n").encode()
        return original(directory, args, deadline)
    monkeypatch.setattr(ri, "_git", roots)
    before = resolve(repo)
    commits.reverse()
    assert resolve(repo) == before
