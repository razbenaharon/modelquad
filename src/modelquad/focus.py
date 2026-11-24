# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Focused second opinions: three fixed questions about named material.

A focused review is an ordinary review (the same gates, package, approval and
run) whose task text starts with one of three fixed instructions. ModelQuad
adds that instruction itself, so the package id, the preview and the run all
cover the same text, and Claude cannot drift from it between preview and run.

* ``plan``: review an implementation plan against its requirements
  (task kind ``architecture``).
* ``decision``: challenge an architecture decision (RFC or ADR)
  (task kind ``architecture``).
* ``requirements``: check an implementation against stated requirements
  (task kind ``review``).

The set is closed. A repository that removes a task kind in
``.modelquad.toml`` removes the focused reviews of that kind with it.

``mentioned_files`` is a hint for Claude, like ``review.may_also_need``: paths
the task text or a sent document names that exist, pass the read rules and
were not sent. Nothing is added to the package by it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Focus:
    name: str
    task_type: str
    title: str
    instruction: str


_COMMON = (
    "Report each problem as a FINDING line in the format below. Its location is the file and "
    "line that establishes it (relative/path.ext:line), as the format says; never a heading. In "
    "its reasoning, name the section it rests on, say what the material states, and say which "
    "decision the problem affects, as 'Affects: <decision>'. Facts about this project come only "
    "from the supplied material; do not assume what it does not state. Established technical "
    "knowledge (how a named database, protocol, library or tool behaves) is evidence too: use "
    "it, say plainly what it implies, and mark it as general knowledge. When material you need is "
    "missing, write a line 'MISSING: <what is missing> | <why it matters to the decision>' "
    "instead of guessing, and name any missing file in the envelope's files_needed. A review "
    "with no findings is a valid answer when the material supports it: then write "
    "'FINDINGS: none' and say in one sentence why.")

FOCI: dict[str, Focus] = {
    "plan": Focus(
        name="plan", task_type="architecture", title="Review an implementation plan",
        instruction=(
            "FOCUSED REVIEW: an implementation plan, before coding starts. The supplied files are "
            "the plan, its requirements and its constraints; the task text below names which is "
            "which and may quote an excerpt of the plan. Answer three questions, each under its "
            "own heading: 1. Contradictions: where does the plan contradict a stated requirement "
            "or constraint? Quote both sides. 2. Assumptions to verify: which assumptions does "
            "the plan rest on that the material does not establish, and how could each be "
            "checked cheaply? 3. Decide before coding: which open decisions must be settled "
            "before implementation starts, and what each one blocks? Do not review code style "
            "or propose a different plan unless a requirement forces it. ")),
    "decision": Focus(
        name="decision", task_type="architecture", title="Challenge an architecture decision",
        instruction=(
            "FOCUSED REVIEW: challenge an architecture decision. The supplied files are an RFC "
            "or ADR, the alternatives it considered and the relevant constraints; the task text "
            "names the decision. Do not simply agree or disagree. Answer three questions, each "
            "under its own heading: 1. Holds when: under which conditions (load, scale, team, "
            "cost, failure modes, timelines) is the decision sound, according to the material? "
            "2. Could reverse it: which facts, if true or if they change, would make an "
            "alternative better? Say which alternative. 3. Check that would decide: which "
            "concrete measurement, prototype or test would settle the open points, and what "
            "result would favour which option? ")),
    "requirements": Focus(
        name="requirements", task_type="review", title="Check implementation against requirements",
        instruction=(
            "FOCUSED REVIEW: check an implementation against stated requirements. The supplied "
            "files are the requirement, the implementation and its tests; the task text names "
            "which requirements to check. For each requirement, write one line "
            "'REQ <id or short name> | met | gap | unproven | <evidence>': met when the code "
            "and a test show it, gap when the code contradicts it or leaves it out, unproven "
            "when the code may meet it but no supplied test shows it. Cite file:line for the "
            "evidence. Then report each gap, and each place where the tests do not yet answer "
            "whether a requirement holds, as a FINDING. Do not report style or problems "
            "unrelated to the requirements. ")),
}

#: The closed names, in the order the docs list them.
NAMES = tuple(FOCI)


def get(name) -> Focus | None:
    return FOCI.get(name) if isinstance(name, str) else None


def task_text(name: str | None, prompt: str) -> str:
    """The task text as sent: the fixed instruction first, then Claude's text."""
    focus = get(name)
    if focus is None:
        return prompt
    return f"{focus.instruction}{_COMMON}\n\nTASK:\n{prompt}"


# ------------------------------------------------------------- the file hint

#: At most this many mentioned files are offered per package.
MAX_MENTIONED = 8
#: Only text documents are scanned for mentioned paths; code is not.
_DOC_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".adoc")
#: A name in backticks or quotes is one unit, spaces included: `docs/Release Notes.md`.
_QUOTED = re.compile(r"[`\"']([^`\"'\r\n]{1,200}\.[A-Za-z0-9]{1,8})[`\"']")
#: A bare relative path with an extension: docs/sync.md, src/a/b.py, (x.toml). Not after
#: a drive letter, a colon, a slash or a word character, so C:x.md and a/../x.md stay out.
_PATH = re.compile(r"(?<![\w/.\\:-])((?:[\w.-]+[/\\])*[\w-][\w.-]*\.[A-Za-z0-9]{1,8})(?![\w/\\:-])")


def _candidates(text: str) -> list[str]:
    found = []
    spans = [match.group(1) for match in _QUOTED.finditer(text)]
    rest = _QUOTED.sub(" ", text)
    for raw in (*spans, *(match.group(1) for match in _PATH.finditer(rest))):
        name = raw.strip().replace("\\", "/").removeprefix("./").rstrip(".")
        parts = name.split("/")
        if (not name or name.startswith("/") or ":" in name or ".." in parts
                or "." not in parts[-1]):
            continue
        found.append(name)
    return found


def _plain_file(root: str, name: str) -> bool:
    """True when every component under root is a plain folder or file, never a link."""
    import os
    import stat

    from .execution.noshell import _linked

    path = root
    parts = name.split("/")
    try:
        for index, part in enumerate(parts):
            path = os.path.join(path, part)
            info = os.lstat(path)
            if _linked(info):
                return False
            last = index == len(parts) - 1
            if not (stat.S_ISREG(info.st_mode) if last else stat.S_ISDIR(info.st_mode)):
                return False
    except (OSError, ValueError):
        return False
    return True


def mentioned_files(working_directory: str, task_text: str, packed) -> list[str]:
    """Existing workspace files that the task text or a sent document names, not sent.

    ``task_text`` is the task text after the credential screen and ``packed``
    the (name, text) pairs the packer sent, both already checked and
    redacted: no file is opened here. A candidate must be a plain file under
    the working folder reached through no link, pass the same read rules as
    a named file, and not have a secret-shaped name. A hint: nothing is
    added to the package and its id does not change.
    """
    import os

    from .execution import file_rules, noshell

    try:
        root = os.path.abspath(working_directory)
        texts = [task_text] if isinstance(task_text, str) else []
        texts += [text for name, text in packed if name.casefold().endswith(_DOC_SUFFIXES)]
        taken = {name.casefold() for name, _ in packed}
        names: list[str] = []
        for text in texts:
            for name in _candidates(text):
                try:
                    name = noshell.canonical_path(name)
                except noshell.TargetError:
                    continue
                if (name.casefold() in taken or name in names or noshell._secret_reference(name)
                        or not _plain_file(root, name)):
                    continue
                names.append(name)
        if not names:
            return []
        allowed, _withheld = file_rules.screen(root, names)
    except Exception:  # noqa: BLE001 -- a hint must never fail a package
        return []
    allowed = set(allowed)
    return [name for name in names if name in allowed][:MAX_MENTIONED]


def mentioned_line(paths) -> str:
    return ("Named in the task text or a sent document, not sent (a new package can add them "
            "if the user agrees): " + ", ".join(paths) + ".")
