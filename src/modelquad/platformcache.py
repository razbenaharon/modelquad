# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Seed CPython's ``platform`` cache on Windows before anything can spawn.

The product-runtime popup was removed at its source earlier, and
the packaged product **still opened one**: a Sandbox acceptance run against a
freshly installed build at ``043e8b0`` observed
``cmd.exe /c "ver"`` -- with its ``conhost.exe`` -- parented directly by
``modelquad.exe`` on *every* startup path measured, ``--version`` and
``--help`` included. The same code run from source spawned nothing, across a
full import of every ``modelquad`` submodule and a real ``--version`` call,
so the caller lived somewhere in the packaged executable (since removed)
rather than in our own startup path.

**So this does not chase the caller, it removes what the caller reaches.**
``platform.uname()`` memoises into ``platform._uname_cache`` and returns it
unconditionally when set. ``system()``, ``release()``, ``version()``,
``machine()`` and ``node()`` are all thin readers of that cache. Seed it with
values obtained spawn-free and every one of them is answered from memory --
whoever asks, ours or a dependency's, without knowing this happened.

**What this does not cover, stated plainly.** A caller that invokes
``platform.win32_ver()`` *directly* bypasses ``_uname_cache`` entirely and will
still reach ``_syscmd_ver()``. The packaged trace is what decides whether any
such caller exists; this module may not be read as proof that none does.

**The values are CPython's own, not an approximation.** The release string comes
from ``platform``'s own ``_WIN32_CLIENT_RELEASES`` / ``_WIN32_SERVER_RELEASES``
tables and the machine string from its ``_get_machine_win32()``, so the
identity this installs is the identity ``platform`` would have computed -- the
popup is traded away, and nothing else is. The kernel version comes from
``RtlGetVersion`` (:func:`_windows_facts`), which reads the same thing ``ver``
reports and is not subject to the manifest compatibility shim.

**Two edges, closed after review.** A ``sys.getwindowsversion()``
read that fails seeds *nothing*: the client/server distinction decides which
release table CPython consults, and guessing "client" would cache a client
release string on a server, where nothing downstream could tell it from an
earned one. And the five fields go through the same ``_unknown_as_blank()``
normalisation ``uname()`` applies to its own result -- a literal ``unknown``
node or machine becomes ``""`` -- so the seeded cache is not merely *usually*
what ``uname()`` would have built, it is what ``uname()`` would have built.
"""

from __future__ import annotations

import ctypes
import os
import platform
import socket
import sys

__all__ = ["preseed_windows_uname", "windows_uname_result"]


def _rtl_get_version() -> tuple[int, int, int] | None:
    """(major, minor, build) as the kernel reports it, or None if unavailable.

    ``RtlGetVersion`` (ntdll) is the documented shim-free version query:
    unlike ``GetVersionEx`` -- which ``sys.getwindowsversion()`` wraps -- it
    returns the real OS version whatever the caller's manifest claims. That
    is the same property ``cmd /c ver`` has, which is why CPython spawns
    ``ver`` and why this reads the kernel instead: the same value, no
    process. Failure is None, never a guessed number (AGENTS.md invariant 4).

    A separate function so a test can substitute the kernel reading and watch
    the collected value follow it.
    """

    class _OSVERSIONINFOW(ctypes.Structure):
        _fields_ = [
            ("dwOSVersionInfoSize", ctypes.c_ulong),
            ("dwMajorVersion", ctypes.c_ulong),
            ("dwMinorVersion", ctypes.c_ulong),
            ("dwBuildNumber", ctypes.c_ulong),
            ("dwPlatformId", ctypes.c_ulong),
            ("szCSDVersion", ctypes.c_wchar * 128),
        ]

    info = _OSVERSIONINFOW()
    info.dwOSVersionInfoSize = ctypes.sizeof(_OSVERSIONINFOW)
    try:
        status = ctypes.windll.ntdll.RtlGetVersion(ctypes.byref(info))
    except (OSError, AttributeError):
        return None
    if status != 0:  # STATUS_SUCCESS; anything else leaves `info` untrustworthy
        return None
    return int(info.dwMajorVersion), int(info.dwMinorVersion), int(info.dwBuildNumber)


def _windows_facts() -> tuple[str, str | None, str | None]:
    """(family, version, arch) on Windows, without spawning anything.

    The version is the kernel version from :func:`_rtl_get_version`; the
    architecture comes from the same two environment variables CPython's own
    ``_get_machine_win32()`` reads.

    ``sys.getwindowsversion().platform_version`` is deliberately not used: it
    is ``kernel32.dll``'s version resource, which tracks the servicing
    baseline rather than the running build (measured 2026-09-10: ``ver`` and
    ``RtlGetVersion`` said 10.0.26200, ``platform_version`` said 10.0.26100).
    """

    kernel = _rtl_get_version()
    version: str | None = None if kernel is None else "{0}.{1}.{2}".format(*kernel)
    arch = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE")
    return "Windows", version, (arch or None)


def _release_for(major: int, minor: int, *, server: bool) -> str:
    """CPython's own release string for this version, via its own tables.

    Reused rather than reimplemented: a second copy of the mapping would be a
    second answer to "what does `platform` call this Windows", and the point of
    this module is that the answer does not change. Windows 11 reports major 10
    and CPython therefore calls it "10"; that is CPython's behaviour and
    reproducing it faithfully is the requirement, not correcting it.
    """

    client = getattr(platform, "_WIN32_CLIENT_RELEASES", {})
    release = client.get((major, minor)) or client.get((major, None)) or ""
    if server:
        table = getattr(platform, "_WIN32_SERVER_RELEASES", {})
        release = table.get((major, minor)) or table.get((major, None)) or release
    return release


def _unknown_as_blank(value: str) -> str:
    """CPython's own normalisation of a ``uname()`` field, reused when present.

    ``platform.uname()`` finishes with ``map(_unknown_as_blank, vals)``: a
    field whose value is the literal ``"unknown"`` becomes ``""``, "the more
    portable" spelling in CPython's words. A seeded cache skips ``uname()``
    entirely, so it has to apply the same step itself or a ``"unknown"`` node
    would stay observable where CPython would have blanked it. The private
    helper is preferred so this cannot drift from CPython; the fallback is its
    3.11 body, one line, for an interpreter that renames it.
    """

    helper = getattr(platform, "_unknown_as_blank", None)
    if callable(helper):
        return helper(value)
    return "" if value == "unknown" else value  # pragma: no cover - CPython renamed it


def windows_uname_result() -> platform.uname_result | None:
    """The value `platform.uname()` would produce, built without spawning.

    ``None`` when this is not Windows, when the kernel version could not be
    read, or when ``sys.getwindowsversion()`` -- which decides whether the
    client or the server release table applies -- could not be read. An
    incomplete or guessed cache is worse than none, because it would be
    believed. Unknown stays unknown (``AGENTS.md`` invariant 4).
    """

    if sys.platform != "win32":
        return None

    family, version, machine = _windows_facts()
    if not version:
        return None

    try:
        major, minor, _build = (int(part) for part in version.split(".", 2))
    except (TypeError, ValueError):
        return None

    try:
        # VER_NT_SERVER is 3, the same test `win32_ver()` makes. A pure
        # attribute read of an already-populated structure; no process.
        server = getattr(sys.getwindowsversion(), "product_type", None) == 3
    except Exception:
        # Fail closed. `product_type` selects the release table,
        # and assuming "client" here would cache a client release string on a
        # server -- a wrong answer nothing downstream could distinguish from a
        # right one. No seed means `uname()` earns the answer as it always did.
        return None

    try:
        node = socket.gethostname()
    except OSError:
        node = ""

    # `_get_machine_win32()` is two environment reads. Preferred over the value
    # `_windows_facts()` returns only because it is *the same function*
    # `uname()` would have called, so nothing downstream can observe a
    # difference between a seeded cache and an earned one.
    getter = getattr(platform, "_get_machine_win32", None)
    if callable(getter):
        machine = getter() or machine or ""
    else:  # pragma: no cover - only if CPython renames the private helper
        machine = machine or os.environ.get("PROCESSOR_ARCHITECTURE", "") or ""

    # The same final step `uname()` applies to its own tuple: a literal
    # "unknown" in any field becomes "".
    values = (
        family or "Windows",
        node or "",
        _release_for(major, minor, server=server),
        version,
        machine or "",
    )
    return platform.uname_result(*map(_unknown_as_blank, values))


def preseed_windows_uname() -> bool:
    """Populate `platform._uname_cache` spawn-free. True when this call did it.

    Idempotent, and deliberately **not** destructive: a cache that is already
    populated is left exactly as it is. Something else has already paid for
    that answer -- possibly this function on an earlier call, possibly a real
    `uname()` -- and replacing it could move a value another component has
    already read. The only state this is willing to create is state that does
    not exist yet.

    Never raises. It runs before almost everything, and a startup helper that
    can fail the process is a worse defect than the popup it removes.
    """

    try:
        if sys.platform != "win32":
            return False
        if getattr(platform, "_uname_cache", None) is not None:
            return False
        seeded = windows_uname_result()
        if seeded is None:
            return False
        platform._uname_cache = seeded
        return True
    except Exception:  # pragma: no cover - defensive; see the docstring
        return False
