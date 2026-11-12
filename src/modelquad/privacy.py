# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The privacy boundary for stored text.

AGENTS.md says command lines, tool stdout/stderr and absolute paths do not
reach metadata or history. Before this module that was held by every call
site remembering to hold it, which is the shape of guarantee that decays: a
review found absolute paths that had reached persisted evidence through
code that looked correct, and raw exception strings that carried the path
that caused them.

So the guarantee is enforced where the data crosses the boundary, once, in a
module that imports nothing from the rest of the package:

- :func:`redact_paths` -- the textual primitive. Known user and machine
  roots (the home folder, ``LOCALAPPDATA``, ``APPDATA``, ``TEMP``, OneDrive
  folders, the working folder and any root the caller names) are replaced
  first, case-insensitively and including their spaces; then absolute
  Windows, UNC (either slash), ``file://``, URL-encoded drive, MSYS
  (``/c/...``), drive-less ``\\Users``, ``~`` and POSIX home/temp paths
  become ``<path>``.
- :func:`sanitize_persisted_text` and
  :func:`sanitize_persisted_metadata_json` -- what ``service/history.py``
  runs free text and metadata through before they are written.

**This module enables no collection.** Every function here only removes
information or refuses it. ModelQuad has no telemetry.
"""

from __future__ import annotations

import functools
import os
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path redaction
# ---------------------------------------------------------------------------

# Where a path inside a longer message stops: whitespace, the quotes that
# normally delimit it, and the angle brackets Windows forbids in a name, so
# "<C:\x>" keeps its closing bracket.
_STOP = r"\s\"';<>"

# ``C:\Users\...`` / ``C:/Users/...``. Stops at whitespace and the quote
# characters that normally delimit a path inside a longer message, so the
# surrounding diagnostic text survives. A folder name with a space in it is
# why the known roots below are replaced first.
_WINDOWS_ABS_PATH = re.compile(r"[A-Za-z]:[\\/][^" + _STOP + r"]*")

# ``C%3A%5CUsers%5C...`` -- a drive path that went through URL encoding.
_ENCODED_DRIVE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Za-z]%3A(?:%5C|%2F)[^" + _STOP + r"]*")

# ``file:///C:/Users/...`` / ``file://server/share/...``. The URI goes whole,
# scheme included, so no "fil" is left in front of the placeholder.
_FILE_URI = re.compile(r"(?i)\bfile:/{2,3}[^" + _STOP + r"]*")

# ``\\server\share\...`` -- a UNC path names a host as well as a location.
_UNC_PATH = re.compile(r"\\\\[^" + _STOP + r"]+")

# ``//server/share/...`` -- the same with forward slashes. Never after a
# colon, so ``https://`` is not one, and a host and a share are both needed,
# so a "// comment" stays.
_UNC_FORWARD_PATH = re.compile(r"(?<![\w:/])//[^/" + _STOP + r"]+/[^" + _STOP + r"]*")

# ``/c/Users/...`` -- a drive as Git for Windows (MSYS) writes it.
_MSYS_DRIVE_PATH = re.compile(r"(?<![\w/.:~-])/[A-Za-z]/[^" + _STOP + r"]+")

# ``\Users\...`` -- a path rooted on the current drive, without its letter.
_ROOTED_USER_PATH = re.compile(
    r"(?i)(?<![\w\\:])\\(?:Users|home|Documents and Settings)\\[^" + _STOP + r"]*"
)

# ``~\project`` / ``~/project`` -- the home folder by another name.
_TILDE_PATH = re.compile(r"(?<![\w~])~[\\/][^" + _STOP + r"]*")

# POSIX absolute paths, restricted to the roots that actually carry user or
# machine-identifying material. Redacting every ``/x`` would destroy ordinary
# prose ("exit 1 (tests failed) on 3 target(s)") for no privacy gain.
_POSIX_ABS_PATH = re.compile(
    r"(?<![\w])/(?:Users|home|tmp|var|private|mnt|media|opt|root)/[^" + _STOP + r"]*"
)

# A business OneDrive folder is named after the organization
# ("OneDrive - Contoso Ltd"). When its root is not known, the name still goes.
_ONEDRIVE_ORG = re.compile(r"(?i)\bOneDrive - [^\\/\r\n\"';<>]+")

PATH_PLACEHOLDER = "<path>"

#: Environment variables that name a user or machine root.
_ROOT_ENVIRONMENT = ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP",
                     "OneDrive", "OneDriveCommercial", "OneDriveConsumer", "HOME")

_DRIVE_PATH = re.compile(r"[A-Za-z]:[\\/]")


def _known_roots(extra: Iterable[Any] = ()) -> tuple[str, ...]:
    """Absolute roots worth replacing whole, longest first.

    A bare drive or filesystem root is never one: replacing a drive root would
    swallow every path's useful tail without protecting anything.
    """

    candidates: list[str] = [os.environ.get(name) or "" for name in _ROOT_ENVIRONMENT]
    for read in (Path.home, Path.cwd):
        try:
            candidates.append(str(read()))
        except (OSError, RuntimeError):
            pass
    candidates.extend(str(item) for item in extra if item)
    roots = set()
    for raw in candidates:
        value = raw.strip().rstrip("\\/")
        if len(value) <= 3 or not (value.startswith(("/", "\\\\")) or _DRIVE_PATH.match(value)):
            continue
        roots.add(value)
    return tuple(sorted(roots, key=lambda root: (-len(root), root.casefold())))


@functools.lru_cache(maxsize=32)
def _roots_pattern(roots: tuple[str, ...]) -> "re.Pattern[str] | None":
    if not roots:
        return None
    alternatives = []
    for root in roots:
        pieces = re.split(r"[\\/]+", root)
        alternatives.append(r"[\\/]+".join(re.escape(piece) for piece in pieces))
    # The root, then the rest of that path up to whitespace or a quote. The
    # root must end at a separator or the end of the path, so "C:\Users\Ann"
    # does not eat "C:\Users\Anna".
    return re.compile(r"(?:" + "|".join(alternatives) + r")(?=$|[\\/" + _STOP + r"])"
                      r"(?:[\\/][^" + _STOP + r"]*)?", re.IGNORECASE)


def redact_paths(text: str, *, roots: Iterable[Any] = ()) -> str:
    """Replace absolute filesystem paths in ``text`` with ``<path>``.

    Known roots go first, matched whole and case-insensitively, so a home
    folder whose name has a space ("John Smith") is removed whole.
    ``roots`` adds folders the caller knows about, such as the working
    folder of the task. Total: it never raises, and text containing no path
    is returned unchanged, so it is safe to run on every persisted string.
    """

    out = text
    pattern = _roots_pattern(_known_roots(roots))
    if pattern is not None:
        out = pattern.sub(PATH_PLACEHOLDER, out)
    out = _ONEDRIVE_ORG.sub("OneDrive", out)
    # A file URI first: its scheme would otherwise read as the drive "e:".
    # After the roots, so "file:///<path>" also goes whole.
    out = _FILE_URI.sub(PATH_PLACEHOLDER, out)
    out = _ENCODED_DRIVE_PATH.sub(PATH_PLACEHOLDER, out)
    out = _WINDOWS_ABS_PATH.sub(PATH_PLACEHOLDER, out)
    out = _UNC_PATH.sub(PATH_PLACEHOLDER, out)
    out = _UNC_FORWARD_PATH.sub(PATH_PLACEHOLDER, out)
    out = _ROOTED_USER_PATH.sub(PATH_PLACEHOLDER, out)
    out = _MSYS_DRIVE_PATH.sub(PATH_PLACEHOLDER, out)
    out = _TILDE_PATH.sub(PATH_PLACEHOLDER, out)
    return _POSIX_ABS_PATH.sub(PATH_PLACEHOLDER, out)


def contains_absolute_path(text: str) -> bool:
    """Whether ``text`` still carries an absolute path. Used by the gates."""

    return redact_paths(text) != text


# How much free text a single persisted diagnostic may carry. A message long
# enough to be a document is no longer a diagnostic, and length is the one
# property that turns an accidental inclusion into a bulk disclosure.
MAX_PERSISTED_TEXT_CHARS = 2000


def sanitize_persisted_text(value: Any, *, roots: Iterable[Any] = ()) -> Any:
    """Make one free-text value safe to store, or pass a non-string through.

    Non-strings are returned untouched: the columns this guards
    (``execution_history.error``, ``task_status_reason``,
    ``delegation_attempts.detail``) are nullable text, and ``None`` must stay
    ``None`` rather than becoming ``"None"``.
    """

    if not isinstance(value, str):
        return value
    return redact_paths(value, roots=roots)[:MAX_PERSISTED_TEXT_CHARS]


def sanitize_persisted_metadata_json(metadata: Any, *, roots: Iterable[Any] = ()) -> str:
    """Serialize execution metadata for storage with every path removed.

    The structure is normalized through JSON first (``default=str``, exactly
    as the caller did before) and only then walked, so a value that is not
    natively serializable is redacted in the form it will actually be stored
    in rather than in some other form this function guessed at.

    Redacting the parsed structure rather than the serialized text is
    deliberate: substituting inside a JSON document risks producing something
    that no longer parses, and unreadable history is its own kind of failure.
    """

    import json

    normalized = json.loads(json.dumps(metadata, default=str))
    return json.dumps(_redact_structure(normalized, tuple(roots)))


def _redact_structure(value: Any, roots: tuple[Any, ...] = ()) -> Any:
    if isinstance(value, str):
        return redact_paths(value, roots=roots)
    if isinstance(value, dict):
        # A key can carry a path as well as a value can.
        return {redact_paths(key, roots=roots): _redact_structure(item, roots)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_structure(item, roots) for item in value]
    return value


# Project notes have a separate ingestion contract. The history sanitizers
# above keep their own behavior.
_ENCODED_RUN = re.compile(r"[A-Za-z0-9+/]{64,}={0,2}")


def _looks_encoded(value: str) -> bool:
    """A long base64-alphabet run is refused unless it reads as a relative path.

    Path-like means: at least one slash, no plus sign, and no slash-separated
    segment that mixes upper case, lower case and digits (random base64 segments
    of useful length nearly always mix all three).
    Surrounding punctuation does not matter, so "<payload>." and "x_<payload>"
    are refused like a bare payload.
    A segment of 64 characters or more is refused when it has an upper-case
    letter or a digit (long lower-case names read as words). This is a
    heuristic for accidental bulk text, not a defence against a deliberate
    attacker who splits a payload with slashes or spaces.
    """
    for match in _ENCODED_RUN.finditer(value):
        run = match.group(0).rstrip("=")
        segments = run.split("/")
        path_like = (len(segments) > 1 and "+" not in run and all(
            not (len(seg) >= 64 and any(c.isupper() or c.isdigit() for c in seg))
            and not (any(c.isupper() for c in seg) and any(c.islower() for c in seg)
                                    and any(c.isdigit() for c in seg))
            for seg in segments))
        if not path_like:
            return True
    return False


class ContextTextRefused(ValueError):
    """Fixed labels only, with no submitted text or underlying exception."""


_CONTEXT_PAYLOAD = re.compile(
    r"```|~~~|(?m:^\s*(?:user|assistant|system|developer|human)\s*:)|"
    r"(?m:^\s*(?:diff --git|@@ |--- a/|\+\+\+ b/|def \w+\(|class \w+|"
    r"import \w+|from \w+ import|function\s+\w+|SELECT\s+.+FROM\b))|"
    r"-----BEGIN [^-]*(?:PRIVATE KEY|CERTIFICATE)|"
    r"(?i:<(?:user|assistant|system|tool)>|\"(?:role|messages|transcript|"
    r"source_code|file_contents|prompt|stdout|stderr)\"\s*:)|"
    r"(?m:^\s*(?:const|let|var)\s+\w+\s*=)|"
    r"data:[^\s,]*;base64,|"
    r"(?i:\b(?:base64|b64|hex|binary)\s*[:=]\s*\S+)|"
    r"(?:\\x[0-9a-fA-F]{2}){4,}|(?:%[0-9a-fA-F]{2}){8,}|"
    r"(?:\\u[0-9a-fA-F]{4}){4,}|"
    r"(?i:\b(?:other|another|foreign)[ -](?:project|repository|repo)\b)"
)
_CONTEXT_CREDENTIAL = re.compile(
    r"(?i:\b(?:authorization\s*:\s*(?:bearer|basic)\s+\S+|"
    r"(?:api[_-]?key|access[_-]?token|password|secret|credential)\s*[=:]\s*[^\s,;]+))|"
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b|"
    r"(?i:https?://)[^\s/@]+:[^\s/@]+@[^\s]+"
)
_CONTEXT_PATH = re.compile(
    r"(?i:file://)[^\s\"';]*|~[/\\][^\s\"';]*|"
    r"(?<![A-Za-z0-9])[A-Za-z]:[^\s\"';]+|\\\\[^\s\"';]+|"
    r"(?<![\w/])/(?!/)[^\s\"';<>]+"
)


def sanitize_context_text(value: Any, *, relative_path: bool = False) -> str:
    """Reject recognizable payloads; redact credentials and absolute paths.

    This is an idempotent heuristic, not proof of semantic absence. Schema,
    byte caps and repository containment are enforced separately by the store.
    Relative paths are never rewritten into a different file reference.
    """
    if type(value) is not str:
        raise ContextTextRefused("context_text_invalid")
    if any(unicodedata.category(char) in {"Cc", "Cs"} and char not in "\n\t" for char in value):
        raise ContextTextRefused("context_payload_refused")
    if _CONTEXT_PAYLOAD.search(value) or _looks_encoded(value):
        raise ContextTextRefused("context_payload_refused")
    out = _CONTEXT_CREDENTIAL.sub("<credential>", value)
    # A slash inside a relative path must not be mistaken for an absolute path.
    if relative_path:
        if out != value or value.startswith(("/", "~", "file:")):
            raise ContextTextRefused("context_path_refused")
        return out
    return _CONTEXT_PATH.sub(PATH_PLACEHOLDER, out)
