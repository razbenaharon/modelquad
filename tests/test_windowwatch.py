# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``scripts/windowwatch.py`` measures honestly, and lets go of what it takes.

The observer is the instrument the no-window work rests on: every measured
popup count came out of it.
An instrument that over-reports is worse than no instrument, because the
conclusions still look measured. So the properties asserted here are the ones
that decide whether a reading means what its label says -- and the review that
found them (F4-F7) found each one by reading, not by a failing run, which is
exactly why they need tests that would have failed.

Deterministic by construction. Nothing here waits for a real window, names a
live pid, or binds anything: the Win32 surface is a stub whose behaviour the
test chooses, and the two tests that do touch ``user32`` call it with a handle
that is invalid on purpose. ``pytest -n auto`` runs this file in parallel with
the rest of the suite and a window flashing on the developer's desktop must
not change a single assertion in it.
"""

from __future__ import annotations

import ast
import ctypes
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWWATCH = REPO_ROOT / "scripts" / "windowwatch.py"

# ``windowwatch`` imports ``ctypes.wintypes``, which does not exist off Windows,
# so the module cannot even be loaded elsewhere. The document-hygiene test at
# the bottom of this file deliberately does not import it, and runs anywhere.
pytestmark_nt = pytest.mark.skipif(os.name != "nt", reason="windowwatch is a Win32 instrument")


def _load():
    spec = importlib.util.spec_from_file_location("_windowwatch_under_test", WINDOWWATCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ww():
    """A fresh module object per test.

    ``_declare_signatures`` mutates the shared ``WinDLL`` singletons, and the
    ledger is a module global; a test that stubs either must not be able to
    reach the next one.
    """
    return _load()


# --- F8: no evidence document carries a control byte --------------------------

#: Built from segments rather than written as a literal, because a literal is
#: precisely what went wrong: C-style escape expansion over this path turned
#: ``\app`` into BEL + ``pp``, ``\versions`` into VT + ``ersions`` and
#: ``\0.2.0`` into NUL + ``.2.0`` on the way into the evidence document, and the
#: NUL then made a tracked Markdown file binary to Git and unsearchable by
#: ``rg``. A test for that defect must not be able to reintroduce it.
INSTALLED_EXE = chr(92).join(
    (
        "C:",
        "Users",
        "example",
        "AppData",
        "Local",
        "MultiAgentOrchestrator",
        "app",
        "versions",
        "0.2.0+ad87cea",
        "orchestrator.exe",
    )
)


def _tracked_markdown() -> list[Path]:
    documents = sorted(REPO_ROOT.glob("*.md"))
    for tree in ("docs",):
        documents.extend(sorted((REPO_ROOT / tree).rglob("*.md")))
    return documents


def test_no_evidence_document_carries_a_control_byte():
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




# --- F6: every Win32 call has a signature -------------------------------------


def _called_win32_names() -> dict[str, set[str]]:
    """Every ``_user32``/``_kernel32``/``_ntdll`` entry point the file *calls*.

    Read out of the source rather than listed here, for the same reason
    ``test_scripts_no_window.py`` discovers its carriers instead of naming
    them: a list is a thing to forget to update, and the next Win32 call added
    to this file must be held to the rule the moment it is added.
    """

    tree = ast.parse(WINDOWWATCH.read_text(encoding="utf-8"), filename=str(WINDOWWATCH))
    declarations = {
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_declare_signatures"
    }
    inside_declarations = {id(n) for decl in declarations for n in ast.walk(decl)}

    found: dict[str, set[str]] = {"_user32": set(), "_kernel32": set(), "_ntdll": set()}
    for node in ast.walk(tree):
        if id(node) in inside_declarations or not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in found
        ):
            found[func.value.id].add(func.attr)
    return found


@pytestmark_nt
def test_every_win32_call_in_the_file_carries_argtypes(ww):
    """An undeclared ``ctypes`` call converts by guesswork.

    ``argtypes`` is the reliable "was this declared" signal -- it defaults to
    ``None``, while ``restype`` defaults to ``c_int`` and so cannot distinguish
    "declared as returning an int" from "never declared at all".
    """

    undeclared = []
    for dll_name, entry_points in _called_win32_names().items():
        dll = getattr(ww, dll_name)
        for name in sorted(entry_points):
            if getattr(dll, name).argtypes is None:
                undeclared.append(f"{dll_name}.{name}")
    assert not undeclared, f"called with no argtypes, so converted by guesswork: {undeclared}"
    # And the scan is not vacuously empty.
    assert len(_called_win32_names()["_user32"]) >= 10


@pytestmark_nt
@pytest.mark.parametrize(
    "dll_name, name",
    (
        ("_kernel32", "OpenProcess"),
        ("_kernel32", "CreateToolhelp32Snapshot"),
        ("_user32", "SetWinEventHook"),
        ("_user32", "GetAncestor"),
        ("_user32", "DispatchMessageW"),
    ),
)
def test_pointer_width_returns_are_not_left_as_the_default_c_int(ww, dll_name, name):
    """Each of these returns a handle, an HWND or an LRESULT -- pointer-width.

    ``ctypes``' default ``restype`` is ``c_int``, so on this 64-bit build every
    one of them would have been silently truncated to its low 32 bits. That is
    not theoretical for ``SetWinEventHook``: its result is what is later handed
    to ``UnhookWinEvent``, which is the leak F6 names.
    """

    restype = getattr(getattr(ww, dll_name), name).restype
    assert ctypes.sizeof(restype) == ctypes.sizeof(ctypes.c_void_p), (name, restype)


@pytestmark_nt
def test_unhookwinevent_accepts_a_hook_value_wider_than_32_bits(ww):
    """The defect, reproduced against the real ``user32``.

    ``0x1_0000_0001`` is not a hook, and ``UnhookWinEvent`` is documented to
    return FALSE for one that is not -- so this is a safe call. What it proves
    is the conversion: undeclared, ``ctypes`` converts the argument as ``c_int``
    and raises ``ArgumentError: int too long to convert`` before Windows is
    reached at all; declared, the full 64-bit value goes through and Windows
    answers. A hook handle above 2**32 is uncommon, not impossible, and the run
    in which it happened would have leaked its hook without saying so.
    """

    assert ww._user32.UnhookWinEvent(0x1_0000_0001) == 0


# --- F6: nothing is acquired outside the cleanup path -------------------------


class _FakeUser32:
    """Just enough ``user32`` to run ``watch()`` without touching the desktop.

    Its message queue is empty: ``PeekMessageW`` says so on the first ask, so
    a run against this fake reaches its deadline and drains nothing, which is
    the quiet-desktop case.
    """

    def __init__(self, hook: int = 0x1_0000_0007) -> None:
        self.hook = hook
        self.unhooked: list[int] = []
        self.unhook_result = 1

    def PeekMessageW(self, *_args):  # noqa: N802 -- it is a Win32 name
        return 0

    def SetWinEventHook(self, *_args):  # noqa: N802 -- it is a Win32 name
        return self.hook

    def UnhookWinEvent(self, hook):  # noqa: N802
        self.unhooked.append(int(hook))
        return self.unhook_result


class _FakeTable:
    """A sampler that records being started and stopped.

    ``stop()`` returns a bool because the real one does: whether the thread
    actually finished inside the join timeout is a fact the stop record
    carries. ``on_stop`` is the seam for the case that matters most --
    work the sampler does *while* it is being shut down, after the
    point at which the stop record used to have already been written.
    """

    def __init__(self, stops: bool = True, sample_failures=None) -> None:
        self.started = self.stopped = 0
        self.stops = stops
        self.on_stop = None
        #: What the real ``ProcessTable`` reports about samples that raised.
        #: A stand-in by default; a test that cares about the
        #: contents hands in a real ``FailureLedger``.
        self.sample_failures = sample_failures or SimpleNamespace(failures={})

    def start(self) -> None:
        self.started += 1

    def stop(self) -> bool:
        self.stopped += 1
        if self.on_stop is not None:
            self.on_stop()
        return self.stops

    def get(self, pid):
        return {"pid": pid, "image": None, "command_line": None, "parent_pid": None, "created": None}

    def ancestors(self, pid, limit=24, start=None):
        # `start` mirrors the real signature: the caller
        # passes the generation it already resolved so one record cannot mix
        # one process's identity with another's ancestry.
        return []


@pytestmark_nt
def test_a_failed_first_write_still_releases_the_hook_and_the_thread(ww, tmp_path, monkeypatch):
    """The leak F6 names, reproduced at its trigger.

    The hook used to be installed and the session's ``start`` record written
    before the ``try`` block existed, so any error on that first write -- a full
    disk is the realistic one -- escaped with a system-wide WinEvent hook
    installed and the sampler thread running, and nothing left that could
    release either. Here the first write raises deliberately; both must still be
    released before ``watch`` re-raises.
    """

    fake_user32 = _FakeUser32()
    table = _FakeTable()
    monkeypatch.setattr(ww, "_user32", fake_user32)
    monkeypatch.setattr(ww, "ProcessTable", lambda *a, **k: table)
    monkeypatch.setattr(ww, "_WINEVENTPROC", lambda callback: callback)

    real_dumps = json.dumps

    def exploding_dumps(record, **kwargs):
        if record.get("event") == "start":
            raise OSError(28, "No space left on device")
        return real_dumps(record, **kwargs)

    monkeypatch.setattr(ww.json, "dumps", exploding_dumps)

    with pytest.raises(OSError):
        ww.watch(5.0, tmp_path / "evidence.jsonl", label=None, all_windows=False)

    assert fake_user32.unhooked == [fake_user32.hook], "the hook outlived the failed run"
    assert table.stopped == 1, "the sampler thread outlived the failed run"


@pytestmark_nt
def test_a_hook_that_cannot_be_installed_still_closes_what_was_opened(ww, tmp_path, monkeypatch):
    """``SetWinEventHook`` failing is the one path that returns rather than raises."""

    fake_user32 = _FakeUser32(hook=0)
    table = _FakeTable()
    monkeypatch.setattr(ww, "_user32", fake_user32)
    monkeypatch.setattr(ww, "ProcessTable", lambda *a, **k: table)
    monkeypatch.setattr(ww, "_WINEVENTPROC", lambda callback: callback)

    out = tmp_path / "evidence.jsonl"
    assert ww.watch(5.0, out, label=None, all_windows=False) == 2
    assert table.stopped == 1
    assert fake_user32.unhooked == [], "there was no hook to release"
    assert out.exists() and out.read_text(encoding="utf-8") == ""


# --- F4: a window is not a popup ----------------------------------------------


class _Desktop:
    """A desktop whose windows this test decides, in place of ``user32``.

    Nothing here waits for a real window. A test that did would be at the mercy
    of whatever the developer's machine was doing while ``pytest -n auto`` ran,
    which for this Task's subject matter is the easiest possible mistake to
    make: the suite itself opens console windows.
    """

    def __init__(self) -> None:
        self.windows: dict[int, dict] = {}

    def add(self, hwnd: int, *, window_class: str, visible: bool = False, pid: int = 4242) -> None:
        self.windows[hwnd] = {"class": window_class, "visible": visible, "pid": pid}

    # -- the `user32` surface `Watcher` uses ---------------------------------

    def IsWindow(self, hwnd):  # noqa: N802
        return int(hwnd.value if hasattr(hwnd, "value") else hwnd) in self.windows

    def GetAncestor(self, hwnd, _flag):  # noqa: N802
        return int(hwnd.value if hasattr(hwnd, "value") else hwnd)  # every window here is a root

    def IsWindowVisible(self, hwnd):  # noqa: N802
        return int(self.windows[int(hwnd.value if hasattr(hwnd, "value") else hwnd)]["visible"])

    def GetWindowThreadProcessId(self, hwnd, out_pid):  # noqa: N802
        out_pid._obj.value = self.windows[int(hwnd.value if hasattr(hwnd, "value") else hwnd)]["pid"]
        return 1

    def GetClassNameW(self, hwnd, buf, _size):  # noqa: N802
        buf.value = self.windows[int(hwnd.value if hasattr(hwnd, "value") else hwnd)]["class"]
        return len(buf.value)

    def GetWindowTextW(self, hwnd, buf, _size):  # noqa: N802
        buf.value = ""
        return 0

    def GetWindowRect(self, _hwnd, _rect):  # noqa: N802
        return 0


@pytest.fixture()
def desktop(ww, monkeypatch):
    fake = _Desktop()
    monkeypatch.setattr(ww, "_user32", fake)
    return fake


def _watcher(ww, desktop, **kwargs):
    records: list[dict] = []
    watcher = ww.Watcher(_FakeTable(), records.append, **kwargs)
    return watcher, records


@pytestmark_nt
def test_a_console_window_that_never_appears_is_recorded_but_not_counted(ww, desktop):
    """The over-count F4 names, in its most common shape.

    ConPTY creates a ``PseudoConsoleWindow`` as plumbing for a console that has
    no window: created, never shown, destroyed. Under the defect that was one
    more "console popup" in the tally, and the tally is what the evidence
    document quotes.
    """

    desktop.add(0x11, window_class="PseudoConsoleWindow", visible=False)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x11)
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x11)

    assert watcher.recorded == 1, "the window is still evidence and is still written down"
    assert sum(watcher.counts.values()) == 0, "but it is not a popup anybody saw"
    end = [record for record in records if record["kind"] == "window_end"]
    assert end[0]["ever_visible"] is False
    assert end[0]["visible_seconds"] == 0.0


@pytestmark_nt
def test_a_window_that_is_shown_is_counted_exactly_once(ww, desktop):
    desktop.add(0x12, window_class="ConsoleWindowClass", visible=False)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x12)
    desktop.windows[0x12]["visible"] = True
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x12)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x12)  # a repeated SHOW is not a second popup

    assert sum(watcher.counts.values()) == 1
    assert watcher.recorded == 1
    # The second SHOW produces no second record either: the window did not
    # reappear, Windows simply said so twice.
    assert [record["event"] for record in records] == ["create", "show"]


@pytestmark_nt
def test_a_window_already_visible_at_first_sighting_is_counted(ww, desktop):
    """A SHOW whose CREATE this observer never saw is still a popup.

    The hook is installed mid-desktop, and the notification is queued: a window
    can be up before its record is read. Counting only on a *subsequent* SHOW
    would silently drop exactly the short-lived flashes this instrument exists
    to catch.

    This asserted the count alone, and that is why it went on passing through
    a review: the count was right and the stamp underneath it was not. The
    CREATE's stamp stood in for a SHOW that happened at an unknown later
    moment, so the visible interval starts too early -- which is the queue
    latency the event-time fix removed, put back by a different route. The number is an
    upper bound and the record now says so.
    """

    desktop.add(0x13, window_class="ConsoleWindowClass", visible=True)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x13, 1_000)
    watcher.on_event(ww.EVENT_OBJECT_HIDE, 0x13, 3_000)

    assert sum(watcher.counts.values()) == 1
    show = [record for record in records if record.get("event") == "show"][0]
    assert show["event_ms"] == 1_000
    assert show["show_stamp"] == "create_event", "no SHOW said when this window appeared"
    end = [record for record in records if record["kind"] == "window_end"][0]
    assert end["visible_seconds"] == 2.0, "measured from the create stamp, for want of a show one"
    assert end["timing_source"] == "create_event", (
        "an event-sourced label on a stamp that came from a different event is the defect"
    )


@pytestmark_nt
def test_a_delayed_create_gives_up_its_stamp_when_the_real_show_arrives(ww, desktop):
    """The second half of the delayed-stamp case: the SHOW is behind the CREATE.

    Under ``WINEVENT_OUTOFCONTEXT`` the CREATE and the SHOW are both queued,
    and a backlog can mean the CREATE is read after the window is already up
    with its SHOW still waiting. The window was created at 1.0 s and appeared
    at 1.5 s; measuring from the create stamp charges the screen with the half
    second the window existed hidden -- 2.0 s for a window that was up for
    1.5 s -- and labels the result ``event`` as though Windows had said so.

    The stamp the CREATE supplies is provisional, so the SHOW replaces it.
    """

    desktop.add(0x59, window_class="ConsoleWindowClass", visible=True)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x59, 1_000)  # read late; already on screen
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x59, 1_500)  # the queued SHOW, behind it
    watcher.on_event(ww.EVENT_OBJECT_HIDE, 0x59, 3_000)

    end = [record for record in records if record["kind"] == "window_end"][0]
    assert end["visible_seconds"] == 1.5, "the create-to-show latency is not time on screen"
    assert end["timing_source"] == "event", "and now the stamp really is the show's own"
    assert sum(watcher.counts.values()) == 1, "one window, first sighted twice"
    assert [record.get("event") for record in records].count("show") == 1


@pytestmark_nt
def test_a_window_that_never_appeared_reports_no_time_on_screen(ww, desktop, monkeypatch):
    """``_close`` measured from *creation* when no SHOW had ever arrived.

    ``started = shown_at or monotonic`` reads as a sensible fallback and is
    not one: for a window that was never shown there is nothing to fall back
    to, so a `PseudoConsoleWindow` that sat as invisible plumbing for ten
    seconds was reported as ten seconds "on screen". The clock is stepped by
    hand, so the number is this test's and not this machine's.
    """

    desktop.add(0x14, window_class="PseudoConsoleWindow", visible=False)
    clock = [100.0]
    monkeypatch.setattr(ww.time, "monotonic", lambda: clock[0])
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x14)
    clock[0] = 110.0
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x14)

    end = [record for record in records if record["kind"] == "window_end"][0]
    assert end["visible_seconds"] == 0.0, "it was never on screen, so no duration is"
    assert end["lifetime_seconds"] == 10.0, "the old figure is kept, under the name it actually had"


@pytestmark_nt
def test_a_window_that_did_appear_reports_the_time_between_show_and_end(ww, desktop, monkeypatch):
    desktop.add(0x16, window_class="ConsoleWindowClass", visible=False)
    clock = [100.0]
    monkeypatch.setattr(ww.time, "monotonic", lambda: clock[0])
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x16)
    clock[0] = 109.0
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x16)
    clock[0] = 110.0
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x16)

    end = [record for record in records if record["kind"] == "window_end"][0]
    assert (end["visible_seconds"], end["lifetime_seconds"]) == (1.0, 10.0)


@pytestmark_nt
def test_a_window_hidden_and_shown_again_accumulates_both_intervals(ww, desktop, monkeypatch):
    """Hide does not evict the record, so the second show used to be ignored.

    ``_mark_shown`` returned early whenever ``shown_at`` was set, and ``_close``
    on a HIDE never cleared it -- so a window shown, hidden and shown again
    measured from its *first* show and charged the hidden gap to the screen.
    """

    desktop.add(0x15, window_class="ConsoleWindowClass", visible=False)
    clock = [0.0]
    monkeypatch.setattr(ww.time, "monotonic", lambda: clock[0])
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x15)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x15)
    clock[0] = 2.0
    watcher.on_event(ww.EVENT_OBJECT_HIDE, 0x15)
    clock[0] = 30.0
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x15)
    clock[0] = 33.0
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x15)

    ends = [record for record in records if record["kind"] == "window_end"]
    assert ends[-1]["visible_seconds"] == 5.0, "2s + 3s on screen, not the 33s it existed"
    assert ends[-1]["lifetime_seconds"] == 33.0
    assert sum(watcher.counts.values()) == 1, "one window, however many times it appeared"


@pytestmark_nt
def test_the_report_separates_windows_created_from_windows_seen(ww, tmp_path, desktop):
    """The summary line is what an evidence document quotes."""

    desktop.add(0x21, window_class="PseudoConsoleWindow", visible=False)
    desktop.add(0x22, window_class="ConsoleWindowClass", visible=False)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x21)
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x21)
    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x22)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x22)
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x22)

    # Through `_write`, so the file carries the sound session pair a real
    # capture always writes (a file with no session at all is
    # not a quiet desktop). This test is about the summary line, not sessions.
    evidence = _write(tmp_path, records)
    printed = io.StringIO()
    assert ww.report(evidence, stream=printed) == 0
    text = printed.getvalue()

    assert "2 window(s), 2 of them console-class, 1 of those ever on screen" in text
    assert "[NEVER ON SCREEN]" in text
    assert text.count("[NEVER ON SCREEN]") == 1
    assert "on screen      never;" in text


# --- The observer must not be the popup it is looking for ---------------------


def test_the_instrument_documents_how_to_start_it_without_a_popup():
    """A launch procedure is the whole fix, so it has to still be there.

    ``windowwatch.py`` runs under console-subsystem ``python.exe``. Started
    from a parent with no console -- a scheduled task, a service, an agent
    harness -- Windows allocates one for it, with a window, before the file's
    first line runs and therefore before its hook exists: an unobserved popup
    from the tool that exists to find popups. Nothing
    inside the file can fix that, because by then the window is up. So the
    resolution is documented, and a documented resolution that quietly goes
    missing in a refactor is no resolution.

    Pins a decision rather than reintroducing a defect: there was no code to
    revert. Imports nothing and runs on every platform.
    """

    source = WINDOWWATCH.read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(source, filename=str(WINDOWWATCH))) or ""

    assert "pythonw.exe" in docstring and "CREATE_NO_WINDOW" in docstring, (
        "the module docstring must name a console-less way to launch it"
    )
    assert "existing terminal" in docstring or "already open" in docstring



# --- An HWND is a number Windows hands out again ------------------------------


#: A sound session pair, wrapped around fixtures that are about something else.
#: A file with no session records at all is not a quiet
#: desktop -- it is evidence of nothing -- and a window record with
#: no `observation_raced` field predates the mid-assembly check rather than
#: having passed it. Both are right for real evidence files and merely make
#: these formatting fixtures unrealistic, so the fixtures are completed here
#: instead of the rules being softened. A test that is *about* a missing
#: session or a legacy record writes its own file and gets neither.
_SOUND_START = {"kind": "session", "event": "start", "label": None}
_SOUND_STOP = {
    "kind": "session", "event": "stop", "label": None,
    "queue_drained": True, "dropped_events": 0, "dropped_events_exact": True,
    "sampler_stopped": True, "raced_records": 0,
    "cleanup_failures": {}, "callback_failures": {}, "sample_failures": {},
}


def _write(tmp_path, records, name: str = "evidence.jsonl") -> Path:
    complete = list(records)
    if not any(record.get("kind") == "session" for record in complete):
        complete = [_SOUND_START, *complete, _SOUND_STOP]
    complete = [
        {**record, "observation_raced": False}
        if record.get("kind") == "window" and "observation_raced" not in record
        else record
        for record in complete
    ]
    evidence = tmp_path / name
    evidence.write_text("\n".join(json.dumps(record) for record in complete), encoding="utf-8")
    return evidence


def _print(ww, evidence, expected: int = 0) -> str:
    """``report()`` over one evidence file, returning what it printed.

    ``expected`` is the exit code the file deserves, and it is asserted rather
    than ignored: a report over sessions that never closed
    returns ``EXIT_UNSOUND_CAPTURE``, because "0 window(s)" from a capture that
    was cut short reads exactly like "0 window(s)" from a quiet desktop.
    """

    printed = io.StringIO()
    assert ww.report(evidence, stream=printed) == expected
    return printed.getvalue()


@pytestmark_nt
def test_a_recycled_hwnd_is_reported_as_two_windows_not_one(ww, tmp_path, desktop, monkeypatch):
    """The omission half of HWND reuse, and the quietest of its three effects.

    ``report`` skipped any HWND it had already printed, so the second window
    to hold a recycled handle was not merely mis-described -- it was absent.
    Windows frees a handle for reuse the moment the window is destroyed, and
    the windows this instrument exists to catch are short-lived by definition,
    so the collision is likeliest for exactly the flashes that matter.

    Here one handle is used twice: shown the first time, never shown the
    second. Two blocks, one popup.
    """

    monkeypatch.setattr(ww.time, "monotonic", lambda: 0.0)
    desktop.add(0x40, window_class="ConsoleWindowClass", visible=False)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x40, 1_000)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x40, 1_000)
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x40, 1_500)
    # Windows hands the same handle to the next window. A different process,
    # and this one never reaches the screen.
    desktop.add(0x40, window_class="PseudoConsoleWindow", visible=False, pid=5151)
    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x40, 9_000)
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x40, 9_400)

    text = _print(ww, _write(tmp_path, records))

    assert "2 window(s), 2 of them console-class, 1 of those ever on screen" in text, text
    assert text.count("[NEVER ON SCREEN]") == 1, text
    assert "0.5s of a 0.5s lifetime" in text, "the first window's duration"
    assert "never; existed 0.4s" in text, "and the second one's, not the first's again"


@pytestmark_nt
def test_a_recycled_hwnd_does_not_inherit_the_earlier_window_s_visibility(ww, tmp_path, desktop, monkeypatch):
    """The reverse of the case above, and the one that manufactures evidence.

    ``_ever_visible`` was a set of HWNDs for the whole file, so a handle that
    had *ever* been on screen made every later window holding that handle a
    popup. That direction invents a flash the user never saw and puts a real
    process's command line under it, which is the failure mode this whole
    tool is an argument against.
    """

    monkeypatch.setattr(ww.time, "monotonic", lambda: 0.0)
    desktop.add(0x41, window_class="ConsoleWindowClass", visible=False)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x41, 0)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x41, 0)
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x41, 100)
    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x41, 200)
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x41, 300)

    entries = ww._occurrences([json.loads(json.dumps(record)) for record in records])

    assert [entry["shown"] for entry in entries] == [True, False]
    assert entries[0]["end"] is not entries[1]["end"], "each instance keeps its own end record"


@pytestmark_nt
def test_two_sessions_appended_to_one_file_are_not_one_window(ww, tmp_path, desktop, monkeypatch):
    """``--out`` appends, which is what makes the collision ordinary.

    A second run of the instrument against the same evidence file starts a
    fresh desktop's worth of handles, and there is no reason the numbers
    should not repeat. Under the defect the second session's window silently
    inherited the first session's visibility and end record.
    """

    monkeypatch.setattr(ww.time, "monotonic", lambda: 0.0)
    desktop.add(0x42, window_class="ConsoleWindowClass", visible=False)
    records: list[dict] = []

    first = ww.Watcher(_FakeTable(), records.append, session="session-a")
    records.append({"kind": "session", "event": "start", "label": None})
    first.on_event(ww.EVENT_OBJECT_CREATE, 0x42, 0)
    first.on_event(ww.EVENT_OBJECT_SHOW, 0x42, 0)
    first.on_event(ww.EVENT_OBJECT_HIDE, 0x42, 700)
    # No destroy: the first run's deadline arrived while the window was alive.

    second = ww.Watcher(_FakeTable(), records.append, session="session-b")
    records.append({"kind": "session", "event": "start", "label": None})
    second.on_event(ww.EVENT_OBJECT_CREATE, 0x42, 0)
    second.on_event(ww.EVENT_OBJECT_DESTROY, 0x42, 250)

    text = _print(ww, _write(tmp_path, records), ww.EXIT_UNSOUND_CAPTURE)

    assert "2 window(s), 2 of them console-class, 1 of those ever on screen" in text, text
    assert "never; existed 0.25s" in text, text


@pytestmark_nt
def test_an_evidence_file_with_no_occurrence_field_is_still_separated(ww, tmp_path):
    """The fallback, on a file written before ``occurrence`` existed.

    Grouping by ``occurrence`` is the mechanism; reading the lifecycle in
    order is what makes it a correctness fix rather than a format change. A
    destroy frees the handle and a session ``start`` closes everything still
    open, so an old file is separated by the two boundaries it always carried.
    """

    def legacy(hwnd, kind="window", **overrides):
        record = _window_record(hwnd=hwnd, **overrides)
        record["kind"] = kind
        record.pop("occurrence", None)
        return record

    records = [
        {"kind": "session", "event": "start", "label": None},
        legacy(0x43, event="create", visible=False, ever_visible=False),
        legacy(0x43, event="show", visible=True, ever_visible=True),
        {"kind": "window_end", "hwnd": 0x43, "event": "destroy", "window_class": "ConsoleWindowClass",
         "console_class": True, "pid": 1, "image": None, "ever_visible": True,
         "visible_seconds": 1.0, "lifetime_seconds": 1.0},
        legacy(0x43, event="create", visible=False, ever_visible=False),
        {"kind": "window_end", "hwnd": 0x43, "event": "destroy", "window_class": "ConsoleWindowClass",
         "console_class": True, "pid": 2, "image": None, "ever_visible": False,
         "visible_seconds": 0.0, "lifetime_seconds": 0.25},
    ]

    entries = ww._occurrences(records)
    assert [entry["shown"] for entry in entries] == [True, False]
    assert [entry["end"]["lifetime_seconds"] for entry in entries] == [1.0, 0.25]

    text = _print(ww, _write(tmp_path, records), ww.EXIT_UNSOUND_CAPTURE)
    assert "2 window(s), 2 of them console-class, 1 of those ever on screen" in text, text


@pytestmark_nt
def test_every_record_a_session_writes_carries_its_occurrence(ww, desktop):
    """The identity has to be on the record or none of the above can join on it.

    Pins the design decision rather than reintroducing a defect: there is
    nothing to revert here, because the field did not exist before. What it
    catches is a future record type written without one, which would silently
    fall back to the HWND for that record alone -- the worst of both.
    """

    desktop.add(0x44, window_class="ConsoleWindowClass", visible=False)
    watcher, records = _watcher(ww, desktop, session="fixed-session")

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x44, 0)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x44, 0)
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x44, 100)
    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x44, 200)

    assert all(record.get("occurrence") for record in records), records
    assert {record["occurrence"] for record in records} == {"fixed-session:1", "fixed-session:2"}


# --- Dispatch time is not event time ------------------------------------------


@pytestmark_nt
def test_a_queued_show_and_hide_are_measured_by_when_they_happened(ww, desktop, monkeypatch):
    """The number the evidence document quotes, taken from the wrong clock.

    ``WINEVENT_OUTOFCONTEXT`` queues the notification and the callback runs
    when this thread reads it, so the dispatch clock measures the drain's
    backlog, not the window's time on screen. A window up for 80 ms whose
    SHOW and HIDE sat in the queue together and were dispatched back to back
    measured as a millisecond -- of queue-processing latency, reported as the
    user's experience.

    Here the dispatch clock deliberately disagrees with the event clock: the
    two events are dispatched 1 ms apart having happened 80 ms apart. 0.08 is
    the answer; 0.001 is the defect.
    """

    desktop.add(0x51, window_class="ConsoleWindowClass", visible=False)
    dispatch = [500.0]
    monkeypatch.setattr(ww.time, "monotonic", lambda: dispatch[0])
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x51, 1_000_000)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x51, 1_000_020)
    dispatch[0] = 500.001  # the whole backlog arrives in one batch
    watcher.on_event(ww.EVENT_OBJECT_HIDE, 0x51, 1_000_100)

    end = [record for record in records if record["kind"] == "window_end"][0]
    assert end["visible_seconds"] == 0.08, "measured from dwmsEventTime, not from the drain"
    assert end["lifetime_seconds"] == 0.1
    assert end["timing_source"] == "event"


@pytestmark_nt
def test_the_dispatch_clock_is_the_documented_fallback_and_says_so(ww, desktop, monkeypatch):
    """A call with no event stamp still measures, and the record admits which.

    A duration whose provenance is not written down is the whole of the
    dispatch-time defect, so the fallback is labelled rather than silent.
    """

    desktop.add(0x52, window_class="ConsoleWindowClass", visible=False)
    clock = [10.0]
    monkeypatch.setattr(ww.time, "monotonic", lambda: clock[0])
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x52)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x52)
    clock[0] = 12.5
    watcher.on_event(ww.EVENT_OBJECT_DESTROY, 0x52)

    end = [record for record in records if record["kind"] == "window_end"][0]
    assert end["visible_seconds"] == 2.5
    assert end["timing_source"] == "dispatch"


@pytestmark_nt
def test_a_tick_count_that_wraps_does_not_invent_a_fifty_day_window(ww, desktop, monkeypatch):
    """``dwmsEventTime`` is a 32-bit millisecond counter and it wraps.

    Every 49.7 days, and a machine left running reaches it. A plain
    subtraction across the wrap gives 4,294,967 seconds, which would be
    reported as a console window on screen for seven weeks.
    """

    desktop.add(0x53, window_class="ConsoleWindowClass", visible=False)
    monkeypatch.setattr(ww.time, "monotonic", lambda: 0.0)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x53, 0xFFFF_FF00)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x53, 0xFFFF_FF00)
    watcher.on_event(ww.EVENT_OBJECT_HIDE, 0x53, 0x0000_0064)  # 356 ms later, past the wrap

    end = [record for record in records if record["kind"] == "window_end"][0]
    assert end["visible_seconds"] == 0.356


@pytestmark_nt
def test_a_window_still_on_screen_at_the_end_is_closed_out_not_left_open(ww, desktop, monkeypatch):
    """``finish()``: the recording ends, the window does not.

    Its interval stayed open, so its time on screen was counted as nothing at
    all -- and the one class of window most likely to still be up at the
    cutoff is the long-lived console host.

    This test used to advance the stop clock by injecting an unrelated event
    just before ``finish()``, which is exactly the condition that hid a
    review finding: the last event stamp and the stop moment agreed, because the test
    had arranged for them to. The unrelated event is still here and it is now
    a decoy -- the recording ends 7.5 s after it, and 8.5 s is the answer.
    """

    desktop.add(0x54, window_class="ConsoleWindowClass", visible=False)
    monkeypatch.setattr(ww.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(ww, "_tick_count", lambda: 9_500)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x54, 1_000)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x54, 1_000)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x55, 2_000)  # an unrelated window, long before the stop

    assert watcher.finish() == 1
    end = [record for record in records if record["kind"] == "window_end"]
    assert len(end) == 1
    assert end[0]["event"] == "recording_end", "the observation ended, not the window"
    assert end[0]["visible_seconds"] == 8.5, "measured to the stop, not to the last thing that moved"
    assert end[0]["timing_source"] == "event"
    assert watcher.finish() == 0, "and it is not closed out twice"


@pytestmark_nt
def test_a_quiet_window_open_at_the_stop_keeps_its_whole_tail(ww, desktop, monkeypatch):
    """The window nothing happened to, which is the usual one.

    ``finish()`` closed open intervals against ``_last_event_ms`` -- the last
    stamp any WinEvent carried. For a window that was shown and then sat there
    on a quiet desktop, the last such stamp is its own SHOW, so the whole time
    it was on screen measured as zero. A minute of console window, reported as
    ``0.0s``, in the file the Issue's conclusions are drawn from.

    Not one event after the show, deliberately: the previous test's injected
    event is what kept this from being noticed, so this one has nothing at all
    to fall back to.
    """

    desktop.add(0x5A, window_class="ConsoleWindowClass", visible=False)
    monkeypatch.setattr(ww, "_tick_count", lambda: 61_000)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x5A, 1_000)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x5A, 1_000)

    assert watcher.finish() == 1
    end = [record for record in records if record["kind"] == "window_end"][0]
    assert end["visible_seconds"] == 60.0, "a minute on screen is not zero seconds on screen"
    assert end["lifetime_seconds"] == 60.0
    assert end["timing_source"] == "event", "GetTickCount is the clock dwmsEventTime is read from"


@pytestmark_nt
def test_a_run_with_nothing_open_at_the_stop_asks_windows_for_nothing(ww, desktop, monkeypatch):
    """``finish()`` reads the tick only when a stamp is needed.

    Pins a design decision rather than catching a defect: the common ending is
    a session with no window still up, and it must not depend on a Win32 call
    to reach it -- which is also what keeps the ``watch()`` tests below able to
    run against a ``_kernel32`` stub that has nothing but ``CloseHandle``.
    """

    def refuse():
        raise AssertionError("the tick was read with no interval to close")

    desktop.add(0x5B, window_class="ConsoleWindowClass", visible=False)
    monkeypatch.setattr(ww, "_tick_count", refuse)
    watcher, _records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x5B, 1_000)  # created, never shown

    assert watcher.finish() == 0


@pytestmark_nt
def test_the_report_does_not_end_a_window_at_a_hide_it_came_back_from(ww, tmp_path, desktop, monkeypatch):
    """``report`` kept the last end record whatever followed it.

    Shown, hidden, shown again, and the recording ends: the last
    ``window_end`` on file is the hide, so the report announced that the
    window had ended -- by hide, with the duration of its first appearance --
    while it was on screen the whole time the reader was being told about.
    """

    desktop.add(0x56, window_class="ConsoleWindowClass", visible=False)
    monkeypatch.setattr(ww.time, "monotonic", lambda: 0.0)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x56, 0)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x56, 0)
    watcher.on_event(ww.EVENT_OBJECT_HIDE, 0x56, 2_000)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x56, 3_000)

    # Through `_write`, so the file carries the sound session pair a real
    # capture always writes (a file with no session at all is
    # not a quiet desktop). This test is about the summary line, not sessions.
    evidence = _write(tmp_path, records)
    printed = io.StringIO()
    assert ww.report(evidence, stream=printed) == 0
    text = printed.getvalue()

    assert "ended by hide" not in text, text
    assert "still up when the recording ended" in text, text


@pytestmark_nt
def test_the_report_prints_a_finished_window_as_still_up_with_its_duration(ww, tmp_path, desktop, monkeypatch):
    """``finish()``'s record is not a destroy and must not read as one."""

    desktop.add(0x57, window_class="ConsoleWindowClass", visible=False)
    monkeypatch.setattr(ww.time, "monotonic", lambda: 0.0)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x57, 0)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x57, 0)
    # The stop stamp, passed rather than inferred from a fabricated event: a
    # caller that knows when the recording ended may say so.
    watcher.finish(4_000)

    # Through `_write`, so the file carries the sound session pair a real
    # capture always writes (a file with no session at all is
    # not a quiet desktop). This test is about the summary line, not sessions.
    evidence = _write(tmp_path, records)
    printed = io.StringIO()
    assert ww.report(evidence, stream=printed) == 0
    text = printed.getvalue()

    assert "4.0s of a 4.0s lifetime (still up when the recording ended)" in text, text
    assert "ended by recording_end" not in text


@pytestmark_nt
def test_the_hook_callback_hands_the_watcher_the_event_stamp(ww, tmp_path, monkeypatch):
    """The stamp is useless if the callback drops it, which is what it did.

    ``_time`` was the parameter's name and the underscore was accurate: it was
    never read. Asserted through ``watch()`` so the thing under test is the
    real callback Windows is given, not a hand-built call.
    """

    seen: list[tuple] = []

    class _CapturingUser32(_FakeUser32):
        def SetWinEventHook(self, *args):  # noqa: N802
            self.proc = args[3]
            return self.hook

    user32 = _CapturingUser32()
    monkeypatch.setattr(ww, "_user32", user32)
    monkeypatch.setattr(ww, "ProcessTable", lambda *a, **k: _FakeTable())
    monkeypatch.setattr(ww, "_WINEVENTPROC", lambda callback: callback)
    monkeypatch.setattr(ww, "CLEANUP", ww.CleanupLedger())
    monkeypatch.setattr(ww.Watcher, "on_event", lambda _self, *args: seen.append(args))

    assert ww.watch(0.0, tmp_path / "evidence.jsonl", label=None, all_windows=False) == 0
    user32.proc(user32.hook, ww.EVENT_OBJECT_SHOW, 0x61, ww.OBJID_WINDOW, 0, 7, 1_234_567)

    assert seen == [(ww.EVENT_OBJECT_SHOW, 0x61, 1_234_567)]


# --- F8: what the capture path does, and does not, do to a string -------------


def _window_record(**overrides) -> dict:
    record = {
        "kind": "window",
        "timestamp": "2026-09-10T00:00:00.000+00:00",
        "monotonic": 0.0,
        "label": None,
        "event": "create",
        "hwnd": 0x31,
        "window_class": "ConsoleWindowClass",
        "title": "",
        "console_class": True,
        "visible": True,
        "ever_visible": True,
        "rect": None,
        "pid": 4242,
        "image": None,
        "command_line": None,
        "parent_pid": None,
        "ancestors": [],
    }
    record.update(overrides)
    return record


def _reported(ww, tmp_path, record) -> str:
    """One window record, rendered. Wrapped in a sound session by `_write`,
    because a file with no session at all reports as
    evidence of nothing -- correct for a real file, and beside the point for a
    fixture that exists to check how one field is printed."""

    printed = io.StringIO()
    assert ww.report(_write(tmp_path, [record]), stream=printed) == 0
    return printed.getvalue()


@pytestmark_nt
def test_a_windows_path_survives_capture_and_report_byte_for_byte(ww, tmp_path):
    """It was not ``windowwatch`` that applied the escapes, and this pins it.

    ``json.dumps`` writes a backslash as ``\\\\`` and reading the line back
    gives the same string, so the whole capture-and-report path is faithful:
    the corruption in the evidence document was applied on the way *into* the
    document, downstream of everything this file does. Which also means a
    future "helpful" escaping added here would be a regression, and this is
    what would catch it.
    """

    command_line = INSTALLED_EXE + " --version"
    text = _reported(ww, tmp_path, _window_record(command_line=command_line, image=INSTALLED_EXE))

    assert command_line in text
    assert chr(7) not in text and chr(11) not in text and chr(0) not in text


@pytestmark_nt
def test_a_control_character_in_captured_text_is_escaped_in_the_report(ww, tmp_path):
    """The hazard the report *can* remove: text copied out of another process.

    A window title and a command line are somebody else's bytes, and this
    output is read by a human and pasted into a tracked document. One raw NUL
    there is what review finding F8 was.
    """

    text = _reported(ww, tmp_path, _window_record(title="alarm" + chr(7) + "and" + chr(0) + "end"))

    assert "alarm\\x07and\\x00end" in text
    assert chr(7) not in text and chr(0) not in text


# --- F5: a pid is not an identity ---------------------------------------------

#: Windows allocates process ids in multiples of four, so an odd one cannot be
#: a live process on this machine. That makes these safe to build a synthetic
#: table around: ``OpenProcess`` refuses them, so the table falls back to what
#: it remembers, which is exactly the dead-ancestor path under test. Nothing
#: here names a pid that another test -- or another ``pytest -n auto`` worker --
#: could be holding.
GHOST_CHILD = 999_991
GHOST_PARENT = 999_993
GHOST_GRANDPARENT = 999_995


def _generation(pid: int, created: int, image: str, parent_pid: int | None = None) -> dict:
    return {"pid": pid, "image": image, "command_line": image, "parent_pid": parent_pid, "created": created}


def _world_entry(created: int, image: str, parent_pid: int | None = None) -> dict:
    """One process, as the fake OS knows it: when it started, and who from.

    ``parent_pid`` belongs to the *generation*, not to the number it holds,
    which is the distinction that matters here: a handle keeps answering
    for this entry after the pid has moved on to somebody else.
    """

    return {
        "created": created,
        "image": image,
        "command_line": image + " --flag",
        "parent_pid": parent_pid,
    }


class _FakeKernel32:
    """kernel32's process surface over a table this test decides.

    Written for a finding about the *gap between two
    calls*, so what this fake models above all is that a handle is a snapshot
    of a process while a pid is only a number: ``OpenProcess`` copies the
    generation that holds the pid at that instant into the handle, and every
    later reading goes through the handle. ``on_open`` is the seam that makes
    the defect reproducible without waiting for Windows to recycle anything --
    it fires *after* an open and can replace the process behind the pid, which
    is exactly the race a second ``OpenProcess`` would lose.

    ``ERROR_ACCESS_DENIED`` and ``ERROR_INVALID_PARAMETER`` are set for real,
    through ``ctypes.set_last_error``, because telling the two apart is what
    lets the table report a dead ancestor while refusing to invent a live one.
    """

    def __init__(self, live: dict[int, dict] | None = None, denied: frozenset[int] = frozenset()) -> None:
        self.live: dict[int, dict] = dict(live or {})
        self.denied = set(denied)
        self.handles: dict[int, dict] = {}
        self.opens: list[int] = []
        self.closed: list[int] = []
        self.on_open = None
        self._next = 0x1_0000

    # -- the surface `ProcessTable` uses -------------------------------------

    def OpenProcess(self, _access, _inherit, pid):  # noqa: N802 -- it is a Win32 name
        pid = int(pid)
        self.opens.append(pid)
        if pid in self.denied:
            ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED: alive, and not for you
            return 0
        generation = self.live.get(pid)
        if generation is None:
            ctypes.set_last_error(87)  # ERROR_INVALID_PARAMETER: not a process
            return 0
        self._next += 1
        self.handles[self._next] = dict(generation)  # the handle keeps THIS process
        if self.on_open is not None:
            self.on_open(pid)
        return self._next

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(int(handle))
        return 1

    # -- wiring ---------------------------------------------------------------

    def install(self, ww, monkeypatch) -> None:
        """Point the module's process readings at this world."""

        monkeypatch.setattr(ww, "_kernel32", self)
        monkeypatch.setattr(ww, "CLEANUP", ww.CleanupLedger())
        monkeypatch.setattr(ww, "_created_at", lambda handle: self.handles[int(handle)]["created"])
        monkeypatch.setattr(ww, "_image_path", lambda handle: self.handles[int(handle)]["image"])
        monkeypatch.setattr(ww, "_command_line", lambda handle: self.handles[int(handle)]["command_line"])
        # Through the handle, like every other reading: the handle holds the
        # generation that was open, whatever the pid means by now.
        monkeypatch.setattr(ww, "_parent_from_handle", lambda handle: self.handles[int(handle)]["parent_pid"])

    def leaked(self) -> set[int]:
        return set(self.handles) - set(self.closed)


@pytestmark_nt
def test_the_owner_is_described_through_the_handle_its_creation_time_came_from(ww, monkeypatch):
    """Pid reuse, happening inside the pid-reuse fix itself.

    ``get()`` used to open the pid, read its creation time, close it, and then
    reopen the pid to describe it -- filing the second process's image path and
    command line under the first process's creation time. Between those two
    calls Windows is free to recycle the pid, and the record it produced then
    was internally impossible: a real creation time and a real command line
    belonging to two different processes, with nothing downstream able to tell.

    Reproduced by replacing the process behind the pid the instant after it is
    first opened. The assertion is not on the mechanism but on the record: its
    creation time and its image must describe the *same* process.
    """

    kernel = _FakeKernel32({GHOST_CHILD: _world_entry(1_000, "gen-1000.exe")})
    kernel.install(ww, monkeypatch)
    kernel.on_open = lambda _pid: kernel.live.update({GHOST_CHILD: _world_entry(2_000, "gen-2000.exe")})

    resolved = ww.ProcessTable().get(GHOST_CHILD)

    assert resolved["image"] == f"gen-{resolved['created']}.exe", (
        f"the creation time and the identity came from different processes: {resolved}"
    )
    assert kernel.opens == [GHOST_CHILD], "two opens is the gap the pid can be recycled in"
    assert not kernel.leaked(), "the retained handle must still be released"


@pytestmark_nt
def test_the_owner_s_parent_is_read_through_the_handle_not_looked_up_by_pid(ww, monkeypatch):
    """The last field that still arrived by pid.

    ``_facts`` described the process the handle named, and then asked a
    ``CreateToolhelp32Snapshot`` walk who its parent was -- a lookup by
    number, made after the open, answered by whoever holds the number *now*.
    So a record could carry generation one's image, command line and creation
    time above generation two's parent, and ``ancestors()`` would then walk
    the wrong chain from it without anything to notice by: its creation-time
    check compares a child against its parent, and here the parent is a real
    live process that really does predate this one.

    The pid is replaced the instant after the open, as in the test above, and
    the snapshot the old code would have consulted is made to report the
    successor's parent -- because that is what a snapshot taken after the
    recycle would say.
    """

    lookups: list[int] = []

    def snapshot():
        lookups.append(1)
        return [(GHOST_CHILD, GHOST_GRANDPARENT)]  # the successor's parent

    monkeypatch.setattr(ww, "snapshot_pids", snapshot)
    kernel = _FakeKernel32({GHOST_CHILD: _world_entry(1_000, "gen-1000.exe", parent_pid=GHOST_PARENT)})
    kernel.install(ww, monkeypatch)
    kernel.on_open = lambda _pid: kernel.live.update(
        {GHOST_CHILD: _world_entry(2_000, "gen-2000.exe", parent_pid=GHOST_GRANDPARENT)}
    )

    resolved = ww.ProcessTable().get(GHOST_CHILD)

    assert resolved["created"] == 1_000 and resolved["image"] == "gen-1000.exe"
    assert resolved["parent_pid"] == GHOST_PARENT, (
        f"the parent belongs to the successor, not to the process described: {resolved}"
    )
    assert lookups == [], "a pid lookup asks about whoever holds the number now"


@pytestmark_nt
def test_sampling_takes_the_parent_from_the_process_it_opened(ww, monkeypatch):
    """The other side of the same gap: the snapshot is taken *before* the open.

    ``sample()`` walks ``snapshot_pids()`` and carried each pair's parent into
    the description of whatever it managed to open afterwards. If the pid was
    recycled in between -- the walk is milliseconds of work over a few hundred
    processes, and short-lived spawns are the subject of this whole tool --
    the generation stored had the successor's identity and the predecessor's
    parent.

    Here the snapshot says pid 4 started it; by the time the open happens the
    number belongs to a different process, which pid ``GHOST_PARENT`` started.
    """

    monkeypatch.setattr(ww, "snapshot_pids", lambda: [(GHOST_CHILD, 4)])
    kernel = _FakeKernel32({GHOST_CHILD: _world_entry(2_000, "successor.exe", parent_pid=GHOST_PARENT)})
    kernel.install(ww, monkeypatch)

    table = ww.ProcessTable()
    table.sample()

    stored = table.generations(GHOST_CHILD)[0]
    assert stored["image"] == "successor.exe"
    assert stored["parent_pid"] == GHOST_PARENT, (
        f"the snapshot's parent belongs to the pid's previous holder: {stored}"
    )


@pytestmark_nt
def test_a_parent_the_handle_will_not_name_is_unknown_rather_than_guessed(ww, monkeypatch):
    """Pins the choice made where the handle cannot answer.

    A pid lookup could still produce a number here, and the number would look
    exactly as plausible as a right one. Unknown stays unknown (``AGENTS.md``
    invariant 4), and the rest of the record is unaffected.
    """

    monkeypatch.setattr(ww, "snapshot_pids", lambda: [(GHOST_CHILD, 4)])
    kernel = _FakeKernel32({GHOST_CHILD: _world_entry(2_000, "orphan.exe")})
    kernel.install(ww, monkeypatch)
    monkeypatch.setattr(ww, "_parent_from_handle", lambda _handle: None)

    resolved = ww.ProcessTable().get(GHOST_CHILD)

    assert resolved["parent_pid"] is None
    assert resolved["image"] == "orphan.exe", "and nothing else about the record is lost"


@pytestmark_nt
def test_an_unexpected_open_failure_is_not_read_as_proof_the_process_is_gone(ww, monkeypatch):
    """``_ERROR_INVALID_PARAMETER`` was defined and never checked.

    ``get()`` asked whether the failure was ``ERROR_ACCESS_DENIED`` and fell
    back to the table's memory on everything else -- while its own contract
    says only error 87 proves the pid is not a process. Anything else that can
    make ``OpenProcess`` fail (1450, ``ERROR_NO_SYSTEM_RESOURCES``, is the
    realistic one on a machine under load, which is when this instrument is
    running) was therefore read as a death certificate, and a live process
    holding a recycled number was handed the image path and command line of
    whatever the table last saw under it.
    """

    kernel = _FakeKernel32()
    kernel.install(ww, monkeypatch)

    def refuse(_access, _inherit, _pid):
        ctypes.set_last_error(1450)  # ERROR_NO_SYSTEM_RESOURCES: not a verdict on the pid
        return 0

    monkeypatch.setattr(kernel, "OpenProcess", refuse)
    table = ww.ProcessTable()
    table._remember(GHOST_CHILD, _generation(GHOST_CHILD, 1_000, "C:\\GHOST\\previous-holder.exe"))

    resolved = table.get(GHOST_CHILD)

    assert resolved["image"] is None, f"an error this file cannot read is not evidence: {resolved}"
    assert resolved["created"] is None
    assert table.generations(GHOST_CHILD), "and nothing the table saw was dropped"


@pytestmark_nt
def test_a_live_owner_that_refuses_to_be_opened_is_unknown_not_the_remembered_one(ww, monkeypatch):
    """``ERROR_ACCESS_DENIED`` means the process is there, not that it is that one.

    When the creation time could not be read, ``get()`` returned the newest
    generation the table remembered under that pid -- for a *live* process. A
    protected process (System, anything elevated) refuses the open every time,
    so a pid it now holds, which some ordinary process held an hour ago, was
    attributed the ordinary process's image and command line. That is an
    invented owner, and an invented ancestor chain above it.
    """

    kernel = _FakeKernel32(denied=frozenset({GHOST_CHILD}))
    kernel.install(ww, monkeypatch)
    table = ww.ProcessTable()
    table._remember(GHOST_CHILD, _generation(GHOST_CHILD, 1_000, "C:\\GHOST\\previous-holder.exe"))

    resolved = table.get(GHOST_CHILD)

    assert resolved["image"] is None, f"a live process that cannot be verified is unknown: {resolved}"
    assert resolved["command_line"] is None
    assert resolved["created"] is None
    assert table.generations(GHOST_CHILD), "and the table still remembers what it saw; nothing was dropped"


@pytestmark_nt
def test_a_live_owner_whose_creation_time_will_not_read_is_unknown(ww, monkeypatch):
    """The other half: the handle opens and ``GetProcessTimes`` fails anyway.

    Rarer than the refusal above and it means the same thing -- the process is
    alive and this instrument cannot say which generation it is -- so it gets
    the same answer rather than the table's memory.
    """

    kernel = _FakeKernel32({GHOST_CHILD: _world_entry(1_000, "live.exe")})
    kernel.install(ww, monkeypatch)
    monkeypatch.setattr(ww, "_created_at", lambda _handle: None)
    table = ww.ProcessTable()
    table._remember(GHOST_CHILD, _generation(GHOST_CHILD, 1_000, "C:\\GHOST\\previous-holder.exe"))

    resolved = table.get(GHOST_CHILD)

    assert resolved["image"] is None, f"unverifiable is unknown, not stale: {resolved}"
    assert not kernel.leaked()


@pytestmark_nt
def test_a_dead_ancestor_is_still_named_from_what_the_table_remembers(ww, monkeypatch):
    """The behaviour the fix above must not take with it.

    A pid that is *not* a process fails with ``ERROR_INVALID_PARAMETER``, and
    that is the dead-ancestor case this whole table exists for: parents die
    faster than their children, and a chain the observer cannot name is the
    thing the popup investigation could not answer before it. Refusing to
    answer here too would be an over-broad fix, and this is what would
    catch it.
    """

    kernel = _FakeKernel32()  # nothing is alive
    kernel.install(ww, monkeypatch)
    table = ww.ProcessTable()
    table._remember(GHOST_PARENT, _generation(GHOST_PARENT, 100, "long-dead-parent.exe"))

    assert table.get(GHOST_PARENT)["image"] == "long-dead-parent.exe"


@pytestmark_nt
def test_sampling_describes_the_generation_it_probed_and_not_its_successor(ww, monkeypatch):
    """``sample()`` carried the same two-open gap as ``get()``.

    Less visible than the window-owner case and the same defect: the table
    would remember a generation whose creation time and command line came from
    two different processes, and every later lookup and chain walk would then
    read that as one.
    """

    monkeypatch.setattr(ww, "snapshot_pids", lambda: [(GHOST_CHILD, 4)])
    kernel = _FakeKernel32({GHOST_CHILD: _world_entry(1_000, "gen-1000.exe")})
    kernel.install(ww, monkeypatch)
    kernel.on_open = lambda _pid: kernel.live.update({GHOST_CHILD: _world_entry(2_000, "gen-2000.exe")})

    table = ww.ProcessTable()
    table.sample()

    stored = table.generations(GHOST_CHILD)
    assert len(stored) == 1
    assert stored[0]["image"] == f"gen-{stored[0]['created']}.exe", stored[0]


@pytestmark_nt
def test_a_live_process_is_not_reported_as_the_dead_one_that_held_its_pid(ww):
    """The window-owner half of F5, which the chain walk could never catch.

    ``ancestors()`` compares a child's creation time against its parent's, so
    it can notice a recycled *parent*. It cannot notice that the child itself
    is the wrong process -- and the child is the window's owner, the one whose
    image path and command line the evidence document prints under the popup.

    Reproduced without waiting for Windows to recycle anything: the table is
    given a remembered generation for this process's own pid, created a second
    before the real one, exactly as it would hold after the previous holder of
    that pid exited. ``get()`` must resolve the generation that is alive.
    """

    table = ww.ProcessTable()
    table.sample()
    live = table.get(os.getpid())
    assert live["created"] is not None and live["image"], live

    ghost = _generation(os.getpid(), live["created"] - 10_000_000, "C:\\GHOST\\previous-holder.exe")
    table._remember(os.getpid(), ghost)
    assert len(table.generations(os.getpid())) == 2, "both generations are kept; nothing is overwritten"

    resolved = table.get(os.getpid())
    assert resolved["created"] == live["created"]
    assert resolved["image"] == live["image"], "the window would have been attributed to a dead process"


@pytestmark_nt
def test_sampling_records_a_new_generation_of_a_pid_it_already_knows(ww, monkeypatch):
    """``sample()`` skipped any pid it had seen, so a reused pid never updated."""

    monkeypatch.setattr(ww, "snapshot_pids", lambda: [(GHOST_CHILD, 4)])
    kernel = _FakeKernel32({GHOST_CHILD: _world_entry(1_000, "image-1000")})
    kernel.install(ww, monkeypatch)
    # The pid is recycled between the two samples.
    kernel.on_open = lambda _pid: kernel.live.update({GHOST_CHILD: _world_entry(2_000, "image-2000")})

    table = ww.ProcessTable()
    table.sample()
    kernel.on_open = None
    table.sample()

    assert [entry["image"] for entry in table.generations(GHOST_CHILD)] == ["image-1000", "image-2000"]


@pytestmark_nt
def test_a_second_sample_of_an_unchanged_pid_describes_nothing_again(ww, monkeypatch):
    """The probe replaces the skip; it must not replace the cheapness of it.

    Reading a command line out of a process's PEB is three cross-process memory
    reads. Doing that for every process ten times a second, rather than once
    per generation, would be a real cost -- so the guard is that ``_facts``
    is called once when the generation is unchanged, not five times.
    """

    monkeypatch.setattr(ww, "snapshot_pids", lambda: [(GHOST_CHILD, 4)])
    kernel = _FakeKernel32({GHOST_CHILD: _world_entry(1_000, "image")})
    kernel.install(ww, monkeypatch)
    described: list[int] = []
    real_facts = ww.ProcessTable._facts
    monkeypatch.setattr(
        ww.ProcessTable,
        "_facts",
        lambda self, handle, pid, **kwargs: (described.append(pid), real_facts(self, handle, pid, **kwargs))[1],
    )

    table = ww.ProcessTable()
    for _ in range(5):
        table.sample()
    assert described == [GHOST_CHILD]
    # The probe still costs one open per pid per sample, and no more.
    assert kernel.opens == [GHOST_CHILD] * 5
    assert len(kernel.leaked()) == 0, "a probe that does not close its handle leaks one per sample"


@pytestmark_nt
def test_generation_at_never_returns_a_process_that_did_not_exist_yet(ww):
    table = ww.ProcessTable()
    table._remember(GHOST_PARENT, _generation(GHOST_PARENT, 100, "first.exe"))
    table._remember(GHOST_PARENT, _generation(GHOST_PARENT, 300, "second.exe"))

    assert table.generation_at(GHOST_PARENT, 50) is None
    assert table.generation_at(GHOST_PARENT, 200)["image"] == "first.exe"
    assert table.generation_at(GHOST_PARENT, 300)["image"] == "second.exe"
    assert table.generation_at(GHOST_PARENT, 999)["image"] == "second.exe"


@pytestmark_nt
def test_the_chain_walks_the_parent_generation_that_was_alive_at_the_time(ww):
    """A grandparent's pid, reused between the two, must not enter the chain.

    Three remembered processes, two of them sharing pid ``GHOST_PARENT``: the
    child was created at 200, so its parent is the generation created at 100
    and not the one created at 500, however much more recent the second one is.
    Choosing generation before comparing creation times is what makes the
    difference; comparing first would have found the newer parent, seen that it
    postdates its child, and truncated a chain that is perfectly resolvable.
    """

    table = ww.ProcessTable()
    table._remember(GHOST_CHILD, _generation(GHOST_CHILD, 200, "child.exe", parent_pid=GHOST_PARENT))
    table._remember(GHOST_PARENT, _generation(GHOST_PARENT, 100, "real-parent.exe", parent_pid=GHOST_GRANDPARENT))
    table._remember(GHOST_PARENT, _generation(GHOST_PARENT, 500, "impostor.exe", parent_pid=None))
    table._remember(GHOST_GRANDPARENT, _generation(GHOST_GRANDPARENT, 50, "grandparent.exe"))

    chain = [entry.get("image") or entry.get("note") for entry in table.ancestors(GHOST_CHILD)]
    assert chain == ["real-parent.exe", "grandparent.exe"]


@pytestmark_nt
def test_the_chain_starts_from_the_generation_the_caller_resolved(ww):
    """One record may not mix one process's identity with another's ancestry.

    ``on_event`` resolved the owner through a handle and then
    called ``ancestors(pid)``, which looked the same number up *again*. If the
    pid was recycled between those two calls the record carried the first
    generation's image and command line beside the successor's chain -- the
    split identity already closed inside ``get()``, reappearing across the two
    calls that build one record.

    Both generations are remembered here, so the chain each one produces is
    the only thing that distinguishes them: passing ``start`` is what makes
    the answer belong to the generation the caller described rather than to
    whatever currently holds the number.
    """

    table = ww.ProcessTable()
    original = _generation(GHOST_CHILD, 200, "original.exe", parent_pid=GHOST_PARENT)
    successor = _generation(GHOST_CHILD, 800, "successor.exe", parent_pid=GHOST_GRANDPARENT)
    table._remember(GHOST_CHILD, original)
    table._remember(GHOST_CHILD, successor)
    table._remember(GHOST_PARENT, _generation(GHOST_PARENT, 100, "original-parent.exe"))
    table._remember(GHOST_GRANDPARENT, _generation(GHOST_GRANDPARENT, 700, "successor-parent.exe"))

    assert [e.get("image") for e in table.ancestors(GHOST_CHILD, start=original)] == [
        "original-parent.exe"
    ]
    assert [e.get("image") for e in table.ancestors(GHOST_CHILD, start=successor)] == [
        "successor-parent.exe"
    ]


@pytestmark_nt
def test_a_parent_only_a_snapshot_ever_asserted_is_not_walked(ww):
    """An unverified link may not be presented as a verified ancestor.

    The same defect, second half. ``sample()`` stores a snapshot-derived
    ``parent_pid`` for a process it could not open, and ``get()`` hands that
    entry back for a pid that is provably gone -- so the chain walk could
    follow a parent no handle ever confirmed, in exactly the shape a confirmed
    ancestor is reported in. The walk names the pid and stops instead, which
    keeps the link visible without claiming it was verified.
    """

    table = ww.ProcessTable()
    orphan = ww._unknown(GHOST_CHILD, parent_pid=GHOST_PARENT, from_snapshot=True)
    table._remember(GHOST_CHILD, orphan)
    table._remember(GHOST_PARENT, _generation(GHOST_PARENT, 100, "would-be-parent.exe"))

    chain = table.ancestors(GHOST_CHILD, start=orphan)
    assert [entry.get("note") for entry in chain] == [
        "parent unverified (snapshot only); chain ends here"
    ]
    assert chain[0]["image"] is None, "an unverified link is not given a name"
    assert chain[0]["pid"] == GHOST_PARENT, "but the pid is still reported"


@pytestmark_nt
def test_a_pid_seen_alive_but_never_described_ends_the_chain(ww):
    """The sighting `sample()` used to throw away.

    When a pid is unopenable the sampler kept the first generation it had and
    discarded the sighting -- so a *successor* it could never describe left no
    trace at all, and `generation_at()` then handed back the older generation
    for a child created afterwards. A real image path, a real command line, and
    the wrong process: the pid-reuse attribution this table exists to prevent,
    arriving through the one path that recorded nothing.

    The sighting is remembered as an ambiguity instead, and the walk stops at
    it. `is_ambiguous` is set directly here because the condition is "the
    sampler saw this and could not open it", which a fixture cannot stage
    against a real process on demand.
    """

    table = ww.ProcessTable()
    child = _generation(GHOST_CHILD, 200, "child.exe", parent_pid=GHOST_PARENT)
    table._remember(GHOST_CHILD, child)
    table._remember(GHOST_PARENT, _generation(GHOST_PARENT, 100, "long-dead.exe"))

    assert [e.get("image") for e in table.ancestors(GHOST_CHILD, start=child)] == [
        "long-dead.exe"
    ], "the control: an unambiguous pid is still named"

    table._ambiguous.add(GHOST_PARENT)
    chain = table.ancestors(GHOST_CHILD, start=child)
    assert [entry.get("note") for entry in chain] == [
        "pid may hold an undescribed generation; chain ends here"
    ]
    assert chain[0]["image"] is None, "an ambiguous pid is not given a name"


@pytestmark_nt
def test_a_handle_that_stops_naming_a_window_marks_the_record(ww, desktop, monkeypatch):
    """The multi-read race, detected rather than assumed away.

    `on_event()` asks the same foreign handle for its root, pid, class,
    visibility and rectangle in turn, and Windows documents that the window may
    be destroyed and the handle reused between any two of those calls. There is
    no API that reads them atomically, so the race cannot be closed from here.
    What can be done is notice it: if the handle has stopped naming a window by
    the time the reads finish, the record may describe more than one window and
    says so.

    The record is still written. Dropping it would lose a window that really did
    appear, which is the opposite of what this instrument is for.
    """

    desktop.add(0x6A, window_class="ConsoleWindowClass", visible=True)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x6A, 1_000)
    written = [r for r in records if r["kind"] == "window"]
    assert written and written[0]["observation_raced"] is False, (
        "the ordinary case must record the check, not omit the field"
    )

    # A handle that answers the early questions and then stops being a window.
    desktop.add(0x6B, window_class="ConsoleWindowClass", visible=True)
    watcher2, records2 = _watcher(ww, desktop)
    answers = iter([True, False])
    real_is_window = ww._user32.IsWindow
    monkeypatch.setattr(
        ww._user32, "IsWindow", lambda handle: next(answers, False) and real_is_window(handle)
    )
    watcher2.on_event(ww.EVENT_OBJECT_CREATE, 0x6B, 1_000)
    written2 = [r for r in records2 if r["kind"] == "window"]
    assert written2, "the window is still recorded; a raced read is not a dropped one"
    assert written2[0]["observation_raced"] is True


@pytestmark_nt
def test_a_report_over_a_raced_record_does_not_read_as_clean(ww, tmp_path):
    """A flag nothing reads is not a fix.

    `--report` is the documented way the evidence file is read, and a record
    assembled from a handle that stopped naming a window may conflate two
    windows. The report names those records and returns non-zero, so a reader
    who never saw the capture cannot take the summary at face value.
    """

    records = [
        {"kind": "session", "event": "start", "label": "raced"},
        {
            "kind": "window", "event": "EVENT_OBJECT_CREATE", "hwnd": 4242,
            "occurrence": "s:1", "timestamp": "2026-09-12T00:00:00Z",
            "window_class": "ConsoleWindowClass", "console_class": True,
            "visible": True, "ever_visible": True, "pid": 1234,
            "observation_raced": True,
        },
        {"kind": "session", "event": "stop", "label": "raced", "windows": 1,
         "visible_windows": 1, "queue_drained": True, "sampler_stopped": True,
         "cleanup_failures": {}, "callback_failures": {}, "sample_failures": {}},
    ]
    text = _print(ww, _write(tmp_path, records), ww.EXIT_UNSOUND_CAPTURE)
    assert "may not all describe the same window" in text
    assert "4242" in text


@pytestmark_nt
def test_a_report_over_a_lost_backlog_does_not_read_as_clean(ww, tmp_path):
    """"0 window(s)" from a broken capture is the dangerous shape.

    A run whose queue was never drained saw less than it should have, and the
    summary it produces is indistinguishable from a quiet desktop's. The report
    says so and returns non-zero rather than leaving it to a terminal nobody
    kept.
    """

    records = [
        {"kind": "session", "event": "start", "label": "lossy"},
        {"kind": "session", "event": "stop", "label": "lossy", "windows": 0,
         "visible_windows": 0, "queue_drained": False, "dropped_events": 17,
         "dropped_events_exact": False, "sampler_stopped": True,
         "cleanup_failures": {}, "callback_failures": {}, "sample_failures": {}},
    ]
    text = _print(ww, _write(tmp_path, records), ww.EXIT_UNSOUND_CAPTURE)
    assert "NOT a sound capture" in text
    assert "17+ queued event(s) were never dispatched" in text


@pytestmark_nt
def test_a_report_over_a_sound_capture_still_returns_zero(ww, tmp_path):
    """The control: the three warnings above must not fire on a good file."""

    records = [
        {"kind": "session", "event": "start", "label": "clean"},
        {"kind": "session", "event": "stop", "label": "clean", "windows": 0,
         "visible_windows": 0, "queue_drained": True, "dropped_events": 0,
         "dropped_events_exact": True, "sampler_stopped": True,
         "cleanup_failures": {}, "callback_failures": {}, "sample_failures": {}},
    ]
    text = _print(ww, _write(tmp_path, records), 0)
    assert "WARNING" not in text


@pytestmark_nt
def test_a_dead_pid_that_was_once_undescribable_is_not_named(ww):
    """Provably gone is not permission to hand back the last generation.

    `get()` reads `ERROR_INVALID_PARAMETER` as "this pid is
    dead, so the table's memory is all there is" -- which is right, unless the
    sampler once saw this pid alive and could not describe it. Then the newest
    remembered generation may not be the one that just died, and returning it
    attaches a real image and command line to whatever asked.
    """

    table = ww.ProcessTable()
    table._remember(GHOST_PARENT, _generation(GHOST_PARENT, 100, "predecessor.exe"))

    def gone(_pid):
        ctypes.set_last_error(ww._ERROR_INVALID_PARAMETER)
        return None

    table._open = gone
    assert table.get(GHOST_PARENT)["image"] == "predecessor.exe", (
        "the control: without ambiguity the remembered generation is the answer"
    )

    table._ambiguous.add(GHOST_PARENT)
    assert table.get(GHOST_PARENT)["image"] is None, (
        "a pid that was once alive and undescribable may not be named from memory"
    )


@pytestmark_nt
def test_a_report_over_records_that_predate_the_race_check_says_so(ww, tmp_path):
    """A missing flag is not a passed check.

    `observation_raced` did not exist before the race check, so a record written by an
    older build carries no field at all. Reading that as `false` would report
    "the check ran and found nothing" about a build that never checked.
    """

    legacy = {
        "kind": "window", "event": "EVENT_OBJECT_CREATE", "hwnd": 7, "occurrence": "s:1",
        "timestamp": "t", "window_class": "ConsoleWindowClass", "console_class": True,
        "visible": False, "ever_visible": False, "pid": 9,
    }
    path = tmp_path / "legacy.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in (_SOUND_START, legacy, _SOUND_STOP)),
        encoding="utf-8",
    )
    printed = io.StringIO()
    assert ww.report(path, stream=printed) == ww.EXIT_UNSOUND_CAPTURE
    assert "predate the mid-assembly validity check" in printed.getvalue()


@pytestmark_nt
def test_a_stop_record_that_says_nothing_about_health_is_not_sound(ww):
    """Absent is not sound.

    `session_health()` tested for `is False`, so a stop record that simply had
    no `queue_drained` -- truncated, or written by an older build -- passed as
    though it had reported success. "No evidence" and "evidence of success"
    must not read the same.
    """

    assert ww.session_health({"queue_drained": True, "sampler_stopped": True}) == []
    why = ww.session_health({})
    assert any("queue drained" in reason for reason in why)
    assert any("sampler stopped" in reason for reason in why)


@pytestmark_nt
def test_a_file_with_no_session_at_all_is_evidence_of_nothing(ww, tmp_path):
    """An empty file is not a quiet desktop."""

    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    printed = io.StringIO()
    assert ww.report(path, stream=printed) == ww.EXIT_UNSOUND_CAPTURE
    assert "evidence of nothing" in printed.getvalue()


@pytestmark_nt
def test_a_stop_record_that_could_not_be_written_fails_the_run(ww, tmp_path, monkeypatch):
    """A file that cannot say how complete it is may not exit 0.

    The write failure was printed and then forgotten, so a run that produced an
    evidence file with no stop record at all still returned success -- and a
    reader of that file has no way to tell an interrupted capture from a quiet
    desktop, which is the unsound-capture case arriving by a different route.
    """

    class _RefusesTheStop:
        """A file object that accepts everything except the stop record."""

        def __init__(self, handle):
            self._handle = handle

        def write(self, text):
            if '"stop"' in text:
                raise OSError("the disk went away")
            return self._handle.write(text)

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

    out = tmp_path / "evidence.jsonl"
    real_open = type(out).open

    def refusing_open(self, *args, **keywords):
        return _RefusesTheStop(real_open(self, *args, **keywords))

    monkeypatch.setattr(type(out), "open", refusing_open)
    code = _hidden_run(ww, monkeypatch, _FakeUser32(), out)
    assert code == ww.EXIT_UNSOUND_CAPTURE, (
        "a run whose stop record never reached the file reported success"
    )


@pytestmark_nt
def test_a_parent_that_postdates_its_child_still_truncates_the_chain(ww):
    """The existing guard survives the rewrite: unknown stays unknown."""

    table = ww.ProcessTable()
    table._remember(GHOST_CHILD, _generation(GHOST_CHILD, 200, "child.exe", parent_pid=GHOST_PARENT))
    table._remember(GHOST_PARENT, _generation(GHOST_PARENT, 900, "impostor.exe"))

    chain = table.ancestors(GHOST_CHILD)
    assert [entry.get("note") for entry in chain] == ["pid reused; chain ends here"]
    assert chain[0]["image"] is None


# --- F7: a bounded run stays bounded under a sustained event stream -----------


class _FloodingUser32(_FakeUser32):
    """A message queue that is refilled faster than it can be drained.

    The condition F7 is about, and the one a live test cannot reliably produce:
    it needs a desktop generating events faster than this process dispatches
    them for the whole of a run. ``peeks`` is capped so that the defect fails
    the test loudly instead of hanging the suite -- an infinite loop under
    ``pytest -n auto`` is not a test result, it is a stuck worker.
    """

    CAP = 100_000

    def __init__(self, queued: int | None = None) -> None:
        super().__init__()
        self.peeks = 0
        self.dispatched = 0
        #: ``None`` is the sustained-stream case -- the queue never runs
        #: dry. A number is a *finite* backlog, which is what the queue becomes
        #: the moment the hook is released, and is the condition the final
        #: drain exists for.
        self.queued = queued

    def PeekMessageW(self, *_args):  # noqa: N802
        self.peeks += 1
        if self.peeks > self.CAP:
            raise AssertionError(f"the drain never re-checked its deadline ({self.CAP} messages in)")
        if self.queued is None:
            return 1
        if self.queued <= 0:
            return 0
        self.queued -= 1
        return 1

    def TranslateMessage(self, *_args):  # noqa: N802
        return 1

    def DispatchMessageW(self, *_args):  # noqa: N802
        self.dispatched += 1
        return 0


@pytestmark_nt
def test_the_drain_stops_at_its_batch_size(ww, monkeypatch):
    flooding = _FloodingUser32()
    monkeypatch.setattr(ww, "_user32", flooding)
    handled, quit_seen = ww._drain(ww.wintypes.MSG(), deadline=ww.time.monotonic() + 3600)
    assert (handled, quit_seen) == (ww._DRAIN_BATCH, False)


@pytestmark_nt
def test_the_drain_stops_at_the_deadline_before_its_batch_size(ww, monkeypatch):
    """The deadline is checked *inside* the loop, which is the whole finding."""

    flooding = _FloodingUser32()
    monkeypatch.setattr(ww, "_user32", flooding)
    handled, quit_seen = ww._drain(ww.wintypes.MSG(), deadline=ww.time.monotonic() - 1)
    assert (handled, quit_seen) == (1, False)
    assert handled < ww._DRAIN_BATCH


@pytestmark_nt
def test_a_bounded_run_terminates_even_when_the_queue_never_empties(ww, tmp_path, monkeypatch):
    """``--seconds 300`` used to mean "300 seconds, unless the desktop is busy".

    The deadline was checked only outside the drain, and the drain ran until
    ``PeekMessageW`` returned false. The run that measured 3,559 console
    windows in 300 seconds was already within an order of magnitude of the
    event rate at which that loop stops returning -- so the instrument could
    have run past its own bound precisely during the observation that mattered
    most.

    The clock is a counter rather than the wall clock, so the assertion is on
    the loop's structure and not on how fast this machine happens to be.
    """

    flooding = _FloodingUser32()
    table = _FakeTable()
    monkeypatch.setattr(ww, "_user32", flooding)
    monkeypatch.setattr(ww, "ProcessTable", lambda *a, **k: table)
    monkeypatch.setattr(ww, "_WINEVENTPROC", lambda callback: callback)

    ticks = iter(range(10_000_000))
    monkeypatch.setattr(ww.time, "monotonic", lambda: next(ticks) / 1000.0)
    monkeypatch.setattr(ww.time, "sleep", lambda _seconds: None)
    # The final drain is bounded too, and against a queue that
    # never runs dry it is the bound that decides. Small budgets here so the
    # assertion is about the structure and not about this machine's speed.
    monkeypatch.setattr(ww, "_FINAL_DRAIN_LIMIT", 300)
    monkeypatch.setattr(ww, "_DROP_COUNT_LIMIT", 700)

    out = tmp_path / "evidence.jsonl"
    assert ww.watch(1.0, out, label=None, all_windows=False) == ww.EXIT_UNSOUND_CAPTURE
    # One `monotonic()` per dispatched message plus the outer checks: a 1.0s
    # budget on a 1ms tick is about a thousand messages, four batches' worth,
    # and the final drain adds its own 300.
    assert flooding.dispatched < 2000, flooding.dispatched
    assert flooding.unhooked == [flooding.hook]

    # "Terminated" and "terminated having dropped events" are different
    # results, and the file says which this was.
    stop = _stop_records(out)[0]
    assert stop["queue_drained"] is False
    assert stop["dropped_events"] == 700 and stop["dropped_events_exact"] is False


# --- F6: cleanup results are checked, not dropped -----------------------------


@pytestmark_nt
def test_the_ledger_counts_a_close_that_failed(ww, monkeypatch):
    """``CloseHandle``'s BOOL was being dropped, so a leak was unobservable."""

    monkeypatch.setattr(ww, "_kernel32", SimpleNamespace(CloseHandle=lambda handle: 0))
    monkeypatch.setattr(ww, "_user32", SimpleNamespace(UnhookWinEvent=lambda hook: 0))
    ledger = ww.CleanupLedger()

    assert ledger.failures == {}
    assert ledger.close_handle(0x1234) is False
    assert ledger.unhook(0x1234) is False
    assert ledger.failures == {"CloseHandle": 1, "UnhookWinEvent": 1}


# --- The backlog at the deadline is drained, or counted -----------------------


def _hidden_run(ww, monkeypatch, user32, out, *, seconds: float = 0.0, table=None) -> int:
    """``watch()`` against a chosen ``user32``, touching no desktop."""

    monkeypatch.setattr(ww, "_user32", user32)
    monkeypatch.setattr(ww, "ProcessTable", lambda *a, _t=(table or _FakeTable()), **k: _t)
    monkeypatch.setattr(ww, "_WINEVENTPROC", lambda callback: callback)
    monkeypatch.setattr(ww, "CLEANUP", ww.CleanupLedger())
    return ww.watch(seconds, out, label=None, all_windows=False)


@pytestmark_nt
def test_an_open_window_is_closed_against_the_stop_not_a_later_clock(ww, desktop, monkeypatch):
    """A caller-supplied stop stamp wins over a live clock.

    ``finish()`` read ``GetTickCount`` itself, and the run calls it *after*
    ``_final_drain``, which may take up to ``_FINAL_DRAIN_SECONDS``. So a
    window still on screen was credited with the drain's duration on top of its
    own -- and once the hook is released a hide or destroy inside that span
    cannot be observed at all, so the extra time is unobservable by
    construction, not merely generous.

    The live clock here reads 40s while the stop stamp says 10s. The window was
    shown at 0, so the honest answer is ten seconds.
    """

    desktop.add(0x5B, window_class="ConsoleWindowClass", visible=False)
    monkeypatch.setattr(ww, "_tick_count", lambda: 40_000)
    watcher, records = _watcher(ww, desktop)

    watcher.on_event(ww.EVENT_OBJECT_CREATE, 0x5B, 0)
    watcher.on_event(ww.EVENT_OBJECT_SHOW, 0x5B, 0)

    assert watcher.finish(stop_ms=10_000) == 1
    assert watcher.finished_at == 10_000
    end = [record for record in records if record["kind"] == "window_end"][0]
    assert end["visible_seconds"] == 10.0, (
        "the interval was closed against a clock read after the final drain"
    )


@pytestmark_nt
def test_the_stop_stamp_is_read_before_the_final_drain_runs(ww, tmp_path, monkeypatch):
    """The ordering the fix depends on, asserted rather than assumed.

    The fix needs the tick to be captured immediately after unhooking
    and passed in after the drain. The test above proves ``finish()`` honours a
    supplied stamp; this one proves the run actually reads that stamp *before*
    draining, which is the half a later refactor could quietly undo.
    """

    reads = []
    monkeypatch.setattr(ww, "_tick_count", lambda: (reads.append("tick"), 10_000)[1])

    drained_after = []
    real_drain = ww._final_drain

    def watched_drain(message, **keywords):
        drained_after.append(len(reads))
        return real_drain(message, **keywords)

    monkeypatch.setattr(ww, "_final_drain", watched_drain)
    _hidden_run(ww, monkeypatch, _FakeUser32(), tmp_path / "evidence.jsonl")

    assert drained_after, "the drain never ran; the ordering is not being measured"
    assert drained_after[0] >= 1, (
        "the final drain ran before the stop stamp was read, so the stamp "
        "includes the drain's own duration"
    )


@pytestmark_nt
def test_the_backlog_queued_at_the_deadline_is_still_dispatched(ww, tmp_path, monkeypatch):
    """The events the run used to throw away.

    ``WINEVENT_OUTOFCONTEXT`` means Windows queues a message rather than
    calling in at the moment of the event, so at the deadline there is a
    backlog -- largest on exactly the busy run whose numbers matter most. The
    loop dispatched at most one more message, exited, and unhooked, and every
    message still queued was a window that appeared and was never attributed,
    in a file that reads as though it had been.

    Here 500 messages are waiting when the observation window closes. All 500
    must be dispatched, and the stop record must say the queue ran dry.
    """

    flooding = _FloodingUser32(queued=500)
    out = tmp_path / "evidence.jsonl"
    assert _hidden_run(ww, monkeypatch, flooding, out) == 0

    assert flooding.dispatched == 500, "the queued backlog was dropped at the cutoff"
    stop = _stop_records(out)[0]
    assert stop["final_drain_dispatched"] == 500
    assert stop["queue_drained"] is True
    assert stop["dropped_events"] == 0


@pytestmark_nt
def test_a_backlog_too_large_to_drain_is_recorded_as_a_number_not_a_silence(ww, tmp_path, monkeypatch):
    """Terminated, and terminated having dropped N events, are different results.

    An evidence instrument that cannot finish is a fact about the run. An
    evidence instrument that cannot finish and does not say so produces a file
    a reader will take as complete, which is the worse failure of the two --
    so the budget is bounded and what the budget could not reach is counted.
    """

    flooding = _FloodingUser32(queued=5_000)
    monkeypatch.setattr(ww, "_FINAL_DRAIN_LIMIT", 100)
    out = tmp_path / "evidence.jsonl"
    assert _hidden_run(ww, monkeypatch, flooding, out) == ww.EXIT_UNSOUND_CAPTURE

    assert flooding.dispatched == 100, "the dispatch budget is what bounds the drain"
    stop = _stop_records(out)[0]
    assert stop["final_drain_dispatched"] == 100
    assert stop["queue_drained"] is False
    assert stop["dropped_events"] == 4_900, "counted, not guessed"
    assert stop["dropped_events_exact"] is True


@pytestmark_nt
def test_a_backlog_too_large_even_to_count_says_so(ww, tmp_path, monkeypatch):
    """Removing a message is cheap; it is not free, so counting is bounded too.

    Past that bound the number is a lower bound and the record says which,
    rather than reporting a cap as though it were a measurement.
    """

    flooding = _FloodingUser32(queued=5_000)
    monkeypatch.setattr(ww, "_FINAL_DRAIN_LIMIT", 10)
    monkeypatch.setattr(ww, "_DROP_COUNT_LIMIT", 50)
    out = tmp_path / "evidence.jsonl"
    assert _hidden_run(ww, monkeypatch, flooding, out) == ww.EXIT_UNSOUND_CAPTURE

    stop = _stop_records(out)[0]
    assert stop["dropped_events"] == 50
    assert stop["dropped_events_exact"] is False
    assert stop["queue_drained"] is False


@pytestmark_nt
def test_the_final_drain_stops_at_its_own_deadline(ww, monkeypatch):
    """The wall clock bounds it too, so a slow dispatcher cannot hang the exit."""

    flooding = _FloodingUser32()  # never runs dry
    monkeypatch.setattr(ww, "_user32", flooding)
    monkeypatch.setattr(ww, "_DROP_COUNT_LIMIT", 10)

    result = ww._final_drain(ww.wintypes.MSG(), deadline=ww.time.monotonic() - 1, limit=10_000)

    assert result["final_drain_dispatched"] == 1, "the deadline is checked inside the loop"
    assert result["queue_drained"] is False
    assert result["dropped_events"] == 10 and result["dropped_events_exact"] is False


@pytestmark_nt
def test_the_hook_is_released_before_the_backlog_is_drained(ww, tmp_path, monkeypatch):
    """Unhooking first is what makes the backlog finite rather than a target.

    Drain-then-unhook would be a queue refilled by the very events the drain
    is trying to reach the end of, which is the shape of the run that never
    terminates (review finding F7). Unhook-then-drain is a fixed number of
    messages and cannot fail to end.
    """

    order: list[str] = []

    class _OrderedUser32(_FloodingUser32):
        def UnhookWinEvent(self, hook):  # noqa: N802
            order.append("unhook")
            return super().UnhookWinEvent(hook)

        def PeekMessageW(self, *args):  # noqa: N802
            if self.queued:
                order.append("peek")
            return super().PeekMessageW(*args)

    out = tmp_path / "evidence.jsonl"
    assert _hidden_run(ww, monkeypatch, _OrderedUser32(queued=3), out) == 0

    assert order == ["unhook", "peek", "peek", "peek"], order


class _DispatchingUser32(_FakeUser32):
    """A queue whose messages actually reach the hook this run installed.

    ``_FloodingUser32`` counts dispatches; this one performs them, which is
    what this check needs: the failure it is about happens *inside* the
    callback, so the callback has to be the one Windows would call and it has
    to be reached the way Windows would reach it -- through the drain.
    """

    def __init__(self, events) -> None:
        super().__init__()
        self.events = list(events)
        self.dispatched = 0
        self.proc = None

    def SetWinEventHook(self, *args):  # noqa: N802
        self.proc = args[3]
        return self.hook

    def PeekMessageW(self, *_args):  # noqa: N802
        return 1 if self.events else 0

    def TranslateMessage(self, *_args):  # noqa: N802
        return 1

    def DispatchMessageW(self, *_args):  # noqa: N802
        event, hwnd, event_ms = self.events.pop(0)
        self.dispatched += 1
        # OBJID_WINDOW, no child: the shape the callback accepts.
        self.proc(self.hook, event, hwnd, 0, 0, 7, event_ms)
        return 0


# --- An instrument that fails silently reports a run it did not have ----------


@pytestmark_nt
def test_a_sample_that_raises_is_counted_rather_than_passed_over(ww, monkeypatch):
    """``except Exception: pass`` in the sampler loop.

    The loop is right not to stop: a process that exits mid-walk is ordinary,
    and an observer may not abort the observation it is in the middle of. But
    a sample that did not happen is a hole in the pid -> facts map, and every
    ancestor chain walked out of it afterwards is missing whatever lived in
    that gap -- while the stop record went on saying ``sampler_stopped: true``
    as though the sampler had been doing its job for the whole run.

    One iteration exactly: the stub sets the stop event before raising, so
    nothing here spins or waits.
    """

    table = ww.ProcessTable(interval=0.0)

    def boom():
        table._stop.set()
        raise RuntimeError("the snapshot walk failed")

    monkeypatch.setattr(table, "sample", boom)

    table._loop()  # returns, rather than propagating

    assert table.sample_failures.failures == {"RuntimeError": 1}


@pytestmark_nt
def test_the_stop_record_carries_what_the_sampler_could_not_do(ww, tmp_path, monkeypatch):
    """A count nobody can read is the same silence with extra steps.

    ``cleanup_failures`` is in the stop record because the evidence file is
    the only place a reader of it could check; the sampler's failures are
    there for the same reason and beside it.
    """

    ledger = ww.FailureLedger()
    ledger.note("OSError")
    ledger.note("OSError")
    table = _FakeTable(sample_failures=ledger)

    out = tmp_path / "evidence.jsonl"
    assert _hidden_run(ww, monkeypatch, _FakeUser32(), out, table=table) == ww.EXIT_UNSOUND_CAPTURE

    assert _stop_records(out)[0]["sample_failures"] == {"OSError": 2}


@pytestmark_nt
def test_a_callback_that_raises_is_counted_beside_the_drain_that_dispatched_it(
    ww, tmp_path, monkeypatch
):
    """The reviewer's own scenario, end to end.

    A hook callback may not raise into Windows, so the exception is swallowed
    -- and it was swallowed whole. ``_final_drain`` counts the message as
    dispatched either way, so the stop record for this run said
    ``final_drain_dispatched: 1``, ``queue_drained: true`` and
    ``dropped_events: 0``: a file that reads as a complete, clean recording of
    a window that was never attributed at all. The three numbers are still
    true; what was missing is the fourth, which is what makes them readable.
    """

    def boom(_self, *_args):
        raise RuntimeError("attribution failed")

    monkeypatch.setattr(ww.Watcher, "on_event", boom)
    user32 = _DispatchingUser32([(ww.EVENT_OBJECT_SHOW, 0x71, 1_000)])

    out = tmp_path / "evidence.jsonl"
    assert _hidden_run(ww, monkeypatch, user32, out) == ww.EXIT_UNSOUND_CAPTURE, "and it must not raise into Windows"

    assert user32.dispatched == 1
    stop = _stop_records(out)[0]
    assert stop["final_drain_dispatched"] == 1
    assert stop["queue_drained"] is True and stop["dropped_events"] == 0
    assert stop["windows"] == 0, "the message was dispatched and produced no record"
    assert stop["callback_failures"] == {"RuntimeError": 1}, (
        f"a dispatched event that attributed nothing left no trace: {stop}"
    )


# --- The stop record is written after the shutdown it describes ---------------


def _stop_records(out) -> list[dict]:
    return [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == "stop"
    ]


@pytestmark_nt
def test_the_stop_record_counts_a_handle_the_sampler_dropped_on_its_way_out(ww, tmp_path, monkeypatch):
    """``stop_record`` ran *before* ``table.stop`` during the unwind.

    ``ExitStack`` releases in reverse registration order, and the sampler was
    registered first, so it was released last -- after the record that was
    supposed to report on it. The sampler opens and closes a process handle
    per process ten times a second and keeps doing so until its stop is
    acknowledged, so a close that failed during those last iterations was
    invisible in the only place a reader of the evidence file could look.

    Here the sampler drops exactly one handle as it shuts down. The stop
    record must know.
    """

    fake_user32 = _FakeUser32()
    table = _FakeTable()
    monkeypatch.setattr(ww, "_user32", fake_user32)
    monkeypatch.setattr(ww, "_kernel32", SimpleNamespace(CloseHandle=lambda _handle: 0))
    monkeypatch.setattr(ww, "ProcessTable", lambda *a, **k: table)
    monkeypatch.setattr(ww, "_WINEVENTPROC", lambda callback: callback)
    monkeypatch.setattr(ww, "CLEANUP", ww.CleanupLedger())
    table.on_stop = lambda: ww.CLEANUP.close_handle(0x1234)

    out = tmp_path / "evidence.jsonl"
    assert ww.watch(0.0, out, label=None, all_windows=False) == ww.EXIT_UNSOUND_CAPTURE

    stop = _stop_records(out)
    assert stop and stop[0]["cleanup_failures"] == {"CloseHandle": 1}, stop


@pytestmark_nt
def test_the_stop_record_says_when_the_sampler_did_not_stop(ww, tmp_path, monkeypatch):
    """A two-second join that times out leaves a thread running past the run.

    ``ProcessTable.stop`` returned nothing, so the session claimed to be over
    while the sampler might still be sampling -- and every handle it opened
    after that point is outside the counts the stop record reports. Saying so
    is cheap; discovering it from a file that does not say so is not.
    """

    fake_user32 = _FakeUser32()
    monkeypatch.setattr(ww, "_user32", fake_user32)
    monkeypatch.setattr(ww, "_WINEVENTPROC", lambda callback: callback)
    monkeypatch.setattr(ww, "CLEANUP", ww.CleanupLedger())

    for stops, expected in ((True, True), (False, False)):
        table = _FakeTable(stops=stops)
        monkeypatch.setattr(ww, "ProcessTable", lambda *a, _t=table, **k: _t)
        out = tmp_path / f"evidence-{stops}.jsonl"
        # A sampler that stopped is a sound capture; one that did not is not,
        # and the exit code separates them.
        code = 0 if stops else ww.EXIT_UNSOUND_CAPTURE
        assert ww.watch(0.0, out, label=None, all_windows=False) == code
        assert _stop_records(out)[0]["sampler_stopped"] is expected


@pytestmark_nt
def test_a_sampler_thread_that_outlives_its_join_is_reported_as_not_stopped(ww):
    """The real ``ProcessTable.stop``, against a thread that will not die.

    Deterministic: the thread is a stand-in whose ``join`` returns and whose
    ``is_alive`` stays true, which is precisely what a two-second timeout on a
    wedged sampler looks like. Nothing here starts a thread or waits for one.
    """

    table = ww.ProcessTable()
    assert table.stop() is True, "a table that was never started stopped fine"

    table = ww.ProcessTable()
    table._thread = SimpleNamespace(join=lambda timeout=None: None, is_alive=lambda: True)
    assert table.stop() is False


@pytestmark_nt
def test_the_hook_is_released_first_and_the_record_written_last(ww, tmp_path, monkeypatch):
    """The whole stop-record fix in one assertion: the order of the three steps.

    Unhook first, because an event arriving after the observation is over
    cannot be accounted for. The sampler next, so that everything it does is
    already done. The record last, so it can describe both.
    """

    order: list[str] = []

    class _OrderedUser32(_FakeUser32):
        def UnhookWinEvent(self, hook):  # noqa: N802
            order.append("unhook")
            return super().UnhookWinEvent(hook)

    table = _FakeTable()
    table.on_stop = lambda: order.append("sampler")
    monkeypatch.setattr(ww, "_user32", _OrderedUser32())
    monkeypatch.setattr(ww, "ProcessTable", lambda *a, **k: table)
    monkeypatch.setattr(ww, "_WINEVENTPROC", lambda callback: callback)
    monkeypatch.setattr(ww, "CLEANUP", ww.CleanupLedger())

    out = tmp_path / "evidence.jsonl"
    real_dumps = json.dumps
    monkeypatch.setattr(
        ww.json,
        "dumps",
        lambda record, **kw: (
            order.append("record") if record.get("event") == "stop" else None,
            real_dumps(record, **kw),
        )[1],
    )

    assert ww.watch(0.0, out, label=None, all_windows=False) == 0
    assert order == ["unhook", "sampler", "record"], order
    assert table.stopped == 1, "the explicit call and the unwind must not both stop it"


@pytestmark_nt
def test_the_stop_record_carries_the_cleanup_result(ww, tmp_path, monkeypatch):
    """The evidence file is the only place a reader could check this."""

    fake_user32 = _FakeUser32()
    fake_user32.unhook_result = 0  # the unhook failed
    table = _FakeTable()
    monkeypatch.setattr(ww, "_user32", fake_user32)
    monkeypatch.setattr(ww, "ProcessTable", lambda *a, **k: table)
    monkeypatch.setattr(ww, "_WINEVENTPROC", lambda callback: callback)
    monkeypatch.setattr(ww, "CLEANUP", ww.CleanupLedger())

    out = tmp_path / "evidence.jsonl"
    assert ww.watch(0.0, out, label=None, all_windows=False) == ww.EXIT_UNSOUND_CAPTURE
    stop = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == "stop"
    ]
    assert stop and stop[0]["cleanup_failures"] == {"UnhookWinEvent": 1}
