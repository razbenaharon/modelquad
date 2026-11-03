# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The checked-in repository file ``.modelquad.toml``: untrusted, narrow-only.

A repository can carry a small TOML file at its top folder that makes Codex
use *narrower* there: turn Codex off, remove task kinds, lower the file caps,
require a work (workspace) Codex account, or make Claude's offers rarer. It
can never widen anything, approve anything, name files to send, or change how
Codex is started. Anyone can commit this file to any repository, so it is
read as untrusted data:

* Location: the Git top folder of the working directory (Git with a deadline;
  the nearest folder holding ``.git`` when Git cannot answer), or the working
  directory itself outside Git. Never searched above that folder.
* Opened only as a plain regular file whose folders are not links
  (``project_notes.open_regular``), at most 8 KiB, parsed with ``tomllib``.
* Merge: the most restrictive of ModelQuad's built-in policy and the file.
* Unknown keys -- including approval-like or widening ones such as ``allow``,
  ``auto_approve``, ``permissions``, ``proactive``, ``trusted``, ``include``,
  ``files``, ``model``, ``effort`` or ``sandbox`` -- are ignored and listed for
  the person; nothing acts on them. Unknown keys alone never refuse.
* Fail closed (owner decision D3): a file that is a link or not a plain file, is too
  large, is not valid TOML, has a missing or unknown ``schema``, or has a value
  of the wrong type or out of range refuses every Codex call in the repository
  until it is fixed or removed.
* No free text reaches the model: a refusal is one closed reason code plus a
  fixed one-line message built from closed parts. Key names a reply mentions
  are ModelQuad's own known keys, never the file's.
* Read fresh at each gate; never cached across calls and never stored. Only
  the closed reason code may be recorded in history.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import re
import stat
import time
import tomllib

from .execution.noshell import MAX_FILES as _PACKER_MAX_FILES
from .execution.noshell import MAX_TOTAL_BYTES as _PACKER_MAX_TOTAL_BYTES

FILE_NAME = ".modelquad.toml"
MAX_FILE_BYTES = 8 * 1024
SCHEMA = 1
DISCOVERY_SECONDS = 5.0

#: The read-only task kinds Codex may run at all (``execution.base.READ_ONLY_TASK_TYPES``).
TASK_KINDS = ("review", "exploration", "architecture")
#: ModelQuad's own caps, from the packer: the file can only lower them.
BUILTIN_MAX_FILES = _PACKER_MAX_FILES
BUILTIN_MAX_TOTAL_KIB = _PACKER_MAX_TOTAL_BYTES // 1024
ACCOUNTS = ("any", "workspace")
OFFERS = ("default", "on_request")

# Closed outcome codes. Only these are ever recorded or shown to the model.
CODEX_OFF = "repo_policy_codex_off"
KIND_REMOVED = "repo_policy_kind_removed"
ACCOUNT_WORKSPACE_REQUIRED = "repo_policy_account_workspace_required"
INVALID = "repo_policy_invalid"
UNREADABLE = "repo_policy_unreadable"
REASONS = frozenset({CODEX_OFF, KIND_REMOVED, ACCOUNT_WORKSPACE_REQUIRED, INVALID, UNREADABLE})

# Closed problem labels: why a file was refused as unreadable or invalid.
_PROBLEM_TEXT = {
    "unreadable": "could not be read",
    "linked": "is a link, or sits in a linked folder",
    "not_regular": "is not a plain file",
    "oversized": "is larger than 8 KiB",
    "not_utf8": "is not UTF-8 text",
    "invalid_toml": "is not valid TOML",
    "schema_missing": "has no schema line (add schema = 1)",
    "schema_unknown": "has a schema other than 1",
    "wrong_type": "has a value of the wrong type",
    "out_of_range": "has a value ModelQuad does not accept",
}
PROBLEMS = frozenset(_PROBLEM_TEXT)

#: The keys schema 1 knows. Every other key is ignored and reported.
_KNOWN = {
    "": ("schema", "codex", "offers"),
    "codex": ("allowed", "task_kinds", "max_files", "max_total_kib", "account"),
    "offers": ("codex",),
}
_KNOWN_KEYS = frozenset({"schema", "codex", "offers",
                         *(f"{table}.{key}" for table in ("codex", "offers") for key in _KNOWN[table])})
_MAX_UNKNOWN_REPORTED = 32
_SHOWN_KEY = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class RepoPolicy:
    """The effective policy for one call. ``repr`` carries no path or file text.

    ``refusal`` is set when the file itself is broken (fail closed): every
    Codex call in the repository is refused with it. ``unknown_keys`` is for
    human-only surfaces (``modelquad doctor``); a model-facing reply carries
    at most their count.
    """

    source: str = "builtin"                 # "builtin" (no file) or "file"
    allowed: bool = True
    task_kinds: frozenset = frozenset(TASK_KINDS)
    max_files: int = BUILTIN_MAX_FILES
    max_total_kib: int = BUILTIN_MAX_TOTAL_KIB
    account: str = "any"
    offers_codex: str = "default"
    refusal: str | None = None
    problem: str | None = None
    problem_key: str | None = None
    unknown_keys: tuple = field(default=(), repr=False)

    @property
    def max_total_bytes(self) -> int:
        return self.max_total_kib * 1024

    @property
    def caps(self) -> dict:
        """The packer caps this policy allows: never above ModelQuad's own."""
        return {"max_files": min(self.max_files, BUILTIN_MAX_FILES),
                "max_total_bytes": min(self.max_total_bytes, _PACKER_MAX_TOTAL_BYTES)}

    def refusal_for(self, task_type, *, account_kind=None, check_account=True) -> str | None:
        """The closed reason this call is refused for, or None to go on.

        ``account_kind`` is the Codex account kind word (personal, workspace,
        unknown); anything but "workspace" fails ``account = "workspace"``.
        """
        if self.refusal is not None:
            return self.refusal
        if not self.allowed:
            return CODEX_OFF
        kind = getattr(task_type, "value", task_type)
        if kind not in self.task_kinds:
            return KIND_REMOVED
        if check_account and self.account == "workspace" and account_kind != "workspace":
            return ACCOUNT_WORKSPACE_REQUIRED
        return None

    def facts(self) -> dict:
        """A closed, content-free summary a model-facing reply may carry."""
        return {
            "source": self.source,
            "codex_allowed": self.allowed and self.refusal is None,
            "task_kinds": [kind for kind in TASK_KINDS if kind in self.task_kinds],
            "max_files": self.caps["max_files"],
            "max_total_kib": self.caps["max_total_bytes"] // 1024,
            "account": self.account,
            "offers_codex": self.offers_codex,
            "refusal": self.refusal,
            "unknown_key_count": len(self.unknown_keys),
        }


BUILTIN = RepoPolicy()


def narrow(base: RepoPolicy, other: RepoPolicy) -> RepoPolicy:
    """The most restrictive of two policies, field by field."""
    return RepoPolicy(
        source="file" if "file" in (base.source, other.source) else "builtin",
        allowed=base.allowed and other.allowed,
        task_kinds=frozenset(base.task_kinds & other.task_kinds),
        max_files=min(base.max_files, other.max_files),
        max_total_kib=min(base.max_total_kib, other.max_total_kib),
        account="workspace" if "workspace" in (base.account, other.account) else "any",
        offers_codex="on_request" if "on_request" in (base.offers_codex, other.offers_codex) else "default",
        refusal=base.refusal or other.refusal,
        problem=base.problem if base.refusal else other.problem,
        problem_key=base.problem_key if base.refusal else other.problem_key,
        unknown_keys=tuple(dict.fromkeys((*base.unknown_keys, *other.unknown_keys))),
    )


def _broken(code: str, problem: str, key: str | None = None, unknown=()) -> RepoPolicy:
    # A broken file refuses Codex; the other fields keep the narrowest values
    # too, so nothing reading them alone could mistake it for a grant.
    return RepoPolicy(source="file", allowed=False, task_kinds=frozenset(), max_files=0,
                      max_total_kib=0, account="workspace", offers_codex="on_request",
                      refusal=code, problem=problem,
                      problem_key=key if key in _KNOWN_KEYS else None, unknown_keys=tuple(unknown))


# ----------------------------------------------------------------- discovery


def _git_marker(directory: Path) -> Path | None:
    """The nearest folder, this one or above, that holds a ``.git`` entry."""
    for folder in (directory, *directory.parents):
        try:
            (folder / ".git").lstat()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        return folder
    return None


def _same_or_inside(inner: Path, outer: Path) -> bool:
    inner_text = os.path.normcase(os.path.realpath(inner))
    outer_text = os.path.normcase(os.path.realpath(outer))
    try:
        return os.path.commonpath((inner_text, outer_text)) == outer_text
    except ValueError:
        return False


def find_root(working_directory) -> Path:
    """The folder whose ``.modelquad.toml`` applies to ``working_directory``.

    Git's top folder when Git answers within the deadline; otherwise the
    nearest folder holding ``.git``; outside Git, the working directory. The
    file is never looked for anywhere else.
    """
    directory = Path(os.path.abspath(working_directory))
    marker = _git_marker(directory)
    if marker is None:
        return directory
    try:
        from .jobs import changes

        deadline = time.monotonic() + DISCOVERY_SECONDS
        output = changes._git(directory, ["rev-parse", "--show-toplevel"], deadline)
        top = Path(output.decode("utf-8", "strict").strip())
        if top.is_absolute() and _same_or_inside(directory, top):
            return top
    except Exception:  # noqa: BLE001 -- Git missing, slow or refusing: use the marker
        pass
    return marker


# ------------------------------------------------------------------- reading


def _read(root: Path) -> bytes | RepoPolicy | None:
    """The file's bytes, None when there is no file, or a refusing policy."""
    from .project_notes import ObservationError, open_regular
    from .jobs.changes import _linked

    path = root / FILE_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return _broken(UNREADABLE, "unreadable")
    if _linked(info):
        return _broken(UNREADABLE, "linked")
    try:
        fd = open_regular(path)
    except FileNotFoundError:
        return None
    except ObservationError:
        # A plain single-link file refused here sits in a linked folder (or
        # carries a reparse or cloud-placeholder attribute).
        plain = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        return _broken(UNREADABLE, "linked" if plain else "not_regular")
    except OSError:
        return _broken(UNREADABLE, "unreadable")
    try:
        with os.fdopen(fd, "rb", buffering=0) as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return _broken(UNREADABLE, "not_regular")
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        return _broken(UNREADABLE, "unreadable")
    if len(raw) > MAX_FILE_BYTES:
        return _broken(UNREADABLE, "oversized")
    return raw


def _shown_key(name: str) -> str:
    return _SHOWN_KEY.sub("?", name)[:64]


def _unknown(table: dict, prefix: str, sink: list) -> None:
    known = _KNOWN[prefix]
    for key in table:
        if key not in known and len(sink) < _MAX_UNKNOWN_REPORTED:
            sink.append(_shown_key(f"{prefix}.{key}" if prefix else str(key)))


def _int(value) -> bool:
    return type(value) is int


def parse(raw: bytes) -> RepoPolicy:
    """Schema 1, exactly. Any problem refuses; unknown keys are only listed."""
    try:
        text = raw.decode("utf-8-sig", "strict")
    except UnicodeError:
        return _broken(INVALID, "not_utf8")
    try:
        document = tomllib.loads(text)
    except Exception:  # noqa: BLE001 -- TOMLDecodeError, or a recursion limit on deep nesting
        return _broken(INVALID, "invalid_toml")
    unknown: list[str] = []
    _unknown(document, "", unknown)
    if "schema" not in document:
        return _broken(INVALID, "schema_missing", "schema", unknown)
    schema = document["schema"]
    if not _int(schema):
        return _broken(INVALID, "wrong_type", "schema", unknown)
    if schema != SCHEMA:
        return _broken(INVALID, "schema_unknown", "schema", unknown)
    values: dict = {}
    for table_name in ("codex", "offers"):
        table = document.get(table_name, {})
        if type(table) is not dict:
            return _broken(INVALID, "wrong_type", table_name, unknown)
        _unknown(table, table_name, unknown)
        values[table_name] = table
    codex, offers = values["codex"], values["offers"]
    policy = BUILTIN

    if "allowed" in codex:
        if type(codex["allowed"]) is not bool:
            return _broken(INVALID, "wrong_type", "codex.allowed", unknown)
        policy = replace(policy, allowed=codex["allowed"])
    if "task_kinds" in codex:
        kinds = codex["task_kinds"]
        if type(kinds) is not list or any(type(kind) is not str for kind in kinds):
            return _broken(INVALID, "wrong_type", "codex.task_kinds", unknown)
        if any(kind not in TASK_KINDS for kind in kinds):
            return _broken(INVALID, "out_of_range", "codex.task_kinds", unknown)
        policy = replace(policy, task_kinds=frozenset(kinds))
    for key, ceiling in (("max_files", BUILTIN_MAX_FILES), ("max_total_kib", BUILTIN_MAX_TOTAL_KIB)):
        if key in codex:
            value = codex[key]
            if not _int(value):
                return _broken(INVALID, "wrong_type", f"codex.{key}", unknown)
            if not 0 <= value <= ceiling:
                return _broken(INVALID, "out_of_range", f"codex.{key}", unknown)
            policy = replace(policy, **{key: value})
    if "account" in codex:
        if type(codex["account"]) is not str:
            return _broken(INVALID, "wrong_type", "codex.account", unknown)
        if codex["account"] not in ACCOUNTS:
            return _broken(INVALID, "out_of_range", "codex.account", unknown)
        policy = replace(policy, account=codex["account"])
    if "codex" in offers:
        if type(offers["codex"]) is not str:
            return _broken(INVALID, "wrong_type", "offers.codex", unknown)
        if offers["codex"] not in OFFERS:
            return _broken(INVALID, "out_of_range", "offers.codex", unknown)
        policy = replace(policy, offers_codex=offers["codex"])
    return replace(narrow(BUILTIN, policy), source="file", unknown_keys=tuple(unknown))


def load(working_directory) -> RepoPolicy:
    """The effective policy for a call in ``working_directory``, read now.

    No file: the built-in policy. Never raises; any failure to find the root
    or read the file as a plain file refuses (fail closed).
    """
    try:
        root = find_root(working_directory)
        raw = _read(root)
    except Exception:  # noqa: BLE001 -- nothing about the path or the error escapes
        return _broken(UNREADABLE, "unreadable")
    if raw is None:
        return BUILTIN
    if isinstance(raw, RepoPolicy):
        return raw
    return parse(raw)


# ------------------------------------------------------------------ messages


def message(code: str, policy: RepoPolicy | None = None, *, task_type=None, account_kind=None) -> str:
    """One fixed line for a refusal: names the file and how to fix it."""
    if code == CODEX_OFF:
        return (f"This repository's {FILE_NAME} turns Codex off ([codex] allowed = false), so "
                "ModelQuad started nothing. To allow Codex here, change that line or remove the file.")
    if code == KIND_REMOVED:
        kind = getattr(task_type, "value", task_type)
        named = f"{kind} " if kind in TASK_KINDS else ""
        return (f"This repository's {FILE_NAME} does not allow Codex {named}tasks ([codex] task_kinds), "
                "so ModelQuad started nothing. Ask for a kind it lists, or edit task_kinds.")
    if code == ACCOUNT_WORKSPACE_REQUIRED:
        which = {"personal": "a personal account"}.get(account_kind, "an account ModelQuad cannot "
                                                                     "confirm is a work account")
        return (f"This repository's {FILE_NAME} allows Codex only on a work account "
                f"([codex] account = \"workspace\"), and Codex here is on {which}, so ModelQuad "
                "started nothing. Use a work Codex account here, or change that line.")
    problem = policy.problem if policy is not None and policy.problem in PROBLEMS else None
    key = policy.problem_key if policy is not None and policy.problem_key in _KNOWN_KEYS else None
    what = _PROBLEM_TEXT.get(problem, "could not be used")
    if key and problem in {"wrong_type", "out_of_range"}:
        what += f" ({key})"
    if code == UNREADABLE:
        return (f"This repository's {FILE_NAME} {what}, so ModelQuad refuses Codex here. Replace it "
                "with a plain text file of at most 8 KiB in the repository's top folder, or remove it.")
    return (f"This repository's {FILE_NAME} {what}, so ModelQuad refuses Codex here until it is "
            "fixed. Use the schema 1 format in ModelQuad's docs/REPO-POLICY.md, or remove the file.")


def refusal(policy: RepoPolicy, task_type, *, account_kind=None, check_account=True) -> dict | None:
    """The closed refusal fields for a gate, or None to go on."""
    code = policy.refusal_for(task_type, account_kind=account_kind, check_account=check_account)
    if code is None:
        return None
    return {"reason": code,
            "message": message(code, policy, task_type=task_type, account_kind=account_kind)}
