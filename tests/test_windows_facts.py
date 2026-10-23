# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The spawn-free Windows facts ``platformcache`` seeds ``platform`` with."""

import os
import platform
import subprocess
import sys

import pytest

from conftest import platform_reference
from orchestrator import platformcache

def test_reading_the_windows_facts_spawns_no_process(monkeypatch):
    """The facts are read from the OS, never from a command.

    On Windows every ``platform`` function that goes through ``uname()``
    reaches ``_syscmd_ver()``, which runs ``cmd /c ver`` with no
    ``creationflags``; from a console-less parent that flashes a window.
    Asserted at the ``subprocess`` boundary, so it is deterministic.
    """

    spawned: list[object] = []
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(
            subprocess, name, lambda *args, _n=name, **kwargs: (spawned.append((_n, args)), "")[1],
        )
    monkeypatch.setattr(os, "system", lambda command: spawned.append(("os.system", command)))
    monkeypatch.setattr(platform, "_uname_cache", None, raising=False)

    platformcache._windows_facts()

    assert spawned == [], f"reading the Windows facts spawned: {spawned}"


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows readings are what changed")
def test_windows_facts_match_what_platform_reports():
    """Same values, cheaper mechanism -- so the privacy posture is unchanged.

    This is the other half of the fix above: dropping ``platform`` would be a
    silent change to what is collected if the replacements disagreed, and
    the seeded ``platform`` cache is about the *values*, not about
    which API produced them. So the values are still compared against
    ``platform`` itself, on the machine running the test -- that guarantee is
    the reason this test exists and it is not weakened here.

    What changed is where ``platform`` is asked (review finding F3). Calling
    ``platform_module.system()`` *in this process* is the exact code path the
    fix removes: on Windows it reaches ``_syscmd_ver()``, which runs
    ``check_output('ver', shell=True)`` unsuppressed, so the required
    validation run could itself flash a console window on the developer's
    desktop -- and only sometimes, because ``platform._uname_cache`` decides
    whether the call spawns at all. A test for a nondeterministic popup that
    nondeterministically causes one is not a test anybody can read.

    Warming the cache first was considered and rejected: warming it *is* the
    spawn, so it moves the window rather than removing it. The reference is
    read in a child launched with ``CREATE_NO_WINDOW`` instead
    (``conftest.platform_reference``). The child's cache is cold, so the real
    ``platform`` path runs every time rather than being skipped by accident,
    and its ``cmd.exe`` inherits a console that has no window.
    """

    reference = platform_reference()

    family, version, arch = platformcache._windows_facts()
    assert family == reference["system"]
    assert version == reference["version"], "the version `platform.version()` reports, exactly"
    assert arch == reference["machine"]


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows readings are what changed")
def test_the_kernel_reading_is_available_and_is_what_the_snapshot_carries():
    """The `RtlGetVersion` plumbing works, and its answer is the collected one.

    The cheapest thing that could break silently: a `ctypes` mistake -- a wrong
    struct size, a missing `dwOSVersionInfoSize`, a non-zero NTSTATUS -- makes
    `_rtl_get_version()` return `None`, the version becomes unknown, and every
    *equivalence* assertion in this module still passes because `None` is a
    legal fail-soft value. So the reading is asserted present and asserted to
    be the value that reaches the snapshot, separately from whether it matches
    anything.
    """

    kernel = platformcache._rtl_get_version()
    assert kernel is not None, "RtlGetVersion failed; the version would silently be unknown"
    major, minor, build = kernel
    assert (major, minor) >= (6, 0) and build > 0, f"implausible kernel version: {kernel}"

    assert platformcache._windows_facts()[1] == f"{major}.{minor}.{build}"


def test_the_version_follows_the_kernel_reading_wherever_it_comes_from(monkeypatch):
    """Substitute the kernel reading; the collected value has to follow it.

    Platform-independent on purpose. `_windows_facts()` is called directly, so
    no `ctypes` call happens and this arm of the guarantee is checked on every
    host the suite runs on -- including CI, where the two tests below are
    skipped and would otherwise leave the source of this value untested.

    What it catches: any future reading that is not the kernel one. Revert
    `_windows_facts` to `sys.getwindowsversion()` and `monkeypatch.setattr`
    fails outright on the vanished seam; wire it to a second source and the
    substituted value stops coming through.
    """

    monkeypatch.setattr(platformcache, "_rtl_get_version", lambda: (11, 3, 90001))
    assert platformcache._windows_facts()[1] == "11.3.90001"

    monkeypatch.setattr(platformcache, "_rtl_get_version", lambda: None)
    assert platformcache._windows_facts()[1] is None, (
        "an unreadable kernel version must stay unknown, never a guessed number"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="the shim only exists on Windows")
def test_the_version_does_not_follow_the_shimmed_reading(monkeypatch):
    """The divergent case the earlier rounds never forced.

    Rounds 1 to 5 read `sys.getwindowsversion().major/minor/build` and claimed
    the result was what `platform` reports, on the stated grounds that
    `platform.win32_ver()` reads those same fields. It does not. CPython
    3.11.9::

        major, minor, build = map(int, _syscmd_ver()[2].split('.'))
        except ValueError:
            major, minor, build = winver.platform_version or winver[:3]

    `_syscmd_ver()` is `cmd /c ver`, so `platform.version()` is *primarily* the
    `ver` reading and `getwindowsversion()` is only its fallback. `ver` reports
    the running kernel and is not rewritten by the manifest compatibility shim;
    `GetVersionEx`, which `sys.getwindowsversion()` wraps, is. On this
    unshimmed host the two coincide, which is exactly why every test passed --
    the equivalence was a property of the machine, not a guarantee.

    So the shimmed reading is *forced* to differ here, and the collected value
    must ignore it. That is the whole finding, in one assertion: on a shimmed
    host -- a compatibility manifest, or a differently manifested frozen
    executable -- the old code would have moved a documented, collected value
    and this test would have been the only thing to say so.
    """

    class _Shimmed:
        major, minor, build = 6, 1, 7601  # Windows 7, what a shim typically claims
        platform_version = (6, 1, 7601)
        service_pack_major = 1
        product_type = 1

    kernel = platformcache._rtl_get_version()
    assert kernel is not None
    truth = "{0}.{1}.{2}".format(*kernel)
    assert truth != "6.1.7601", "the stub must diverge from this host to mean anything"

    monkeypatch.setattr(sys, "getwindowsversion", _Shimmed)

    assert platformcache._windows_facts()[1] == truth, (
        "the version followed the shimmed GetVersionEx reading; `cmd /c ver`, "
        "which is what `platform` actually parses, would have reported the kernel"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows readings are what changed")
def test_platform_version_is_rejected_because_it_disagrees_with_ver():
    """Why `platform_version` is still not the source -- with the right reason.

    Review asked for `sys.getwindowsversion().platform_version` twice, so the
    rejection is pinned rather than re-argued. **The conclusion from those
    rounds stands; the reason given for it was wrong.**

    The old reason: `platform.win32_ver()` reads `major`/`minor`/`build`, so
    equivalence with `platform` means reading those and not the fifth field.
    That is false -- `win32_ver` reads `cmd /c ver` first and touches
    `getwindowsversion()` only as a fallback, which prefers `platform_version`
    over `winver[:3]` at that.

    The real reason is simpler and survives being checked: `platform_version`
    is not a kernel query at all. It is `kernel32.dll`'s own version resource,
    which tracks the servicing baseline rather than the running build, so it
    does not agree with `ver`. Measured here, 2026-09-10::

        cmd /c ver raw       : Microsoft Windows [Version 10.0.26200.9445]
        platform.version()   : 10.0.26200
        RtlGetVersion        : 10.0.26200      <- the source, and it agrees
        platform_version     : (10, 0, 26100)  <- disagrees
        kernel32.dll version : 10.0.26100.9444 <- where the 26100 comes from

    Adopting it would move a documented, collected value by a hundred builds
    while fixing no popup. The divergence is guarded, because a host where the
    two coincide has nothing to detect and the equivalence test above is doing
    the work there on its own.

    Nothing here spawns: `sys.getwindowsversion()` is a direct call, and the
    `platform` reference still comes from the hidden child.
    """

    collected = platformcache._windows_facts()[1]
    accurate = ".".join(str(part) for part in sys.getwindowsversion().platform_version)

    assert collected == platform_reference()["version"], (
        "the collected value must equal what `platform` reports, which on an "
        "unshimmed host is `ver`'s kernel reading"
    )
    if accurate != collected:
        assert platformcache._windows_facts()[1] != accurate, (
            "switching to platform_version would silently change the collected value"
        )
