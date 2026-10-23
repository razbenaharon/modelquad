# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Guards on the development policy and its pre-flight entry point.

Only things that are mechanically true or false. It cannot check that a
document is *right* -- nothing automated can -- so it checks the class of
defect this repository keeps producing instead, which is a document that
disagrees with the code while being quoted as fact.

The checks are implemented in `scripts/check.py` so that a developer and CI run
the same code rather than two descriptions of it. This module is how CI runs
them: the Python suite is the non-bypassable path, and a pre-commit hook is
not.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from conftest import no_window_kwargs

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_PY = REPO_ROOT / "scripts" / "check.py"
SCRIPTS_README = REPO_ROOT / "scripts" / "README.md"


@pytest.fixture(scope="module")
def check():
    """Load `scripts/check.py` as a module.

    It is a script, not a package: an agent must be able to run it from a
    fresh worktree with no install.
    """
    spec = importlib.util.spec_from_file_location("_modelquad_check", CHECK_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------- the doc invariants


def test_relative_links_resolve(check):
    """Every relative link in an authored Markdown file points at something.

    Repository-wide.
    """
    problems = check._check_links()
    assert not problems, f"unresolved relative links: {problems}"


def test_agents_current_state_matches_the_code(check):
    """`AGENTS.md` "Current state" states four numbers that live in code.

    It said DB schema `15` against a `SCHEMA_VERSION = 16`. A wrong number in
    the file an arriving agent trusts first is worse than an absent one,
    because nothing downstream re-derives it.
    """
    problems = check._check_current_state()
    assert not problems, f"AGENTS.md disagrees with the code: {problems}"


def test_documented_commands_run_on_windows(check):
    """Instructional docs may not carry POSIX-only command syntax.

    `VAR=value command` fails in PowerShell -- proven, not assumed:

        PYTHONPATH=src : The term 'PYTHONPATH=src' is not recognized ...

    ModelQuad is Windows-first (`AGENTS.md`, "Current state") and
    `windows-ci.yml` runs `shell: pwsh`, so the primary shell could not type
    the repository's own most-repeated validation command.

    Lines beginning `$ ` are exempt: those are transcripts of what actually
    happened, and rewriting one to look portable would falsify the record.
    """
    problems = check._check_posix_only_commands()
    assert not problems, f"POSIX-only commands in instructional docs: {problems}"


def test_no_credential_shapes_in_authored_source(check, capsys):
    """The repository's own source carries no credential shapes.

    `.py` is in the scanned suffixes on purpose: the source is what ships.
    """
    code = check.check_secrets()
    captured = capsys.readouterr().out
    assert code == check.EXIT_OK, f"secret scan failed:\n{captured}"


# ------------------------------------------------ the entry point's own shape


def test_the_secret_pattern_list_did_not_shrink(check):
    assert len(check.SECRET_PATTERNS) >= 8, "the secret pattern list shrank unexpectedly"


def test_check_py_is_stdlib_only():
    """It must run in a fresh worktree with nothing installed.

    An agent that cannot validate until it has built an environment will skip
    validating.
    """
    source = CHECK_PY.read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:from|import)\s+([A-Za-z_][\w.]*)", source, re.MULTILINE))
    allowed = {
        "__future__", "argparse", "ast", "importlib", "os", "re", "subprocess", "sys",
        "pathlib", "shutil",
    }
    third_party = {name.split(".")[0] for name in imports} - allowed
    assert not third_party, f"check.py imports non-stdlib modules: {third_party}"


def test_check_py_is_not_a_second_release_procedure(check):
    """The pre-flight subset must not grow into a release procedure.

    A `check.py` that freezes, signs or promotes would be a second,
    hand-maintained copy of what CI does -- so the guard is on what it can
    actually *do*, not on which words its prose uses.
    """
    # The job list stays this small. Adding a group is a deliberate act, and
    # one that has to come back through this test.
    assert set(check.CHECKS) == {"python", "docs", "secrets"}, (
        f"check.py grew a new check group: {sorted(check.CHECKS)}. "
        f"Adding a group is a deliberate act; update this test with it."
    )

    # It may run the interpreter. It may not drive a build chain.
    source = CHECK_PY.read_text(encoding="utf-8")
    executables = set(re.findall(r'_run\(\s*\[?\s*([A-Za-z_.\[\]"]+)', source))
    assert executables <= {"sys.executable", "command"}, (
        f"check.py invokes something unexpected: {executables}"
    )
    for forbidden in ("pyinstaller", "signtool", "build_beta.py"):
        assert forbidden not in source.lower(), (
            f"check.py drives {forbidden!r}; ModelQuad ships source only"
        )



def test_every_check_group_is_documented():
    """`check.py --list` and `scripts/README.md` must not drift apart."""
    source = CHECK_PY.read_text(encoding="utf-8")
    block = re.search(r"^CHECKS = \{(.*?)^\}", source, re.MULTILINE | re.DOTALL)
    assert block, "cannot find the CHECKS table in check.py"
    groups = set(re.findall(r'^\s*"(\w+)":', block.group(1), re.MULTILINE))
    assert groups, "CHECKS parsed to zero groups"

    doc = SCRIPTS_README.read_text(encoding="utf-8")
    undocumented = sorted(g for g in groups if f"check.py {g}" not in doc)
    assert not undocumented, f"check groups absent from scripts/README.md: {undocumented}"


def test_check_py_can_be_pointed_at_another_tree_and_refuses_a_non_tree(tmp_path, check):
    """The trusted copy judges the candidate's tree via --repo."""
    import subprocess
    import sys as _sys

    other = tmp_path / "candidate"
    (other / "scripts").mkdir(parents=True)
    (other / "src").mkdir()
    module = check
    assert module._bind_repo(other) == other.resolve()
    assert module.REPO_ROOT == other.resolve() and module.SRC == other.resolve() / "src"
    refused = subprocess.run(
        [_sys.executable, str(CHECK_PY), "--repo", str(tmp_path / "nowhere"), "fast"],
        capture_output=True, text=True, timeout=120, **no_window_kwargs(),
    )
    assert refused.returncode == 2 and "not a ModelQuad tree" in refused.stderr
    listed = subprocess.run([_sys.executable, str(CHECK_PY), "--repo", str(other), "--list"],
                            capture_output=True, text=True, timeout=120, **no_window_kwargs())
    assert listed.returncode == 0


def test_check_py_runs_a_real_check_against_a_rebound_tree():
    """`--repo` end to end, not only the binding.

    The docs check is the cheapest real check; run against this repository
    through --repo it must pass exactly as it does without the flag, which
    proves every path the check derives is derived from the bound root.
    """
    import subprocess
    import sys as _sys

    result = subprocess.run(
        [_sys.executable, str(CHECK_PY), "--repo", str(REPO_ROOT), "docs"],
        capture_output=True, text=True, timeout=600, cwd=str(REPO_ROOT),
        **no_window_kwargs(),
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "checking " + str(REPO_ROOT.resolve()) in result.stdout
