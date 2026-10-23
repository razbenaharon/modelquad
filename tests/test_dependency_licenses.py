# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline license inventory for declared Python runtime dependencies."""

from importlib import metadata
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]

# Change the license policy here; unknown/missing licenses must fail closed.
ALLOWED_LICENSES = {
    "MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC",
    "PSF", "PSF-2.0", "Python-2.0", "MPL-2.0", "0BSD",
}

# Explicit current-dependency exceptions, not permissions for other packages.
PYTHON_EXCEPTIONS: dict[str, set[str]] = {}


def test_dependency_licenses():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = list(project["dependencies"])
    # Runtime extras ship too; dev tools do not. Honor platform markers.
    for group, dependencies in project.get("optional-dependencies", {}).items():
        if group != "dev":
            requirements.extend(dependencies)

    failures = []
    checked = set()
    for declared in requirements:
        requirement = Requirement(declared)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        name = canonicalize_name(requirement.name)
        if name in checked:
            continue
        checked.add(name)
        try:
            info = metadata.metadata(requirement.name)
        except metadata.PackageNotFoundError:
            failures.append(f"Python {name}: distribution not installed; license unknown")
            continue
        license_id = (info.get("License-Expression") or info.get("License") or "").strip()
        if license_id not in ALLOWED_LICENSES and license_id not in PYTHON_EXCEPTIONS.get(name, set()):
            failures.append(f"Python {name}: missing or unapproved license {license_id[:200]!r}")

    assert not failures, "Dependency license inventory failed:\n" + "\n".join(failures)
