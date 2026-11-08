# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Tracked documents stay plain text."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tracked_markdown() -> list[Path]:
    documents = sorted(REPO_ROOT.glob("*.md"))
    for tree in ("docs",):
        documents.extend(sorted((REPO_ROOT / tree).rglob("*.md")))
    return documents


def test_the_readme_links_resolve_off_github():
    """README.md is the package's long description (pyproject ``readme``).

    PyPI cannot resolve a path relative to the repository, so every image and
    link must be absolute (or an anchor in the README itself).
    """
    import re

    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    relative = re.findall(r'(?:src|srcset|href)="(?!https://|#)[^"]*"', text)
    relative += re.findall(r"\]\((?!https://|#)[^)]*\)", text)
    assert not relative, f"relative links in README.md: {relative}"


def test_no_document_carries_a_control_byte():
    """A NUL in a tracked Markdown file is not a cosmetic problem.

    Git classifies the file as binary, so it stops normalising its line
    endings and stops diffing it; ``rg`` reports "binary file matches" and
    prints nothing. The document becomes unreadable to every tool the next
    person would reach for, and nothing announces it. Runs on every platform:
    it reads bytes and imports nothing.
    """

    allowed = {0x09, 0x0A, 0x0D}  # tab, and the two line-ending bytes
    offenders = []
    for document in _tracked_markdown():
        raw = document.read_bytes()
        bad = sorted({byte for byte in raw if byte < 0x20 and byte not in allowed})
        if bad:
            offenders.append(f"{document.relative_to(REPO_ROOT).as_posix()}: {[hex(b) for b in bad]}")
    assert not offenders, f"control bytes in tracked Markdown: {offenders}"
