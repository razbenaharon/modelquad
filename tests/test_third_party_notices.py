# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The licence files, and the dependency list a source install pulls in.

The failure the dependency half guards against is quiet: someone adds a
dependency to ``pyproject.toml`` and nothing tells the reader about it. A
missing row breaks no test and produces no runtime error.

These tests deliberately do not check *whether an obligation is satisfied* --
that is a question for legal counsel. They check what a machine can decide:
that nothing installed is missing from the list, that a copyleft dependency is
never listed silently, and that the places stating ModelQuad's own licence
agree.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
THIRD_PARTY = REPO_ROOT / "THIRD_PARTY.md"

_spec = importlib.util.spec_from_file_location(
    "third_party_inventory", REPO_ROOT / "scripts" / "third_party_inventory.py"
)
inventory = importlib.util.module_from_spec(_spec)
sys.modules["third_party_inventory"] = inventory
_spec.loader.exec_module(inventory)

#: Licence families whose obligations a permissive-only reading would miss.
COPYLEFT = re.compile(r"\b(?:A?GPL|LGPL|MPL|EPL|CDDL|EUPL)\b|General Public|Mozilla Public", re.I)


@pytest.fixture(scope="module")
def third_party_text() -> str:
    return THIRD_PARTY.read_text(encoding="utf-8")


def test_every_direct_python_dependency_is_listed(third_party_text: str):
    required = inventory.source_install_packages()
    assert required, "pyproject.toml's dependency lists could not be parsed"
    # Names are matched case- and separator-insensitively.
    haystack = inventory._normalize(third_party_text)
    missing = [name for name in required if inventory._normalize(name) not in haystack]
    assert not missing, (
        "required by pyproject.toml (dependencies and the service extra) "
        f"but absent from THIRD_PARTY.md: {missing}. Regenerate with "
        "`python scripts/third_party_inventory.py --notice`."
    )


def test_a_copyleft_dependency_is_never_listed_silently(third_party_text: str):
    """THIRD_PARTY.md says no dependency declares a copyleft licence. If one
    arrives, that sentence has to change with it, deliberately."""
    copyleft = [row["name"] for row in inventory.python_inventory()
                if row["license"] and COPYLEFT.search(row["license"])]
    if copyleft:
        assert "No dependency in it declares a\ncopyleft licence." not in third_party_text
        for name in copyleft:
            assert name in third_party_text
    else:
        assert "No dependency in it declares a\ncopyleft licence." in third_party_text


def test_the_list_does_not_claim_to_be_legal_advice(third_party_text: str):
    assert "not legal advice" in third_party_text.lower()


def test_the_inventory_script_runs_and_finds_the_direct_dependencies():
    rows = inventory.python_inventory()
    assert rows
    roots = [row for row in rows if row["bundled_root"]]
    assert len(roots) == len(inventory.source_install_packages())


def test_the_roots_are_the_core_dependencies_and_the_service_extra():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = list(project["dependencies"]) + list(project["optional-dependencies"]["service"])
    names = {inventory._normalize(re.match(r"[A-Za-z0-9_.\-]+", d).group(0)) for d in declared}
    assert {inventory._normalize(n) for n in inventory.source_install_packages()} == names
    assert "mcp" in names, "the MCP SDK is a core dependency"


def test_requested_extras_of_a_dependency_are_followed():
    """``pyjwt[crypto]`` (required by ``mcp``) brings in ``cryptography``. An
    inventory that drops every ``extra ==`` requirement misses it."""
    requirement = 'cryptography>=3.4.0; extra == "crypto"'
    assert inventory._parse(requirement, frozenset()) is None
    assert inventory._parse(requirement, frozenset({"crypto"})) == ("cryptography", frozenset())
    assert inventory._parse("pyjwt[crypto]>=2.10.1", frozenset()) == ("pyjwt", frozenset({"crypto"}))


def test_a_dependency_reached_through_an_extra_is_listed(third_party_text: str):
    rows = {inventory._normalize(row["name"]) for row in inventory.python_inventory()}
    if "pyjwt" not in rows:
        pytest.skip("mcp is not installed in this environment")
    for name in ("cryptography", "cffi", "pycparser"):
        assert name in rows, name
        assert f"`{name}`" in third_party_text, name


def test_the_licence_files_and_pyproject_agree():
    """Not a check that the licence is *right*, but that the places stating it
    do not drift apart. If the licence changes, they move in one commit."""
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    notice = (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "Apache License" in license_text and "Version 2.0, January 2004" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    project = tomllib.loads(pyproject)["project"]
    # PEP 639: an SPDX expression plus the files that ship in the metadata.
    assert project["license"] == "Apache-2.0"
    assert set(project["license-files"]) == {"LICENSE", "NOTICE"}
    # PEP 639 deprecates licence classifiers alongside an SPDX expression.
    assert not [c for c in project["classifiers"] if c.startswith("License ::")]
    assert "Apache License, Version 2.0" in notice
    assert "Copyright 2026 Raz Ben Aharon" in notice


def test_every_python_file_carries_the_spdx_identifier():
    """Each authored Python file names its licence in its first lines, so a file
    copied out of the tree still says what it is."""
    missing = []
    for root in ("src", "tests", "scripts"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            head = path.read_text(encoding="utf-8").splitlines()[:3]
            if "# SPDX-License-Identifier: Apache-2.0" not in head:
                missing.append(path.relative_to(REPO_ROOT).as_posix())
    head = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8").splitlines()[:3]
    if "# SPDX-License-Identifier: Apache-2.0" not in head:
        missing.append("conftest.py")
    assert not missing, f"Python files without the SPDX line: {missing[:20]}"
