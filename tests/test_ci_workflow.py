# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Guards on the Windows CI and release workflow definitions.

CI is the only evidence that the product installs and passes its suite without
a maintainer's machine state, so the workflow failing *open* -- passing for the
wrong reason, or not running at all -- is worth testing for directly. A YAML
parser accepts several things GitHub Actions rejects or silently ignores, so
these are not redundant with "the file parses".
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from modelquad.procutil import no_window_kwargs

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows-ci.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def job(workflow: dict) -> dict:
    return workflow["jobs"]["windows"]


def _commands(job: dict) -> str:
    return "\n".join(step["run"] for step in job["steps"] if "run" in step)


def test_workflow_exists_and_parses(workflow: dict):
    assert workflow["jobs"], "no jobs defined"


def test_job_level_env_never_uses_the_runner_context():
    """The `runner` context does not exist at job level.

    A workflow referencing it in `jobs.<id>.env` is rejected before any job
    starts -- the run fails in 0s with "workflow file issue" and no logs.
    yaml.safe_load parses it happily, so this is the only cheap guard.
    """
    source = WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"^    env:\n(?:^(?:      .*|\s*)\n)*", source, re.MULTILINE)
    if block is None:
        return  # no job-level env at all, which is the current shape
    assert "runner." not in block.group(0), (
        "job-level env references the runner context; GitHub will reject the workflow"
    )


def test_runs_on_github_hosted_windows(job: dict):
    """The product is Windows-only, and CI never uses a self-hosted runner."""
    assert job["runs-on"] == "windows-latest"


def test_the_workflow_holds_no_write_permission(workflow: dict, job: dict):
    """CI runs code from pull requests; it must not be able to write anything."""
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in job


def test_never_installs_the_package_editable(job: dict):
    """An editable install resolves modelquad out of src/.

    The suite would then pass while the installed distribution is broken.
    """
    assert not re.search(r"pip\s+install\s+(-e|--editable)\b", _commands(job))


def test_installs_the_extras_the_suite_needs(job: dict):
    install = re.search(r'pip install "\.\[([^\]]+)\]"', _commands(job))
    assert install is not None, "the package is not installed from the checkout"
    # The MCP SDK is a core dependency; `mcp` survives only as an empty alias.
    assert set(install.group(1).split(",")) == {"dev", "service"}


def test_the_state_root_is_isolated_before_anything_runs(job: dict):
    names = [step.get("name", "") for step in job["steps"]]
    isolate = names.index("Set up an isolated state root")
    assert isolate < names.index("Install the package (non-editable) and its extras")
    assert "LOCALAPPDATA=" in job["steps"][isolate]["run"]


def test_the_suite_runs_in_two_disjoint_phases_through_the_console_script(job: dict):
    """Timing tests start only after the parallel pass, with xdist off.

    The `pytest` console script rather than `python -m pytest`, which would put
    the checkout on sys.path ahead of the installed distribution.
    """
    runs = [step["run"].strip() for step in job["steps"] if "pytest" in step.get("run", "")
            and "pip install" not in step["run"]]
    assert len(runs) == 2
    parallel, serial = runs
    assert parallel.startswith("pytest ") and serial.startswith("pytest ")
    assert '-n auto -m "not serial_timing"' in parallel
    assert "-n 0 -m serial_timing" in serial


def test_parallel_workers_are_capped_at_four(job: dict):
    parallel = next(step for step in job["steps"] if step.get("name") == "Run the suite (parallel)")
    assert parallel["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "4"


def test_no_job_runs_on_a_self_hosted_runner(workflow: dict):
    """The public repository never gets a self-hosted runner: CI runs code
    from pull requests, and a self-hosted machine would run it."""
    for name, definition in workflow["jobs"].items():
        assert "self-hosted" not in str(definition["runs-on"]), name


def test_the_suite_job_has_room_for_the_whole_suite(job: dict):
    """The suite takes about 75 minutes on a hosted runner; 60 cut it off."""
    assert job["timeout-minutes"] >= 150


def test_the_suite_runs_on_the_oldest_and_newest_supported_python(job: dict):
    versions = job["strategy"]["matrix"]["python-version"]
    assert "3.11" in versions and "3.13" in versions
    setup = next(step for step in job["steps"] if "setup-python" in step.get("uses", ""))
    assert setup["with"]["python-version"] == "${{ matrix.python-version }}"
    # Only the newer interpreter may be advisory; 3.11 always gates.
    assert "3.11" not in str(job.get("continue-on-error", False))


def _dco_script(workflow: dict) -> str:
    return workflow["jobs"]["dco"]["steps"][1]["run"]


def test_every_new_commit_must_carry_a_dco_sign_off(workflow: dict):
    job = workflow["jobs"]["dco"]
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0, "the commit range needs history"
    script = _dco_script(workflow)
    assert "%ae" in script
    assert "git rev-list --no-merges" in script
    assert "exit $missing" in script


def test_the_dco_check_compares_the_email_as_a_fixed_string(workflow: dict):
    """GitHub noreply addresses (``123+user@users.noreply.github.com``) carry
    a ``+``. Interpolated into ``grep -E`` it is a quantifier, and every
    noreply sign-off failed. The e-mail must be matched literally, against
    the parsed Signed-off-by trailers only."""
    script = _dco_script(workflow)
    assert "%(trailers:key=Signed-off-by,valueonly)" in script
    assert 'grep -Fqi -- "<$email>"' in script
    assert "grep -qiE" not in script and "grep -E" not in script


def test_a_failed_rev_list_fails_the_dco_job(workflow: dict):
    """An unreadable range must not become an empty loop that passes."""
    script = _dco_script(workflow)
    assert "set -euo pipefail" in script
    assert re.search(r'if ! shas=\$\(git rev-list --no-merges "\$range"\); then\s+echo[^\n]*\n\s+exit 1', script)
    assert "for sha in $shas" in script
    # The root-commit case still checks the commit alone.
    assert 'range="$PUSH_AFTER"' in script


NOREPLY = "181751257+razbenaharon@users.noreply.github.com"


def _git(repo: Path, *args: str, env: dict[str, str]) -> str:
    completed = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
        cwd=repo, env=env, capture_output=True, text=True, check=True, **no_window_kwargs(),
    )
    return completed.stdout


@pytest.mark.parametrize("signed_as, accepted", [
    (f"Raz Ben Aharon <{NOREPLY}>", True),
    (f"Raz Ben Aharon <{NOREPLY.upper()}>", True),
    # The `+` is literal: dropping it, or reading it as "one or more", fails.
    ("Raz Ben Aharon <181751257razbenaharon@users.noreply.github.com>", False),
    ("Raz Ben Aharon <1817512577+razbenaharon@users.noreply.github.com>", False),
    ("Someone Else <someone@example.com>", False),
])
def test_the_dco_check_accepts_a_noreply_sign_off(tmp_path: Path, signed_as: str, accepted: bool):
    """The workflow's comparison, run against a real commit by a noreply author.

    The trailer read is the workflow's own ``git log`` format; the match is
    ``grep -Fqi`` -- a case-insensitive fixed-string search -- reproduced here
    as a lower-cased substring test.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Raz Ben Aharon", "GIT_AUTHOR_EMAIL": NOREPLY,
        "GIT_COMMITTER_NAME": "Raz Ben Aharon", "GIT_COMMITTER_EMAIL": NOREPLY,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    })
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", env=env)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "change",
         "-m", "A body line.", "-m", f"Signed-off-by: {signed_as}", env=env)

    email = _git(repo, "show", "-s", "--format=%ae", "HEAD", env=env).strip()
    assert email == NOREPLY
    trailers = _git(repo, "log", "-1", "--format=%(trailers:key=Signed-off-by,valueonly)",
                    "HEAD", env=env)
    assert (f"<{email}>".lower() in trailers.lower()) is accepted

    if accepted:
        # The pattern the workflow used to build never matched this address.
        assert not re.search(rf"^Signed-off-by: .+ <{email}>\s*$", f"Signed-off-by: {signed_as}",
                             re.IGNORECASE | re.MULTILINE)


def test_the_secret_scan_reads_the_whole_history(workflow: dict):
    job = workflow["jobs"]["secrets"]
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    scan = job["steps"][1]
    assert re.fullmatch(r"gitleaks/gitleaks-action@[0-9a-f]{40}", scan["uses"])


_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", re.MULTILINE)


def test_every_action_is_pinned_by_commit_sha_with_its_version():
    """A tag can be moved to other code; a commit SHA cannot. The comment
    names the release so a reader (and Dependabot) can tell what it is."""
    source = WORKFLOW.read_text(encoding="utf-8")
    uses = _USES.findall(source)
    assert uses, "no `uses:` lines found"
    for target, rest in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", target), target
        assert re.fullmatch(r"\s+# v\d+\.\d+\.\d+\s*", rest), f"{target}: no `# vX.Y.Z` comment"


def test_dependabot_keeps_the_action_pins_current():
    config = yaml.safe_load(
        (WORKFLOW.parents[1] / "dependabot.yml").read_text(encoding="utf-8")
    )
    entries = [u for u in config["updates"] if u["package-ecosystem"] == "github-actions"]
    assert entries and entries[0]["schedule"]["interval"] == "weekly"


def test_a_private_repository_runs_only_on_manual_dispatch(workflow: dict):
    """Hosted minutes are free for public repositories only. While the new
    repository is private, nothing runs unless someone dispatches it."""
    for name, definition in workflow["jobs"].items():
        condition = definition.get("if", "")
        assert "github.event.repository.private" in condition, name
        assert "workflow_dispatch" in condition, name


def test_the_dco_skip_needs_more_than_dependabots_author_email(workflow: dict):
    """Anyone can set an author e-mail. A Dependabot commit is skipped only
    when GitHub is also the committer and GitHub reports the signature
    verified; a failed lookup reads as unverified."""
    script = _dco_script(workflow)
    skip = script[script.index("dependabot[bot]"):script.index("continue")]
    assert "%ce" in script
    assert '[ "$committer" = "noreply@github.com" ]' in skip
    assert "--jq '.commit.verification.verified' || echo false" in skip
    assert '[ "$verified" = "true" ]' in skip
    env = workflow["jobs"]["dco"]["steps"][1]["env"]
    assert env["GH_TOKEN"] == "${{ github.token }}"
    assert env["REPO"] == "${{ github.repository }}"
    # The lookup needs read access only.
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in workflow["jobs"]["dco"]


def test_a_failed_advisory_leg_is_reported_on_the_summary(job: dict):
    """continue-on-error keeps the run green, so an advisory failure must be
    written where a reader sees it, for exactly the advisory versions."""
    report = job["steps"][-1]
    assert report["name"] == "Report a failed advisory leg"
    condition = report["if"]
    assert condition.startswith("${{ failure() && ")
    advisory = set(re.findall(r"'(3\.\d+)'", str(job["continue-on-error"])))
    assert advisory and set(re.findall(r"'(3\.\d+)'", condition)) == advisory
    assert "GITHUB_STEP_SUMMARY" in report["run"]
    assert "::warning" in report["run"]


def test_dependabot_watches_the_python_dependencies():
    config = yaml.safe_load(
        (WORKFLOW.parents[1] / "dependabot.yml").read_text(encoding="utf-8")
    )
    entries = [u for u in config["updates"] if u["package-ecosystem"] == "pip"]
    assert len(entries) == 1
    pip = entries[0]
    assert pip["directory"] == "/"
    assert pip["schedule"]["interval"] == "weekly"
    assert 1 <= pip["open-pull-requests-limit"] <= 5


RELEASE = WORKFLOW.parent / "release.yml"


@pytest.fixture(scope="module")
def release() -> dict:
    return yaml.safe_load(RELEASE.read_text(encoding="utf-8"))


def _on(definition: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    return definition.get("on", definition.get(True))


def test_release_runs_only_on_a_version_tag(release: dict):
    """No manual dispatch and no branch push: a release is a pushed v-tag."""
    trigger = _on(release)
    assert set(trigger) == {"push"}
    assert set(trigger["push"]) == {"tags"}
    assert trigger["push"]["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]


def test_release_grants_nothing_by_default(release: dict):
    assert release["permissions"] == {}
    assert release["jobs"]["build"]["permissions"] == {"contents": "read"}


def test_only_the_publish_job_can_mint_an_oidc_token(release: dict):
    assert set(release["jobs"]) == {"build", "publish"}
    publish = release["jobs"]["publish"]
    assert publish["needs"] == "build"
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["environment"]["name"] == "pypi"
    assert publish["environment"]["url"].startswith("https://pypi.org/project/modelquad/")
    # Trusted Publishing: no stored token is handed to the publish action.
    assert "${{ secrets." not in RELEASE.read_text(encoding="utf-8")


def test_release_checks_the_tag_against_the_package_version(release: dict):
    steps = release["jobs"]["build"]["steps"]
    names = [step.get("name") for step in steps]
    check = names.index("The tag matches the package version")
    build = names.index("Build and check the distributions")
    assert check < build
    script = steps[check]["run"]
    assert 'tag="${GITHUB_REF_NAME#v}"' in script
    assert "src/modelquad/__init__.py" in script
    assert 'if [ "$tag" != "$ver" ]; then' in script and "exit 1" in script
    assert "python -m build" in steps[build]["run"]
    assert "twine check --strict" in steps[build]["run"]


def test_the_release_version_lookup_reads_the_real_version():
    """The workflow's own one-liner, run against this checkout."""
    line = re.search(r'ver=\$\(python -c "(.+)"\)$', RELEASE.read_text(encoding="utf-8"),
                     re.MULTILINE)
    assert line is not None
    code = line.group(1).replace('\\"', '"')
    root = WORKFLOW.parents[2]
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=root, capture_output=True, text=True,
        check=True, **no_window_kwargs(),
    )
    namespace: dict = {}
    exec((root / "src" / "modelquad" / "__init__.py").read_text(encoding="utf-8"), namespace)
    assert completed.stdout.strip() == namespace["__version__"]


def test_every_release_action_is_pinned_by_commit_sha_with_its_version(release: dict):
    uses = _USES.findall(RELEASE.read_text(encoding="utf-8"))
    assert any(target.startswith("pypa/gh-action-pypi-publish@") for target, _ in uses)
    for target, rest in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", target), target
        assert re.fullmatch(r"\s+# v\d+\.\d+\.\d+\s*", rest), f"{target}: no `# vX.Y.Z` comment"
    for name, definition in release["jobs"].items():
        assert "self-hosted" not in str(definition["runs-on"]), name
    checkout = release["jobs"]["build"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["persist-credentials"] is False
