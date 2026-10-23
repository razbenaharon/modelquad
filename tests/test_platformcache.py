# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The `platform` cache seed that removes the packaged popup.

An earlier fix removed the popup at its source and the **packaged** product still
opened one: a Sandbox acceptance run against a freshly installed build at
`043e8b0` observed `cmd.exe /c "ver"` and its `conhost.exe` parented directly
by `modelquad.exe` on every startup path measured, `--version` included,
while the same code from source spawned nothing. So the caller was inside the
frozen bundle (since removed), and the fix removes what it reaches rather than
hunting it:
`platform.uname()` returns `platform._uname_cache` unconditionally when set,
and `system()`, `release()`, `version()`, `machine()` and `node()` all read it.

**Every behavioural assertion runs in a hidden child process.** Not ceremony:
`tests/test_no_platform_spawn.py` forbids this suite from calling any
uname-reaching `platform` function in-process, because on a cold cache such a
call *is* the popup -- the regression suite for "no console windows" opening
one is a defect this Issue already paid for once. A child is also the more
honest test: it has a genuinely cold cache, it is the real code path, and
nothing is mocked into agreeing.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import no_window_kwargs, platform_reference

REPO_ROOT = Path(__file__).resolve().parents[1]
nt_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")


def _probe(body: str) -> dict:
    """Run `body` in a child interpreter and return the JSON it prints.

    The child starts with a cold `platform._uname_cache`, which is the state
    the fix exists for and the state no in-process test can honestly recreate.
    """

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    done = subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        # `CREATE_NO_WINDOW`, so a probe about console windows never opens one.
        **no_window_kwargs(),
    )
    assert done.returncode == 0, f"probe failed: {done.stderr.strip()}"
    return json.loads(done.stdout.strip().splitlines()[-1])


# --- 1. the seed spawns nothing ----------------------------------------------


@nt_only
def test_the_seed_creates_no_process():
    """The thing that removes a spawn may not itself spawn.

    Every `subprocess` entry point and both `os` spawn helpers are replaced in
    the child with recorders that raise. A seed that reached `platform.uname()`
    or `win32_ver()` to fill its own cache would trip one of them -- the exact
    circularity this fix has to avoid.
    """

    result = _probe(
        "import json, os, subprocess\n"
        "calls = []\n"
        "def trip(label):\n"
        "    def recorder(*a, **k):\n"
        "        calls.append(label)\n"
        "        raise AssertionError(label)\n"
        "    return recorder\n"
        "for name in ('Popen', 'run', 'call', 'check_call', 'check_output'):\n"
        "    setattr(subprocess, name, trip('subprocess.' + name))\n"
        "os.system = trip('os.system')\n"
        "os.popen = trip('os.popen')\n"
        "from modelquad.platformcache import preseed_windows_uname\n"
        "seeded = preseed_windows_uname()\n"
        "print(json.dumps({'seeded': seeded, 'calls': calls}))\n"
    )
    assert result["seeded"] is True
    assert result["calls"] == []


# --- 2. uname() is answered from the cache -----------------------------------


@nt_only
def test_uname_after_the_seed_never_reaches_syscmd_ver():
    """`_syscmd_ver` is `cmd /c ver`; after seeding it must not be reached."""

    result = _probe(
        "import json, platform\n"
        "def explode(*a, **k):\n"
        "    raise AssertionError('reached')\n"
        "platform._syscmd_ver = explode\n"
        "platform.win32_ver = explode\n"
        "from modelquad.platformcache import preseed_windows_uname\n"
        "seeded = preseed_windows_uname()\n"
        "u = platform.uname()\n"
        "print(json.dumps({'seeded': seeded, 'system': u.system, 'version': u.version}))\n"
    )
    assert result["seeded"] is True
    assert result["system"] == "Windows"
    assert result["version"]


# --- 3. the dependent readers consume the cached values ----------------------


@nt_only
def test_the_dependent_apis_read_the_seeded_values():
    """`system/release/version/machine/node` are readers of the same cache.

    Each is called with `_syscmd_ver` and `win32_ver` rigged to raise, so a
    reader that bypassed the cache fails loudly instead of quietly spawning.
    """

    result = _probe(
        "import json, platform\n"
        "def explode(*a, **k):\n"
        "    raise AssertionError('bypassed the cache')\n"
        "platform._syscmd_ver = explode\n"
        "platform.win32_ver = explode\n"
        "from modelquad.platformcache import preseed_windows_uname\n"
        "preseed_windows_uname()\n"
        "c = platform._uname_cache\n"
        "print(json.dumps({\n"
        "  'system': [platform.system(), c.system],\n"
        "  'release': [platform.release(), c.release],\n"
        "  'version': [platform.version(), c.version],\n"
        "  'machine': [platform.machine(), c.machine],\n"
        "  'node': [platform.node(), c.node],\n"
        "  'processor': platform.processor(),\n"
        "}))\n"
    )
    processor = result.pop("processor")
    for field, (read, cached) in result.items():
        assert read == cached, f"{field} did not come from the cache: {read!r} != {cached!r}"
    assert processor, "processor is env-derived and must still resolve"


# --- 4. the identity is CPython's own ----------------------------------------


@nt_only
def test_the_seeded_identity_matches_what_platform_would_report():
    """The popup is traded away and nothing else is.

    A seed that removed the spawn but moved `platform.version()` would be the
    worse defect: the build manifest and the collected environment snapshot
    both read these values. Compared against `conftest.platform_reference()`,
    which reads the real, spawning answer in a hidden child -- so the
    comparison is against truth, not against the seed's own arithmetic.
    """

    reference = platform_reference()
    seeded = _probe(
        "import json\n"
        "from modelquad.platformcache import windows_uname_result\n"
        "u = windows_uname_result()\n"
        "print(json.dumps({'system': u.system, 'release': u.release,\n"
        "                  'version': u.version, 'machine': u.machine}))\n"
    )
    for field in ("system", "release", "version", "machine"):
        assert seeded[field] == reference[field], (
            f"seeded {field} diverged from what platform reports: "
            f"{seeded[field]!r} != {reference[field]!r}"
        )


# --- 4a. the two edges review found -------------------------------


@nt_only
def test_a_failed_getwindowsversion_read_seeds_nothing():
    """Fail closed, not "client".

    `product_type` decides whether CPython consults its client or its server
    release table. The seed used to catch a failed `sys.getwindowsversion()`
    and carry on as a client, which on a server would have cached a client
    release string -- indistinguishable downstream from an earned one. Now the
    read failing means no seed at all, and `uname()` earns the answer as it
    always did. Rigged in the child so the real function is never touched.
    """

    result = _probe(
        "import json, platform, sys\n"
        "def broken(*a, **k):\n"
        "    raise OSError('rigged: getwindowsversion unavailable')\n"
        "sys.getwindowsversion = broken\n"
        "from modelquad.platformcache import preseed_windows_uname, windows_uname_result\n"
        "result_is_none = windows_uname_result() is None\n"
        "seeded = preseed_windows_uname()\n"
        "print(json.dumps({'result_is_none': result_is_none, 'seeded': seeded,\n"
        "                  'cache_is_none': platform._uname_cache is None}))\n"
    )
    assert result["result_is_none"] is True
    assert result["seeded"] is False
    assert result["cache_is_none"] is True


@nt_only
def test_a_literal_unknown_is_blanked_the_way_uname_blanks_it():
    """`uname()` ends with `map(_unknown_as_blank, vals)`; so must the seed.

    A seeded cache skips `uname()` entirely, so a node or machine that reads as
    the literal string `unknown` would stay observable where CPython would have
    returned `""`. Both readers are rigged to say `unknown` in the child and
    the seeded fields must come back empty.
    """

    result = _probe(
        "import json, platform, socket\n"
        "socket.gethostname = lambda: 'unknown'\n"
        "platform._get_machine_win32 = lambda: 'unknown'\n"
        "from modelquad.platformcache import windows_uname_result\n"
        "u = windows_uname_result()\n"
        "print(json.dumps({'node': u.node, 'machine': u.machine,\n"
        "                  'system': u.system, 'version': u.version}))\n"
    )
    assert result["node"] == ""
    assert result["machine"] == ""
    assert result["system"] == "Windows"
    assert result["version"]


# --- 5. non-Windows is untouched ---------------------------------------------


def test_nothing_happens_off_windows():
    """POSIX `uname()` is a syscall, so there is nothing here to fix."""

    result = _probe(
        "import json, sys\n"
        "sys.platform = 'linux'\n"
        "from modelquad.platformcache import preseed_windows_uname, windows_uname_result\n"
        "print(json.dumps({'seeded': preseed_windows_uname(),\n"
        "                  'result_is_none': windows_uname_result() is None}))\n"
    )
    assert result["seeded"] is False
    assert result["result_is_none"] is True


# --- 6/7. idempotent, and never destructive ----------------------------------


@nt_only
def test_seeding_twice_changes_nothing_and_never_replaces_an_existing_cache():
    """Three entry points call this; only the first may do anything.

    And a cache someone else populated is left exactly as it was: something has
    already paid for that answer and may already have handed it out, so the
    only state this function creates is state that did not exist.
    """

    result = _probe(
        "import json, platform\n"
        "from modelquad.platformcache import preseed_windows_uname\n"
        "first = preseed_windows_uname()\n"
        "cached = platform._uname_cache\n"
        "second = preseed_windows_uname()\n"
        "same = platform._uname_cache is cached\n"
        "mine = platform.uname_result('Windows', 'someone-else', '9', '9.9.9', 'ARM64')\n"
        "platform._uname_cache = mine\n"
        "third = preseed_windows_uname()\n"
        "untouched = platform._uname_cache is mine\n"
        "print(json.dumps({'first': first, 'second': second, 'same': same,\n"
        "                  'third': third, 'untouched': untouched}))\n"
    )
    assert result["first"] is True
    assert result["second"] is False
    assert result["same"] is True
    assert result["third"] is False
    assert result["untouched"] is True


# --- 8. the packaged entry points seed first ---------------------------------


def _first_modelquad_import_line(source: str) -> int:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("modelquad"):
            if node.module != "modelquad.platformcache":
                return node.lineno
    return 10**9


def _seed_call_line(source: str) -> int:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "preseed_windows_uname":
                return node.lineno
    return 10**9


def test_the_cli_seeds_before_it_parses(mode="source"):
    """Source mode matters too: a developer's terminal is where it was noticed."""

    source = (REPO_ROOT / "src/modelquad/cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    # `main` handles the fail-safe hook commands and hands every command,
    # those included, to `_run_command`, which is where the parser is built.
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_run_command"
        for node in ast.walk(functions["main"])
    ), "cli.main() no longer runs commands through _run_command"
    main = functions["_run_command"]
    calls = [
        node.lineno for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "preseed_windows_uname"
    ]
    assert calls, "cli.main() does not seed the platform cache"
    parser = [
        node.lineno for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "build_parser"
    ]
    assert parser and calls[0] < parser[0], "cli.main() builds the parser before seeding"
