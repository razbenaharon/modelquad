#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Generate the third-party dependency and licence inventory.

Evidence, not recollection. Every row is read from the dependency's own
distribution metadata (``*.dist-info/METADATA``) -- the authoritative statement
of licence made by the copyright holder -- rather than from a remembered fact
about the project.

    python scripts/third_party_inventory.py            # human table
    python scripts/third_party_inventory.py --json      # machine-readable
    python scripts/third_party_inventory.py --notice    # THIRD_PARTY.md table

It starts from what a source install pulls in -- ``pyproject.toml``'s
``dependencies`` plus the ``service`` extra -- and walks their runtime
requirements, including the extras one dependency requests of another
(``mcp`` requires ``pyjwt[crypto]``, which is what brings in
``cryptography``). Environment markers are evaluated for the generating
interpreter, so a backport an older Python would need is not listed. ModelQuad is distributed as source only and bundles
none of them; the inventory is for information.

**Limits, stated rather than hidden.** This reports what the *current
environment* has installed, so a dependency that is only installed on another
platform reads ``not installed here`` rather than being silently omitted, and
a licence field a project fills in loosely is reported as loosely as it was
written. It is an engineering inventory that tells counsel where to look; it
is not itself a legal conclusion, and it does not attempt to decide whether an
obligation is satisfied.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Iterator

try:
    from packaging.requirements import InvalidRequirement, Requirement
except ImportError:  # pragma: no cover - packaging ships with pip/build/pytest
    Requirement = None  # type: ignore[assignment,misc]
    InvalidRequirement = ValueError  # type: ignore[assignment,misc]

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
#: The extras a working install needs: the local service. The MCP server is
#: part of the core dependencies.
RUNTIME_EXTRAS = ("service",)


def _root_requirements() -> list[str]:
    """The requirement strings of a source install with the runtime extras.

    Read from ``pyproject.toml`` rather than duplicated here: the point of
    the accompanying test is that adding a dependency and forgetting the
    notice is a failure, and that only works if this list is pyproject's list.
    """

    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8")).get("project", {})
    if "dependencies" not in project:  # pragma: no cover - pyproject rewritten
        raise SystemExit(f"could not find the dependencies list in {PYPROJECT}")
    requirements = list(project["dependencies"])
    optional = project.get("optional-dependencies", {})
    for extra in RUNTIME_EXTRAS:
        if extra not in optional:  # pragma: no cover
            raise SystemExit(f"could not find the {extra!r} extra in {PYPROJECT}")
        requirements.extend(optional[extra])
    return requirements


def source_install_packages() -> list[str]:
    """The distribution names a source install with the runtime extras requires."""

    names: list[str] = []
    for requirement in _root_requirements():
        parsed = _parse(requirement, ())
        if parsed is not None and parsed[0] not in names:
            names.append(parsed[0])
    return names


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse(requirement: str, extras: tuple[str, ...] | frozenset[str]) -> tuple[str, frozenset[str]] | None:
    """``(name, requested extras)`` of a requirement that applies, or None.

    A requirement applies when its marker holds for this interpreter, either
    unconditionally or for one of the ``extras`` requested of the distribution
    that declares it. Requirements behind an extra nobody requested are opt-in
    and not part of what the install contains.
    """

    if Requirement is None:  # pragma: no cover - fallback without packaging
        if "extra ==" in requirement:
            return None
        head = requirement.split(";", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?", head)
        if not match:
            return None
        requested = match.group(2) or ""
        return match.group(1), frozenset(e.strip() for e in requested.split(",") if e.strip())
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement:
        return None
    if parsed.marker is not None:
        environments = [{"extra": ""}] + [{"extra": extra} for extra in sorted(extras)]
        if not any(parsed.marker.evaluate(environment) for environment in environments):
            return None
    return parsed.name, frozenset(parsed.extras)


def _dependencies(requirements: list[str], extras: frozenset[str]) -> Iterator[tuple[str, frozenset[str]]]:
    for requirement in requirements:
        parsed = _parse(requirement, extras)
        if parsed is not None:
            yield parsed


def _declared_license(metadata: Any) -> str:
    expression = metadata.get("License-Expression")
    if expression:
        return expression.strip()
    raw = metadata.get("License")
    if raw and raw.strip():
        # Some projects paste the entire licence text into this field.
        return raw.strip().splitlines()[0][:80]
    classifiers = [
        c for c in (metadata.get_all("Classifier") or []) if c.startswith("License ::")
    ]
    if classifiers:
        return classifiers[0].split("::")[-1].strip()
    return "UNDECLARED"


def python_inventory() -> list[dict[str, Any]]:
    """Every Python distribution reachable from the direct dependencies."""

    import importlib.metadata as md

    roots = list(_dependencies(_root_requirements(), frozenset()))
    found: dict[str, dict[str, Any]] = {}
    # (distribution, extras requested of it) pairs already expanded: the same
    # package can be reached again with an extra that pulls in more.
    expanded: set[tuple[str, frozenset[str]]] = set()
    queue = list(roots)
    root_keys = {_normalize(name) for name, _ in roots}

    while queue:
        name, extras = queue.pop()
        key = _normalize(name)
        extras = frozenset(_normalize(extra) for extra in extras)
        if (key, extras) in expanded:
            continue
        expanded.add((key, extras))
        try:
            metadata = md.metadata(name)
        except Exception:
            found.setdefault(key, {
                "name": name,
                "version": None,
                "license": None,
                "bundled_root": key in root_keys,
                "status": "not installed here",
            })
            continue
        found.setdefault(key, {
            "name": metadata.get("Name") or name,
            "version": md.version(name),
            "license": _declared_license(metadata),
            "bundled_root": key in root_keys,
            "status": "installed",
        })
        queue.extend(_dependencies(md.requires(name) or [], extras))

    return sorted(found.values(), key=lambda row: _normalize(row["name"]))



def _table(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| Package | Version | Declared licence | Depth | Present here |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        version = row["version"] or "n/a"
        license_value = row["license"] or "not readable"
        depth = "direct" if row["bundled_root"] else "transitive"
        present = "yes" if row["status"] == "installed" else row["status"]
        lines.append(
            f"| `{row['name']}` | {version} | {license_value} | {depth} | {present} |"
        )
    return "\n".join(lines)


def notice_body() -> str:
    return _table(python_inventory()) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Emit the inventory as JSON.")
    output.add_argument(
        "--notice", action="store_true", help="Emit the Markdown table used by THIRD_PARTY.md."
    )
    args = parser.parse_args(argv)

    if args.json:
        print(
            json.dumps(
                {"python": python_inventory()},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.notice:
        print(notice_body(), end="")
        return 0

    print("Python distributions a source install pulls in")
    print(_table(python_inventory()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
