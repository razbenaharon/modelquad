# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""What installing a host integration actually did to its MCP registration.

The defect this module closes
-----------------------------

Onboarding used to report every non-failing host install with one sentence:
"Connected Claude Code." Three materially different state transitions shared
that sentence -- a brand-new registration, an already-identical one, and the
*replacement of a different pre-existing ModelQuad registration*. Observed on
a real Windows install on 2026-08-29: the machine already carried

    "modelquad": {"command": "modelquad-mcp"}

installing rewrote it to the frozen absolute shim, and the only thing the user
was told was "Connected Claude Code."

Worse than the wording: nothing remembered the displaced value. Each JSON
host adapter upserts ``mcpServers.modelquad`` on install and *deletes* it on
uninstall, so the full lifecycle was

    before install   user's own registration
    install          overwritten, previous value discarded
    uninstall        key deleted outright

which means uninstalling ModelQuad destroyed a ModelQuad configuration the
user had before ModelQuad was ever installed. The invariant this module
establishes is the negation of that:

    **Installing must never silently destroy a previously valid ModelQuad
    host configuration.**

Ordering is the whole of it
---------------------------

The first version of this module recorded the displaced value *after* the
adapter had already written over it. That is not a transaction: if the record
failed to persist, the original was gone, nothing could restore it, and the UI
still said "Connected". So the recovery evidence is now written **before** the
destructive replacement is attempted, and the record is discarded again if the
install turns out not to have displaced anything. The states this permits are:

===========================  ==================================================
evidence, no replacement     harmless; discarded on the same call
evidence, replacement        the intended steady state
replacement, no evidence     **never reachable** -- the write is not attempted
===========================  ==================================================

Identity is a lineage, not a value
----------------------------------

``previous_entry`` is immutable: it is the user's original, and only the first
thing this product displaced is owed back. But "is the current registration
still ours?" cannot be answered against a single value, because an upgrade
legitimately rewrites ModelQuad's own registration. So every value ModelQuad
itself writes is appended to ``installed_lineage``, and uninstall restores when
the current registration matches *any* of them. A value that is in neither the
lineage nor the original is the user's later decision, and is left alone.

Why the outcome is computed here and not inside each adapter
------------------------------------------------------------

``hostruntime`` already owns the single interpretation of *where* each host
records ModelQuad and *what* that record says -- ``doctor`` classifies
registrations through it. Reading the registration before and after the
adapter runs reuses that one interpretation instead of teaching the adapter to
report a previous value in its own dialect.

Sensitivity of what is stored
-----------------------------

An MCP entry may carry ``args`` and ``env``, and ``env`` is where a host
keeps API keys. **This file may therefore contain secrets**, and an earlier
version of this docstring wrongly claimed otherwise. They are kept because
dropping them would make the restore unfaithful -- putting back a registration
without its credentials is its own kind of destruction. The obligations that
follow are enforced, not merely intended:

* Scope -- only ModelQuad's own entry is ever read or stored. Sibling MCP
  servers in the same file are never copied, reported, or rewritten.
* At rest -- every write creates a private file: a verified protected Windows
  DACL for the current user and SYSTEM, or POSIX 0600. Failure refuses the
  write (see :func:`_write_backups`).
* Never surfaced -- the entry is not carried on
  :class:`HostRegistrationOutcome` or :class:`RestoreResult`, so it cannot
  reach CLI text, ``--json``, ``doctor``, logs or a support
  bundle. Only the launch *command* is ever reported.
* Lifetime -- the record is deleted once the obligation is discharged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
from typing import Any

from ..branding import MCP_SERVER_NAME


#: The file, inside this installation's own state root, that remembers what a
#: host's ModelQuad registration was before this installation replaced it.
BACKUP_FILE_NAME = "host-registration-backup.json"

#: Record layout version. Bumped when the shape below changes so a record
#: written by an older build is recognised rather than misread.
BACKUP_SCHEMA = 3

_MCP_KEY_PATH = ("mcpServers", MCP_SERVER_NAME)


class RegistrationStatus(str, Enum):
    """What an install did to one host's ModelQuad MCP registration.

    ``CONNECTED_REPLACED`` is the value the old boolean-shaped result could
    not express, and the only reason the other four have to be spelled out:
    once one outcome must be distinguished truthfully, "it worked" stops being
    an answer for any of them.

    ``CONFIG_UNREADABLE`` and ``REGISTRATION_SKIPPED`` exist for the same
    reason one step further out. An adapter can finish *without raising* and
    still not have registered anything -- it refuses to touch a config file
    it cannot parse, and says so in its own report. Before
    these two, that outcome was classified ``CONNECTED_NEW``, because the
    comparison only asked whether a registration existed *before*. Nothing
    existed before and nothing existed after, so "a new connection was made"
    was the answer, and onboarding told the user their host could reach
    ModelQuad while the same screen's detail said it was not registered.

    The invariant they restore: **a connected status requires a registration
    to actually exist on disk once the adapter has run.**
    """

    #: No ModelQuad registration existed; this install created one.
    CONNECTED_NEW = "connected_new"
    #: A ModelQuad registration already existed and was identical.
    CONNECTED_UNCHANGED = "connected_unchanged"
    #: A DIFFERENT ModelQuad registration existed and was replaced.
    CONNECTED_REPLACED = "connected_replaced"
    #: The adapter finished, but the host's config could not be read or
    #: parsed, so no registration could be written and none is in place.
    CONFIG_UNREADABLE = "config_unreadable"
    #: The adapter finished and deliberately left the registration alone
    #: (an unsupported shape it will not rewrite), so none is in place.
    REGISTRATION_SKIPPED = "registration_skipped"
    #: The host is present but the adapter could not finish.
    FAILED = "failed"
    #: The host is not installed on this machine; nothing was attempted.
    NOT_DETECTED = "not_detected"


@dataclass(frozen=True)
class HostRegistrationOutcome:
    """The structured truth about one host's registration change.

    Carried instead of a bare ``{"detected": ..., "error": ...}`` dict so the
    UI never has to infer a state transition from a string.

    Deliberately absent: the previous *entry*. It may contain ``env``, and
    this object is rendered into CLI output and ``--json``. Only the launch
    command travels, and even that is diagnostic -- an absolute path, so it
    belongs in ``detail``, never in a ``user_detail`` sentence.
    """

    host: str
    status: RegistrationStatus
    previous_registration_present: bool = False
    previous_registration_different: bool = False
    previous_command: str | None = None
    new_command: str | None = None
    error: str | None = None
    dry_run: bool = False
    #: Whether an uninstall could actually put the previous registration back
    #: for THIS host. Onboarding wording depends on it: promising a restore that cannot
    #: happen is the same class of untruth as saying nothing at all.
    restore_supported: bool = False
    #: The adapter's own file-level report, when one was produced.
    report: Any | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        """True when the host ended up connected, however it got there."""

        return self.status in (
            RegistrationStatus.CONNECTED_NEW,
            RegistrationStatus.CONNECTED_UNCHANGED,
            RegistrationStatus.CONNECTED_REPLACED,
        )

    @property
    def detected(self) -> bool:
        """True when the host was found on this machine."""

        return self.status is not RegistrationStatus.NOT_DETECTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "client": self.host,
            "status": self.status.value,
            "detected": self.detected,
            "ok": self.ok,
            "previous_registration_present": self.previous_registration_present,
            "previous_registration_different": self.previous_registration_different,
            "previous_command": self.previous_command,
            "new_command": self.new_command,
            "restore_supported": self.restore_supported,
            "error": self.error,
            "dry_run": self.dry_run,
            "report": self.report.to_dict() if self.report is not None else None,
        }


def classify_registration_change(
    *,
    before_entry: dict | None,
    before_command: str | None,
    after_entry: dict | None,
    after_command: str | None,
) -> RegistrationStatus | None:
    """Decide which of the three connected outcomes this change was.

    Compares the whole entry when both sides have one -- an entry whose
    ``command`` is unchanged but whose ``args`` were rewritten IS a
    replacement, and comparing only the command would call it unchanged. A
    host with no readable entry falls back to the command string.

    Returns ``None`` when there is **no registration on disk afterwards**. That
    is not a connected outcome of any kind, and this function deliberately
    cannot name it: only the caller, holding the adapter's own report, knows
    whether the host's config was unparseable or the adapter declined to
    rewrite a shape it does not own. Returning ``None`` rather than a status
    forces that question to be answered instead of defaulted.

    The guard lives here rather than at the single call site because the
    defect it closes was exactly a caller not asking. With ``before`` empty and
    ``after`` empty this used to answer ``CONNECTED_NEW`` -- "nothing was there
    and nothing is there, so a new connection was made" -- and onboarding
    reported a host it had failed to register as connected.
    """

    registered_after = after_entry is not None or after_command is not None
    if not registered_after:
        return None

    had_before = before_entry is not None or before_command is not None
    if not had_before:
        return RegistrationStatus.CONNECTED_NEW
    if before_entry is not None and after_entry is not None:
        same = before_entry == after_entry
    else:
        same = before_command == after_command
    if same:
        return RegistrationStatus.CONNECTED_UNCHANGED
    return RegistrationStatus.CONNECTED_REPLACED


def restore_supported(host: str, target_dir: Path) -> bool:
    """Whether an uninstall could structurally put this host's entry back.

    True for a JSON host, whose entry is a value this module can rewrite
    verbatim. Onboarding asks this before it promises anything.
    """

    from ..hostruntime import json_mcp_config_path

    return json_mcp_config_path(host, Path(target_dir)) is not None


# ---------------------------------------------------------------------------
# The transactional record
# ---------------------------------------------------------------------------


class BackupPersistenceError(RuntimeError):
    """The displaced registration could not be durably recorded.

    Raised rather than swallowed: the caller's correct response is to abandon
    the replacement, not to proceed and hope. See the ordering note in the
    module docstring.
    """


def backup_path(state_root: Path | None = None) -> Path:
    """Where the displaced registrations for THIS installation are recorded.

    Kept inside the installation's own state root rather than beside the
    host's config: it is this installation's obligation, it must not survive
    into an unrelated one, and writing bookkeeping into a file the host owns
    is how the original problem started.
    """

    if state_root is None:
        from ..paths import state_root as _state_root

        state_root = _state_root()
    return Path(state_root) / BACKUP_FILE_NAME


def _read_backups(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    hosts = payload.get("hosts") if isinstance(payload, dict) else None
    return hosts if isinstance(hosts, dict) else {}


def read_owned_settings(path: Path | None = None) -> dict[str, Any]:
    """Settings ModelQuad added, alongside registration recovery evidence."""
    try:
        payload = json.loads((path or backup_path()).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    settings = payload.get("owned_settings") if isinstance(payload, dict) else None
    return settings if isinstance(settings, dict) else {}


def write_owned_settings(settings: dict[str, Any]) -> None:
    path = backup_path()
    _write_backups(path, _read_backups(path), owned_settings=settings)


def read_pending_hook_ownership() -> dict[str, Any]:
    """A hook install never overwrites unreadable registration recovery data."""
    path = backup_path()
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(payload, dict) or payload.get('schema') != BACKUP_SCHEMA
                    or not isinstance(payload.get('hosts'), dict)
                    or not isinstance(payload.get('owned_settings', {}), dict)):
                raise BackupPersistenceError('Hook ownership is unavailable.')
        return read_owned_settings()
    except (OSError, UnicodeError, ValueError):
        raise BackupPersistenceError('Hook ownership is unavailable.') from None


def _write_backups(
    path: Path, hosts: dict[str, Any], *, owned_settings: dict[str, Any] | None = None
) -> None:
    """Persist the record user-private, and fail loudly if it cannot be.

    The file can hold an ``env`` block. Windows creates an exclusive private
    temp file and verifies its protected user-and-SYSTEM DACL before writing
    content. Private protection overrides the old file's ACL on every write.
    POSIX retains 0600 creation. A security failure refuses persistence.
    """

    from .atomicwrite import atomic_write

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            if not path.exists():
                os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        if owned_settings is None:
            owned_settings = read_owned_settings(path)
        payload = {"schema": BACKUP_SCHEMA, "hosts": hosts}
        if owned_settings:
            payload["owned_settings"] = owned_settings
        atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", private=True)
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except OSError as exc:
        raise BackupPersistenceError(
            "Could not record the existing registration in a private backup."
        ) from exc


def record_replaced_registration(
    host: str,
    *,
    previous_entry: dict | None,
    previous_command: str | None,
    state_root: Path | None = None,
) -> bool:
    """Remember the registration about to be displaced. Returns True if newly written.

    Called **before** the adapter writes, so the evidence exists before the
    thing it is evidence of is destroyed.

    **Once** is the whole subtlety. Re-running install would otherwise
    re-record the value ModelQuad itself wrote a minute earlier as though it
    were the user's original, and the real original -- the only thing worth
    restoring -- would be lost on the second install rather than the first. So
    an existing record for a host is never overwritten; the first thing this
    installation displaced is the thing it owes back.

    Raises :class:`BackupPersistenceError` if the record cannot be persisted.
    """

    path = backup_path(state_root)
    hosts = _read_backups(path)
    if host in hosts:
        return False
    hosts[host] = {
        "previous_entry": previous_entry,
        "previous_command": previous_command,
        # Explicit, rather than inferred from previous_entry being non-None:
        # "entry is None" and "nothing was displaced" are different facts and
        # must not share one representation.
        "has_previous": True,
        "installed_lineage": [],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_backups(path, hosts)
    return True


def note_installed_registration(
    host: str,
    *,
    installed_entry: dict | None,
    installed_command: str | None,
    state_root: Path | None = None,
) -> None:
    """Add a value ModelQuad itself wrote to this host's lineage.

    The answer to "is the registration still ours?" has to survive an upgrade
    that legitimately rewrites it. Pinning one ``installed_command`` made the
    *next* version's own registration look like a user edit, so uninstall
    would decline to restore and the original would be stranded. A lineage
    cannot make that mistake: every value this product wrote is ours, and
    anything else is the user's.
    """

    fingerprint = _fingerprint(installed_entry, installed_command)
    if fingerprint is None:
        return
    path = backup_path(state_root)
    hosts = _read_backups(path)
    record = hosts.get(host)
    if not isinstance(record, dict):
        # A fresh install displaced nothing, so there is no record yet -- but
        # ModelQuad still has to know WHICH registration it created, or a
        # later uninstall cannot tell its own value from one the user
        # repointed afterwards, and deletes both alike. That was the whole
        # fresh-install defect.
        #
        # ``previous_entry`` stays absent. Inventing one to make the logic
        # uniform would be a fabricated claim that something was displaced,
        # and a later restore would write a value that never existed.
        record = {
            "previous_entry": None,
            "previous_command": None,
            "has_previous": False,
            "installed_lineage": [],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    elif fingerprint in (
        record.get("installed_lineage")
        if isinstance(record.get("installed_lineage"), list)
        else []
    ):
        return
    lineage = record.get("installed_lineage")
    if not isinstance(lineage, list):
        lineage = []
    if fingerprint not in lineage:
        lineage.append(fingerprint)
    record["installed_lineage"] = lineage
    hosts[host] = record
    _write_backups(path, hosts)


def _fingerprint(entry: dict | None, command: str | None) -> Any | None:
    """How one registration value is compared against another.

    The whole entry when there is one -- an upgrade that changes only ``args``
    is still ModelQuad's own write, and comparing commands alone would miss
    it. The bare command for hosts that have no readable entry.
    """

    if entry is not None:
        return entry
    if command is not None:
        return {"command": command}
    return None


def recorded_replacement(host: str, *, state_root: Path | None = None) -> dict | None:
    """The record for one host, or None when this installation displaced nothing."""

    record = _read_backups(backup_path(state_root)).get(host)
    return record if isinstance(record, dict) else None


def registration_owned(record: dict, entry: dict | None, command: str | None) -> bool:
    """An exact recorded fingerprint, including args and env, proves ownership."""
    lineage = record.get("installed_lineage")
    return isinstance(lineage, list) and _fingerprint(entry, command) in lineage


def forget_replacement(host: str, *, state_root: Path | None = None) -> None:
    """Drop one host's record once the obligation has been discharged.

    Also the rollback path: a record written in anticipation of a replacement
    that did not happen is removed on the same call that wrote it.
    """

    path = backup_path(state_root)
    hosts = _read_backups(path)
    if hosts.pop(host, None) is None:
        return
    _write_backups(path, hosts)


class RestoreStatus(str, Enum):
    """What an uninstall did about a previously displaced registration."""

    #: Nothing was displaced by this installation; ordinary removal applies.
    NOTHING_RECORDED = "nothing_recorded"
    #: The user's original registration was put back.
    RESTORED = "restored"
    #: The registration changed after install, so it was left alone.
    USER_MODIFIED = "user_modified"
    #: ModelQuad's own registration was removed and nothing was owed back.
    #: The ordinary fresh-install uninstall, reported so ``--json`` can say
    #: which of "removed ours" and "left the user's alone" happened.
    REMOVED = "removed"
    #: There was no ModelQuad registration left to remove (for example the
    #: user ran ``claude mcp remove modelquad`` first). Not a failure.
    ALREADY_REMOVED = "already_removed"
    #: A record exists and should have been restored, but the write failed.
    FAILED = "failed"


@dataclass(frozen=True)
class RestoreResult:
    """The outcome of the compare-before-restore step.

    Carries commands, never entries: this object is rendered to the terminal
    and serialised into ``--json``, and the stored entry may hold ``env``.
    """

    host: str
    status: RestoreStatus
    restored_command: str | None = None
    current_command: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not RestoreStatus.FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "status": self.status.value,
            "restored_command": self.restored_command,
            "current_command": self.current_command,
            "detail": self.detail,
        }


#: Sentinel meaning "read the current registration yourself". Distinct from
#: ``None``, which is the meaningful answer "there is no registration".
_UNREAD = object()


def restore_previous_registration(
    host: str,
    target_dir: Path,
    *,
    dry_run: bool = False,
    state_root: Path | None = None,
    current_command: Any = _UNREAD,
    current_entry: Any = _UNREAD,
) -> RestoreResult:
    """Put back what this installation displaced -- but only if it is still ours.

    Compare-before-restore, deliberately. A user who repointed ModelQuad after
    installing has made a newer decision than the one recorded here, and
    blindly restoring a stale value over it would be the same class of silent
    destruction this module exists to prevent -- just aimed the other way. So
    the current registration must still match something this product wrote;
    anything else is the user's, and is left untouched.

    The ordering this has to survive: the adapter's own uninstall *deletes*
    ModelQuad's key, so the write has to happen after it or it would simply be
    removed again -- while the comparison has to happen before it, or it would
    compare against the hole the removal just left. ``current_command`` and
    ``current_entry`` are how the caller passes the readings it took
    beforehand; omitted, they are read here, which is correct only when
    nothing has removed them yet.
    """

    from ..hostruntime import (
        json_mcp_config_path,
        mcp_config_parses,
        read_registered_command,
        read_registered_entry,
    )
    from .jsonmerge import upsert_json_key
    from .report import ActionKind

    record = recorded_replacement(host, state_root=state_root)
    if record is None:
        return RestoreResult(host, RestoreStatus.NOTHING_RECORDED)

    target = Path(target_dir)
    current_cmd = (
        read_registered_command(host, target) if current_command is _UNREAD else current_command
    )
    current_ent = (
        read_registered_entry(host, target) if current_entry is _UNREAD else current_entry
    )

    config_path = json_mcp_config_path(host, target)
    previous_entry = record.get("previous_entry")
    previous_command = record.get("previous_command")
    # Records written before this field existed are read by inference rather
    # than assumed to have displaced nothing.
    has_previous = record.get("has_previous")
    if not isinstance(has_previous, bool):
        has_previous = previous_entry is not None or previous_command is not None

    if current_ent is None and current_cmd is None:
        # Nothing was registered when this uninstall began. An unreadable
        # config reads the same way, so it is told apart structurally first.
        if mcp_config_parses(host, target) is False:
            return RestoreResult(
                host,
                RestoreStatus.FAILED,
                detail=(
                    f"Could not read {config_path}, so the ModelQuad registration "
                    "could not be checked. Its recorded details are kept."
                ),
            )
        if not has_previous:
            if not dry_run:
                forget_replacement(host, state_root=state_root)
            return RestoreResult(
                host,
                RestoreStatus.ALREADY_REMOVED,
                detail="The ModelQuad registration had already been removed; nothing else was needed.",
            )
        # The user removed the registration themselves. Their latest choice
        # stands, so the earlier entry is not written back -- but it is the
        # only copy, so it is kept for a manual restore.
        return RestoreResult(
            host,
            RestoreStatus.ALREADY_REMOVED,
            detail=(
                "The ModelQuad registration had already been removed, so the one in "
                "place before this installation was not put back. Its details are "
                f"kept in {backup_path(state_root)}."
            ),
        )

    owned = registration_owned(record, current_ent, current_cmd)

    if not owned:
        # The registration on disk is not one this product wrote, so it is
        # the user's later decision -- whether or not anything was displaced
        # when ModelQuad was installed. The adapter's unconditional removal
        # already deleted it, so preserving means writing it straight BACK.
        if config_path is None or not isinstance(current_ent, dict):
            # Defensive: reported as a failure so the exit code is non-zero,
            # because a user-modified registration may have been lost.
            return RestoreResult(
                host,
                RestoreStatus.FAILED,
                current_command=current_cmd,
                detail=(
                    "The ModelQuad registration was changed after this "
                    "installation wrote it, and could not be preserved "
                    "automatically for this host."
                ),
            )
        if not dry_run:
            # The adapter's uninstall saved this file before its own write.
            upsert_json_key(config_path, _MCP_KEY_PATH, current_ent, dry_run=False, backup=False)
            # The displaced original will now never be restored automatically
            # -- the user's newer choice stands -- so the record has no
            # remaining recovery purpose, and keeping it would keep an ``env``
            # block (possibly a credential) for a restore that cannot happen.
            forget_replacement(host, state_root=state_root)
        return RestoreResult(
            host,
            RestoreStatus.USER_MODIFIED,
            current_command=current_cmd,
            detail=(
                "The ModelQuad registration was changed after this installation "
                "wrote it, so your newer registration was preserved and the "
                "earlier one was not put back."
            ),
        )

    if not has_previous:
        # ModelQuad created this registration and nothing was ever displaced,
        # so the adapter's removal is the whole correct outcome -- once it has
        # actually happened. A removal that did not land keeps the record.
        if not dry_run and read_registered_entry(host, target) is not None:
            return RestoreResult(
                host,
                RestoreStatus.FAILED,
                current_command=current_cmd,
                detail=(
                    "The ModelQuad registration could not be removed; run the "
                    "uninstall again once the file can be written."
                ),
            )
        if not dry_run:
            forget_replacement(host, state_root=state_root)
        return RestoreResult(
            host,
            RestoreStatus.REMOVED,
            current_command=current_cmd,
            detail=(
                "The ModelQuad registration this installation created was "
                "removed. Nothing was replaced when it was installed, so "
                "nothing needed to be put back."
            ),
        )

    # Something was displaced, and the registration is still ours.
    restored = previous_entry.get("command") if isinstance(previous_entry, dict) else None
    restored = restored if isinstance(restored, str) else None
    if config_path is None or not isinstance(previous_entry, dict):
        return RestoreResult(
            host,
            RestoreStatus.FAILED,
            current_command=current_cmd,
            detail=(
                "The registration in place before this installation cannot be put "
                f"back automatically; its details are kept in {backup_path(state_root)}."
            ),
        )
    if dry_run:
        return RestoreResult(
            host,
            RestoreStatus.RESTORED,
            restored_command=restored,
            current_command=current_cmd,
            detail="The registration in place before this installation would be put back.",
        )

    action = upsert_json_key(config_path, _MCP_KEY_PATH, previous_entry, dry_run=False, backup=False)
    if action.kind is ActionKind.SKIPPED:
        # Never silently swallowed: the record is deliberately KEPT so the
        # value stays recoverable on a later attempt.
        return RestoreResult(
            host,
            RestoreStatus.FAILED,
            restored_command=restored,
            current_command=current_cmd,
            detail=(
                "The registration in place before this installation could not be "
                "put back; it is still recorded and can be restored manually."
            ),
        )
    forget_replacement(host, state_root=state_root)
    return RestoreResult(
        host,
        RestoreStatus.RESTORED,
        restored_command=restored,
        current_command=current_cmd,
        detail="The registration that was in place before this installation was put back.",
    )


def install_with_registration(
    host: str, target_dir: Path, adapter: Any, *, dry_run: bool = False
) -> Any:
    """Back up before writing, and track ownership for every install entry point."""
    from ..hostruntime import (
        read_registered_command, read_registered_entry, registration_is_readable,
    )

    if not registration_is_readable(host):
        return adapter.install(target_dir, dry_run=dry_run)
    before_entry = read_registered_entry(host, target_dir)
    before_command = read_registered_command(host, target_dir)
    provisional = False
    if not dry_run:
        if before_entry is not None or before_command is not None:
            provisional = record_replaced_registration(
                host, previous_entry=before_entry, previous_command=before_command
            )
        else:
            # Even if the later lineage write fails, uninstall must see a
            # record and preserve an unrecognised value rather than blind-delete.
            path = backup_path()
            hosts = _read_backups(path)
            if host not in hosts:
                hosts[host] = {
                    "previous_entry": None, "previous_command": None,
                    "has_previous": False, "installed_lineage": [],
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
                try:
                    _write_backups(path, hosts)
                except BackupPersistenceError:
                    pass  # Fresh installs retain base semantics: connect without evidence.
    try:
        report = adapter.install(target_dir, dry_run=dry_run)
    except (OSError, ValueError, BackupPersistenceError):
        if provisional:
            _retire_or_keep_after_failed_install(
                host, target_dir, before_entry=before_entry, before_command=before_command
            )
        elif not dry_run:
            # A fresh install can also fail after the registration write.
            try:
                after_entry = read_registered_entry(host, target_dir)
                after_command = read_registered_command(host, target_dir)
                if after_entry is None and after_command is None:
                    _forget_fresh_placeholder(host)
                else:
                    note_installed_registration(
                        host, installed_entry=after_entry, installed_command=after_command,
                    )
            except (OSError, ValueError, BackupPersistenceError):
                pass
        raise
    if not dry_run:
        after_entry = read_registered_entry(host, target_dir)
        after_command = read_registered_command(host, target_dir)
        if before_entry is None and before_command is None:
            if after_entry is None and after_command is None:
                _forget_fresh_placeholder(host)
        status = classify_registration_change(
            before_entry=before_entry, before_command=before_command,
            after_entry=after_entry, after_command=after_command,
        )
        if status is not None:
            if provisional and status is not RegistrationStatus.CONNECTED_REPLACED:
                try:
                    path = backup_path()
                    hosts = _read_backups(path)
                    record = hosts[host]
                    record.update(previous_entry=None, previous_command=None, has_previous=False)
                    _write_backups(path, hosts)
                except BackupPersistenceError:
                    pass
            try:
                note_installed_registration(
                    host, installed_entry=after_entry, installed_command=after_command
                )
            except BackupPersistenceError:
                from dataclasses import replace
                from .report import ActionKind, FileAction

                report = replace(report, actions=report.actions + (FileAction(
                    backup_path(), ActionKind.UNCHANGED,
                    detail=("Connected, but registration ownership could not be recorded. "
                            "Later user edits cannot be reliably protected on uninstall."),
                ),))
    return report


def _forget_fresh_placeholder(host: str) -> None:
    record = recorded_replacement(host)
    if record is not None and record.get("has_previous") is False and not record.get("installed_lineage"):
        try:
            forget_replacement(host)
        except BackupPersistenceError:
            pass  # A failed retirement leaves evidence available for retry.


def _retire_or_keep_after_failed_install(
    client: str,
    target_dir: Path,
    *,
    before_entry: dict | None,
    before_command: str | None,
) -> None:
    """Decide what a partially-applied install owes, after it has failed.

    The rule is deliberately asymmetric. Keeping a recovery record that turns
    out not to be needed costs one small file in the state folder. Deleting one that *was* needed destroys a registration the user
    had before ModelQuad existed, permanently and silently. So the record is
    retired only on proof, and every uncertain reading keeps it:

    * registration still byte-identical to the pre-install value -> the write
      never landed, the obligation is fictional, retire it;
    * registration differs -> ModelQuad's value is on disk, this IS the
      partial-install case, keep the record *and* claim the value into the
      lineage so a later uninstall can recognise it as ours and restore the
      original;
    * the registration cannot be re-read at all -> ambiguous, keep.
    """

    from ..hostruntime import read_registered_command, read_registered_entry

    try:
        now_entry = read_registered_entry(client, target_dir)
        now_command = read_registered_command(client, target_dir)
    except Exception:  # noqa: BLE001 -- unreadable is ambiguous, so keep the record
        return

    if now_entry == before_entry and now_command == before_command:
        try:
            forget_replacement(client)
        except Exception:  # noqa: BLE001 -- the install failure is the news
            pass
        return

    # The registration was replaced before the adapter failed. Without this,
    # the value on disk is in no lineage, so a later uninstall would read it
    # as a user edit and "preserve" ModelQuad's own half-written registration
    # instead of restoring the user's original.
    try:
        note_installed_registration(
            client, installed_entry=now_entry, installed_command=now_command
        )
    except BackupPersistenceError:
        # The original is already recorded, which is the part that matters.
        pass
