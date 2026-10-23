# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Atomic publication, exclusive ownership, and the shared-temp rename race."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading

import pytest

from orchestrator.adapters import atomicwrite


def _shared_temp_write(path, content):
    """The earlier algorithm, retained only to pin the failure class."""
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if temporary.is_symlink():
            raise ValueError("pre-existing symlink") from None
        temporary.unlink()
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    temporary.replace(path)


@pytest.mark.parametrize("shared_temp", [True, False], ids=["old-collision", "current"])
def test_two_writers_at_publication(tmp_path, monkeypatch, shared_temp):
    """A closes; B closes; A replaces; B replaces, with no timing guesses.

    Pausing at Path.replace means neither temp is open, including on Windows.
    Timeouts only turn a broken handshake into a failure instead of a hang.
    """
    target = tmp_path / "usage-codex.json"
    original = b'{"original": true}'
    target.write_bytes(original)
    payloads = {
        name: json.dumps({"writer": name, "data": name * 65536}).encode()
        for name in ("a", "b")
    }
    write = _shared_temp_write if shared_temp else atomicwrite.atomic_write
    ready = {name: threading.Event() for name in payloads}
    release = {name: threading.Event() for name in payloads}
    temporaries = {}
    errors = {}
    unlinked = []
    real_replace = Path.replace
    real_unlink = Path.unlink

    def paused_replace(temporary, destination):
        if destination == target:
            name = threading.current_thread().name
            temporaries[name] = temporary
            ready[name].set()
            assert release[name].wait(10), f"writer {name} was not released"
        return real_replace(temporary, destination)

    def tracked_unlink(path, *args, **kwargs):
        unlinked.append((threading.current_thread().name, path))
        return real_unlink(path, *args, **kwargs)

    def worker(name):
        try:
            write(target, payloads[name])
        except BaseException as exc:
            errors[name] = exc

    monkeypatch.setattr(Path, "replace", paused_replace)
    monkeypatch.setattr(Path, "unlink", tracked_unlink)
    threads = {
        name: threading.Thread(target=worker, args=(name,), name=name, daemon=True)
        for name in payloads
    }
    started = []
    try:
        for name in ("a", "b"):
            threads[name].start()
            started.append(threads[name])
            assert ready[name].wait(10), f"writer {name} did not reach publication: {errors}"
        before_publication = target.read_bytes()
        staged = {name: path.read_bytes() for name, path in temporaries.items()}
        release["a"].set()
        threads["a"].join(10)
        assert not threads["a"].is_alive()
        after_a = target.read_bytes()
        b_still_exists = temporaries["b"].exists()
        release["b"].set()
    finally:
        for event in release.values():
            event.set()
        for thread in started:
            thread.join(10)

    assert all(not thread.is_alive() for thread in started)
    assert before_publication == original
    assert all(path.parent == target.parent for path in temporaries.values())
    if shared_temp:
        assert temporaries["a"] == temporaries["b"]
        assert unlinked == [("b", temporaries["a"])]
        assert staged["a"] == payloads["b"]  # B destroyed A's staged bytes.
        assert after_a == payloads["b"]  # A unknowingly published B's bytes.
        assert not b_still_exists
        assert set(errors) == {"b"}
        assert isinstance(errors["b"], FileNotFoundError)
    else:
        assert temporaries["a"] != temporaries["b"]
        assert staged == payloads
        assert unlinked == []
        assert after_a == payloads["a"]
        assert b_still_exists
        assert errors == {}
    assert target.read_bytes() == payloads["b"]
    assert json.loads(target.read_bytes()) == json.loads(payloads["b"])
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("content", [b"\x00\xff\n\r\n", "caf\u00e9\nsecond\r\n"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_content_modes_and_parent_creation(tmp_path, content, encoding):
    target = tmp_path / "new" / "nested" / "config"
    atomicwrite.atomic_write(target, content, encoding=encoding)
    expected = content if isinstance(content, bytes) else content.replace("\n", os.linesep).encode(encoding)
    assert target.read_bytes() == expected
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("failure", ["write", "replace", "encoding"])
def test_failure_preserves_target_and_only_removes_own_temp(tmp_path, monkeypatch, failure):
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    foreign = target.with_suffix(".json.tmp")
    foreign.write_bytes(b"another writer")
    error = OSError("injected failure")
    descriptors = []
    real_fdopen = os.fdopen

    class BrokenWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def write(self, content):
            self.handle.write(content[:3])
            self.handle.flush()
            raise error

        def __exit__(self, *args):
            self.handle.close()

    def fdopen(descriptor, *args, **kwargs):
        descriptors.append(descriptor)
        handle = real_fdopen(descriptor, *args, **kwargs)
        return BrokenWriter(handle) if failure == "write" else handle

    def broken_replace(*args):
        raise error

    monkeypatch.setattr(os, "fdopen", fdopen)
    if failure == "replace":
        monkeypatch.setattr(Path, "replace", broken_replace)
    with pytest.raises(LookupError if failure == "encoding" else OSError) as raised:
        atomicwrite.atomic_write(target, "replacement", encoding="invalid-encoding" if failure == "encoding" else "utf-8")
    if failure != "encoding":
        assert raised.value is error
    assert target.read_bytes() == b"original"
    assert foreign.read_bytes() == b"another writer"
    assert set(tmp_path.iterdir()) == {target, foreign}
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_cleanup_error_does_not_mask_publication_error(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    error = OSError("publication failed")
    cleanup_paths = []

    def broken_replace(*args):
        raise error

    def broken_unlink(path, *args, **kwargs):
        cleanup_paths.append(path)
        raise PermissionError("cleanup failed")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", broken_replace)
        patch.setattr(Path, "unlink", broken_unlink)
        with pytest.raises(OSError) as raised:
            atomicwrite.atomic_write(target, b"replacement")
    assert raised.value is error
    assert len(cleanup_paths) == 1
    temporary = cleanup_paths[0]
    assert temporary != target
    assert temporary.parent == target.parent
    assert temporary.read_bytes() == b"replacement"
    assert target.read_bytes() == b"original"
    temporary.unlink()


def test_fdopen_failure_does_not_close_another_writers_descriptor(tmp_path, monkeypatch):
    """Force descriptor reuse immediately after a failed text wrapper."""
    target = tmp_path / "config.json"
    other = tmp_path / "other-writer"
    descriptors = []
    real_fdopen = os.fdopen

    def fdopen(*args, **kwargs):
        try:
            return real_fdopen(*args, **kwargs)
        except LookupError:
            # If fdopen closed its input, the OS can hand that number straight
            # to another writer before atomic_write gets to its cleanup.
            descriptors.append(os.open(other, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            raise

    monkeypatch.setattr(os, "fdopen", fdopen)
    try:
        with pytest.raises(LookupError):
            atomicwrite.atomic_write(target, "replacement", encoding="invalid-encoding")
        assert len(descriptors) == 1
        os.write(descriptors[0], b"another writer")
        assert list(tmp_path.iterdir()) == [other]
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
    assert other.read_bytes() == b"another writer"


@pytest.mark.parametrize("symlink", [False, True], ids=["file", "symlink"])
def test_exclusive_creation_leaves_occupied_candidate_untouched(tmp_path, monkeypatch, symlink):
    if symlink and os.name == "nt":
        pytest.skip("creating symlinks on Windows needs elevated privilege")
    target = tmp_path / "config.json"
    occupied = tmp_path / "config.json.occupied.tmp"
    decoy = tmp_path / "decoy"
    decoy.write_bytes(b"do not overwrite")
    if symlink:
        occupied.symlink_to(decoy)
    else:
        occupied.write_bytes(b"do not overwrite")
    monkeypatch.setattr(atomicwrite.tempfile, "_get_candidate_names", lambda: iter(["occupied", "fresh"]))
    atomicwrite.atomic_write(target, b"replacement")
    assert occupied.is_symlink() is symlink
    assert occupied.read_bytes() == b"do not overwrite"
    assert decoy.read_bytes() == b"do not overwrite"
    assert target.read_bytes() == b"replacement"
    assert set(tmp_path.iterdir()) == {target, occupied, decoy}


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not describe Windows ACLs")
def test_new_file_is_private(tmp_path):
    target = tmp_path / "config.json"
    atomicwrite.atomic_write(target, "private")
    assert target.stat().st_mode & 0o777 == 0o600
