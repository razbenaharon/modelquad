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


# ------------------------------------------------ the banned-phrase guard


def _fresh_check(tree: Path):
    """A private copy of `check.py` bound to `tree`, so no other test sees the binding."""
    spec = importlib.util.spec_from_file_location("_modelquad_check_wording", CHECK_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._bind_repo(tree)
    return module


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "tree"
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def test_public_text_uses_none_of_the_banned_phrases():
    """README, docs/, the skills and the server instructions pass as they are.

    A private copy bound to this repository: the shared fixture may have been
    pointed at another tree by an earlier test.
    """
    module = _fresh_check(REPO_ROOT)
    assert module._wording_files(), "the wording guard found nothing to scan"
    assert module._server_instruction_texts(), "the wording guard read no server instructions"
    problems = module._check_banned_phrases()
    assert not problems, f"banned phrases in public text: {problems}"


@pytest.mark.parametrize(
    ("relative", "text", "label"),
    [
        ("README.md", "Use the official Codex plugin.\n", "official"),
        ("README.md", "ModelQuad is not a router.\n", "route, router, routing"),
        ("README.md", "Keep coding when Claude\nruns out.\n", "keep coding when Claude runs out"),
        ("README.md", "It helps you save your Claude\nquota.\n", "save quota or usage"),
        ("PRIVACY.md", "Combine both of your subscriptions.\n", "combine or pool subscriptions"),
        ("docs/GUIDE.md", "A safe second opinion.\n", "safe"),
        ("plugin/skills/review/SKILL.md", "Never hit a limit again.\n", "beat or bypass a limit"),
        ("src/modelquad/adapters/templates/claude/skills/modelquad/SKILL.md",
         "Unlimited reviews.\n", "unlimited"),
        ("README.md", "כלי רשמי לעבודה.\n", "רשמי"),
        ("README.md", "עם ניתוב בין הכלים.\n", "ניתוב"),
        ("src/modelquad/adapters/server_instructions.py",
         '_INTRO = (\n    "ModelQuad is an "\n    "auto-switch for your plans."\n)\n', "auto-switch"),
        # Code people and Claude read: string literals, read with ast.
        ("src/modelquad/review.py",
         'def line():\n    return {"note": "A safe second opinion."}\n', "safe"),
        ("src/modelquad/cli.py", 'HELP = f"Use the official {name} CLI."\n', "official"),
        ("src/modelquad/execution/noshell.py",
         '_GUIDANCE = {"x": ("Open it "\n    "securely.")}\n', "secure"),
        ("src/modelquad/mcp/server.py",
         'def modelquad_status():\n    """Shows the router state."""\n', "route, router, routing"),
    ],
)
def test_the_docs_check_fails_on_a_planted_banned_phrase(tmp_path, capsys, relative, text, label):
    """`python scripts/check.py docs` fails, naming the file and the phrase."""
    module = _fresh_check(_tree(tmp_path, {relative: text}))
    problems = module._check_banned_phrases()
    assert any(problem.startswith(relative + ":") and f"({label})" in problem for problem in problems), problems

    assert module.check_docs() == module.EXIT_FAILED
    out = capsys.readouterr().out
    assert "FAIL  docs: public text uses none of the banned phrases" in out


def test_ordinary_words_that_contain_a_banned_one_pass(tmp_path):
    """Whole words only: no false hit inside a longer word or on a bare verb."""
    text = (
        "Your approval is asked for each call, a routine check.\n"
        "Before it changes a file, ModelQuad saves a full copy. The choice is saved.\n"
        "Safety rules and security reports. How the layers combine.\n"
        "Claude Code's bypass-permissions mode still asks.\n"
        "הרשמים שלי מהסשן.\n"
    )
    module = _fresh_check(_tree(tmp_path, {"README.md": text}))
    assert module._check_banned_phrases() == []


def test_code_identifiers_and_developer_docstrings_pass(tmp_path):
    """In code, only text a reader sees is scanned.

    A literal without a space is an identifier, key or code; a docstring is
    for developers unless it is a `modelquad_*` tool's description.
    """
    source = (
        '"""Module docstring about the router."""\n'
        'MODE = "routing_mode"\n'
        'KIND = "safe"\n'
        'def _helper():\n'
        '    """Internal: the routing record."""\n'
        '    return "`kind` names the record."\n'
    )
    module = _fresh_check(_tree(tmp_path, {"src/modelquad/mcp/server.py": source}))
    assert module._code_texts(), "the wording guard read no code strings"
    assert module._check_banned_phrases() == []

    module._bind_repo(_tree(tmp_path / "bare", {
        "src/modelquad/mcp/server.py": 'X = "Pick a routing mode."\n'}))
    problems = module._check_banned_phrases()
    assert problems == ["src/modelquad/mcp/server.py:1: 'routing' (route, router, routing)"], problems


def test_the_allowed_identifiers_stay_tiny():
    module = _fresh_check(REPO_ROOT)
    assert module.BANNED_PHRASE_ALLOWED_IDENTIFIERS == frozenset()
    assert len(module.BANNED_PHRASE_ALLOWED_LINES) <= 3


def test_an_allow_listed_line_passes_and_only_that_line(tmp_path):
    """The allow-list is exact lines, never a file or a word."""
    module = _fresh_check(tmp_path)
    allowed = sorted(module.BANNED_PHRASE_ALLOWED_LINES)[0]
    module._bind_repo(_tree(tmp_path, {"README.md": f"{allowed}\nIt is endorsed by nobody.\n"}))
    problems = module._check_banned_phrases()
    assert problems == ["README.md:2: 'endorsed' (endorsed)"], problems
