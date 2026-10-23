# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Survey every ModelQuad state store on this machine, and say whether they agree.

``statecontext`` answers "which state does this process own?" and
``hostruntime`` answers "which state will a host integration own?". Neither can, on
its own, detect the failure this workstream exists to end -- because each is
answered from *inside* one runtime, and the whole defect was that two runtimes
each had a locally correct answer.

This module asks the machine-global question instead: **how many ModelQuad
state stores exist here, and do they hold the same product state?** That check
is the one that would have caught the live 2026-08-26 defect from any surface,
including from the runtime that was wrong.

Read-only, by rule. Nothing here writes, migrates, merges, or deletes a store.
Two stores that disagree are evidence, not something to silently reconcile:
picking a winner would destroy the evidence of which surface the user was
actually talking to, and the settings a user set through the HTTP API
are not obviously less real than the ones a host's MCP wrote. The remedy is to
make the runtimes agree (``repair``), not to edit history until they appear to.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .procutil import pid_state, process_creation_epoch, process_image_name
from .statecontext import (
    DATABASE_FILE_NAME,
    StateContext,
    StateMode,
    canonical_state_root,
    resolve_state_context,
)


#: Settings every production surface must agree on. A divergence in any of
#: these is the user-visible symptom: a value set in one surface that another
#: surface does not observe. Deliberately not "every key in the table" --
#: adding a key here is a statement that it is genuinely shared product state.
SHARED_SETTING_KEYS = (
    "routing_mode",
    "routing_paused",
    "decision_mode",
    "routing_settings",
    "session_resource_strategy",
    "plan_advice_enabled",
)

#: Tables whose newest row dates a store's last use. Used only to say which
#: store has been active, never to decide which one wins.
_ACTIVITY_TABLES = ("routing_decisions", "execution_history", "delegation_attempts")


@dataclass(frozen=True)
class StateStore:
    """One ModelQuad state root found on this machine."""

    root: Path
    mode: StateMode
    exists: bool
    database_exists: bool
    database_bytes: int | None = None
    schema_version: int | None = None
    #: Raw settings rows, ``key -> stored JSON text``. Only the keys in
    #: :data:`SHARED_SETTING_KEYS` are read. A key ABSENT here is not an
    #: error: settings are written lazily, so an unset key means "this store
    #: has never been told otherwise" and the surface reading it gets the
    #: product default. That is why divergence is judged on keys present in
    #: more than one store, never on row counts.
    settings: dict[str, str] = field(default_factory=dict)
    last_activity: str | None = None
    #: The argv recorded by whichever process last claimed this store's
    #: service pidfile. Process identity, not self-report.
    service_argv: list[str] | None = None
    #: The pid and start time that pidfile records, when it records them.
    service_pid: int | None = None
    service_started_at: str | None = None
    #: Whether that pid is ``"alive"``, ``"dead"`` or ``"unknown"`` right now
    #: (:func:`procutil.pid_state`), or None when no pid is recorded. A file
    #: on disk is not a process: a crashed service leaves its pidfile behind,
    #: and treating that as "running" was a false alarm.
    service_process: str | None = None
    #: For a live pid only: is that process the one the pidfile recorded?
    #: True needs both halves of identity -- the OS-reported image is the
    #: recorded launch command's executable, AND the process was created no
    #: later than the pidfile's ``started_at`` (a pid recycled after the
    #: record was written cannot be the recorded process, whatever its
    #: image). False means the pid was most likely reused by something else;
    #: None means it could not be told.
    service_image_verified: bool | None = None
    #: The image half on its own, so a note can say which half was missing.
    service_image_matched: bool | None = None
    installation_id: str | None = None
    error: str | None = None

    @property
    def service_live(self) -> bool:
        """A service is running against this store: live pid, matching image."""

        return self.service_process == "alive" and self.service_image_verified is True

    def to_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "mode": self.mode.value,
            "exists": self.exists,
            "database_exists": self.database_exists,
            "database_bytes": self.database_bytes,
            "schema_version": self.schema_version,
            "settings": dict(self.settings),
            "last_activity": self.last_activity,
            "service_argv": list(self.service_argv) if self.service_argv else None,
            "service_pid": self.service_pid,
            "service_started_at": self.service_started_at,
            "service_process": self.service_process,
            "service_image_verified": self.service_image_verified,
            "service_image_matched": self.service_image_matched,
            "service_live": self.service_live,
            "installation_id": self.installation_id,
            "error": self.error,
        }


@dataclass(frozen=True)
class HostRegistration:
    """What one host is configured to launch, and whether that is canonical."""

    host_id: str
    command: str | None
    resolved: str | None
    canonical: bool
    reason: str
    surface: str = "mcp"

    @property
    def label(self) -> str:
        suffix = {"mcp": "", "hook": " (gate hook)", "status_line": " (status line)"}
        return self.host_id + suffix[self.surface]

    def to_dict(self) -> dict[str, object]:
        return {
            "host_id": self.host_id,
            "surface": self.surface,
            "command": self.command,
            "resolved": self.resolved,
            "canonical": self.canonical,
            "reason": self.reason,
        }


class Coexistence(str, Enum):
    """Which of the supported pre-existing state layouts this machine is in.

    Enumerated so the behaviour for each is *stated* rather than emergent. The
    behaviour is deliberately the same in all of them -- **report, never
    reconcile** -- and this enum is what makes that a decision instead of an
    omission:

    ``NONE``
        No store yet. The owning runtime creates its own on first write.
    ``SINGLE``
        Exactly one store, and it is the one this runtime owns. Nothing to do.
    ``FOREIGN_ONLY``
        Exactly one store, and it belongs to the *other* mode. Almost always a
        developer checkout on a machine with no installed product, or the
        reverse. Not migrated: a runtime adopting a store it does not own is
        how one product silently becomes another.
    ``EQUIVALENT``
        Both exist and agree on every shared setting they both hold. Still
        reported, because two stores still means history and identity are
        split even when settings happen to match today.
    ``CONFLICT``
        Both exist and disagree. The live 2026-08-26 case.

    Why no automatic migration, in any of them: the two stores are not a
    "current" and a "stale" one. Both were being written the same minute, by
    different surfaces the same user was using. Copying either direction
    destroys real settings, and merging execution history risks duplicating
    rows that already carry decision lineage other tables join against. The
    remedy for a split is to stop it being split -- point the host at the
    runtime that owns the canonical state (``repair``) -- after which the user
    decides what, if anything, to carry over.
    """

    NONE = "none"
    SINGLE = "single"
    FOREIGN_ONLY = "foreign_only"
    EQUIVALENT = "equivalent"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class StateSurvey:
    """Store coexistence and active control-plane disagreement are independent.

    ``active_split_brain`` says whether something on this machine is
    actively using a runtime other than the one this process owns: a
    ModelQuad-managed host integration that names another runtime (including
    an unpinned registration), or a service process proven live and
    identity-matched against another store. False proves only
    that none of the inspected managed integrations names that runtime and no
    recorded service pid is provably that service; it cannot prove nobody
    runs it directly from a shell, or through a host this survey does not
    inspect. Preserved stores are therefore notes when nothing active is
    found, never silently reconciled.
    """

    context: StateContext
    stores: tuple[StateStore, ...]
    hosts: tuple[HostRegistration, ...]
    store_findings: tuple[str, ...] = ()
    host_findings: tuple[str, ...] = ()
    coexistence: Coexistence = Coexistence.NONE
    notes: tuple[str, ...] = ()
    #: Stores other than the owned one against which a service is provably
    #: running right now (:attr:`StateStore.service_live`).
    live_foreign_stores: tuple[StateStore, ...] = ()

    @property
    def active_split_brain(self) -> bool:
        return any(not host.canonical for host in self.hosts) or bool(self.live_foreign_stores)

    @property
    def findings(self) -> tuple[str, ...]:
        return self.store_findings + self.host_findings

    @property
    def consistent(self) -> bool:
        return not (self.store_findings or self.host_findings)

    @property
    def populated_stores(self) -> tuple[StateStore, ...]:
        return tuple(store for store in self.stores if store.database_exists)

    def to_dict(self) -> dict[str, object]:
        return {
            "context": self.context.to_dict(),
            "stores": [store.to_dict() for store in self.stores],
            "hosts": [host.to_dict() for host in self.hosts],
            "consistent": self.consistent,
            "coexistence": self.coexistence.value,
            "findings": list(self.findings),
            "store_findings": list(self.store_findings),
            "host_findings": list(self.host_findings),
            "active_split_brain": self.active_split_brain,
            "live_foreign_stores": [str(store.root) for store in self.live_foreign_stores],
            "notes": list(self.notes),
        }


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def inspect_store(root: Path, mode: StateMode) -> StateStore:
    """Read one state root without modifying it.

    The database is opened read-only through a ``file:...?mode=ro`` URI, so
    inspecting a store can never create one, never migrate one, and never take
    a write lock away from a running service.
    """

    database = root / DATABASE_FILE_NAME
    identity = _read_json(root / "identity.json")
    pidfile = _read_json(root / "service.pid")
    argv = pidfile.get("argv") if pidfile else None
    service_argv = [str(item) for item in argv] if isinstance(argv, list) else None

    base = {
        "root": root,
        "mode": mode,
        "exists": root.is_dir(),
        "database_exists": database.is_file(),
        "service_argv": service_argv,
        "installation_id": (identity or {}).get("installation_id"),
        **_service_process_facts(pidfile, service_argv),
    }
    if not database.is_file():
        return StateStore(**base)

    try:
        size = database.stat().st_size
    except OSError:
        size = None

    settings: dict[str, str] = {}
    schema_version: int | None = None
    last_activity: str | None = None
    error: str | None = None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error as exc:
        error = f"{type(exc).__name__}: {exc}"
    else:
        try:
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "settings" in tables:
                placeholders = ",".join("?" for _ in SHARED_SETTING_KEYS)
                for key, value in connection.execute(
                    f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
                    SHARED_SETTING_KEYS,
                ):
                    settings[str(key)] = str(value)
            for table in _ACTIVITY_TABLES:
                if table not in tables:
                    continue
                row = connection.execute(f"SELECT MAX(created_at) FROM {table}").fetchone()
                if row and row[0] and (last_activity is None or str(row[0]) > last_activity):
                    last_activity = str(row[0])
        except sqlite3.Error as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            connection.close()

    return StateStore(
        **base,
        database_bytes=size,
        schema_version=schema_version,
        settings=settings,
        last_activity=last_activity,
        error=error,
    )


def _service_process_facts(
    pidfile: dict | None, service_argv: list[str] | None
) -> dict[str, object]:
    """Is the process a store's pidfile names really that service, right now?

    Liveness first (:func:`procutil.pid_state`, three-valued so a probe that
    could not answer is never read as an answer), then identity, in two
    halves that must both hold: the OS-reported image of the live pid is the
    executable the pidfile recorded launching (the check ``service stop``
    makes before it will touch a pid), and the process was created no later
    than the ``started_at`` the pidfile recorded -- every development
    service is a Python executable, so the image alone cannot tell a
    recycled pid from the recorded one, but a process that came into being
    after the record was written cannot be the process it names. A dead pid
    is a leftover file, not a running service. Nothing here depends on how
    old the file or the store's rows are: the creation-time check orders two
    timestamps of the same launch, it does not age anything, and a
    developer's store is legitimately written during development.
    """

    facts: dict[str, object] = {
        "service_pid": None,
        "service_started_at": None,
        "service_process": None,
        "service_image_verified": None,
        "service_image_matched": None,
    }
    if not pidfile:
        return facts
    pid = pidfile.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return facts
    started_at = pidfile.get("started_at")
    facts["service_pid"] = pid
    facts["service_started_at"] = started_at if isinstance(started_at, str) and started_at else None
    state = pid_state(pid)
    facts["service_process"] = state
    if state != "alive" or not service_argv:
        return facts
    image = process_image_name(pid)
    if image is None:
        return facts
    recorded = service_argv[0]
    windows_path = "\\" in recorded or ":" in recorded[:2]
    expected = ntpath.basename(recorded) if windows_path else posixpath.basename(recorded)
    if os.name == "nt":
        image_matches = image.casefold() == expected.casefold()
    else:
        image_matches = image == expected
    facts["service_image_matched"] = image_matches
    if not image_matches:
        facts["service_image_verified"] = False
        return facts
    recorded_epoch = _recorded_start_epoch(facts["service_started_at"])
    created = process_creation_epoch(pid)
    if recorded_epoch is None or created is None:
        return facts  # identity not established either way
    # The record is written after the spawn, so a genuine service was created
    # at or before it, within the reading's own tolerance.
    facts["service_image_verified"] = created <= recorded_epoch + _creation_slack_seconds()
    return facts


def _creation_slack_seconds() -> float:
    """How much later than its own record a genuine service may appear created.

    Measurement tolerance between two instants of one launch, not an age:
    one second for Windows's absolute ``GetProcessTimes`` instant against a
    microsecond record, two seconds where the reading is POSIX elapsed time
    at one-second resolution taken a moment after the probe. The POSIX
    reading also shifts under a wall-clock step between the record and the
    probe (see :func:`procutil.process_creation_epoch`); the product does
    not try to detect that, and a genuine service it then fails to verify is
    reported as *not established*, never as absent.
    """

    return 1.0 if os.name == "nt" else 2.0


def _recorded_start_epoch(started_at: object) -> float | None:
    if not isinstance(started_at, str) or not started_at.strip():
        return None
    text = started_at.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _same_root(left: Path, right: Path) -> bool:
    """Do two spellings name one physical directory?

    Lexical inequality is not otherness: a ``MODELQUAD_STATE_ROOT`` spelled
    with ``..``, through a junction, or in another case reaches the owned
    store all the same, and must never make the owning service read as a
    foreign one. Resolution is read-only.
    """

    def key(path: Path) -> str:
        try:
            resolved = os.path.realpath(str(path))
        except (OSError, ValueError):
            resolved = os.path.abspath(str(path))
        return os.path.normcase(os.path.normpath(resolved))

    return key(left) == key(right)


def known_state_roots() -> list[tuple[Path, StateMode]]:
    """Every root a ModelQuad runtime on this machine could resolve to.

    Both canonical roots are always listed, even when only one exists, so a
    report can say "the other one is absent" rather than staying silent about
    a location the reader has to know to ask about.
    """

    roots: list[tuple[Path, StateMode]] = []
    seen: set[str] = set()
    for mode in (StateMode.INSTALLED, StateMode.DEVELOPMENT):
        root, _fallback = canonical_state_root(mode)
        key = str(root).casefold()
        if key not in seen:
            seen.add(key)
            roots.append((root, mode))
    context = resolve_state_context()
    key = str(context.state_root).casefold()
    if key not in seen:
        roots.append((context.state_root, context.mode))
    return roots


def _host_registrations(which=None) -> tuple[HostRegistration, ...]:
    """Inspect managed MCP, gate and status-line wiring without launching it.

    An absent MCP entry must not hide a wired CLI surface. Conversely, a user's
    replacement status line is not ours to judge, even when its text mentions
    ModelQuad. Ownership and runtime identity remain separate predicates.
    """
    from .adapters import claude_adapter
    from .adapters.cliintegration import owns_cli_command, owns_status_line_command
    from .hostruntime import (
        classify_cli_registration,
        classify_registration,
        classify_status_line_registration,
        read_registered_command,
    )
    from .registry import default_registry

    kwargs = {} if which is None else {"which": which}
    registrations: list[HostRegistration] = []
    for plugin in default_registry().all():
        for host in plugin.host_adapters:
            try:
                target_dir = host.default_target_dir()
                command = read_registered_command(host.id, target_dir)
                surfaces = []
                if command is not None:
                    surfaces.append(("mcp", classify_registration(command, **kwargs)))
                cli_commands = []
                if host.id == "claude":
                    cli_commands.extend(
                        ("hook", command, ("hook-gate",))
                        for command in dict.fromkeys(claude_adapter.read_hook_commands(target_dir))
                    )
                    cli_commands.append((
                        "status_line", claude_adapter.read_status_line_command(target_dir),
                        ("usage-hook", "claude"),
                    ))
                for surface, command, arguments in cli_commands:
                    # The status line has one more owned generation than the
                    # gate -- the loose script we used to install -- so it is
                    # judged by the wider predicate, and by the classifier that
                    # can name that script instead of reporting "launches
                    # python".
                    if surface == "status_line":
                        owns, classify = owns_status_line_command, classify_status_line_registration
                    else:
                        owns, classify = owns_cli_command, classify_cli_registration
                    if owns(command, arguments):
                        surfaces.append((surface, classify(command, **kwargs)))
            except (OSError, ValueError):
                continue
            for surface, verdict in surfaces:
                registrations.append(HostRegistration(
                    host_id=host.id, surface=surface,
                    command=verdict.command, resolved=verdict.resolved,
                    canonical=verdict.canonical, reason=verdict.reason,
                ))
    return tuple(registrations)


def _divergent_settings(stores: tuple[StateStore, ...]) -> list[str]:
    """Shared setting keys that two populated stores disagree about.

    Compared only across stores that BOTH hold the key. An absent key is a
    lazily-unwritten default, not a conflict -- treating it as one would report
    a divergence on every fresh install.
    """

    divergent: list[str] = []
    for key in SHARED_SETTING_KEYS:
        values = {store.settings[key] for store in stores if key in store.settings}
        if len(values) > 1:
            divergent.append(key)
    return divergent


def survey_state(*, which=None) -> StateSurvey:
    """Inspect every state store and host registration, and report disagreements."""

    context = resolve_state_context()
    stores = tuple(inspect_store(root, mode) for root, mode in known_state_roots())
    hosts = _host_registrations(which=which)

    store_findings: list[str] = []
    notes: list[str] = []
    populated = tuple(store for store in stores if store.database_exists)
    foreign = tuple(store for store in populated if not _same_root(store.root, context.state_root))
    live_foreign = tuple(store for store in foreign if store.service_live)
    active_split_brain = any(not host.canonical for host in hosts) or bool(live_foreign)
    divergent: list[str] = []
    if len(populated) > 1:
        roots = ", ".join(str(store.root) for store in populated)
        divergent = _divergent_settings(populated)
        for store in live_foreign:
            store_findings.append(_live_foreign_service_finding(store, context.state_root))
        if active_split_brain:
            store_findings.append(f"More than one ModelQuad database exists on this machine: {roots}.")
            if divergent:
                store_findings.append(
                    "These stores disagree about settings every surface must share: "
                    + ", ".join(divergent) + "."
                )
        else:
            notes.append(
                f"Preserved legacy/development state: multiple stores exist ({roots}); "
                "a second store belongs to the other mode. No inspected ModelQuad-managed "
                "host integration launches another runtime, and no recorded service pid is "
                "provably running against that store, so it is not the active host control "
                "plane. ModelQuad neither migrates nor deletes it. This is a reading of the "
                "host integrations and recorded service pids only: a runtime started directly "
                "from a shell is not visible here."
            )
            for store in foreign:
                unestablished = _unestablished_service_note(store)
                if unestablished:
                    notes.append(unestablished)
            if divergent:
                notes.append("The preserved stores' settings differ in: " + ", ".join(divergent) + ".")

    host_findings = tuple(f"{host.label}: {host.reason}" for host in hosts if not host.canonical)

    return StateSurvey(
        context=context,
        stores=stores,
        hosts=hosts,
        store_findings=tuple(store_findings),
        host_findings=host_findings,
        notes=tuple(notes),
        coexistence=_classify_coexistence(context, populated, divergent),
        live_foreign_stores=live_foreign,
    )


def _live_foreign_service_finding(store: StateStore, owned_root: Path) -> str:
    """A service is provably running against a store this runtime does not own.

    Names the process so the reader can find it, and gives the one recovery
    that can actually resolve it. Deliberately not ``repair`` (which points
    host integrations at this installation and stops no process) and never
    "kill pid N" (the listener may be another developer checkout mid-run);
    the same rule the service controller applies to a foreign port owner.
    """

    command = " ".join(store.service_argv or ())
    started = f", started {store.service_started_at}" if store.service_started_at else ""
    return (
        f"A ModelQuad service is running against {store.root} (pid {store.service_pid}{started}, "
        f"command: {command}), while this runtime owns {owned_root}: whatever that service "
        "serves to its API clients is reading the other store. Stop it from the ModelQuad that "
        "started it (run 'modelquad service stop' in that checkout), then run `modelquad state` "
        "again. This copy cannot stop another "
        "ModelQuad's service for you."
    )


def _unestablished_service_note(store: StateStore) -> str | None:
    """Say what the product could not establish, rather than saying OK.

    A dead pid is today's preserved-state case and needs no extra words. A
    pid that could not be probed, or a live pid whose image is not the
    recorded executable (or could not be read), is neither "running" nor
    "not running": the note says so, so a reader is not told more than the
    product knows.
    """

    if store.service_pid is None or store.service_process in (None, "dead"):
        return None
    where = f"{store.root} records service pid {store.service_pid}"
    if store.service_process == "unknown":
        return (
            f"{where}, which could not be probed from this process; whether a service is "
            "running against that store is not established."
        )
    if store.service_image_verified is False:
        return (
            f"{where}, which is alive but is not the recorded service "
            f"({(store.service_argv or ['?'])[0]}, started {store.service_started_at}): either "
            "another program or a process created after that record now holds the pid, so no "
            "service is known to be running against that store."
        )
    if store.service_image_matched is True:
        return (
            f"{where}, which is alive and is the recorded executable, but its creation time or "
            "the record's start time could not be read to confirm it predates the record; "
            "whether a service is running against that store is not established."
        )
    return (
        f"{where}, which is alive, but its process image could not be read to confirm it is "
        "the recorded service; whether a service is running against that store is not "
        "established."
    )


def _classify_coexistence(
    context: StateContext,
    populated: tuple[StateStore, ...],
    divergent: list[str],
) -> Coexistence:
    """Name the layout this machine is in. See :class:`Coexistence`."""

    if not populated:
        return Coexistence.NONE
    if len(populated) == 1:
        owned = str(populated[0].root).casefold() == str(context.state_root).casefold()
        return Coexistence.SINGLE if owned else Coexistence.FOREIGN_ONLY
    return Coexistence.CONFLICT if divergent else Coexistence.EQUIVALENT
