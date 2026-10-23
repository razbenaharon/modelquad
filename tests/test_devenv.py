# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Where a development or test child process writes its temporary files.

`scripts/devenv.py` and the call sites that use it. The defect this guards is
not a wrong answer but a *widening* one: the redirection is opt-in, it applies
to child environments only, and it may not disturb anything else those
environments carry.

Every test states its own environment. Reading the machine's real
`MODELQUAD_TEMP_ROOT` would make these pass or fail depending on whose
workstation ran them, which is the property the suite is supposed to have
rather than the one it is asserting.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import random
import shutil
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from conftest import no_window_kwargs

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_PY = REPO_ROOT / "scripts" / "check.py"
DEVENV_PY = REPO_ROOT / "scripts" / "devenv.py"

#: Stand-ins for whatever the machine's real temporary directory is. Never
#: created, never written to -- they exist to be compared against.
AMBIENT_TEMP = os.path.join("Z:" + os.sep, "ambient", "temp")
AMBIENT_TMP = os.path.join("Z:" + os.sep, "ambient", "tmp")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def devenv():
    return _load(DEVENV_PY, "_test_devenv")


@pytest.fixture(scope="module")
def check():
    return _load(CHECK_PY, "_test_devenv_check")


@pytest.fixture(autouse=True)
def no_ambient_temp_root(monkeypatch):
    """This machine may well have one set; no test in this file may read it."""
    monkeypatch.delenv("MODELQUAD_TEMP_ROOT", raising=False)


# ------------------------------------------------------------- the mechanism


def test_without_the_variable_nothing_is_redirected(devenv):
    """The state of every machine that has not opted in."""
    env = {"TEMP": AMBIENT_TEMP, "TMP": AMBIENT_TMP, "PATH": "x", "PYTHONPATH": "src"}
    assert devenv.apply_temp_root(env) == ""
    assert env["TEMP"] == AMBIENT_TEMP and env["TMP"] == AMBIENT_TMP
    assert env["PATH"] == "x" and env["PYTHONPATH"] == "src"
    assert set(env) == {"TEMP", "TMP", "PATH", "PYTHONPATH"}, "no variable was invented"


def test_the_variable_redirects_both_names_and_leaves_everything_else(devenv, tmp_path):
    root = tmp_path / "scratch" / "ModelQuad"
    env = {
        devenv.TEMP_ROOT_VAR: str(root),
        "TEMP": AMBIENT_TEMP,
        "TMP": AMBIENT_TMP,
        "PATH": "x",
        "GH_TOKEN": "unrelated",
    }
    applied = devenv.apply_temp_root(env)
    assert applied == str(root.resolve())
    assert env["TEMP"] == env["TMP"] == applied
    assert root.is_dir(), "created, so the child does not fail on a missing directory"
    assert env["PATH"] == "x" and env["GH_TOKEN"] == "unrelated"
    assert env[devenv.TEMP_ROOT_VAR] == str(root), "the request itself passes through"


def test_a_child_env_without_the_names_gains_them(devenv, tmp_path):
    """An environment that never carried TEMP/TMP still gets the redirection."""
    env = {devenv.TEMP_ROOT_VAR: str(tmp_path / "t")}
    devenv.apply_temp_root(env)
    assert env["TEMP"] == env["TMP"] == str((tmp_path / "t").resolve())


def test_a_blank_root_is_no_root(devenv):
    for blank in ("", "   ", "\t", "\n"):
        env = {devenv.TEMP_ROOT_VAR: blank, "TEMP": AMBIENT_TEMP}
        assert devenv.apply_temp_root(env) == ""
        assert env["TEMP"] == AMBIENT_TEMP


def test_a_relative_root_is_made_absolute(devenv, tmp_path, monkeypatch):
    """TEMP is resolved by each child against its own working directory, and
    the development tooling launches children in several."""
    monkeypatch.chdir(tmp_path)
    env = {devenv.TEMP_ROOT_VAR: "scratch"}
    applied = devenv.apply_temp_root(env)
    assert Path(applied).is_absolute()
    assert Path(applied) == (tmp_path / "scratch").resolve()


def test_an_unusable_root_degrades_to_the_ambient_directory(devenv, tmp_path):
    """An unmounted drive must cost a slower test run, never a failed one."""
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    env = {devenv.TEMP_ROOT_VAR: str(blocker / "under-a-file"), "TEMP": AMBIENT_TEMP}
    assert devenv.apply_temp_root(env) == ""
    assert env["TEMP"] == AMBIENT_TEMP and "TMP" not in env


def test_the_process_environment_is_never_written(devenv, tmp_path, monkeypatch):
    """Requirement 7, and the reason this is a mapping-to-mapping function: the
    redirection must not outlive the child it was built for."""
    monkeypatch.setenv(devenv.TEMP_ROOT_VAR, str(tmp_path / "root"))
    monkeypatch.setenv("TEMP", AMBIENT_TEMP)
    monkeypatch.setenv("TMP", AMBIENT_TMP)
    env = dict(os.environ)
    devenv.apply_temp_root(env)
    assert env["TEMP"] == str((tmp_path / "root").resolve())
    assert os.environ["TEMP"] == AMBIENT_TEMP and os.environ["TMP"] == AMBIENT_TMP


def test_the_repository_names_no_temp_root_of_its_own(devenv):
    """Requirement 6: the location is a property of the machine, so an absolute
    path here would be one more machine-specific fact in a shared tree. The
    variable's name is the only thing this repository is allowed to know."""
    source = DEVENV_PY.read_text(encoding="utf-8")
    absolute = re.findall(r"[A-Za-z]:[\\/][^\s\"'`]*", source)
    assert absolute == [], f"devenv.py hard-codes a path: {absolute}"
    assert devenv.resolved_temp_root({}) == "", "there is no default root"
    assert devenv.TEMP_ROOT_VAR == "MODELQUAD_TEMP_ROOT"


def test_a_real_child_resolves_its_temp_directory_to_the_root(devenv, tmp_path):
    """The point of the whole change, proven through a real interpreter.

    `tempfile.gettempdir()` is what pytest's `tmp_path`, `tmp_path_factory` and
    the `pytest-of-<user>` tree are built on, so a child that resolves it here
    is a child whose pytest temporary files land here -- without `--basetemp`,
    and for every other `tempfile` consumer in that process too.
    """
    root = tmp_path / "scratch"
    env = dict(os.environ)
    env[devenv.TEMP_ROOT_VAR] = str(root)
    assert devenv.apply_temp_root(env) == str(root.resolve())
    proc = subprocess.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path),
        **no_window_kwargs(),
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == root.resolve()


# ---------------------------------------------------------- scripts/check.py


def test_check_py_hands_the_redirected_environment_to_pytest(check, tmp_path, monkeypatch):
    """Requirements 4 and 10: `python -m pytest` is the child that must receive
    it, and PYTHONPATH must survive the addition."""
    root = tmp_path / "scratch"
    monkeypatch.setenv("MODELQUAD_TEMP_ROOT", str(root))
    monkeypatch.setenv("TEMP", AMBIENT_TEMP)
    monkeypatch.setenv("TMP", AMBIENT_TMP)
    monkeypatch.setenv("MODELQUAD_DEVENV_UNRELATED", "survives")

    class _Probe:
        returncode = 0
        stdout = str(check.SRC / "modelquad" / "__init__.py")
        stderr = ""

    # The module's own reference to `subprocess`, never the stdlib module
    # itself: patching that would change every other consumer in the process.
    monkeypatch.setattr(check, "subprocess", types.SimpleNamespace(run=lambda *a, **k: _Probe()))

    seen: list[dict] = []

    def fake_run(command, *, cwd, env=None):
        seen.append({"command": list(command), "env": dict(env or {})})
        return 0

    monkeypatch.setattr(check, "_run", fake_run)
    assert check.check_python() == check.EXIT_OK

    assert len(seen) == len(check._pytest_phases())
    for phase in seen:
        assert "pytest" in phase["command"]
        assert "--basetemp" not in phase["command"], "requirement 5: TEMP/TMP, not --basetemp"
        env = phase["env"]
        assert env["TEMP"] == env["TMP"] == str(root.resolve())
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(check.SRC)
        assert env["MODELQUAD_DEVENV_UNRELATED"] == "survives"
    assert os.environ["TEMP"] == AMBIENT_TEMP, "the script's own environment is untouched"


def test_check_py_without_the_variable_hands_pytest_the_ambient_directory(check, monkeypatch):
    monkeypatch.setenv("TEMP", AMBIENT_TEMP)
    monkeypatch.setenv("TMP", AMBIENT_TMP)
    env, applied = check._child_env()
    assert applied == ""
    assert env["TEMP"] == AMBIENT_TEMP and env["TMP"] == AMBIENT_TMP


def test_a_trusted_verifier_materialised_alone_still_runs(tmp_path, monkeypatch):
    """`check.py` copied out on its own has no `devenv.py` beside it. That must
    cost the redirection, not the gate."""
    lone = tmp_path / "verifier" / "scripts" / "check.py"
    lone.parent.mkdir(parents=True)
    lone.write_bytes(CHECK_PY.read_bytes())
    monkeypatch.setenv("MODELQUAD_TEMP_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("TEMP", AMBIENT_TEMP)

    alone = _load(lone, "_test_devenv_lone_check")
    assert alone._load_devenv() is None, "there is no sibling to load"
    env, applied = alone._child_env()
    assert applied == "" and env["TEMP"] == AMBIENT_TEMP


# ------------------------------- the repository's own bare `pytest` sessions


ROOT_CONFTEST = REPO_ROOT / "conftest.py"


@pytest.mark.parametrize(
    "has_root, worker, disabled, expected",
    [(True, False, False, True), (False, False, False, False),
     (True, True, False, False), (True, False, True, False)],
    ids=["controller", "no-root", "worker", "disabled"],
)
def test_sessionfinish_prunes_only_an_opted_in_controller(
    tmp_path, monkeypatch, has_root, worker, disabled, expected,
):
    hook = _load(ROOT_CONFTEST, "_test_root_conftest_pruning")
    monkeypatch.setattr(hook, "TEMP_ROOT", str(tmp_path) if has_root else "")
    if disabled:
        monkeypatch.setenv("MODELQUAD_TEMP_PRUNE", "0")
    else:
        monkeypatch.delenv("MODELQUAD_TEMP_PRUNE", raising=False)
    calls = []
    fake = types.SimpleNamespace(
        TWO_DAYS=172800, prune_temp_root=lambda *a, **kw: calls.append((a, kw)),
    )
    spec = types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=lambda _: None))
    monkeypatch.setattr(hook.importlib.util, "spec_from_file_location", lambda *a: spec)
    monkeypatch.setattr(hook.importlib.util, "module_from_spec", lambda _: fake)
    config = types.SimpleNamespace(**({"workerinput": {}} if worker else {}))
    hook.pytest_sessionfinish(types.SimpleNamespace(config=config), 0)
    assert bool(calls) is expected
    if expected:
        assert calls[0][0] == (str(tmp_path),)
        assert calls[0][1]["min_age_seconds"] == 172800
        assert calls[0][1]["apply"] is True
        assert calls[0][1]["time_budget_seconds"] == 3


@pytest.mark.parametrize("failure", ["import", "prune"], ids=["import", "prune"])
def test_sessionfinish_cleanup_errors_never_change_exit_status(tmp_path, monkeypatch, failure):
    hook = _load(ROOT_CONFTEST, "_test_root_conftest_pruning_errors")
    monkeypatch.setattr(hook, "TEMP_ROOT", str(tmp_path))
    monkeypatch.delenv("MODELQUAD_TEMP_PRUNE", raising=False)

    def refuse(*args, **kwargs):
        raise RuntimeError("synthetic cleanup failure")

    if failure == "import":
        monkeypatch.setattr(hook.importlib.util, "spec_from_file_location", refuse)
    else:
        fake = types.SimpleNamespace(TWO_DAYS=172800, prune_temp_root=refuse)
        spec = types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=lambda _: None))
        monkeypatch.setattr(hook.importlib.util, "spec_from_file_location", lambda *a: spec)
        monkeypatch.setattr(hook.importlib.util, "module_from_spec", lambda _: fake)
    session = types.SimpleNamespace(config=types.SimpleNamespace(), exitstatus=17)
    hook.pytest_sessionfinish(session, 17)
    assert session.exitstatus == 17

#: The body of the probe run by `_bare_pytest_probe`. Built as a list of lines
#: rather than one triple-quoted blob so that the file it writes stays readable
#: here, where the assertions that consume it live.
PROBE_SOURCE = """import os, pathlib, tempfile


def test_probe(tmp_path, pytestconfig):
    root_hook = next(p for p in pytestconfig.pluginmanager.get_plugins()
                     if getattr(p, '__file__', None) == str(pytestconfig.rootpath / 'conftest.py'))
    fields = [
        'ROOTDIR=' + str(pytestconfig.rootpath),
        'CONFIG=' + str(pytestconfig.inipath),
        'CONFTEST=' + root_hook.__file__,
        'DEVENV=' + root_hook._devenv.__file__,
        'TMPDIR=' + str(os.environ.get('TMPDIR')),
        'PYTHONPATH=' + str(os.environ.get('PYTHONPATH')),
        'TEMP=' + str(os.environ.get('TEMP')),
        'TMP=' + str(os.environ.get('TMP')),
        'GETTEMPDIR=' + tempfile.gettempdir(),
        'TMP_PATH=' + str(tmp_path),
        'MKDTEMP=' + tempfile.mkdtemp(prefix='probe-'),
        'UNRELATED=' + str(os.environ.get('MODELQUAD_DEVENV_UNRELATED')),
    ]
    pathlib.Path(os.environ['DEVENV_PROBE_OUT']).write_text(
        chr(10).join(fields), encoding='utf-8')
"""


def _fake_tempfile():
    """A stand-in for the `tempfile` module, so a test can assert the cache was
    written without redirecting the very session that is running it."""
    return types.SimpleNamespace(tempdir="<untouched>")


def test_adopt_writes_both_names_and_the_tempfile_cache(devenv, tmp_path):
    """`gettempdir()` memoises its answer, so the environment alone is not
    enough: a session that touched a temporary file before this ran would keep
    the cached ambient directory no matter what `TEMP` now says."""
    root = tmp_path / "scratch"
    env = {"TEMP": AMBIENT_TEMP, "TMP": AMBIENT_TMP, "PATH": "x", devenv.TEMP_ROOT_VAR: str(root)}
    cache = _fake_tempfile()

    assert devenv.adopt_temp_root(env, tempfile_module=cache) == str(root.resolve())
    assert env["TEMP"] == env["TMP"] == str(root.resolve())
    assert cache.tempdir == str(root.resolve())
    assert env["PATH"] == "x", "an unrelated variable is not disturbed"


def test_adopt_without_the_variable_writes_nothing_at_all(devenv):
    """Absent, ambient behaviour is preserved *exactly* -- which means the cache
    is not written either, not merely written with the same value."""
    env = {"TEMP": AMBIENT_TEMP, "TMP": AMBIENT_TMP}
    cache = _fake_tempfile()

    assert devenv.adopt_temp_root(env, tempfile_module=cache) == ""
    assert env == {"TEMP": AMBIENT_TEMP, "TMP": AMBIENT_TMP}
    assert cache.tempdir == "<untouched>"


def test_adopt_leaves_an_unusable_root_ambient(devenv, tmp_path):
    """A root that cannot be created degrades, for the reason `apply_temp_root`
    does: the redirection is an optimisation and may not take a run down."""
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    env = {"TEMP": AMBIENT_TEMP, devenv.TEMP_ROOT_VAR: str(blocker / "root")}
    cache = _fake_tempfile()

    assert devenv.adopt_temp_root(env, tempfile_module=cache) == ""
    assert env["TEMP"] == AMBIENT_TEMP and cache.tempdir == "<untouched>"


def _bare_pytest_probe(tmp_path, root, *, extra_args=(), from_dir=None, launcher=None):
    """Run the tracked bare-pytest mechanism in a private fixture checkout.

    Copy its inputs byte for byte, preserving pyproject discovery and
    the scripts-relative import. The checkout belongs to tmp_path, including
    probe and bytecode cleanup; no shared source file is created or removed.
    """
    checkout = tmp_path / "build" / "checkout"
    checkout.mkdir(parents=True)
    for relative in ("conftest.py", "scripts/devenv.py", "scripts/prune_temp.py", "pyproject.toml"):
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / relative).read_bytes())
    (checkout / "tests").mkdir()
    probe = checkout / "test_devenv_probe.py"
    report = tmp_path / "seen.txt"
    probe.write_text(PROBE_SOURCE, encoding="utf-8")

    env = dict(os.environ)
    env.pop("MODELQUAD_TEMP_ROOT", None)
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    # Every name `gettempdir()` consults, not only the two obvious ones. The
    # session running this test has `TMPDIR` set -- its own root conftest set
    # it -- and `dict(os.environ)` inherits that, so pointing just `TEMP`/`TMP`
    # at the ambient directory left the child reading the outer session's root
    # out of `TMPDIR`, and the ambient case asserting against the wrong place.
    for name in ("TMPDIR", "TEMP", "TMP"):
        env[name] = str(ambient)
    env["DEVENV_PROBE_OUT"] = str(report)
    env["MODELQUAD_DEVENV_UNRELATED"] = "survives"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if root is not None:
        env["MODELQUAD_TEMP_ROOT"] = str(root)

    # Mirror the original working directory without importing tests/conftest:
    # this probe specifically proves the repository-root hook on its own.
    cwd = checkout if from_dir is None else checkout / Path(from_dir).relative_to(REPO_ROOT)
    command = list(launcher) if launcher else [sys.executable, "-m", "pytest"]
    proc = subprocess.run(
        [*command, "-q", "-p", "no:cacheprovider",
         *extra_args, os.path.relpath(probe, cwd)],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=300,
        **no_window_kwargs(),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    seen = dict(
        line.split("=", 1)
        for line in report.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert Path(seen["ROOTDIR"]) == checkout
    assert Path(seen["CONFIG"]) == checkout / "pyproject.toml"
    assert Path(seen["CONFTEST"]) == checkout / "conftest.py"
    assert Path(seen["DEVENV"]) == checkout / "scripts" / "devenv.py"
    assert seen["TMPDIR"] == seen["TEMP"] == seen["TMP"]
    return seen, ambient


def test_bare_pytest_probe_never_writes_a_shared_repository_probe(tmp_path, monkeypatch):
    """Guard writes during the real child run, including with source PYTHONPATH."""
    original_text = Path.write_text
    original_bytes = Path.write_bytes
    writes = []

    def guard(writer):
        def write(path, *args, **kwargs):
            assert path.is_relative_to(tmp_path), "the probe must only write its private fixture"
            if path.name.startswith("test_devenv_probe"):
                assert path.parent != REPO_ROOT
                writes.append(path)
            return writer(path, *args, **kwargs)
        return write

    monkeypatch.setattr(Path, "write_text", guard(original_text))
    monkeypatch.setattr(Path, "write_bytes", guard(original_bytes))
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT / "src"))
    seen, _ = _bare_pytest_probe(tmp_path, tmp_path / "scratch")
    checkout = Path(seen["ROOTDIR"])
    assert writes == [checkout / "test_devenv_probe.py"]
    for relative in ("conftest.py", "scripts/devenv.py", "scripts/prune_temp.py", "pyproject.toml"):
        assert (checkout / relative).read_bytes() == (REPO_ROOT / relative).read_bytes()
    assert seen["GETTEMPDIR"] == str((tmp_path / "scratch").resolve())
    assert seen["PYTHONPATH"] == str(REPO_ROOT / "src")


@pytest.mark.parametrize(
    "extra_args, from_tests_dir, console_script",
    [
        pytest.param((), False, False, id="module-from-repo-root"),
        pytest.param((), True, False, id="module-from-inside-tests"),
        # `python -m pytest` and the bare `pytest` console script reach the same
        # code by different routes, and `conftest.py` claims both.
        pytest.param(
            (), False, True, id="console-script-pytest",
            marks=pytest.mark.skipif(
                shutil.which("pytest") is None,
                reason="the `pytest` console script is not on PATH here",
            ),
        ),
        pytest.param(
            ("-n", "2"), False, False, id="xdist-workers",
            marks=pytest.mark.skipif(
                importlib.util.find_spec("xdist") is None,
                reason="pytest-xdist is not installed in this environment",
            ),
        ),
    ],
)
def test_a_bare_pytest_run_puts_every_temporary_file_under_the_root(
    tmp_path, extra_args, from_tests_dir, console_script
):
    """The gap this change closes.

    `AGENTS.md` documents `python -m pytest -q` as the primary Python gate, and
    nothing builds an environment for it -- so this was the one path still
    writing `pytest-of-<user>` to the ambient volume after `check.py` had
    been covered.

    Three things are asserted because they fail separately: `TMP_PATH` proves
    the fixture tree moved, `MKDTEMP` proves the suite's own `tempfile` callers
    moved with it, and `GETTEMPDIR` proves the memoised cache agrees with the
    environment rather than lagging behind it.
    """
    root = tmp_path / "scratch"
    seen, ambient = _bare_pytest_probe(
        tmp_path, root, extra_args=extra_args,
        from_dir=(REPO_ROOT / "tests") if from_tests_dir else None,
        launcher=[shutil.which("pytest")] if console_script else None,
    )
    resolved = str(root.resolve())

    assert seen["TEMP"] == seen["TMP"] == resolved
    assert seen["GETTEMPDIR"] == resolved
    assert Path(seen["TMP_PATH"]).is_relative_to(root.resolve()), seen["TMP_PATH"]
    assert Path(seen["MKDTEMP"]).is_relative_to(root.resolve()), seen["MKDTEMP"]
    assert not Path(seen["TMP_PATH"]).is_relative_to(ambient)
    assert not Path(seen["TMP_PATH"]).exists(), "passed-test directories are removed"
    assert seen["UNRELATED"] == "survives", "an unrelated variable reaches the suite untouched"


def test_a_bare_pytest_run_without_the_variable_stays_ambient(tmp_path):
    """The default on every machine that has chosen no root."""
    seen, ambient = _bare_pytest_probe(tmp_path, None)

    assert seen["TEMP"] == seen["TMP"] == str(ambient)
    assert seen["GETTEMPDIR"] == str(ambient)
    assert Path(seen["TMP_PATH"]).is_relative_to(ambient), seen["TMP_PATH"]
    assert Path(seen["MKDTEMP"]).is_relative_to(ambient), seen["MKDTEMP"]


def test_an_existing_root_that_cannot_be_written_degrades(devenv, tmp_path, monkeypatch):
    """Existence is not writability, and only one of them is the question.

    `mkdir(parents=True, exist_ok=True)` succeeds on a directory that is already
    there and says nothing about whether a file can be created in it -- a
    denying ACL, a read-only mount, a full volume all pass it. Before the write
    probe, such a root was applied and `adopt_temp_root` pinned
    `tempfile.tempdir` to it, which also bypasses `tempfile`'s own search for a
    working candidate: the first `mkdtemp` aborted the run. An optimisation that
    can take the suite down is not the contract `apply_temp_root` documents.

    The module's own `tempfile` reference is replaced rather than the stdlib
    one, so refusing the probe here cannot disturb anything else in this
    process -- including the pytest session running the test.
    """
    root = tmp_path / "exists-but-refuses-writes"
    root.mkdir()

    def refuse(*args, **kwargs):
        raise OSError(13, "permission denied")

    monkeypatch.setattr(devenv, "tempfile", types.SimpleNamespace(mkdtemp=refuse))

    assert devenv.usable_temp_root(str(root)) is False
    env = {"TEMP": AMBIENT_TEMP, "TMP": AMBIENT_TMP, devenv.TEMP_ROOT_VAR: str(root)}
    assert devenv.apply_temp_root(env) == ""
    assert env["TEMP"] == AMBIENT_TEMP and env["TMP"] == AMBIENT_TMP


def test_the_write_probe_creates_the_root_and_leaves_nothing_in_it(devenv, tmp_path):
    """It has to create a real file to answer the question, and it has to not
    keep it -- a probe file left in the temp root is litter in the one directory
    this whole change exists to keep cheap."""
    root = tmp_path / "made-on-demand"
    assert devenv.usable_temp_root(str(root)) is True
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_the_root_conftest_states_the_mechanism_it_uses(devenv):
    """Tracked beside `scripts/devenv.py`, and holding no path of its own."""
    assert ROOT_CONFTEST.is_file()
    source = ROOT_CONFTEST.read_text(encoding="utf-8")

    assert "adopt_temp_root" in source
    assert "--basetemp" not in source, "requirement: TEMP/TMP, never --basetemp"
    assert devenv.TEMP_ROOT_VAR in source
    assert not re.search(r"[A-Za-z]:\\\\", source), "the repository names no drive letter"


def test_the_root_conftest_cannot_reach_the_product_runtime():
    """Product-runtime `tempfile` behaviour is unchanged, for two independent
    reasons, both asserted rather than argued.

    The file is outside `src/`, and `pyproject.toml` builds the distribution
    from `src/` alone -- so it is in no wheel or sdist. And
    `modelquad`'s own validation runner launches pytest inside a *user's*
    project, which takes that project as its rootdir and never collects this
    file.
    """
    assert ROOT_CONFTEST.parent == REPO_ROOT
    assert list((REPO_ROOT / "src").rglob("conftest.py")) == []

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'where = ["src"]' in pyproject
    assert 'package-dir = { "" = "src" }' in pyproject


# -- the review gate runs the repository-wide scans -------------------


def _module_tests(path: Path) -> dict[str, str]:
    """``{name: source}`` of every module-level ``test_`` function in ``path``."""
    source = path.read_text(encoding="utf-8")
    return {
        node.name: ast.get_source_segment(source, node)
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


def test_the_review_gate_selects_every_repo_wide_scan_for_a_one_file_change(check):
    """An unflagged spawn in a test file passed the review gate, because
    the diff named only that file, and was found by `check.py all` and a paid
    hosted run. The scan that caught it now runs in the review gate on any
    change."""
    selected = check.focused_tests(["tests/test_paths.py"])
    assert "tests/test_paths.py" in selected
    assert (
        "tests/test_procutil.py::test_no_subprocess_spawn_in_tests_omits_window_suppression" in selected
    )
    assert all(scan in selected or scan.split("::", 1)[0] in selected for scan in check.REPO_WIDE_SCANS)
    assert check.focused_tests([]) == [], "no change, nothing to run"
    # The tests that keep the list complete run in
    # the review gate too, or a scan left off the list passes it.
    for keeper in ("test_every_test_that_walks_the_repository_is_a_repo_wide_scan",
                   "test_every_repo_wide_scan_names_a_test_that_exists"):
        assert "tests/test_devenv.py::" + keeper in check.REPO_WIDE_SCANS


def test_a_scan_whose_file_is_selected_whole_is_not_named_again(check):
    selected = check.focused_tests(["tests/test_procutil.py"])
    assert "tests/test_procutil.py" in selected
    assert not [entry for entry in selected if entry.startswith("tests/test_procutil.py::")]


def test_the_review_gate_selects_the_codex_package_importer(check):
    """test_execution imports CodexExecutor via execution/__init__.py."""
    selected = check.focused_tests(["src/modelquad/execution/codex.py"])
    assert "tests/test_execution.py" in selected
    assert "tests/test_codex_read_only.py" in selected, "the stem rule still applies"


def _focused_fixture(check, tmp_path, monkeypatch, sources):
    monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
    for relative, source in sources.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8", newline="\n")


def test_the_review_gate_selects_check_py_path_guards(check):
    selected = check.focused_tests(["scripts/check.py"])
    assert "tests/test_development_policy.py" in selected


@pytest.mark.parametrize("changed, expression", [
    ("scripts/check.py", '"scripts/check.py"'),
    ("scripts/check.py", 'REPO_ROOT / "scripts" / "check.py"'),
    ("scripts/check.py", '"scripts" / "check.py"'),
    ("scripts/devenv.py", 'repo_root / "scripts" / "devenv.py"'),
    (".github/workflows/windows-ci.yml", 'REPO_ROOT / ".github" / "workflows" / "windows-ci.yml"'),
])
def test_the_review_gate_selects_literal_and_joined_path_guards(
    check, tmp_path, monkeypatch, capsys, changed, expression
):
    _focused_fixture(check, tmp_path, monkeypatch, {
        "tests/test_guard.py": "def test_guard():\n    target = " + expression + "\n",
        "tests/test_unrelated.py": (
            'target = REPO_ROOT / "other" / "check.py"\n'
            'separate = ("scripts", "check.py")\n'
            'description = "mentions scripts/check.py inside unrelated text"\n'
        ),
    })
    selected = check.focused_tests([changed])
    assert "tests/test_guard.py" in selected
    assert "tests/test_unrelated.py" not in selected
    assert "path=1 [tests/test_guard.py]" in capsys.readouterr().out
    assert selected == sorted(set(selected))


def test_the_review_gate_limits_path_guards_to_non_src_changes(check, tmp_path, monkeypatch):
    _focused_fixture(check, tmp_path, monkeypatch, {
        "tests/test_guard.py": 'target = "src/modelquad/sample.py"\n',
    })
    assert "tests/test_guard.py" not in check.focused_tests(["src/modelquad/sample.py"])




@pytest.mark.parametrize("use", [
    'target = STATUS / "NOW.md"',
    'for name in ("NOW.md", "PLAN.md"):\n    target = STATUS / name',
])
def test_the_review_gate_selects_constant_directory_guards(check, tmp_path, monkeypatch, capsys, use):
    _focused_fixture(check, tmp_path, monkeypatch, {
        "tests/test_guard.py": 'STATUS = REPO / "docs" / "status"\n' + use + '\n',
        "tests/test_unrelated.py": 'STATUS = REPO / "docs" / "other"\n',
    })
    selected = check.focused_tests(["docs/status/NOW.md"])
    assert "tests/test_guard.py" in selected
    assert "tests/test_unrelated.py" not in selected
    assert "dir=1 [tests/test_guard.py]" in capsys.readouterr().out


@pytest.mark.parametrize("changed, source, rule", [
    ("docs/status/NOW.md", 'DOCS = REPO / "docs"\nSTATUS = DOCS / "status"\n', "dir"),
    ("scripts/check.py", 'SCRIPTS = REPO_ROOT / "scripts"\ndef test_guard():\n    target = SCRIPTS / "check.py"\n', "path"),
    ("docs/status/NOW.md", 'DOCS: Path = REPO / "docs"\nALIAS = DOCS\nSTATUS = ALIAS / "status"\n', "dir"),
])
def test_the_review_gate_resolves_module_constant_chains(
    check, tmp_path, monkeypatch, capsys, changed, source, rule
):
    _focused_fixture(check, tmp_path, monkeypatch, {
        "tests/test_guard.py": source + "raise RuntimeError('never execute')\n",
    })
    assert "tests/test_guard.py" in check.focused_tests([changed])
    assert f"{rule}=1 [tests/test_guard.py]" in capsys.readouterr().out


@pytest.mark.parametrize("changed, source", [
    ("docs/status/NOW.md", 'DOCS = REPO / "docs"\n'),
    ("README.md", 'ROOT = Path(__file__).resolve().parents[1]\n'),
    ("README.md", 'ROOT = REPO / "."\n'),
])
def test_the_review_gate_directory_guards_match_only_non_root_exact_parents(
    check, tmp_path, monkeypatch, capsys, changed, source
):
    _focused_fixture(check, tmp_path, monkeypatch, {"tests/test_guard.py": source})
    assert "tests/test_guard.py" not in check.focused_tests([changed])
    assert "dir=0 []" in capsys.readouterr().out


def test_the_review_gate_constant_resolution_is_bounded(check, tmp_path, monkeypatch):
    aliases = '\n'.join(f'ALIAS_{index} = ALIAS_{index + 1}' for index in range(40))
    _focused_fixture(check, tmp_path, monkeypatch, {
        "tests/test_cycle.py": 'FIRST = SECOND\nSECOND = FIRST\nTARGET = FIRST / "status"\n',
        "tests/test_long.py": aliases + '\nALIAS_40 = REPO / "docs"\nTARGET = ALIAS_0 / "status"\n',
    })
    selected = check.focused_tests(["docs/status/NOW.md"])
    assert "tests/test_cycle.py" not in selected
    assert "tests/test_long.py" not in selected


def test_the_review_gate_selection_is_deterministic_with_shuffled_discovery(
    check, tmp_path, monkeypatch, capsys
):
    _focused_fixture(check, tmp_path, monkeypatch, {
        "tests/test_path.py": 'SCRIPTS = REPO / "scripts"\ntarget = SCRIPTS / "check.py"\n',
        "tests/test_dir.py": 'DOCS = REPO / "docs"\nSTATUS = DOCS / "status"\n',
        "tests/test_importer.py": 'import modelquad.sample\n',
        "tests/test_sample_extra.py": '',
        "tests/test_unrelated.py": '',
    })
    changed = ["scripts/check.py", "docs/status/NOW.md", "src/modelquad/sample.py"]
    original_glob = Path.glob

    def shuffled_glob(path, pattern):
        entries = list(original_glob(path, pattern))
        random.Random(17).shuffle(entries)
        return iter(entries)

    first = check.focused_tests(changed)
    first_summary = capsys.readouterr().out
    monkeypatch.setattr(Path, "glob", shuffled_glob)
    second = check.focused_tests(list(reversed(changed)))
    assert second == first
    assert capsys.readouterr().out == first_summary
    assert {"tests/test_path.py", "tests/test_dir.py", "tests/test_importer.py",
            "tests/test_sample_extra.py"} <= set(second)


@pytest.mark.parametrize("statement", [
    "import modelquad.sample.changed as subject",
    "from modelquad.sample.changed import Subject as Renamed",
    "from modelquad.sample import changed as subject",
])
@pytest.mark.parametrize("function_level", [False, True])
def test_the_review_gate_selects_direct_imports_at_every_scope(
    check, tmp_path, monkeypatch, statement, function_level
):
    source = "def test_subject():\n    " + statement if function_level else statement
    # A parsed import must never execute either the module or the test body.
    _focused_fixture(check, tmp_path, monkeypatch, {
        "tests/test_consumer.py": source + "\nraise RuntimeError('never execute')\n",
        "tests/test_unrelated.py": "from modelquad.sample.other import Subject\n",
        "tests/test_changed_extra.py": "",
    })
    selected = check.focused_tests(["src/modelquad/sample/changed.py"])
    assert "tests/test_consumer.py" in selected
    assert "tests/test_unrelated.py" not in selected
    assert "tests/test_changed_extra.py" in selected


@pytest.mark.parametrize("export", [
    "from .changed import Subject as PublicSubject",
    "from modelquad.sample.changed import Subject as PublicSubject",
    "from . import changed as PublicSubject",
    "from .changed import *",
])
@pytest.mark.parametrize("statement", [
    "import modelquad.sample as subject",
    "from modelquad import sample as subject",
])
def test_the_review_gate_resolves_package_reexports(check, tmp_path, monkeypatch, export, statement):
    _focused_fixture(check, tmp_path, monkeypatch, {
        "src/modelquad/sample/__init__.py": export + "\nraise RuntimeError('never execute')\n",
        "tests/test_consumer.py": "def test_subject():\n    from modelquad.sample import PublicSubject\n",
        "tests/test_star.py": "from modelquad.sample import *\n",
        "tests/test_package.py": statement + "\n",
        "tests/test_unrelated.py": "from modelquad.sample import Other\n",
    })
    selected = check.focused_tests(["src/modelquad/sample/changed.py"])
    assert {"tests/test_consumer.py", "tests/test_star.py", "tests/test_package.py"} <= set(selected)
    if "*" not in export:
        assert "tests/test_unrelated.py" not in selected


def test_the_review_gate_selects_broken_syntax_for_pytest(check, tmp_path, monkeypatch):
    _focused_fixture(check, tmp_path, monkeypatch, {
        "tests/test_broken.py": "def test_broken(:\n",
    })
    assert "tests/test_broken.py" in check.focused_tests(["src/modelquad/sample.py"])
    assert check.focused_tests([]) == []


@pytest.mark.parametrize("changed", [
    "src/modelquad/sample/__init__.py",
    "src/modelquad/__init__.py",
])
@pytest.mark.parametrize("statement", [
    "from modelquad.sample.changed import Subject",
    "from modelquad import sample",
])
def test_the_review_gate_selects_package_module_importers(check, tmp_path, monkeypatch, changed, statement):
    _focused_fixture(check, tmp_path, monkeypatch, {
        "tests/test_consumer.py": statement + "\n",
    })
    assert "tests/test_consumer.py" in check.focused_tests([changed])


@pytest.mark.parametrize("changed, source, rule", [
    ("src/modelquad/sample/changed.py", "import modelquad.sample.changed\n", "importer"),
    ("scripts/check.py", 'target = REPO_ROOT / "scripts" / "check.py"\n', "path"),
    ("docs/status/NOW.md", 'DOCS = REPO / "docs"\nSTATUS = DOCS / "status"\n', "dir"),
])
def test_the_review_gate_reports_broad_selection_without_trimming(
    check, tmp_path, monkeypatch, capsys, changed, source, rule
):
    _focused_fixture(check, tmp_path, monkeypatch, {
        f"tests/test_consumer_{index}.py": source
        for index in range(3)
    })
    monkeypatch.setattr(check, "FOCUSED_TEST_FILE_LIMIT", 2)
    selected = check.focused_tests([changed])
    assert {f"tests/test_consumer_{index}.py" for index in range(3)} <= set(selected)
    summary = capsys.readouterr().out
    assert len(summary.splitlines()) == 1
    for text in ("3 whole test file(s)", "stem=", f"{rule}=3", "path=", "scan=", "all files retained"):
        assert text in summary
    check.focused_tests(["src/modelquad/config.py"])
    assert "all files retained" in capsys.readouterr().out


def test_every_repo_wide_scan_names_a_test_that_exists(check):
    assert list(check.REPO_WIDE_SCANS) == sorted(set(check.REPO_WIDE_SCANS))
    for entry in check.REPO_WIDE_SCANS:
        relpath, name = entry.split("::")
        assert name in _module_tests(REPO_ROOT / relpath), entry + " names no test; a rename dropped it"


def _repository_walkers(path: Path) -> set[str]:
    """Module-level helpers in ``path`` that walk a tree and take no arguments.

    A helper handed a root walks whatever the test gives it, usually a temporary
    tree. One that takes nothing can only walk what the module names, which is
    the repository: `_package_sources()` over `src/modelquad` is how a review
    found a credential scan this rule had missed.
    """
    walkers = set()
    source = path.read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("test_"):
            continue
        takes = node.args.posonlyargs or node.args.args or node.args.kwonlyargs or node.args.vararg
        if takes or node.args.kwarg:
            continue
        body = ast.get_source_segment(source, node)
        if ".rglob(" in body or "os.walk(" in body:
            walkers.add(node.name)
    return walkers


def test_every_test_that_walks_the_repository_is_a_repo_wide_scan(check):
    """A new whole-tree invariant joins the review gate, or this fails.

    The rule is structural: a module-level test that walks a tree (`rglob`,
    `os.walk`) and names the repository root, or that calls a module-level
    helper walking a tree with no arguments. A scan reached any other way is
    listed by hand.
    """
    walking = set()
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        walkers = _repository_walkers(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        tests = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        for name, source in _module_tests(path).items():
            if name == "test_every_test_that_walks_the_repository_is_a_repo_wide_scan":
                continue  # its own source spells the rule it looks for
            walks = ".rglob(" in source or "os.walk(" in source
            direct = walks and re.search(r"REPO_ROOT|repo_root|parents\[1\]", source)
            called = {
                node.func.id for node in ast.walk(tests[name])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            if direct or called & walkers:
                walking.add(f"tests/{path.name}::{name}")
    assert walking, "the rule found nothing; it no longer describes the suite"
    assert walking <= set(check.REPO_WIDE_SCANS), sorted(walking - set(check.REPO_WIDE_SCANS))
