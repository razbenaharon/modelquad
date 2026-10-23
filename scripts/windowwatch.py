#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Attribute every window that appears on this Windows desktop to a process.

    python scripts/windowwatch.py --seconds 600 --out evidence.jsonl
    python scripts/windowwatch.py --report evidence.jsonl

An observation instrument, not part of any product or harness path: nothing
imports it, nothing runs it automatically, and it spawns no process at all.
It exists because an earlier fix was closed on a source-level argument --
every spawn carries the flag -- and a later investigation then had to answer a
question that a source scan cannot: *which process owns the window the user
just saw?*

**Why a WinEvent hook and not polling.** The windows in question are visible
for a fraction of a second. ``EnumWindows`` on a timer, a screenshot, or a
``Get-Process`` sample all sample a moment and will simply miss them.
``SetWinEventHook`` is *told* about the show, and the notification is queued to
this thread's message queue, so the record survives even when the window is
already destroyed by the time it is read. That is the difference between
evidence and an anecdote.

**What a record contains**, which is what a popup has to be resolved to: the timestamp, the HWND, the window class and title, the owning
pid, that process's image path and full command line, its parent pid, and its
whole ancestor chain up to the session root -- plus, when the window is later
hidden or destroyed, how long it was on screen.

**Why the ancestor chain and not just the pid.** A flashing ``conhost.exe`` or
``cmd.exe`` is nearly always a symptom: the interesting fact is which
``python.exe`` running which script asked for it. Parents die faster than their
children, so a process table sampled once at the end of a run cannot answer
this. A poller thread keeps a durable pid -> (image, command line, parent,
creation time) map for the whole session, and the chain is walked out of that
map. Creation times are compared where both are known, so a recycled pid is
reported as a break in the chain rather than silently inventing an ancestor.

Read-only by construction: it opens processes for query and memory read, and
appends one JSON object per line to the file named by ``--out``. It never
terminates, injects into, or otherwise touches anything it observes.

**Start a capture from a terminal you already have open.** This file is run by
console-subsystem ``python.exe``, and a console-subsystem process launched by a
parent that has no console of its own is given a fresh one by Windows -- with a
window, on the desktop, before this file's first line executes and therefore
long before its hook is installed. The observer would put a console popup on
the user's screen, in the run whose purpose is to find console popups, and be
structurally incapable of recording it. Launched from an
existing terminal it inherits that terminal's console and allocates nothing.

The hazard is only ever the *launcher*, so if a capture has to be started from
something console-less -- a scheduled task, a service, an agent harness, a
double-click on a shortcut -- give the launcher the same suppression this whole
Issue is about, and the observer inherits a console with no window:

    # PowerShell, from a console-less parent
    Start-Process -FilePath pythonw.exe `
        -ArgumentList 'scripts/windowwatch.py','--seconds','600','--out','evidence.jsonl' `
        -WindowStyle Hidden

    # or, from Python
    subprocess.Popen([sys.executable, "scripts/windowwatch.py", "--seconds", "600",
                      "--out", "evidence.jsonl"], creationflags=0x08000000)

``pythonw.exe`` is the windowless build of the same interpreter and never
allocates a console at all; ``CREATE_NO_WINDOW`` gives ``python.exe`` a console
that has no window. Either works. What does not work is fixing this inside this
file: by the time any line of it runs, the console -- and its window -- already
exist. That is why the resolution here is a documented launch procedure rather
than a code change.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import ExitStack
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

#: The console-hosting window classes. A window of one of these classes IS the
#: black rectangle the user reports; anything else recorded here is context.
#: ``ConsoleWindowClass`` is the classic conhost window, ``PseudoConsoleWindow``
#: the ConPTY one, and the ``CASCADIA_*`` classes belong to Windows Terminal,
#: which hosts a console without ever creating a ``ConsoleWindowClass``.
CONSOLE_CLASSES = frozenset(
    {
        "ConsoleWindowClass",
        "PseudoConsoleWindow",
        "CASCADIA_HOSTING_WINDOW_CLASS",
        "CASCADIA_NOTIFICATION_WINDOW_CLASS",
    }
)

EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
#: Not a Win32 constant: the end this observer imposes on a window that was
#: still on screen when the recording stopped. Distinct from a destroy,
#: because the window did not go away -- the observation did.
_RECORDING_END = -1
_EVENT_NAMES = {
    EVENT_OBJECT_CREATE: "create",
    EVENT_OBJECT_DESTROY: "destroy",
    EVENT_OBJECT_SHOW: "show",
    EVENT_OBJECT_HIDE: "hide",
    _RECORDING_END: "recording_end",
}

OBJID_WINDOW = 0
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
GA_ROOT = 2
WM_QUIT = 0x0012
PM_REMOVE = 0x0001


#: The one ``OpenProcess`` failure that proves a pid is not a live process,
#: and therefore the one that licenses answering from memory instead.
#: Everything else is either the opposite -- ``ERROR_ACCESS_DENIED``
#: (5) says the process is there and will not be opened -- or an error whose
#: meaning this file does not know. This constant was defined and then never
#: checked: the code asked whether the failure was *access denied* and treated
#: every other outcome as proof of death, so one unexpected error was enough
#: to hand a live process the identity of whatever last held its number.
_ERROR_INVALID_PARAMETER = 87


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


#: `dwmsEventTime` is a `GetTickCount` reading: milliseconds since boot, in a
#: 32-bit counter that wraps every 49.7 days. Two stamps are only ever
#: subtracted from each other here, and a masked subtraction is exact across
#: the wrap for any interval shorter than the whole period -- which every
#: window lifetime this instrument measures is.
_TICK_MASK = 0xFFFF_FFFF


def _elapsed_ms(start: int, end: int) -> float:
    """Seconds between two ``dwmsEventTime`` stamps, wrap included."""

    return ((int(end) - int(start)) & _TICK_MASK) / 1000.0


def _tick_count() -> int | None:
    """``GetTickCount`` as of now, in the units ``dwmsEventTime`` is stamped in.

    ``dwmsEventTime`` *is* a ``GetTickCount`` reading taken by Windows at the
    moment of the event, so the two are directly subtractable -- which is what
    lets a window that is still on screen when the recording stops be measured
    against the present rather than against the last thing that happened to
    happen.

    ``None`` means *there is no tick to read here*, which is a state callers
    already handle: off Windows, where there is no ``kernel32`` at all, and on
    a ``kernel32`` that does not expose the call. Asked at two points -- at
    the unhook and, as the documented fallback, inside
    ``finish()`` -- so it answers rather than raising, and a caller that cannot
    have a live clock falls back instead of failing.
    """

    get_tick_count = getattr(_kernel32, "GetTickCount", None)
    if get_tick_count is None:
        return None
    return int(get_tick_count()) & _TICK_MASK


def _last_error() -> int:
    """The last Win32 error, through a name a test can replace.

    Every ``WinDLL`` here is opened ``use_last_error=True``, so this reads the
    per-thread value ``ctypes`` saved for the most recent FFI call. A test that
    stubs ``_kernel32`` with a Python object makes no FFI call at all and so
    cannot set it; the seam exists so such a test can say which failure it is
    modelling instead of being unable to model either.
    """

    return ctypes.get_last_error()


class FailureLedger:
    """A thread-safe tally of failures that may not be allowed to propagate.

    Three places in this file have to swallow an exception: the cleanup calls
    below, the sampler loop, and the WinEvent callback -- the last because
    raising into Windows from a hook is not an option, and the other two
    because an observer may not abort the observation it is in the middle of.
    Swallowing is the correct handling; swallowing *silently* is not, and it
    was what all three did. An evidence instrument that fails
    without a record reports a clean run it did not have.

    So each failure is counted under a name, and the counts go into the
    session's stop record, which is the only place a reader of the evidence
    file could check.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: Counter = Counter()

    @property
    def failures(self) -> dict[str, int]:
        with self._lock:
            return dict(self._failures)

    def note(self, name: str) -> None:
        with self._lock:
            self._failures[name] += 1


class CleanupLedger(FailureLedger):
    """Releases a handle or a hook, and remembers whether the release worked.

    ``CloseHandle`` and ``UnhookWinEvent`` both return a BOOL and both were
    being called for effect with the result dropped. Each
    dropped result hides something worth knowing: this process opens one handle
    per sampled process ten times a second, so a close that quietly fails is a
    leak that grows for the whole run, and an unhook that quietly fails leaves a
    system-wide WinEvent hook installed after the run has said it stopped.

    Counted rather than raised: an observer may not abort the observation it is
    in the middle of. The counts go into the session's ``stop`` record, so the
    evidence file itself says whether the instrument let go of everything it
    took -- which is the only place a reader of that file could check.
    """

    def close_handle(self, handle) -> bool:
        return self._checked("CloseHandle", _kernel32.CloseHandle(handle))

    def unhook(self, hook) -> bool:
        return self._checked("UnhookWinEvent", _user32.UnhookWinEvent(hook))

    def _checked(self, name: str, result) -> bool:
        ok = bool(result)
        if not ok:
            self.note(name)
        return ok


#: One ledger for the process. The sampler thread and the message loop both
#: release handles, hence the lock inside it.
CLEANUP = CleanupLedger()


class _Once:
    """A shutdown step that runs on the normal path *or* the unwind, not both.

    ``watch()`` has to do three things in a fixed order at the end of a run --
    unhook, stop the sampler, write the stop record -- and it has to do them
    whether the run ended at its deadline or at an exception. Registering them
    with the ``ExitStack`` covers the exception; calling them explicitly covers
    the ordinary path, where later steps need to *see* the result of earlier
    ones. Doing both would unhook twice, and the second ``UnhookWinEvent``
    fails and would be recorded as a cleanup failure that never happened.

    So each step is wrapped here: the first call runs it and keeps the answer,
    and every later call returns that same answer.
    """

    def __init__(self, action) -> None:
        self._action = action
        self.done = False
        self.result = None

    def __call__(self):
        if not self.done:
            self.done = True
            self.result = self._action()
        return self.result


# --------------------------------------------------------------- process facts


def _unknown(
    pid: int,
    *,
    parent_pid: int | None = None,
    created: int | None = None,
    from_snapshot: bool = False,
) -> dict:
    """What is recorded about a process that could not be described at all.

    ``from_snapshot`` marks the one case where ``parent_pid`` was taken from a
    toolhelp snapshot rather than confirmed through a handle.
    The field travels with the entry so that a consumer walking parents can
    tell a verified link from an unverified one, instead of the distinction
    being lost the moment the entry is stored.
    """

    return {
        "pid": pid,
        "image": None,
        "command_line": None,
        "parent_pid": parent_pid,
        "created": created,
        "from_snapshot": from_snapshot,
    }


class ProcessTable:
    """A durable pid -> facts map, so a dead ancestor is still nameable.

    Sampled on a thread rather than resolved on demand: by the time a console
    window's ``EVENT_OBJECT_SHOW`` is read, the ``cmd.exe`` that owned it may
    be gone, and its parent almost certainly is. An entry is never evicted --
    a whole observation session's process history is a few thousand small
    dicts, and dropping one is exactly how a chain becomes unattributable.

    **A pid is not an identity, and this table used to treat it as one.** Every
    entry was stored under its pid alone, ``sample()`` skipped any pid it had
    already seen, and ``get()`` returned the first facts ever recorded for it.
    Windows recycles pids freely -- a busy shift on this machine turns over
    thousands -- so a window created by a *new* process holding a *reused* pid
    was attributed to whatever long-dead process the table remembered under
    that number: a real window, a real image path, a real command line, and all
    of them somebody else's. The chain walk's creation-time comparison could
    not catch it, because that compares a child against its parent and here the
    child itself was the wrong process.

    So an entry is a *generation*: pid plus the process creation time, which is
    the pair Windows itself uses to make a pid unique over time. Generations
    are kept oldest-first per pid; ``get()`` resolves the one that is alive
    now, and the ancestor walk resolves the one that was alive when its child
    was created. Nothing is dropped, so a reused pid gains a second generation
    rather than overwriting the first, and the dead ancestor stays nameable --
    which was the point of the table to begin with.
    """

    #: PROCESS_QUERY_INFORMATION | PROCESS_VM_READ -- the pair needed to read a
    #: command line out of the PEB. Query-limited access alone cannot do it.
    _ACCESS = 0x0400 | 0x0010
    _QUERY_LIMITED = 0x1000

    def __init__(self, interval: float = 0.10) -> None:
        self.interval = interval
        #: Samples that raised. The loop may not stop for one -- a process
        #: that vanishes mid-walk is ordinary -- but a run that lost samples
        #: has gaps in the ancestry it can name, and used to say nothing at
        #: all about them while reporting ``sampler_stopped: true``.
        self.sample_failures = FailureLedger()
        self._entries: dict[int, list[dict]] = {}
        #: Pids the sampler saw alive and could NOT open, while it already knew
        #: at least one generation of them. The table cannot
        #: tell whether that sighting was the generation it already has or an
        #: undescribed successor, and the difference decides whether the
        #: remembered one is a real ancestor or a pid-reuse coincidence. So the
        #: ambiguity is recorded rather than resolved by assumption.
        self._ambiguous: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- public ------------------------------------------------------------

    def start(self) -> None:
        self.sample()
        self._thread = threading.Thread(target=self._loop, name="proctable", daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        """Ask the sampler to finish, and say whether it did.

        The join has a timeout, so "stopped" was previously an assumption
        rather than a fact. A sampler still running past it is still opening
        and closing process handles, which means the cleanup counts are still
        moving while the session claims to be over -- so the answer goes into
        the stop record rather than being discarded.
        """

        self._stop.set()
        if self._thread is None:
            return True
        self._thread.join(timeout=2.0)
        return not self._thread.is_alive()

    def generations(self, pid: int) -> list[dict]:
        with self._lock:
            return list(self._entries.get(pid, ()))

    def get(self, pid: int) -> dict:
        """The facts for the generation of ``pid`` that is alive right now.

        **One handle, held across both questions.** The creation time and the
        description used to be read through two separate ``OpenProcess`` calls,
        and the answer was assembled from both: the first call's creation time
        stored against the second call's image path and command line. If the
        pid was recycled between those two calls -- which is the entire subject
        of pid-reuse attribution, now happening inside the very function that
        was written to prevent it -- the record named one process and described
        another, and nothing downstream could tell. So
        the handle is opened once and both readings come out of it. A handle
        keeps its process's identity even after that process exits, which is
        precisely the guarantee a pid does not give.

        **Unknown rather than stale.** Three outcomes are distinguished, where
        there used to be two:

        * The process opens and its creation time reads. That is the live
          generation, and the table's memory is checked against it.
        * The process cannot be opened *because it is gone*
          (``ERROR_INVALID_PARAMETER``, and nothing else). There is no live
          generation to check against and the table's memory is all there is
          -- the dead-ancestor case, which is what this table exists for.
        * Anything else. The open failed for a reason that is not proof of
          death -- ``ERROR_ACCESS_DENIED``, from System and anything elevated,
          or an error this file has no reading for -- or the handle opened and
          ``GetProcessTimes`` failed anyway. Here the table may hold a
          generation under this pid, and returning it would invent an owner or
          an ancestor for a process that could just as easily be a new holder
          of a recycled number. Unknown stays unknown (``AGENTS.md``
          invariant 4).
        """

        if pid <= 0:
            return _unknown(pid)
        opened = self._open(pid)
        if opened is None:
            if _last_error() != _ERROR_INVALID_PARAMETER:
                return _unknown(pid)  # not proof of death: say so rather than guess
            generations = self.generations(pid)
            if not generations:
                return _unknown(pid)
            if self.is_ambiguous(pid):
                # Provably gone now, and at some point it was alive and
                # undescribable -- so the newest generation this table holds
                # may not be the one that just died. Handing it
                # back would attach a real image and command line to whatever
                # asked, which is the pid-reuse attribution the whole table
                # exists to refuse.
                return _unknown(pid)
            return generations[-1]
        handle, limited = opened
        try:
            live = _created_at(handle)
            if live is None:
                return _unknown(pid)  # the handle is good and the answer is not
            for entry in reversed(self.generations(pid)):
                if entry.get("created") == live:
                    return entry
            # Either the pid is new since the last sample, or it is a pid this
            # table already knows under an older, now-dead process. Both are
            # described here, through the handle the creation time came from.
            described = self._facts(handle, pid, limited=limited, created=live)
        finally:
            CLEANUP.close_handle(handle)
        self._remember(pid, described)
        return described

    def is_ambiguous(self, pid: int) -> bool:
        """Whether ``pid`` was ever seen alive and undescribable."""

        with self._lock:
            return pid in self._ambiguous

    def generation_at(self, pid: int, moment: int) -> dict | None:
        """The newest remembered generation of ``pid`` that existed at ``moment``.

        ``moment`` is a FILETIME, the same unit ``created`` is stored in. A
        generation created *after* its child cannot be that child's parent, so
        it is not a candidate however plausible its image path looks.
        """

        candidates = [
            entry
            for entry in self.generations(pid)
            if entry.get("created") is not None and entry["created"] <= moment
        ]
        return candidates[-1] if candidates else None

    def ancestors(self, pid: int, limit: int = 24, start: dict | None = None) -> list[dict]:
        """The chain from ``pid``'s parent upwards, stopping at the first break.

        A parent whose creation time is LATER than its child's is a recycled
        pid, not an ancestor. Reporting it would be worse than reporting
        nothing, so the walk records the break and stops -- unknown stays
        unknown (``AGENTS.md`` invariant 4).

        The generation is chosen before that comparison rather than after it:
        if the table remembers two processes under the parent's pid, the one
        that was alive when this child was created is the ancestor, and the
        other one is a different process that happens to share a number.

        **``start`` is the generation the caller already resolved.** Without
        it this re-resolved ``pid`` through ``get()``, which
        is a *second* lookup of a number the caller had already turned into an
        identity -- and if the pid was recycled in between, the record ended up
        carrying one generation's image and command line beside the
        successor's ancestor chain. Exactly the split identity already closed
        inside ``get()``, reappearing across the two calls that build one
        record. The caller passes the generation it described; only a caller
        that has none lets this resolve one.

        The walk also stops at a parent link that was never confirmed through a
        handle -- ``from_snapshot`` -- rather than following the snapshot's
        word as though it were verified.
        """

        chain: list[dict] = []
        seen = {pid}
        current = self.get(pid) if start is None else start
        for _ in range(limit):
            parent_pid = current.get("parent_pid")
            if not parent_pid or int(parent_pid) in seen:
                break
            if self.is_ambiguous(int(parent_pid)):
                # The sampler saw this pid alive at least once and could not
                # open it, so the table may be missing the very generation that
                # was this child's parent. Naming the generation it does have
                # would be a guess with a real image path on it.
                chain.append({
                    "pid": int(parent_pid),
                    "image": None,
                    "note": "pid may hold an undescribed generation; chain ends here",
                })
                break
            if current.get("from_snapshot"):
                # The link exists, but only a toolhelp snapshot ever asserted
                # it, and the process it belonged to could not be opened to
                # confirm. Naming the parent here would present a guess in the
                # same shape as a verified ancestor.
                chain.append({
                    "pid": int(parent_pid),
                    "image": None,
                    "note": "parent unverified (snapshot only); chain ends here",
                })
                break
            child_created = current.get("created")
            parent = None
            if child_created is not None:
                parent = self.generation_at(int(parent_pid), child_created)
            if parent is None:
                parent = self.get(int(parent_pid))
            parent_created = parent.get("created")
            if child_created is not None and parent_created is not None and parent_created > child_created:
                chain.append({"pid": int(parent_pid), "image": None, "note": "pid reused; chain ends here"})
                break
            chain.append({key: parent.get(key) for key in ("pid", "image", "command_line", "parent_pid")})
            seen.add(int(parent_pid))
            current = parent
        return chain

    def sample(self) -> None:
        """Record any generation the table does not already hold.

        A known pid is no longer skipped outright. It is probed for its current
        creation time -- ``OpenProcess``, ``GetProcessTimes``, close -- and
        described only when that generation is new. Measured on this machine at
        308 processes: 0.9 ms for the whole probe sweep, against the 4.7 ms the
        ``CreateToolhelp32Snapshot`` walk already cost, taking a sample from
        4.7 ms to 5.6 ms and the sampler from 4.7% to 5.6% of one core at 10 Hz.
        The expensive part, reading a command line out of the PEB, still happens
        once per generation and not once per sample.

        A process that refuses even ``PROCESS_QUERY_LIMITED_INFORMATION`` (about
        half of them here: System, and anything elevated) cannot be probed, so
        for those the old behaviour stands and the first generation seen is
        kept. That is a documented limit, not a silent one: a protected process
        is not something this instrument can attribute a window to anyway.

        The probe and the description share **one handle**, for the reason
        ``get()`` gives at length: opening twice leaves a gap in which the pid
        can be recycled, and a generation stored under the first call's
        creation time with the second call's command line is the false
        pid-reuse attribution, reintroduced from inside its own fix. One open is also cheaper than the two this
        used to cost.
        """

        for pid, parent_pid in snapshot_pids():
            opened = self._open(pid)
            if opened is None:
                # Unopenable: protected, or gone between the snapshot and here.
                # Nothing can be verified, so the first generation seen stands.
                # This is the one record whose parent still comes from the
                # snapshot rather than from a handle, and it is
                # the one where nothing else is known either: no image, no
                # command line, no creation time.
                #
                # It is **flagged** as such. Harmless as a
                # description -- a reader cannot mistake it for one -- but not
                # harmless as a *link*: `get()` hands the table's memory back
                # for a pid that is provably gone, so this unverified parent
                # could reach `ancestors()` and be walked as though a handle
                # had confirmed it. The flag is what lets the walk stop.
                if not self.generations(pid):
                    self._remember(
                        pid, _unknown(pid, parent_pid=parent_pid, from_snapshot=True)
                    )
                else:
                    # Known pid, live sighting, no handle. Either the same
                    # generation as before, or a successor this table will
                    # never be able to describe -- and `generation_at()` cannot
                    # tell the two apart, so it would hand back the older
                    # generation for a child created after this moment and
                    # present a pid-reuse coincidence as an ancestor.
                    # Discarding this sighting was what made that silent;
                    # remembering it is what lets the walk stop.
                    with self._lock:
                        self._ambiguous.add(pid)
                continue
            handle, limited = opened
            try:
                live = _created_at(handle)
                if live is None:
                    if self.generations(pid):
                        # The handle opened and the clock would not read, so
                        # this pid was alive and stays undescribed -- the same
                        # hole as an unopenable one, reached through the other
                        # door. Skipping silently let
                        # `generation_at()` answer with the generation it
                        # already had.
                        with self._lock:
                            self._ambiguous.add(pid)
                        continue
                    described = self._facts(handle, pid, limited=limited)
                elif any(entry.get("created") == live for entry in self.generations(pid)):
                    continue
                else:
                    described = self._facts(handle, pid, limited=limited, created=live)
            finally:
                CLEANUP.close_handle(handle)
            self._remember(pid, described)

    # -- internals ----------------------------------------------------------

    def _remember(self, pid: int, entry: dict) -> None:
        with self._lock:
            generations = self._entries.setdefault(pid, [])
            if any(known.get("created") == entry.get("created") for known in generations):
                return  # another thread described the same generation first
            generations.append(entry)
            generations.sort(key=lambda known: (known.get("created") is None, known.get("created")))

    def _open(self, pid: int) -> tuple[int, bool] | None:
        """``(handle, limited)`` for ``pid``, or ``None`` with the reason in errno.

        Every reading this table takes goes through one of these, and the
        handle is held until every reading for that generation is done: a
        handle names a *process*, while a pid names only a number the OS is
        free to hand to somebody else a moment later.

        Full access is tried first because a command line needs it. System and
        elevated processes refuse it, and a partial record -- image path only
        -- beats an absent one, so query-limited is the fallback. When both
        fail the caller needs to know *why*, and only the last Win32 error
        distinguishes "this pid is not a process" from "this process will not
        let you look", so it is left set for ``_last_error()`` to read.
        """

        if pid <= 0:
            ctypes.set_last_error(_ERROR_INVALID_PARAMETER)
            return None
        ctypes.set_last_error(0)
        handle = _kernel32.OpenProcess(self._ACCESS, False, pid)
        if handle:
            return int(handle), False
        ctypes.set_last_error(0)
        handle = _kernel32.OpenProcess(self._QUERY_LIMITED, False, pid)
        if handle:
            return int(handle), True
        return None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sample()
            except Exception as error:  # noqa: BLE001 -- an observer may never raise
                # Not raised, and no longer silent: a missed sample is a hole
                # in the pid -> facts map, and every ancestor chain walked out
                # of it afterwards is missing whatever started in that gap.
                self.sample_failures.note(type(error).__name__)

    def _facts(self, handle: int, pid: int, *, limited: bool, created: int | None = None) -> dict:
        """Everything describable through an already-open handle.

        Takes the handle rather than the pid, which is the whole point: a
        function that reopens the pid describes whatever
        process holds that number *now*, and the caller then files the answer
        under a creation time it read a moment earlier.

        **Including the parent**, which was the one field still arriving by
        pid. It came from a ``CreateToolhelp32Snapshot`` walk -- taken before
        the handle was opened, in ``sample()``, or taken after it, in
        ``get()`` -- and a pid recycled in either gap put another process's
        parent, and the whole ancestor chain above it, on this process's
        record. Every other field here already described the process the
        handle names; this one described whoever held its number at the moment
        of a different call. The handle answers the question
        itself, and where it will not the answer is unknown rather than a
        number obtained by asking about a pid.
        """

        return {
            "pid": pid,
            "image": _image_path(handle),
            "command_line": None if limited else _command_line(handle),
            "parent_pid": _parent_from_handle(handle),
            # The caller's probe wins: it is the value the generation was
            # matched on, and re-reading it here could straddle an exit and
            # store a generation under a creation time nobody looked it up by.
            "created": created if created is not None else _created_at(handle),
        }


def _image_path(handle: int) -> str | None:
    size = wintypes.DWORD(32768)
    buf = ctypes.create_unicode_buffer(size.value)
    if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return buf.value
    return None


def _created_at(handle: int) -> int | None:
    creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    ok = _kernel32.GetProcessTimes(
        wintypes.HANDLE(handle),
        ctypes.byref(creation),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    )
    if not ok:
        return None
    return (creation.dwHighDateTime << 32) | creation.dwLowDateTime


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


#: x64 PEB / RTL_USER_PROCESS_PARAMETERS offsets. Undocumented as a contract,
#: so the reader below is written to fail into ``None`` at every single step:
#: a command line that cannot be read is a gap in one record, never a crash in
#: the observer.
_PEB_PROCESS_PARAMETERS = 0x20
_PARAMS_COMMAND_LINE = 0x70


def _read(handle: int, address: int, size: int) -> bytes | None:
    buf = (ctypes.c_char * size)()
    read = ctypes.c_size_t(0)
    ok = _kernel32.ReadProcessMemory(
        wintypes.HANDLE(handle), ctypes.c_void_p(address), buf, ctypes.c_size_t(size), ctypes.byref(read)
    )
    if not ok or read.value != size:
        return None
    return bytes(buf)


def _command_line(handle: int) -> str | None:
    """The target process's own command line, read out of its PEB.

    WMI answers the same question, but a ``Win32_Process`` query costs tens of
    milliseconds and cannot be issued fast enough to keep a 100ms poller
    honest; this costs three page reads. It only works 64-bit to 64-bit, which
    is what the machine under observation is; anything else returns ``None``.
    """

    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
    info = _PROCESS_BASIC_INFORMATION()
    status = _ntdll.NtQueryInformationProcess(
        wintypes.HANDLE(handle), 0, ctypes.byref(info), ctypes.sizeof(info), None
    )
    if status != 0 or not info.PebBaseAddress:
        return None
    params_bytes = _read(handle, info.PebBaseAddress + _PEB_PROCESS_PARAMETERS, 8)
    if params_bytes is None:
        return None
    params = int.from_bytes(params_bytes, "little")
    if not params:
        return None
    raw = _read(handle, params + _PARAMS_COMMAND_LINE, ctypes.sizeof(_UNICODE_STRING))
    if raw is None:
        return None
    unicode_string = _UNICODE_STRING.from_buffer_copy(raw)
    if not unicode_string.Length or not unicode_string.Buffer:
        return None
    text = _read(handle, unicode_string.Buffer, unicode_string.Length)
    if text is None:
        return None
    return text.decode("utf-16-le", "replace")


def _parent_from_handle(handle: int) -> int | None:
    """The pid that started this handle's process, asked of the process itself.

    ``PROCESS_BASIC_INFORMATION``'s last member is documented as ``Reserved3``
    and is ``InheritedFromUniqueProcessId``; the structure above declares it
    under the public name, which is why it is read out under that one. The
    query needs only ``PROCESS_QUERY_LIMITED_INFORMATION``, so it answers for
    the query-limited handles ``_open`` falls back to as readily as for full
    ones.

    A handle is the only thing in this file that goes on naming one process
    for as long as it is held, which is why the parent is asked of it: a pid
    lookup answers about whoever holds that number at the moment of the
    lookup, and after a recycle that is somebody else entirely. Fails into ``None`` like every other reader here -- a parent this
    file cannot verify is a gap in one record, never a guess in it.
    """

    info = _PROCESS_BASIC_INFORMATION()
    status = _ntdll.NtQueryInformationProcess(
        wintypes.HANDLE(handle), 0, ctypes.byref(info), ctypes.sizeof(info), None
    )
    if status != 0 or not info.Reserved3:
        return None
    return int(info.Reserved3)


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def snapshot_pids() -> list[tuple[int, int]]:
    """Every (pid, parent pid) the process table currently holds."""

    snapshot = _kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return []
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        pairs: list[tuple[int, int]] = []
        if not _kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return []
        while True:
            pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            if not _kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                return pairs
    finally:
        CLEANUP.close_handle(snapshot)


# ------------------------------------------------------------------- watching


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def _window_text(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(wintypes.HWND(hwnd), buf, 512)
    return buf.value


def _rect(hwnd: int) -> dict | None:
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return {
        "left": rect.left,
        "top": rect.top,
        "width": rect.right - rect.left,
        "height": rect.bottom - rect.top,
    }


class Watcher:
    """Turns raw WinEvent notifications into attributed window records.

    **A window is not a popup, and this class used to conflate them.** Every
    console-class ``EVENT_OBJECT_CREATE`` was counted, whether or not the window
    ever reached the screen, and ``_close`` measured from *creation* time and
    ``report`` printed the result as "on screen". Both are wrong for the same
    reason: a top-level window on Windows is routinely created hidden and shown
    later, or created hidden and never shown at all. ``PseudoConsoleWindow`` is
    the case that matters here -- ConPTY creates one as plumbing for a console
    that has no window -- and ``CASCADIA_NOTIFICATION_WINDOW_CLASS`` is another.
    So the popup counts and the durations this instrument reported, which are
    the evidence the whole popup investigation rests on, could both
    overstate.

    Visibility is now tracked rather than assumed. ``counts`` -- the tally
    printed at the end of a run, and the number an evidence document quotes --
    holds only windows that were observed on screen. Everything created is still
    *recorded*, because a console window that never appeared is a fact about the
    workflow worth having; it is simply not counted as something the user saw.
    ``visible_seconds`` is the accumulated time between show and hide, so it
    means what its label says, and the old creation-to-destruction figure is
    kept beside it under the name it actually had: ``lifetime_seconds``.

    **Which clock those durations are read from is the difference between a
    measurement and a coincidence.** They were read from ``time.monotonic()``
    at the moment the callback ran -- which is the moment this thread got
    round to the message, not the moment the window appeared. Under
    ``WINEVENT_OUTOFCONTEXT`` those are different times, and on a busy desktop
    they are different by however far behind the drain is. A SHOW and a HIDE
    that were 80 ms apart on screen, sitting in the queue together and
    dispatched back to back, measured as a millisecond of queue-processing
    latency; the same pair separated by a batch boundary measured as far too
    long. Both are the observer's own scheduling, reported as the user's
    experience.

    Windows hands every WinEvent its own ``dwmsEventTime``, so the durations
    are now taken from that. The dispatch clock is kept as a fallback for a
    call that arrives without one, and each end record says which was used, in
    ``timing_source``, because a duration whose provenance is not written down
    is the thing this whole finding is about.

    **Having the right clock is not yet having the right stamp**, and two
    cases took a stamp from somewhere other than the event they were timing.
    A CREATE read after its window was already on screen has
    no SHOW stamp to start the visible interval from, and using its own put
    the queue latency back into the measurement it had just been taken out of;
    that start is now provisional, replaced when the real SHOW is dispatched
    behind it and labelled ``create_event`` if it never is. And a window still
    up at the cutoff was closed against the last stamp any WinEvent carried,
    which on a quiet desktop is not the end of the recording at all; it is
    closed against a live tick instead.

    ``finish()`` closes the books at session stop. A window still on screen
    when the recording ends used to leave its interval open, so its time
    contributed nothing at all.
    """

    def __init__(
        self,
        table: ProcessTable,
        sink,
        *,
        label: str | None = None,
        all_windows: bool = False,
        session: str | None = None,
    ) -> None:
        self._table = table
        self._sink = sink
        self._label = label
        self._all = all_windows
        #: Every record this watcher writes carries ``<session>:<n>``, which is
        #: the identity an HWND is not: Windows reuses a window handle as
        #: freely as it reuses a pid, and the evidence file is appended to
        #: across runs.
        self._session = session or f"{int(time.time())}-{os.getpid()}"
        self._occurrences = 0
        #: The most recent event stamp seen. A last resort only: ``finish()``
        #: closes an open interval against a live ``GetTickCount``, because the
        #: last unrelated WinEvent is not the end of the recording and on a
        #: quiet desktop is not even close to it.
        self._last_event_ms: int | None = None
        self._known: dict[int, dict] = {}
        #: Visibility bookkeeping, kept beside the record rather than inside it:
        #: a record is one observation and is written once, while this is state
        #: that changes across a window's life.
        self._state: dict[int, dict] = {}
        #: Windows observed on screen, per owning image. The popup count.
        self.counts: Counter = Counter()
        #: Windows recorded at all, visible or not. Always >= sum(counts).
        self.recorded = 0
        #: How many records were assembled while the handle stopped naming a
        #: window. Counted so the run's exit status can reflect
        #: it, not only the file.
        self.raced_records = 0
        #: The stamp `finish()` closed open intervals against, or None while
        #: the observation is still running.
        self.finished_at: int | None = None
        #: Events whose handling raised. See ``note_dispatch_failure``.
        self.dispatch_failures = FailureLedger()

    def note_raced(self) -> None:
        """One record whose handle stopped naming a window mid-assembly."""

        self.raced_records += 1

    def on_event(self, event: int, hwnd: int, event_ms: int | None = None) -> None:
        """Record one WinEvent. ``event_ms`` is Windows' own ``dwmsEventTime``.

        Optional because a caller may not have one -- and because the answer
        when it is missing must be a documented fallback rather than an
        exception in an observer.
        """

        # Noted before any filtering: `finish()` closes an open interval
        # against the last moment this session knows about, and a window this
        # watcher is not recording still tells it what time it is.
        self._note(event_ms)
        if event in (EVENT_OBJECT_DESTROY, EVENT_OBJECT_HIDE):
            self._close(event, hwnd, event_ms)
            return
        if not hwnd or not _user32.IsWindow(wintypes.HWND(hwnd)):
            return
        if _user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT) != hwnd:
            return  # a control, not a window anybody could mistake for a popup
        if hwnd in self._known:
            if event == EVENT_OBJECT_SHOW:
                self._mark_shown(hwnd, event_ms)
            return
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        window_class = _class_name(hwnd)
        is_console = window_class in CONSOLE_CLASSES
        if not is_console and not self._all:
            return
        owner = self._table.get(int(pid.value))
        visible = bool(_user32.IsWindowVisible(wintypes.HWND(hwnd)))
        # Everything above is a separate question asked of a handle that
        # belongs to another process, and Windows documents that such a window
        # may be destroyed -- and its handle reused -- between any two of them.
        # The race cannot be closed from here: there is no API that reads
        # class, pid, visibility and rectangle atomically, and
        # `IsWindow` is explicitly not a guarantee about the next call.
        #
        # So it is detected instead of assumed away. A handle that no longer
        # names a window by the time the reads are finished may have been
        # answering for two different windows while they ran, and a record
        # built from it is marked rather than quietly trusted. Unknown stays
        # unknown (`AGENTS.md` invariant 4): the record is still written --
        # dropping it would lose a window that really did appear -- but nothing
        # downstream may read it as a clean attribution.
        title = _window_text(hwnd)
        rect = _rect(hwnd)
        # After the last read, not before it. Placed earlier,
        # it could not see a window that vanished during the title or rectangle
        # calls -- which is most of the reads it was supposed to cover.
        still_a_window = bool(_user32.IsWindow(wintypes.HWND(hwnd)))
        record = {
            "kind": "window",
            "timestamp": _now(),
            "monotonic": time.monotonic(),
            "label": self._label,
            "event": _EVENT_NAMES.get(event, hex(event)),
            "hwnd": hwnd,
            "occurrence": self._next_occurrence(),
            "event_ms": None if event_ms is None else int(event_ms) & _TICK_MASK,
            "window_class": window_class,
            "title": title,
            "console_class": is_console,
            "visible": visible,
            "ever_visible": False,
            "rect": rect,
            "pid": int(pid.value),
            "image": owner.get("image"),
            "command_line": owner.get("command_line"),
            "parent_pid": owner.get("parent_pid"),
            # From the generation just described, not from a fresh lookup of
            # the same number: one record may not mix one process's identity
            # with another's ancestry.
            "ancestors": self._table.ancestors(int(pid.value), start=owner),
            # True when the handle stopped naming a window while this record
            # was being assembled, so its fields may describe more than one
            # window. False is the ordinary case and is
            # recorded explicitly, so a file written by an older build -- where
            # the field is absent entirely -- is not mistaken for one that
            # checked and found nothing wrong.
            "observation_raced": not still_a_window,
        }
        if not still_a_window:
            self.note_raced()
        self._known[hwnd] = record
        self._state[hwnd] = {
            "shown_at": None,
            "shown_at_ms": None,
            "visible_total": 0.0,
            "event_timed": True,
            # Whether the open interval started from a stamp belonging to a
            # different event, and whether any closed interval did.
            "shown_borrowed": False,
            "borrowed_ever": False,
        }
        self.recorded += 1
        self._sink(record)
        # Two ways a window is already on screen at first sighting, and they
        # are not the same measurement. A SHOW carries the stamp of the moment
        # the window appeared. A CREATE for a window that is *already* visible
        # carries the stamp of the creation, and the show happened somewhere
        # between that and now -- so it is counted, because neither case will
        # produce a further SHOW that could be, but its stamp is marked as
        # standing in for one.
        if event == EVENT_OBJECT_SHOW:
            self._mark_shown(hwnd, event_ms)
        elif visible:
            self._mark_shown(hwnd, event_ms, borrowed=True)

    def note_dispatch_failure(self, error: BaseException) -> None:
        """Count an event whose handling raised, since it cannot be re-raised.

        The WinEvent callback runs on Windows' behalf and may not raise into
        it, so the exception has to be swallowed. What must not be swallowed
        with it is the fact that a message was dispatched and produced no
        record: the drain counts that message as dispatched, so the stop
        record could report ``queue_drained: true`` and ``dropped_events: 0``
        for a run in which windows appeared and went unattributed. Counted
        here by exception type, and reported in the stop record beside the
        drain's own numbers, so the two are read together.
        """

        self.dispatch_failures.note(type(error).__name__)

    def finish(self, stop_ms: int | None = None) -> int:
        """Close the books on every window still on screen. Returns how many.

        A window that was up when the recording ended had an open interval and
        no end record, so its time on screen was simply not counted -- and
        `report` had nothing to print for it but the phrase "still up". An end
        record is written instead, with ``event: recording_end``, which is a
        different fact from a destroy and is labelled as one.

        **Against what, is the whole question.** These intervals were closed
        against ``_last_event_ms``, the last stamp any WinEvent carried -- and
        the window that is still up at the cutoff is typically the quiet one,
        the long-lived console host nothing has happened to. Its own SHOW may
        be the last stamp this session saw, in which case it measured zero;
        on a desktop that went quiet minutes ago it measured the time up to
        whatever last twitched. Either way the number was about other windows'
        activity rather than about this one. The recording ends
        *now*, so the interval is closed against a live ``GetTickCount``, which
        is the same clock ``dwmsEventTime`` is read from. ``stop_ms`` overrides
        it for a caller that has an explicit stop stamp -- and the last event
        stamp remains only as the last resort, for a build with no tick to
        read.
        """

        open_windows = [hwnd for hwnd in self._known if self._state[hwnd]["shown_at"] is not None]
        if not open_windows:
            return 0
        if stop_ms is None:
            stop_ms = _tick_count()
        if stop_ms is None:  # pragma: no cover -- only where there is no tick to read
            stop_ms = self._last_event_ms
        #: The stamp every open interval was closed against, kept so that
        #: *which* clock reading was used is observable rather than inferred
        #: from a duration. The caller passing `stop_ms` is the
        #: whole of that fix, and a fix that cannot be asserted is one the next
        #: refactor removes.
        self.finished_at = stop_ms
        for hwnd in open_windows:
            self._close(_RECORDING_END, hwnd, stop_ms)
        return len(open_windows)

    def _next_occurrence(self) -> str:
        self._occurrences += 1
        return f"{self._session}:{self._occurrences}"

    # -- internals ----------------------------------------------------------

    def _mark_shown(self, hwnd: int, event_ms: int | None = None, *, borrowed: bool = False) -> None:
        """Note that the window is on screen, and from whose stamp.

        ``borrowed`` says that ``event_ms`` is not the show's own: it belongs
        to a CREATE that was read after the window had already appeared, so it
        predates the show by however long the notification sat in the queue.
        That is a starting point, not a measurement, and it is held as
        provisional -- when the queued SHOW is dispatched behind it, its stamp
        replaces the provisional one and the interval is measured from the
        moment the window actually appeared. Nothing has been measured from
        the provisional stamp until an interval closes, so the replacement
        costs nothing.
        """

        first = self._known[hwnd]
        state = self._state[hwnd]
        self._note(event_ms)
        if state["shown_at"] is not None:
            if state["shown_borrowed"] and not borrowed:
                state["shown_at"] = time.monotonic()
                state["shown_at_ms"] = None if event_ms is None else int(event_ms) & _TICK_MASK
                state["shown_borrowed"] = False
            return  # already on screen; a repeated SHOW is not a second popup
        state["shown_at"] = time.monotonic()
        state["shown_at_ms"] = None if event_ms is None else int(event_ms) & _TICK_MASK
        state["shown_borrowed"] = borrowed
        if not first["ever_visible"]:
            first["ever_visible"] = True
            self.counts[first["image"] or "unknown"] += 1
        self._sink(
            {
                **first,
                "event": "show",
                "timestamp": _now(),
                "visible": True,
                # The stamp this interval starts from, and which event it came
                # from -- ``first``'s own ``event_ms`` is the first sighting's,
                # which for a SHOW that followed a CREATE is a different moment
                # entirely.
                "event_ms": state["shown_at_ms"],
                "show_stamp": "create_event" if borrowed else "event",
            }
        )

    def _note(self, event_ms: int | None) -> None:
        if event_ms is not None:
            self._last_event_ms = int(event_ms) & _TICK_MASK

    def _interval(self, start: float, start_ms: int | None, now: float, event_ms: int | None) -> tuple[float, bool]:
        """How long the window was up, and whether the event clock said so.

        Both stamps or neither: mixing them would subtract a ``GetTickCount``
        reading from a ``monotonic()`` one, whose epochs have nothing to do
        with each other, and produce a number with no meaning at all rather
        than an imprecise one.
        """

        if start_ms is not None and event_ms is not None:
            return _elapsed_ms(start_ms, event_ms), True
        return now - start, False

    @staticmethod
    def _timing_source(state: dict, lifetime_from_events: bool) -> str:
        """Which clock produced the durations, and from which events.

        ``event`` is Windows' own ``dwmsEventTime`` for the events being
        measured: when the window actually appeared and when it went away.
        ``dispatch`` is when this process got round to reading the message,
        which on a busy desktop is a different thing.

        ``create_event`` is the honest third answer. The stamps are event
        stamps, but a visible interval started from a CREATE standing in for a
        SHOW this observer was never told the time of, so ``visible_seconds``
        is an upper bound -- the true show is somewhere between that stamp and
        the moment the CREATE was read. Calling that ``event`` claimed a
        provenance the number did not have.
        """

        if not (state["event_timed"] and lifetime_from_events):
            return "dispatch"
        return "create_event" if state["borrowed_ever"] else "event"

    def _close(self, event: int, hwnd: int, event_ms: int | None = None) -> None:
        first = self._known.get(hwnd)
        if first is None:
            return
        state = self._state[hwnd]
        self._note(event_ms)
        now = time.monotonic()
        if state["shown_at"] is not None:
            # A window may be hidden and shown again; the visible time is the
            # sum of those intervals, not the span between the first and last.
            seconds, from_events = self._interval(
                state["shown_at"], state["shown_at_ms"], now, event_ms
            )
            state["visible_total"] += seconds
            state["event_timed"] = state["event_timed"] and from_events
            # Now that something has been measured from it, a provisional
            # start stamp is part of this window's history for good.
            state["borrowed_ever"] = state["borrowed_ever"] or state["shown_borrowed"]
            state["shown_at"] = None
            state["shown_at_ms"] = None
            state["shown_borrowed"] = False
        lifetime, lifetime_from_events = self._interval(
            first["monotonic"], first["event_ms"], now, event_ms
        )
        self._sink(
            {
                "kind": "window_end",
                "timestamp": _now(),
                "label": self._label,
                "event": _EVENT_NAMES.get(event, hex(event)),
                "hwnd": hwnd,
                "occurrence": first["occurrence"],
                "window_class": first["window_class"],
                "console_class": first["console_class"],
                "pid": first["pid"],
                "image": first["image"],
                "ever_visible": first["ever_visible"],
                "visible_seconds": round(state["visible_total"], 3),
                "lifetime_seconds": round(lifetime, 3),
                "timing_source": self._timing_source(state, lifetime_from_events),
            }
        )
        if event == EVENT_OBJECT_DESTROY:
            self._known.pop(hwnd, None)
            self._state.pop(hwnd, None)


_WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)


#: How many messages one drain may dispatch before the deadline is re-checked.
#:
#: `PeekMessage` rather than `GetMessage` so the loop reaches its own deadline
#: without a second thread posting `WM_QUIT` into it -- but the deadline used to
#: be checked only *outside* the drain, and the drain ran until the queue was
#: empty. A queue that is refilled faster than it is drained therefore never
#: ends, and a `--seconds 300` run would not stop at 300 seconds; the run that
#: measured 3,559 console windows in 300 seconds was already within an order of
#: magnitude of the event rate that does it.
#:
#: 256 is large enough that a quiet desktop drains in one pass and small enough
#: that the deadline is honoured to well under a millisecond of dispatch work.
_DRAIN_BATCH = 256


def _drain(message, deadline: float) -> tuple[int, bool]:
    """Dispatch at most ``_DRAIN_BATCH`` queued messages, stopping at ``deadline``.

    Returns how many were dispatched and whether ``WM_QUIT`` was seen. Split out
    of ``watch()`` so that the bound is assertable without a desktop: a stub
    ``PeekMessageW`` that never returns false stands in for the sustained event
    stream, which is the condition the bound exists for and the one a live test
    could not reliably produce.
    """

    handled = 0
    while _user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
        if message.message == WM_QUIT:
            return handled, True
        _user32.TranslateMessage(ctypes.byref(message))
        _user32.DispatchMessageW(ctypes.byref(message))
        handled += 1
        if handled >= _DRAIN_BATCH or time.monotonic() >= deadline:
            break
    return handled, False


#: The budget for the final drain: how many queued messages may still be
#: dispatched after the observation window closes, and for how long.
#:
#: `WINEVENT_OUTOFCONTEXT` means Windows does not call into this process at the
#: moment of the event -- it *queues* a message and the callback runs when this
#: thread reads it. So at the deadline there is a backlog, and it is largest on
#: exactly the run that matters most: the busy one. The loop used to dispatch
#: at most one more message and then unhook, discarding the rest without a word.
#: Every discarded message is a window that appeared
#: and was never attributed, in a file that reads as though it had been.
#:
#: 200,000 at roughly 20 microseconds of dispatch each is a few seconds of work;
#: the five-second wall is what actually binds, and it is deliberately generous
#: against a `--seconds 300` observation.
_FINAL_DRAIN_LIMIT = 200_000
_FINAL_DRAIN_SECONDS = 5.0

#: How far a run will go to *count* what it could not dispatch. Removing a
#: message without dispatching it costs a `PeekMessageW` and nothing else, so
#: this can be much larger than the dispatch budget. Beyond it the count is
#: reported as a lower bound rather than as a number, which is the honest
#: answer: a recorded gap is worth more than a silent one, and a recorded gap
#: that overstates its own precision is worth less than either.
_DROP_COUNT_LIMIT = 1_000_000


def _final_drain(message, *, deadline: float, limit: int | None = None) -> dict:
    """Dispatch the queue Windows already holds, then count what is left.

    Called once, after the hook is released, so the queue it works on is
    finite: nothing can be added to it. Returns the three facts the session's
    stop record needs to make the run's completeness checkable by a reader --
    how many were dispatched, how many were dropped, and whether the drop
    count is exact.

    ``WM_QUIT`` is discarded rather than dispatched. It is not a window event
    and there is no message loop left for it to end.
    """

    limit = _FINAL_DRAIN_LIMIT if limit is None else limit
    dispatched = 0
    complete = True
    while _user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
        if message.message != WM_QUIT:
            _user32.TranslateMessage(ctypes.byref(message))
            _user32.DispatchMessageW(ctypes.byref(message))
            dispatched += 1
        if dispatched >= limit or time.monotonic() >= deadline:
            complete = False
            break

    dropped = 0
    exact = True
    if not complete:
        # The budget ran out with messages still queued. They cannot be
        # attributed any more, but they can be counted, and a number in the
        # stop record is the difference between a gap and a silence.
        while _user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
            dropped += 1
            if dropped >= _DROP_COUNT_LIMIT:
                exact = False
                break
        complete = dropped == 0

    return {
        "final_drain_dispatched": dispatched,
        "queue_drained": complete,
        "dropped_events": dropped,
        "dropped_events_exact": exact,
    }


#: What `watch()` and `report()` return when the capture itself is not sound.
#: Distinct from 2 (bad arguments) and from 0, which must mean "the observation
#: ran and this is what it saw" and nothing weaker.
EXIT_UNSOUND_CAPTURE = 3

#: A newline, named so the multi-line diagnostics below stay readable.
_NL = chr(10)


def session_health(record: dict) -> list[str]:
    """Why this session's file may not be a complete record, or [] if it is.

    Every one of these was already written into the stop record
    and printed to stderr, and *none* of them changed an exit code: a run whose
    hook leaked, whose callback raised on every event, or whose queue was never
    drained returned 0 and printed "0 window(s) recorded", which reads exactly
    like a clean desktop. For an instrument whose entire purpose is to support
    the claim "no window appeared", that is the most dangerous shape a bug can
    take -- the failure and the finding are the same sentence.

    So the same facts are read back here, by one function, used both at the end
    of a capture and by `--report` over a file written earlier. A reader who
    never saw the terminal still cannot mistake a broken capture for a quiet
    one.
    """

    reasons: list[str] = []
    # Absent is not sound. A stop record written by an older
    # build, or truncated, carries no evidence that the capture completed --
    # and "no evidence" must not read the same as "evidence of success", which
    # is the entire defect this predicate exists to close.
    for field in ("queue_drained", "sampler_stopped"):
        if record.get(field) is None:
            reasons.append(f"the stop record does not say whether {field.replace('_', ' ')}")
    if record.get("queue_drained") is False:
        dropped = record.get("dropped_events")
        exact = record.get("dropped_events_exact")
        suffix = "" if exact else "+"
        reasons.append(f"{dropped}{suffix} queued event(s) were never dispatched")
    if record.get("sampler_stopped") is False:
        reasons.append("the process sampler did not stop cleanly")
    if record.get("raced_records"):
        reasons.append(
            f"{record['raced_records']} record(s) were assembled while the handle "
            "stopped naming a window"
        )
    for field, what in (
        ("cleanup_failures", "cleanup"),
        ("callback_failures", "the event callback"),
        ("sample_failures", "process sampling"),
    ):
        failures = record.get(field)
        if failures:
            reasons.append(f"{what} failed during the run: {failures}")
    return reasons


def watch(seconds: float, out_path: Path, *, label: str | None, all_windows: bool) -> int:
    """Observe for ``seconds``, appending one JSON record per line to ``out_path``.

    Every resource acquired here -- the output file, the sampler thread, the
    hook -- is registered with a single ``ExitStack`` in the same statement that
    acquires it, because they used not to be. The hook was installed and the
    session's first record written *before* the ``try`` block began, so an
    ``OSError`` on that first write (a full disk, a read-only path, a directory
    that vanished) left a system-wide WinEvent hook installed and the sampler
    thread running, with no code path that released either.
    Acquire, then register, leaves no window in which a resource exists unowned.

    **Shutdown is an ordered sequence, and the order is load-bearing.** Unhook
    first, so no further event can be queued. Then the sampler, and *check*
    that it stopped. Then, last, the stop record -- because that record's job
    is to describe the shutdown, and it used to be written in the middle of one.
    Its `cleanup_failures` could not include a handle
    the sampler failed to close while it was finishing, and it had no way to
    say that the two-second join had timed out and the sampler was still
    running. Both are now facts in the file.

    The sequence is written twice, deliberately: registered with the
    ``ExitStack`` in reverse, which is the order an exception unwinds it in,
    and called explicitly on the ordinary path, where each step needs the
    previous step's result. ``_Once`` makes the two spellings the same
    execution.
    """

    table = ProcessTable()

    with ExitStack() as stack:
        handle = stack.enter_context(out_path.open("a", encoding="utf-8"))

        def sink(record: dict) -> None:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()  # the observed run may be killed; a buffered record is a lost one

        watcher = Watcher(table, sink, label=label, all_windows=all_windows)
        #: Filled in by the shutdown steps below, read by the stop record. A
        #: dict rather than locals because the record is written from a
        #: callback that closes over this frame.
        outcome: dict = {
            "started": False,
            "sampler_stopped": None,
            # None until the final drain runs: an evidence file that says
            # nothing about its own completeness is exactly the defect.
            "final_drain_dispatched": None,
            "queue_drained": None,
            "dropped_events": None,
            "dropped_events_exact": None,
            "windows_open_at_stop": None,
            # True when the stop record itself could not be written. The file
            # then cannot say how complete it is, and a run
            # that ends without saying so must not exit 0.
            "stop_record_failed": False,
        }

        def write_stop_record() -> None:
            if not outcome["started"]:
                return  # nothing was observed; a stop record would describe no session
            try:
                sink(
                    {
                        "kind": "session",
                        "timestamp": _now(),
                        "label": label,
                        "event": "stop",
                        # Two numbers, because they are two facts: how many windows
                        # existed, and how many of them the user could have seen.
                        "windows": watcher.recorded,
                        "visible_windows": sum(watcher.counts.values()),
                        # None means the shutdown did not reach this step, which
                        # is itself worth reading in an evidence file.
                        "sampler_stopped": outcome["sampler_stopped"],
                        # How complete this file is, said by the file itself.
                        "final_drain_dispatched": outcome["final_drain_dispatched"],
                        "queue_drained": outcome["queue_drained"],
                        "dropped_events": outcome["dropped_events"],
                        "dropped_events_exact": outcome["dropped_events_exact"],
                        "windows_open_at_stop": outcome["windows_open_at_stop"],
                        "cleanup_failures": CLEANUP.failures,
                        # What the instrument itself failed to do. Read beside
                        # the drain numbers above, which count a message as
                        # dispatched whether or not handling it produced a
                        # record, and beside `sampler_stopped`, which says the
                        # sampler finished and not that it was sampling
                        # successfully while it ran.
                        "raced_records": watcher.raced_records,
                        "callback_failures": watcher.dispatch_failures.failures,
                        "sample_failures": table.sample_failures.failures,
                    }
                )
            except OSError as error:
                # The write that failed is very likely the same write that
                # brought the run down. Raising here would replace the original
                # cause with this one, which is the harder failure to diagnose.
                print(f"windowwatch: the stop record could not be written: {error}", file=sys.stderr)
                # Printing was all this did, so a run whose
                # file never learned how complete it was still exited 0. The
                # failure is state now, and the exit status reads it.
                outcome["stop_record_failed"] = True

        stop_record = _Once(write_stop_record)
        stack.callback(stop_record)

        table.start()

        def stop_sampler() -> bool:
            outcome["sampler_stopped"] = table.stop()
            return outcome["sampler_stopped"]

        stop_table = _Once(stop_sampler)
        stack.callback(stop_table)

        def callback(_hook, event, hwnd, id_object, id_child, _thread, event_ms) -> None:
            if id_object != OBJID_WINDOW or id_child != 0:
                return
            try:
                # `event_ms` is `dwmsEventTime`: when the event happened, not
                # when this thread got round to the message.
                watcher.on_event(int(event), int(hwnd) if hwnd else 0, int(event_ms))
            except Exception as error:  # noqa: BLE001 -- a hook callback may never raise into Windows
                watcher.note_dispatch_failure(error)

        # Held in a local for the whole ``with`` block on purpose: Windows keeps
        # only the raw function pointer, so if this object were collected the
        # next event would call into freed memory.
        proc = _WINEVENTPROC(callback)
        hook = _user32.SetWinEventHook(
            EVENT_OBJECT_CREATE,
            EVENT_OBJECT_HIDE,
            None,
            proc,
            0,
            0,
            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
        )
        if not hook:
            print("SetWinEventHook failed; nothing was observed", file=sys.stderr)
            return 2  # the stack still stops the sampler and closes the file
        release_hook = _Once(lambda: CLEANUP.unhook(hook))
        stack.callback(release_hook)

        sink(
            {
                "kind": "session",
                "timestamp": _now(),
                "label": label,
                "event": "start",
                "seconds": seconds,
                "all_windows": all_windows,
                "watcher_pid": os.getpid(),
            }
        )
        outcome["started"] = True
        print(f"windowwatch: recording to {out_path} for {seconds:g}s (pid {os.getpid()})", flush=True)
        message = wintypes.MSG()
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                handled, quit_seen = _drain(message, deadline)
                if quit_seen:
                    break
                # Only yield when the queue actually ran dry. Sleeping after a
                # full batch would throttle the drain to 51,200 messages a
                # second under exactly the load that makes the batch matter.
                if handled == 0:
                    time.sleep(0.005)
        except KeyboardInterrupt:
            pass

        # The observation is over. Unhook before anything else, so nothing
        # further can be queued -- which is what makes the backlog below a
        # finite thing to drain rather than a moving target. Then dispatch it,
        # then stop and verify the sampler (the drain needs the process table
        # alive to attribute what it dispatches), and let the stack write the
        # stop record after all of it.
        release_hook()
        # The instant observation actually ended, read before the drain rather
        # than after it. `finish()` closes each still-open
        # interval against a live `GetTickCount`, and the drain below can run
        # for `_FINAL_DRAIN_SECONDS`; a tick read after it would credit every
        # open window with that span, although once the hook is released a hide
        # or destroy inside it cannot be observed at all. The honest end of the
        # observation is the unhook, so that is the stamp the close uses.
        stop_ms = _tick_count()
        outcome.update(_final_drain(message, deadline=time.monotonic() + _FINAL_DRAIN_SECONDS))
        if not outcome["queue_drained"]:
            print(
                f"windowwatch: {outcome['dropped_events']}"
                f"{'' if outcome['dropped_events_exact'] else '+'} queued event(s) could not be drained",
                file=sys.stderr,
            )
        # Windows still on screen at the cutoff have an open interval and no
        # end record; closing them is the difference between a duration and a
        # silence.
        outcome["windows_open_at_stop"] = watcher.finish(stop_ms=stop_ms)
        stop_table()
        # ``proc`` stays referenced by this frame while the stack unwinds, so
        # the unhook above cannot race the callback object's collection.

    failures = CLEANUP.failures
    if failures:
        print(f"windowwatch: cleanup did not fully succeed: {failures}", file=sys.stderr)
    observation_failures = {
        "callback": watcher.dispatch_failures.failures,
        "sample": table.sample_failures.failures,
    }
    if any(observation_failures.values()):
        print(f"windowwatch: the observer itself failed during the run: {observation_failures}", file=sys.stderr)
    visible = sum(watcher.counts.values())
    print(f"windowwatch: {watcher.recorded} window(s) recorded, {visible} of them seen on screen")
    for image, count in watcher.counts.most_common():
        print(f"  {count:4d}  {image}")
    # The exit code carries the same judgement the stop record does: a
    # capture that lost events, leaked a hook or failed to sample is
    # not a quiet desktop, and a caller that reads only the status must not be
    # told that it was.
    unsound = session_health({
        "queue_drained": outcome["queue_drained"],
        "dropped_events": outcome["dropped_events"],
        "dropped_events_exact": outcome["dropped_events_exact"],
        "sampler_stopped": outcome["sampler_stopped"],
        "raced_records": watcher.raced_records,
        "cleanup_failures": failures,
        "callback_failures": observation_failures["callback"],
        "sample_failures": observation_failures["sample"],
    })
    if outcome["stop_record_failed"]:
        unsound.append("the stop record could not be written, so the file cannot say how complete it is")
    if not outcome["started"]:
        unsound.append("the observation never started, so this file is not a record of a quiet desktop")
    if unsound:
        print(
            "windowwatch: THIS CAPTURE IS NOT SOUND and is not evidence of "
            "what was on screen:" + _NL + "  - " + (_NL + "  - ").join(unsound),
            file=sys.stderr,
        )
        return EXIT_UNSOUND_CAPTURE
    return 0


# --------------------------------------------------------------------- report


def _printable(value):
    r"""Render a captured string with its control characters made visible.

    Nothing in the capture path introduces an escape, and that is asserted
    rather than asserted-to: ``json.dumps`` writes a backslash as ``\\`` and
    reading the line back gives the same string, so a Windows path survives the
    round trip byte for byte. The escapes that corrupted an evidence document
    -- ``\app`` into
    BEL + ``pp``, ``\versions`` into VT + ``ersions``, ``\0.2.0`` into NUL +
    ``.2.0`` -- were applied on the way *into* the document, by C-style escape
    expansion over a report this instrument had printed correctly.

    What this file can do about that is the other direction, which is a real
    hazard rather than a hypothesis: a window title and a command line are
    text this observer copied out of somebody else's process, they may contain
    a genuine control character, and a human pastes this output into a tracked
    Markdown file. One raw NUL there makes that file binary to Git and
    invisible to ``rg``. So a control
    character is printed as ``\xNN`` and a backslash is left exactly alone.
    """

    if not isinstance(value, str):
        return value
    return "".join(char if " " <= char < "\x7f" or char > "\x7f" else f"\\x{ord(char):02x}" for char in value)


def _key(record: dict):
    """What identifies the window instance a record belongs to.

    The ``occurrence`` this observer stamps on every record it writes, when
    the file has one. A file recorded before that field existed falls back to
    the HWND, and ``_occurrences`` below then does the separating by reading
    the lifecycle in order -- which is why the fallback is a correctness
    fallback and not just a compatibility one.
    """

    return record.get("occurrence") or ("hwnd", record.get("hwnd"))


def _occurrences(records: list[dict]) -> list[dict]:
    """One entry per window *instance*, in the order each was first seen.

    **An HWND is not an identity, and this report used to treat it as one.**
    ``ends``, ``shown`` and ``seen`` were each a flat dict or set keyed by HWND
    across the whole file. Windows reuses a window handle after the window is
    destroyed exactly as freely as it reuses a pid -- and ``--out`` appends, so
    one file routinely holds several sessions and makes the collision likelier
    still. The consequences were all silent: a second
    window with a recycled HWND was skipped by ``seen`` and never printed at
    all; if it was printed it inherited the first one's visibility, so a
    console window that flashed could be reported as never on screen or the
    reverse; and it could be given the earlier window's end record, and with
    it that window's duration.

    Two mechanisms, because one of them has to work on a file already written.
    Records this observer writes now carry ``occurrence`` -- session id plus a
    per-session counter -- and are grouped by it. Records without one are
    grouped by reading the stream in order: an instance is open from its first
    sighting until its ``destroy``, a session ``start`` record closes every
    instance still open (which is what separates appended sessions), and the
    next sighting of that HWND therefore begins a new instance.

    Each entry is ``{"first", "shown", "end"}``: the record that introduced
    the window, whether anything ever said it reached the screen, and the
    record that ended it if one did.
    """

    entries: list[dict] = []
    open_instances: dict = {}
    for record in records:
        kind = record.get("kind")
        if kind == "session":
            if record.get("event") == "start":
                # A new session cannot continue the previous one's windows,
                # however familiar their handles look.
                open_instances.clear()
            continue
        if kind not in ("window", "window_end"):
            continue
        key = _key(record)
        entry = open_instances.get(key)
        if kind == "window":
            if entry is None:
                entry = {"first": record, "shown": False, "end": None}
                entries.append(entry)
                open_instances[key] = entry
            elif record.get("event") == "show":
                # It is up again, so whatever ended it did not.
                entry["end"] = None
            # Three independent statements of the same fact, because a run can
            # be cut short and a record lost: a `show` record, a first sighting
            # that already found the window visible, and `ever_visible` on the
            # end record below. Any of them makes it a window somebody saw.
            if record.get("event") == "show" or record.get("visible"):
                entry["shown"] = True
        else:  # window_end
            if entry is None:
                continue  # an end whose beginning is not in this file
            entry["end"] = record
            entry["shown"] = entry["shown"] or bool(record.get("ever_visible"))
            if record.get("event") == "destroy":
                del open_instances[key]  # the handle is free for somebody else now
    return entries


def report(path: Path, stream=None) -> int:
    """Print the causal map from window to process, one block per window.

    Counts and durations distinguish a window that was *created* from one that
    was *on screen*. The distinction is the whole value of
    the report: a `PseudoConsoleWindow` created as ConPTY plumbing and never
    shown is a fact about the workflow, but it is not a flash the user saw,
    and a summary that adds the two together is evidence for a claim nobody
    made.

    One block per window *instance*, not per HWND: see ``_occurrences``, which
    is where handle reuse is handled. An evidence file is appended to, and
    Windows reuses window handles, so "the window with this handle" is a
    question with more than one answer in most files this reads.
    """

    out = stream if stream is not None else sys.stdout
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    windows = 0
    consoles = 0
    visible_consoles = 0
    for entry in _occurrences(records):
        record = entry["first"]
        windows += 1
        is_console = bool(record.get("console_class"))
        appeared = entry["shown"]
        consoles += is_console
        visible_consoles += is_console and appeared
        marker = "  [CONSOLE]" if is_console else ""
        if not appeared:
            marker += "  [NEVER ON SCREEN]"
        print(
            f"\n{record['timestamp']}  hwnd=0x{record['hwnd']:x}  class={_printable(record['window_class'])}{marker}",
            file=out,
        )
        if record.get("label"):
            print(f"  trigger        {record['label']}", file=out)
        print(f"  title          {_printable(record.get('title')) or '(none)'}", file=out)
        print(f"  pid            {record['pid']}", file=out)
        print(f"  image          {_printable(record.get('image'))}", file=out)
        print(f"  command line   {_printable(record.get('command_line'))}", file=out)
        print(f"  parent pid     {record.get('parent_pid')}", file=out)
        for depth, ancestor in enumerate(record.get("ancestors") or [], start=1):
            label = _printable(ancestor.get("image") or ancestor.get("note") or "?")
            print(f"  ancestor {depth:<2}    {ancestor.get('pid')}  {label}", file=out)
            if ancestor.get("command_line"):
                print(f"                 {_printable(ancestor['command_line'][:200])}", file=out)
        end = entry["end"]
        lifetime = end.get("lifetime_seconds") if end else None
        if appeared and end and end["event"] == "recording_end":
            print(
                f"  on screen      {end['visible_seconds']}s of a {lifetime}s lifetime "
                f"(still up when the recording ended)",
                file=out,
            )
        elif appeared and end:
            print(
                f"  on screen      {end['visible_seconds']}s of a {lifetime}s lifetime (ended by {end['event']})",
                file=out,
            )
        elif appeared:
            print("  on screen      still up when the recording ended", file=out)
        elif end:
            print(f"  on screen      never; existed {lifetime}s (ended by {end['event']})", file=out)
        else:
            print("  on screen      never, up to the end of the recording", file=out)
    print(
        f"\n{windows} window(s), {consoles} of them console-class, "
        f"{visible_consoles} of those ever on screen, in {path}",
        file=out,
    )
    # A reader of this report may not have to know that the capture behind it
    # went wrong. Every stop record in the file is judged, and
    # an unsound session is named here rather than left to be noticed in a
    # terminal nobody kept -- because the shape this instrument fails in,
    # "0 window(s)", is identical to the shape it succeeds in when the desktop
    # really was quiet.
    # A record marked `observation_raced` was assembled from a handle that had
    # stopped naming a window by the time the reads finished, so its fields may
    # describe more than one window. Surfaced here because a
    # flag nothing reads is not a fix: the whole point is that a reader of this
    # report cannot mistake a possibly-conflated attribution for a clean one.
    # `is True`, not truthiness: a record written before the
    # detector existed has no field at all, and treating that as `false` would
    # report "the check ran and found nothing" about a build that never
    # checked. Those are counted separately below.
    windows = [r for r in records if r.get("kind") == "window"]
    raced = [r for r in windows if r.get("observation_raced") is True]
    unchecked = [r for r in windows if "observation_raced" not in r]
    if raced:
        print(
            _NL + f"WARNING: {len(raced)} window record(s) were assembled while the "
            "handle stopped naming a window; their pid, class, title and rectangle "
            "may not all describe the same window:",
            file=out,
        )
        for record in raced:
            print(
                f"  - hwnd {record.get('hwnd')} at {record.get('timestamp')} "
                f"({record.get('window_class')!r}, pid {record.get('pid')})",
                file=out,
            )
    if unchecked:
        print(
            _NL + f"WARNING: {len(unchecked)} window record(s) predate the "
            "mid-assembly validity check, so whether their fields describe one "
            "window or two was never established.",
            file=out,
        )
    sessions = [r for r in records if r.get("kind") == "session"]
    stops = [r for r in sessions if r.get("event") == "stop"]
    starts = [r for r in sessions if r.get("event") == "start"]
    missing = len(starts) - len(stops)
    unsound = [(r, session_health(r)) for r in stops]
    unsound = [(r, why) for r, why in unsound if why]
    if missing > 0:
        print(
            _NL + f"WARNING: {missing} session(s) in this file wrote no stop record, "
            "so what they did or did not see is unknown, not clean.",
            file=out,
        )
    for record, why in unsound:
        print(
            _NL + f"WARNING: the session labelled {record.get('label')!r} is NOT a "
            "sound capture and is not evidence of what was on screen:",
            file=out,
        )
        for reason in why:
            print(f"  - {reason}", file=out)
    if not starts and not stops:
        print(
            _NL + "WARNING: this file records no session at all, so it is not "
            "evidence that nothing appeared -- it is evidence of nothing.",
            file=out,
        )
    if missing > 0 or unsound or raced or unchecked or (not starts and not stops):
        return EXIT_UNSOUND_CAPTURE
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--seconds", type=float, default=300.0, help="how long to observe (default 300)")
    parser.add_argument("--out", type=Path, default=None, help="JSONL evidence file (appended to)")
    parser.add_argument("--label", default=None, help="the workflow being run, recorded on every window")
    parser.add_argument(
        "--all-windows",
        action="store_true",
        help="record every top-level window, not only the console-class ones",
    )
    parser.add_argument("--report", type=Path, default=None, help="print the causal map for an evidence file")
    args = parser.parse_args(argv)

    if args.report is not None:
        return report(args.report)
    if os.name != "nt":
        print("windowwatch observes Windows desktop windows; there is nothing to watch here.", file=sys.stderr)
        return 2
    out = args.out or Path(tempfile.gettempdir()) / f"windowwatch-{int(time.time())}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    return watch(args.seconds, out, label=args.label, all_windows=args.all_windows)


def _declare_signatures() -> None:
    """Give every Win32 entry point this file calls a complete signature.

    Not tidiness. An undeclared ``ctypes`` function converts each argument by
    guesswork and its return value as ``c_int``, so a 64-bit handle handed to
    an undeclared call is truncated to 32 bits -- and ``SetWinEventHook``,
    ``OpenProcess`` and ``CreateToolhelp32Snapshot`` all return exactly that.
    The hook value in particular was being passed to an undeclared
    ``UnhookWinEvent``: on a run whose hook handle happened
    to exceed 32 bits the unhook would address the wrong hook, and this
    observer's own system-wide hook would outlive the run that installed it.

    So the list is exhaustive by rule rather than by need, and it lives in one
    place, so that "does this call have a signature?" is answerable by looking
    rather than by grepping the file. Adding a Win32 call without adding it
    here is what ``test_windowwatch.py`` refuses.
    """

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        wintypes.LPFILETIME,
        wintypes.LPFILETIME,
        wintypes.LPFILETIME,
        wintypes.LPFILETIME,
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.LPVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.ReadProcessMemory.restype = wintypes.BOOL
    # No arguments and a DWORD result. Declared like everything else: the
    # rule here is "every call has a signature", not "every call that needs
    # one", so that the answer is checkable by looking.
    _kernel32.GetTickCount.argtypes = []
    _kernel32.GetTickCount.restype = wintypes.DWORD
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    # The ANSI pair, deliberately: ``_PROCESSENTRY32`` declares ``szExeFile``
    # as ``c_char * 260``. The W variants would write UTF-16 into it.
    for walker in (_kernel32.Process32First, _kernel32.Process32Next):
        walker.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
        walker.restype = wintypes.BOOL

    # NTSTATUS is a signed 32-bit code and the PEB address it fills in is a
    # pointer: an undeclared call would truncate the structure pointer on the
    # way in as readily as it mis-reads the status on the way out.
    _ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    ]
    _ntdll.NtQueryInformationProcess.restype = wintypes.LONG

    _user32.SetWinEventHook.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HMODULE,
        _WINEVENTPROC,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _user32.SetWinEventHook.restype = wintypes.HANDLE
    # HWINEVENTHOOK is a HANDLE; ``wintypes`` has no name for it, and the
    # default ``c_int`` conversion is precisely the truncation described above.
    _user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    _user32.UnhookWinEvent.restype = wintypes.BOOL
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [wintypes.HWND, wintypes.LPRECT]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.PeekMessageW.argtypes = [
        wintypes.LPMSG,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.PeekMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [wintypes.LPMSG]
    _user32.TranslateMessage.restype = wintypes.BOOL
    # LRESULT is LONG_PTR, which ``wintypes`` also does not name; c_ssize_t is
    # the same width on both 32- and 64-bit, which ``c_int`` is not.
    _user32.DispatchMessageW.argtypes = [wintypes.LPMSG]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t


if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _declare_signatures()
else:  # pragma: no cover -- import-time only, so --report still works anywhere
    _user32 = _kernel32 = _ntdll = None  # type: ignore[assignment]


if __name__ == "__main__":
    raise SystemExit(main())
