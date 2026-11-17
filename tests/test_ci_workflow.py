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

import re
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
    assert set(install.group(1).split(",")) == {"dev"}


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


def test_the_dco_job_runs_the_checked_in_script_with_the_events_facts(workflow: dict):
    """The rules live in .github/scripts/dco_check.py (tests/test_dco_check.py).
    The job hands it what GitHub reports about the event, as data in the
    environment: the head repository, the account that pushed, the range."""
    job = workflow["jobs"]["dco"]
    checkout, step = job["steps"]
    assert checkout["with"]["fetch-depth"] == 0, "the commit range needs history"
    assert step["run"].strip() == "python3 .github/scripts/dco_check.py"
    assert "${{" not in step["run"]
    assert (WORKFLOW.parents[1] / "scripts" / "dco_check.py").is_file()
    assert step["env"] == {
        "EVENT_NAME": "${{ github.event_name }}",
        "ACTOR_ID": "${{ github.actor_id }}",
        "PR_BASE": "${{ github.event.pull_request.base.sha }}",
        "PR_HEAD": "${{ github.event.pull_request.head.sha }}",
        "PR_HEAD_REPO": "${{ github.event.pull_request.head.repo.full_name }}",
        "PUSH_BEFORE": "${{ github.event.before }}",
        "PUSH_AFTER": "${{ github.sha }}",
        "GH_TOKEN": "${{ github.token }}",
        "REPO": "${{ github.repository }}",
    }
    # The lookup needs read access only.
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in job


def test_the_dco_job_never_runs_with_a_fork_given_the_repositorys_token(workflow: dict):
    """`pull_request_target` would run with write access next to a fork's
    code. The check reads commit metadata and needs nothing of the kind."""
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request_target" not in triggers


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


def test_release_runs_only_when_a_person_starts_it_and_types_publish(release: dict):
    """Pushing a tag publishes nothing. A release is a manual run, started on
    the tag, with the word ``publish`` typed into a required box."""
    trigger = _on(release)
    assert set(trigger) == {"workflow_dispatch"}
    confirm = trigger["workflow_dispatch"]["inputs"]["confirm"]
    assert confirm["required"] is True and confirm["type"] == "string"
    assert "default" not in confirm, "the word is typed, never pre-filled"


def _release_step(release: dict, name: str) -> tuple[int, dict]:
    steps = release["jobs"]["build"]["steps"]
    names = [step.get("name") for step in steps]
    return names.index(name), steps[names.index(name)]


def test_release_refuses_before_building_unless_confirmed_from_a_tag_on_main(release: dict):
    """The gate runs before anything is built, and each refusal fails the job:
    no typed confirmation, a ref that is not a ``vX.Y.Z`` tag, or a tagged
    commit that is not part of ``main``."""
    position, gate = _release_step(release, "A confirmed release of a version tag on main")
    build, _ = _release_step(release, "Build and check the distributions")
    checkout, checked_out = _release_step(release, "Checkout repository")
    assert checkout < position < build
    # The whole history, so that main is there to compare against.
    assert checked_out["with"]["fetch-depth"] == 0
    script = gate["run"]
    assert script.lstrip().startswith("set -euo pipefail")
    # The typed word reaches the script as data, never as part of the script.
    assert gate["env"] == {"CONFIRM": "${{ inputs.confirm }}"}
    assert "${{" not in script
    assert 'if [ "$CONFIRM" != "publish" ]; then' in script
    assert 'if [ "$GITHUB_REF_TYPE" != "tag" ]' in script
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in script
    assert 'if ! git merge-base --is-ancestor "$GITHUB_SHA" refs/remotes/origin/main; then' in script
    assert script.count("exit 1") == 3


def test_release_requires_a_green_windows_ci_run_for_the_tagged_commit(release: dict):
    position, step = _release_step(release, "Windows CI passed on this commit")
    build, _ = _release_step(release, "Build and check the distributions")
    assert position < build
    script = step["run"]
    assert "actions/workflows/windows-ci.yml/runs" in script
    assert "head_sha=${GITHUB_SHA}" in script and "status=success" in script
    assert "exit 1" in script
    # The job's own short-lived token; nothing stored.
    assert step["env"] == {"GH_TOKEN": "${{ github.token }}"}


def test_the_publish_job_repeats_the_confirmation(release: dict):
    condition = release["jobs"]["publish"]["if"]
    assert "inputs.confirm == 'publish'" in condition
    assert "github.ref_type == 'tag'" in condition


def test_release_adds_no_action_beyond_the_reviewed_ones():
    used = {target.split("@")[0] for target, _rest in _USES.findall(RELEASE.read_text(encoding="utf-8"))}
    assert used == {"actions/checkout", "actions/setup-python", "actions/upload-artifact",
                    "actions/download-artifact", "pypa/gh-action-pypi-publish"}
    for target, rest in _USES.findall(RELEASE.read_text(encoding="utf-8")):
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", target), target
        assert re.fullmatch(r"\s+# v\d+\.\d+\.\d+\s*", rest), target


def test_release_grants_nothing_by_default(release: dict):
    assert release["permissions"] == {}
    # Reading the code, and reading whether CI passed; nothing can be written.
    assert release["jobs"]["build"]["permissions"] == {"contents": "read", "actions": "read"}


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
