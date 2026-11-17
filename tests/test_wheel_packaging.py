# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Regression coverage for package data shipped as package resources in the wheel."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from modelquad.procutil import no_window_kwargs


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, **no_window_kwargs()
    )
    assert completed.returncode == 0, (
        f"Command failed: {' '.join(command)}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def env_without_the_source_tree() -> dict[str, str]:
    """The environment every step of a wheel-install test must run under.

    Not just the final probe: the ``pip install`` step above all. ``src/``
    contains ``modelquad.egg-info``, so an ABSOLUTE ``PYTHONPATH=<repo>/src``
    makes pip see ``modelquad 0.2.0`` as an already-satisfied requirement --
    it then reports success, exits 0, and installs nothing at all. The test
    that exists to prove the wheel works without the source checkout would be
    probing an empty virtualenv.

    This was latent rather than harmless: the repository's documented
    invocation is ``PYTHONPATH=src`` (relative), which does not resolve from
    the tests' ``cwd=tmp_path``, so the install happened and the tests passed.
    The release pipeline sets PYTHONPATH absolutely -- correctly, since it must
    certify one specific worktree -- and both tests failed there immediately.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _install_wheel(venv_python: Path, wheel: Path, *, cwd: Path, env: dict[str, str]) -> None:
    """pip install the wheel, then prove it actually landed.

    The assertion is the point. pip skipping an install is silent and exits 0,
    so without this the next failure is a bare ModuleNotFoundError several
    steps later that says nothing about why.
    """
    _run(
        [str(venv_python), "-m", "pip", "install", "--no-deps", "-q", str(wheel)],
        cwd=cwd,
        env=env,
    )
    _run(
        [
            str(venv_python),
            "-c",
            (
                "import importlib.metadata as md\n"
                "md.version('modelquad')  # raises if pip installed nothing\n"
            ),
        ],
        cwd=cwd,
        env=env,
    )


#: The documents NOTICE and README point to, which the sdist must carry.
SDIST_DOCUMENTS = ("PRIVACY.md", "TRADEMARK.md", "THIRD_PARTY.md", "SECURITY.md")


def private_build_root(repo_root: Path, tmp_path: Path) -> Path:
    """Copy the package inputs so setuptools writes only into this test's scratch.

    Both ``build/`` and ``src/modelquad.egg-info`` are written relative to the
    source root, even when ``python -m build --outdir`` names a private output.
    """
    root = tmp_path / "source"
    root.mkdir()
    for name in ("pyproject.toml", "setup.py", "MANIFEST.in", "README.md", "LICENSE", "NOTICE", *SDIST_DOCUMENTS):
        shutil.copy2(repo_root / name, root / name)
    shutil.copytree(
        repo_root / "src",
        root / "src",
        ignore=shutil.ignore_patterns("*.egg-info", "__pycache__", "*.pyc"),
    )
    assert root.resolve() != repo_root.resolve()
    assert not (root / "src" / "modelquad.egg-info").exists()
    return root


def ensure_egg_info(build_root: Path) -> Path:
    """Materialise private ``src/modelquad.egg-info`` for the pip hazard test.

    setuptools writes it into ``package-dir`` during any in-tree build, so a
    checkout that has built once carries it and a fresh clone or a new worktree
    does not. Build it in the test's copied source to avoid mutating the checkout
    and depending on the order the suite runs in.

    Ask setuptools for the metadata directly instead -- the same command an
    in-tree build runs, without building a wheel -- so the precondition is
    established rather than inherited.
    """
    egg_info = build_root / "src" / "modelquad.egg-info"
    _run(
        [
            sys.executable,
            "-c",
            "import setuptools; setuptools.setup()",
            "egg_info",
            "--egg-base",
            "src",
        ],
        cwd=build_root,
        env=env_without_the_source_tree(),
    )
    return egg_info


@pytest.mark.ci_only
def test_the_source_tree_really_does_look_installed_to_pip(tmp_path):
    """The hazard env_without_the_source_tree() exists to remove, proven real.

    Not theoretical: src/ with modelquad.egg-info makes anything that puts src/
    on sys.path makes importlib.metadata -- and therefore pip's
    already-satisfied check -- report modelquad as an installed distribution.
    A wheel install performed under that environment exits 0 and installs
    nothing.
    """
    repo_root = Path(__file__).resolve().parents[1]
    build_root = private_build_root(repo_root, tmp_path)
    assert ensure_egg_info(build_root).is_dir(), (
        "no egg-info: if setuptools stopped creating it the hazard is gone and"
        " this guard should be retired deliberately, not left failing"
    )

    # Asserted as "which distribution wins", not "is one found at all": this
    # machine and the CI runner both have modelquad genuinely installed, so
    # "not found" is not available as a signal anywhere it matters.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(build_root / "src")
    seen = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.metadata as md, sys;"
            " sys.stdout.write(str(md.distribution('modelquad').locate_file('')))",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        **no_window_kwargs(),
    )
    assert seen.returncode == 0, seen.stderr
    winner = Path(seen.stdout.strip()).resolve()
    assert winner == (build_root / "src").resolve(), (
        "expected the source tree to win pip's already-satisfied check when it is"
        f" on PYTHONPATH; the winning distribution was {winner}"
    )

    assert "PYTHONPATH" not in env_without_the_source_tree(), (
        "the isolated environment must not carry PYTHONPATH into the wheel install"
    )


@pytest.mark.ci_only
def test_wheel_install_exposes_package_data_without_the_source_checkout(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    build_root = private_build_root(repo_root, tmp_path)
    isolated_env = env_without_the_source_tree()
    dist_dir = tmp_path / "dist"
    _run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)],
        cwd=build_root,
        env=isolated_env,
    )
    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1

    venv_dir = tmp_path / "venv"
    _run([sys.executable, "-m", "venv", str(venv_dir)], cwd=tmp_path, env=isolated_env)
    venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _install_wheel(venv_python, wheels[0], cwd=tmp_path, env=isolated_env)
    _run(
        [
            str(venv_python),
            "-c",
            (
                "import importlib.resources\n"
                "for package, name in (\n"
                "    ('modelquad.adapters', 'templates/claude/mcp/mcp-servers.json'),\n"
                "    ('modelquad.adapters', 'templates/claude/skills/modelquad/SKILL.md'),\n"
                "):\n"
                "    resource = importlib.resources.files(package).joinpath(*name.split('/'))\n"
                "    assert resource.is_file(), name + ' missing from installed wheel'\n"
                "    assert resource.read_text(encoding='utf-8').strip()\n"
            ),
        ],
        cwd=tmp_path,
        env=isolated_env,
    )


@pytest.mark.ci_only
def test_sdist_carries_the_documents_notice_and_readme_point_to(tmp_path):
    import tarfile

    repo_root = Path(__file__).resolve().parents[1]
    build_root = private_build_root(repo_root, tmp_path)
    dist_dir = tmp_path / "dist"
    _run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(dist_dir)],
        cwd=build_root,
        env=env_without_the_source_tree(),
    )
    (sdist,) = dist_dir.glob("*.tar.gz")
    with tarfile.open(sdist) as source:
        names = {name.split("/", 1)[1] for name in source.getnames() if "/" in name}
    for name in ("LICENSE", "NOTICE", "README.md", *SDIST_DOCUMENTS):
        assert name in names, f"{name} missing from the sdist"


def _wheel_modules(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        return {name for name in archive.namelist() if name.startswith("modelquad/")}


def test_a_leftover_build_folder_adds_nothing_to_the_wheel(tmp_path):
    """A checkout that built an earlier version still has that version's
    modules in ``build/lib``. setuptools makes the wheel from that folder
    without emptying it, so the wheel of the reduced product carried modules
    removed from ``src/`` (found 2026-11-17 in a hand-built wheel). setup.py
    empties the folder first; the wheel holds what ``src/`` holds and no more.
    """
    repo_root = Path(__file__).resolve().parents[1]
    build_root = private_build_root(repo_root, tmp_path)
    stale = build_root / "build" / "lib" / "modelquad"
    (stale / "removed_package").mkdir(parents=True)
    (stale / "removed_module.py").write_text("LEFTOVER = True\n", encoding="utf-8")
    (stale / "removed_package" / "__init__.py").write_text("", encoding="utf-8")
    (stale / "adapters" / "templates").mkdir(parents=True)
    (stale / "adapters" / "templates" / "removed.json").write_text("{}", encoding="utf-8")

    dist_dir = tmp_path / "dist"
    _run([sys.executable, "setup.py", "-q", "bdist_wheel", "--dist-dir", str(dist_dir)],
         cwd=build_root, env=env_without_the_source_tree())
    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1

    source = build_root / "src"
    expected = {path.relative_to(source).as_posix()
                for path in (source / "modelquad").rglob("*")
                if path.is_file() and "__pycache__" not in path.parts}
    shipped = _wheel_modules(wheels[0])
    assert not {name for name in shipped if "removed" in name}
    assert shipped == expected
