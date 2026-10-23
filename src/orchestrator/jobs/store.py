# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Local pending answers, separate from history; never applies returned output.

The JSONL journal contains only transaction filenames, job references, outcomes,
optional random acknowledgment receipts, removed-job identity hashes and a
served-time watermark. A preallocated removal slot holds hashes, states, times
and receipts, without job references or content. Content (including the
original-text fingerprint) exists only in per-job JSON files and their staging
files. A fsynced intent precedes rename/unlink; recovery finishes that intent.
Unjournaled staging files are rolled back. OS locks release on process death.
"""

from __future__ import annotations

import hashlib
import errno
import json
import logging
import math
import os
import secrets
import stat
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Literal

from orchestrator.execution.base import WorkerTaskStatus
from orchestrator.jobs.seam import (
    EXPIRY_WARNING, MAX_RETENTION, RETENTION, CollectResult, JobOutcome,
    JobOutput, JobRef, JobStatus, PendingEntry, _utc,
)

MAX_OUTPUT_BYTES = 1024 * 1024
"""Provisional 1 MiB UTF-8 answer cap; truncation never changes worker claims."""
LOCK_TIMEOUT_SECONDS = 10
INSPECTION_ATTEMPTS = 5
INSPECTION_RETRY_DELAY_SECONDS = 0.02
INSPECTION_RETRY_BUDGET_SECONDS = 0.25
WATERMARK_STEP = timedelta(0)
"""Persist every later served time; equal/earlier idle reads write nothing."""
JOURNAL_MAX_LINES = 1000
JOURNAL_MAX_BYTES = 1024 * 1024
OUTCOME_RETENTION = timedelta(days=30)
"""Content-free outcomes expire 30 days after their recorded event."""
REMOVED_RETENTION = timedelta(days=30)
"""Job-id hashes protect against replay for 30 more days after outcome expiry."""
REMOVAL_SLOT_BYTES = 64 * 1024
REMOVAL_BATCH_SIZE = 32
REMOVAL_SLOT_NAME = "removal.intent"
_SLOT_BANK_BYTES = REMOVAL_SLOT_BYTES // 2
_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Truncation:
    truncated: bool
    original_bytes: int


class StoreCorrupted(ValueError):
    """Pending-output journal or content is corrupt; delete_all still works."""


class PendingOutputCapacityUnavailable(OSError):
    """No proven capacity for durable removal evidence; never a silent mutation."""

    label = "pending_output_capacity_unavailable"

    def __init__(self):
        super().__init__(errno.ENOSPC, self.label)


class PendingOutputTemporarilyUnavailable(OSError):
    """A retained file cannot currently be proven safe to use; retry later."""

    label = "pending_output_temporarily_unavailable"

    def __init__(self):
        super().__init__(errno.EAGAIN, self.label)


class _Inspection(Enum):
    SAFE = auto()
    UNSAFE = auto()
    UNINSPECTABLE = auto()


def _protect_directory(path):
    """Local filesystem protection; jobs has no dependency on the service."""
    if os.name != "nt":
        os.chmod(path, 0o700)
        return
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.SetNamedSecurityInfoW.restype = wintypes.DWORD
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(ctypes.c_void_p.from_buffer(buffer), ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sddl = f"D:P(A;OICI;FA;;;{sid.value})(A;OICI;FA;;;SY)"
        finally:
            k32.LocalFree(ctypes.cast(sid, ctypes.c_void_p))
    finally:
        k32.CloseHandle(token)
    descriptor = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not adv.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        status = adv.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
        if status:
            raise ctypes.WinError(status)
    finally:
        k32.LocalFree(descriptor)


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key = str(key)
            if key in result:
                raise ValueError("worker fact keys collide after string normalization")
            result[key] = _json_value(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_json_value(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: _json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("worker facts must contain only finite floats")
    return value


def _output(raw):
    raw = dict(raw)
    raw["job"] = JobRef(**raw["job"])
    raw["job_status"] = JobStatus(raw["job_status"])
    raw["worker_claim"] = WorkerTaskStatus(raw["worker_claim"])
    for key in ("started_at", "ended_at"):
        raw[key] = datetime.fromisoformat(raw[key])
    return JobOutput(**raw)


class FileOutputStore:
    """OutputSink rooted solely at a caller-selected directory.

    Windows inherits the private directory ACL (current user and SYSTEM);
    POSIX uses 0700 directories / 0600 files. Permission failures fail closed.
    Each operation reloads durable state under both thread and process locks.
    """

    def __init__(self, root: str | os.PathLike[str]):
        root = Path(root)
        if root.is_symlink():
            raise ValueError("store root must not be a symlink")
        self.root = root.absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _protect_directory(self.root)
        with _locks_guard:
            self._thread_lock = _locks.setdefault(self.root.resolve(), threading.RLock())
        with self._locked():
            try:
                self._load(content=False)
            except (StoreCorrupted, PendingOutputTemporarilyUnavailable):
                # Keep a usable deletion handle; every ordinary operation
                # reloads and reports the typed error without exposing content.
                pass

    def _path(self, name, *, check_link=True):
        if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
            raise ValueError("invalid store filename")
        path = self.root / name
        if check_link and path.is_symlink():
            raise ValueError("store files must not be symlinks")
        return path

    def _safe_recovery_file(self, path):
        """Do not read or unlink links, reparse points or non-regular files."""
        return self._inspect(path, retry=False) is _Inspection.SAFE

    def _inspect(self, path, *, retry=True):
        """Three safety outcomes; None means lstat proved the file absent.

        Windows stat's access-denied fallback can report a regular file with
        zero links. That cannot prove safety, and must not hide an answer.
        Delete-only callers leave uncertain files without spending retries.
        """
        for attempt in range(INSPECTION_ATTEMPTS):
            try:
                info = path.lstat()
            except FileNotFoundError:
                if attempt:
                    _logger.warning("Pending-output file inspection resolved: %s (%s, %d attempts; absent)",
                                    path.name, reason, attempt + 1)
                return None
            except OSError as error:
                reason = f"lstat error {getattr(error, 'winerror', None) or error.errno}"
                result = _Inspection.UNINSPECTABLE
            else:
                mode = getattr(info, 'st_mode', None)
                links = getattr(info, 'st_nlink', None)
                if getattr(info, 'st_file_attributes', 0) & 0x400:
                    reason, result = 'reparse attribute', _Inspection.UNSAFE
                elif not isinstance(mode, int):
                    reason, result = 'regular mode unavailable', _Inspection.UNINSPECTABLE
                elif not stat.S_ISREG(mode):
                    reason, result = 'not regular', _Inspection.UNSAFE
                elif isinstance(links, int) and links > 1:
                    reason, result = f'link count {links}', _Inspection.UNSAFE
                elif not isinstance(links, int) or links != 1:
                    reason, result = f'link count {links}', _Inspection.UNINSPECTABLE
                else:
                    if attempt:
                        _logger.warning("Pending-output file inspection resolved: %s (%s, %d attempts; safe)",
                                        path.name, reason, attempt + 1)
                    return _Inspection.SAFE
            if result is _Inspection.UNSAFE:
                _logger.warning("Skipping unsafe pending-output file: %s (%s)",
                                path.name, reason)
                return result
            remaining = INSPECTION_RETRY_BUDGET_SECONDS - self._inspection_spent
            if not retry or attempt + 1 == INSPECTION_ATTEMPTS or remaining <= 0:
                break
            start = time.monotonic()
            time.sleep(min(INSPECTION_RETRY_DELAY_SECONDS, remaining))
            self._inspection_spent += time.monotonic() - start
        _logger.warning("Cannot inspect pending-output file: %s (%s, %d attempts)",
                        path.name, reason, attempt + 1)
        return _Inspection.UNINSPECTABLE

    def _inspect_available(self, path):
        result = self._inspect(path)
        if result is _Inspection.UNINSPECTABLE:
            raise PendingOutputTemporarilyUnavailable()
        return result

    def _open(self, name, flags):
        return os.open(self._path(name), flags | getattr(os, "O_BINARY", 0), 0o600)

    @contextmanager
    def _locked(self):
        """Windows byte-range locking; POSIX flock; both die with the holder."""
        with self._thread_lock:
            fd = self._open("store.lock", os.O_RDWR | os.O_CREAT)
            locked = False
            try:
                deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
                while not locked:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            os.lseek(fd, 0, os.SEEK_SET)
                            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("pending-output lock unavailable") from None
                        time.sleep(0.01)
                self._inspection_spent = 0.0
                yield
            finally:
                if locked:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _sync_directory(self):
        # Windows rename uses a fsynced staged file; directory fsync is POSIX.
        if os.name != "nt":
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _append(self, record):
        data = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        self._allocating_write("outcomes.jsonl", data, append=True)
        self._journal_lines += 1
        self._snapshot_only = False

    def _allocating_write(self, name, data, *, append=False):
        """Single seam for all writes that create or extend storage."""
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT if append else
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(self._open(name, flags), "ab" if append else "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self._sync_directory()

    def _slot_allocated(self):
        """Prove real allocation, not merely a sparse file's logical size."""
        path = self._path(REMOVAL_SLOT_NAME, check_link=False)
        if self._inspect(path) is not _Inspection.SAFE:
            return False
        info = path.stat()
        if info.st_size != REMOVAL_SLOT_BYTES:
            return False
        if os.name != "nt":
            return info.st_blocks * 512 >= REMOVAL_SLOT_BYTES
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class StandardInfo(ctypes.Structure):
            _fields_ = [('allocation', ctypes.c_int64), ('size', ctypes.c_int64),
                        ('links', wintypes.DWORD), ('delete_pending', ctypes.c_ubyte),
                        ('directory', ctypes.c_ubyte)]

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        fd = self._open(REMOVAL_SLOT_NAME, os.O_RDONLY)
        try:
            native = StandardInfo()
            if not kernel32.GetFileInformationByHandleEx(
                msvcrt.get_osfhandle(fd), 1, ctypes.byref(native), ctypes.sizeof(native)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return (native.allocation >= REMOVAL_SLOT_BYTES
                    and not info.st_file_attributes & (0x200 | 0x800))
        finally:
            os.close(fd)

    @staticmethod
    def _slot_frame(sequence, intent):
        data = json.dumps({'sequence': sequence, 'intent': intent},
                          ensure_ascii=True, separators=(',', ':')).encode()
        frame = hashlib.sha256(data).hexdigest().encode() + b'\n' + data + b'\n'
        if len(frame) > _SLOT_BANK_BYTES:
            raise PendingOutputCapacityUnavailable()
        # Random padding resists filesystem compression; repeated bytes do not.
        return frame + secrets.token_bytes(_SLOT_BANK_BYTES - len(frame))

    def _read_slot(self):
        self._slot_sequence = 0
        self._slot_bank = 0
        self._slot = None
        self._slot_needs_scrub = False
        path = self._path(REMOVAL_SLOT_NAME, check_link=False)
        inspection = self._inspect_available(path)
        if inspection is None:
            return
        if inspection is not _Inspection.SAFE or path.stat().st_size != REMOVAL_SLOT_BYTES:
            raise StoreCorrupted('invalid pending-output removal intent')
        try:
            frames = path.read_bytes()
        except PermissionError:
            raise PendingOutputTemporarilyUnavailable() from None
        valid = []
        for bank in range(2):
            try:
                digest, data, _padding = frames[
                    bank * _SLOT_BANK_BYTES:(bank + 1) * _SLOT_BANK_BYTES
                ].split(b'\n', 2)
                if hashlib.sha256(data).hexdigest().encode() != digest:
                    continue
                record = json.loads(data)
                if (not isinstance(record['sequence'], int)
                        or record['sequence'] < 0):
                    continue
                intent = record['intent']
                if intent is not None:
                    self._read_record(self._slot_transaction(intent), None)
                    if (intent['kind'] != 'transaction'
                            or len(intent['operations']) > REMOVAL_BATCH_SIZE
                            or len(intent['hash_outcomes']) > REMOVAL_BATCH_SIZE
                            or any(op['kind'] != 'remove' for op in intent['operations'])):
                        continue
                    for outcome in intent['hash_outcomes']:
                        digest = outcome['job_hash']
                        if digest not in intent['removed']:
                            raise ValueError('invalid removal outcome hash')
                        JobOutcome(JobRef(digest, 0, None), outcome['state'],
                                   datetime.fromisoformat(outcome['ended_at']))
                        if outcome['state'] == 'acknowledged' and not isinstance(outcome['receipt'], str):
                            raise ValueError('invalid removal receipt')
                valid.append((record['sequence'], bank, intent))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        if not valid:
            raise StoreCorrupted('invalid pending-output removal intent')
        self._slot_sequence, self._slot_bank, self._slot = max(valid, key=lambda v: v[0])
        self._slot_needs_scrub = any(intent is not None for _seq, _bank, intent in valid)

    def _ensure_capacity(self):
        try:
            path = self._path(REMOVAL_SLOT_NAME, check_link=False)
            inspection = self._inspect_available(path)
            if inspection is _Inspection.UNSAFE:
                raise PendingOutputCapacityUnavailable()
            if inspection is None:
                # Write bytes, never truncate-to-size (which may be sparse).
                initial = self._slot_frame(0, None) + self._slot_frame(1, None)
                name = secrets.token_hex(16) + '.tmp'
                self._allocating_write(name, initial)
                os.replace(self._path(name), path)
                self._sync_directory()
                self._read_slot()
            if not self._slot_allocated():
                raise PendingOutputCapacityUnavailable()
        except PendingOutputTemporarilyUnavailable:
            raise
        except OSError:
            raise PendingOutputCapacityUnavailable() from None

    def _write_slot(self, intent):
        self._ensure_capacity()
        # Alternate banks: a torn overwrite leaves the previous durable intent.
        fd = self._open(REMOVAL_SLOT_NAME, os.O_RDWR)
        try:
            for _ in range(2 if intent is None else 1):
                sequence = self._slot_sequence + 1
                bank = 1 - self._slot_bank
                frame = self._slot_frame(sequence, intent)
                os.lseek(fd, bank * _SLOT_BANK_BYTES, os.SEEK_SET)
                view = memoryview(frame)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise PendingOutputCapacityUnavailable()
                    view = view[written:]
                os.fsync(fd)
                self._slot_sequence, self._slot_bank, self._slot = sequence, bank, intent
            self._slot_needs_scrub = intent is not None
        except OSError:
            raise PendingOutputCapacityUnavailable() from None
        finally:
            os.close(fd)

    def _flush_slot(self):
        """Only after unlink: transfer the intent into the allocating journal."""
        if self._slot is None:
            if self._slot_needs_scrub:
                self._write_slot(None)
            return
        if self._active is not None:
            self._finish_active()
        if self._slot['id'] not in self._journal_ids:
            self._append(self._slot_transaction(self._slot))
            self._append({'kind': 'close', 'id': self._slot['id']})
            self._journal_ids.add(self._slot['id'])
        self._write_slot(None)

    def _slot_transaction(self, intent):
        """Resolve hash-only slot outcomes through the durable journal catalog."""
        outcomes = []
        for raw in intent['hash_outcomes']:
            job = self._references.get(raw['job_hash'])
            if job is not None:
                outcomes.append({'job': job, **{k: v for k, v in raw.items() if k != 'job_hash'}})
        return {k: v for k, v in intent.items() if k != 'hash_outcomes'} | {'outcomes': outcomes}

    def _ensure_references(self, entries, now):
        """Upgrade legacy answers before removal; absence never invents identity.

        New puts already journal their content-free reference. A legacy store
        with no such reference must register it while space is available;
        otherwise removal fails explicitly, before unlinking any answer.
        """
        missing = {}
        for entry in entries:
            outcome = entry['outcome']
            if outcome is not None:
                job = outcome['job']
                digest = self._job_hash(job['job_id'])
                if digest not in self._references:
                    missing[digest] = job
        if not missing:
            return
        self._finish_active()
        self._flush_slot()
        transaction = {'kind': 'transaction', 'id': secrets.token_hex(16),
                       'watermark': now.isoformat(), 'operations': [],
                       'outcomes': [], 'removed': {}, 'references': missing}
        self._append(transaction)
        self._append({'kind': 'close', 'id': transaction['id']})
        self._references.update(missing)
        self._latest = now

    def _finish_active(self):
        if self._active is not None:
            operations = []
            for op in self._active['operations']:
                if op['kind'] == 'write' and Path(op['file']).stem in self._removed:
                    self._path(op['temp']).unlink(missing_ok=True)
                else:
                    operations.append(op)
            self._apply(operations)
            self._append({'kind': 'close', 'id': self._active['id']})
            self._active = None

    def _apply(self, operations):
        for op in operations:
            candidate = self._path(op["file"], check_link=False)
            if op["kind"] == "write":
                target_inspection = self._inspect_available(candidate)
                if target_inspection is _Inspection.UNSAFE:
                    continue
                staged = self._path(op["temp"], check_link=False)
                staged_inspection = self._inspect_available(staged)
                if staged_inspection is _Inspection.UNSAFE:
                    continue
                # Staging retries must not leave an older target inspection
                # authorizing the rename over a file that is now uncertain.
                target_inspection = self._inspect_available(candidate)
                if target_inspection is _Inspection.UNSAFE:
                    continue
                if staged_inspection is _Inspection.SAFE:
                    if self._inspect(staged, retry=False) is not _Inspection.SAFE:
                        raise PendingOutputTemporarilyUnavailable()
                    os.replace(staged, candidate)
                elif target_inspection is not _Inspection.SAFE:
                    raise OSError("journaled pending output is unavailable")
            elif op["kind"] == "remove":
                inspection = self._inspect(candidate, retry=False)
                if inspection is _Inspection.SAFE:
                    candidate.unlink(missing_ok=True)
                elif inspection is not None:
                    continue
            else:
                raise ValueError("unknown pending-output operation")
            self._checkpoint("operation_applied")
        self._sync_directory()

    def _load(self, *, content=True):
        self._entries = {}
        self._completed = {}
        self._removed = {}
        self._references = {}
        self._latest = None
        self._journal_lines = 0
        self._snapshot_only = False
        self._snapshot_bytes = 0
        self._journal_valid = False
        self._journal_ids = set()
        self._active = None
        journal = self._path("outcomes.jsonl")
        active = None
        if journal.exists():
            # A torn final append was never durably accepted. Preserve earlier
            # records, discard only the incomplete last line, then reconcile.
            with journal.open("r+b") as stream:
                good_end = 0
                for line in stream:
                    if not line.endswith(b"\n"):
                        stream.truncate(good_end)
                        stream.flush()
                        os.fsync(stream.fileno())
                        break
                    self._journal_lines += 1
                    try:
                        record = json.loads(line)
                        self._read_record(record, active)
                    except (ValueError, KeyError, TypeError, AttributeError):
                        raise StoreCorrupted("invalid pending-output journal") from None
                    good_end = stream.tell()
                    if record["kind"] == "snapshot":
                        if active is not None or self._journal_lines != 1:
                            raise StoreCorrupted("invalid pending-output snapshot")
                        self._latest = datetime.fromisoformat(record["watermark"])
                        self._completed = {r["job"]["job_id"]: r for r in record["outcomes"]}
                        self._removed = dict(record.get("removed", {}))
                        self._references = dict(record.get('references', {}))
                        self._snapshot_only = True
                        self._snapshot_bytes = len(line)
                    elif record["kind"] == "transaction":
                        self._snapshot_only = False
                        if active is not None:
                            raise StoreCorrupted("unclosed pending-output transaction")
                        active = record
                        self._journal_ids.add(record['id'])
                        self._latest = datetime.fromisoformat(record["watermark"])
                        for outcome in record["outcomes"]:
                            self._completed[outcome["job"]["job_id"]] = outcome
                        self._removed.update(record.get("removed", {}))
                        self._references.update(record.get('references', {}))
                    elif record["kind"] == "close" and active is not None and record["id"] == active["id"]:
                        active = None
                    else:
                        raise StoreCorrupted("invalid pending-output journal")
        self._journal_valid = True
        self._active = active
        hidden = set()
        if active is not None:
            # Loading is read-only with respect to new space. Removal evidence
            # is already durable. Existing committed answers remain readable
            # while an open write replacing them waits for a close record.
            removals = [op for op in active['operations'] if op['kind'] == 'remove']
            self._apply(removals)
            hidden.update(op['file'] for op in active['operations']
                          if op['kind'] == 'remove'
                          or not op.get('replaces_committed', self._path(op['file']).is_file()))
        self._read_slot()
        if self._slot is not None:
            intent = self._slot
            self._latest = max(self._latest, datetime.fromisoformat(intent['watermark'])) if self._latest else datetime.fromisoformat(intent['watermark'])
            self._completed.update({r['job']['job_id']: r
                                    for r in self._slot_transaction(intent)['outcomes']})
            self._removed.update(intent['removed'])
            # Replay before opening even one content file.
            self._apply(intent['operations'])
            hidden.update(op['file'] for op in intent['operations'])
        active_temps = {op['temp'] for op in active['operations'] if op['kind'] == 'write'} if active else set()
        for path in self.root.glob("*.tmp"):
            if path.name in active_temps:
                continue
            if self._safe_recovery_file(path):
                path.unlink()
        self._sync_directory()
        self._entries = {}
        for path in self.root.glob("*.json") if content else ():
            if path.name in hidden:
                continue
            if path.stem in self._removed:
                if self._safe_recovery_file(path):
                    path.unlink()
                continue
            if self._inspect_available(path) is not _Inspection.SAFE:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                output = _output(raw["output"])
                if path.name != self._filename(output.job.job_id):
                    raise ValueError('invalid pending-output identity')
                retained = datetime.fromisoformat(raw["retained_at"])
                extended = datetime.fromisoformat(raw["extended_until"]) if raw["extended_until"] else None
                CollectResult(output, raw["receipt"], retained, self._expiry(raw), False, extended,
                              raw['truncation']['truncated'], raw['truncation']['original_bytes'])
                Truncation(**raw["truncation"])
                if not isinstance(raw["original_sha256"], str):
                    raise ValueError("invalid fingerprint")
            except PermissionError:
                raise PendingOutputTemporarilyUnavailable() from None
            except (ValueError, KeyError, TypeError, AttributeError):
                raise StoreCorrupted("invalid pending-output content") from None
            job_id = raw["output"]["job"]["job_id"]
            if job_id in self._completed or self._job_hash(job_id) in self._removed:
                # Removal intent wins even if stale content was left behind.
                if self._safe_recovery_file(path):
                    path.unlink()
            else:
                self._entries[job_id] = raw
        self._operations = []
        self._new_outcomes = []
        self._new_removed = {}
        self._new_references = {}

    def _read_record(self, record, active):
        """Validate before using a complete journal line; never echo its bytes."""
        if record["kind"] in {"snapshot", "transaction"}:
            _utc(datetime.fromisoformat(record["watermark"]), "watermark")
            for digest, expires in record.get("removed", {}).items():
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(char not in "0123456789abcdef" for char in digest)):
                    raise ValueError("invalid removed job hash")
                _utc(datetime.fromisoformat(expires), "removed expiry")
            for digest, raw in record.get('references', {}).items():
                job = JobRef(**raw)
                if digest != self._job_hash(job.job_id):
                    raise ValueError('invalid pending-output reference hash')
            for raw in record["outcomes"]:
                JobOutcome(JobRef(**raw["job"]), raw["state"], datetime.fromisoformat(raw["ended_at"]))
                if raw["state"] == "acknowledged" and not isinstance(raw["receipt"], str):
                    raise ValueError("invalid receipt")
            if record["kind"] == "transaction":
                if not isinstance(record["id"], str):
                    raise ValueError("invalid transaction id")
                for op in record["operations"]:
                    self._path(op["file"], check_link=False)
                    if op["kind"] == "write":
                        self._path(op["temp"], check_link=False)
                        if 'replaces_committed' in op and not isinstance(op['replaces_committed'], bool):
                            raise ValueError('invalid pending-output write origin')
                    elif op["kind"] != "remove":
                        raise ValueError("invalid operation")
        elif record["kind"] != "close" or active is None or record["id"] != active["id"]:
            raise ValueError("invalid journal record")

    def _compact(self, now):
        """Atomically replace the closed journal with a content-free snapshot.

        Replacement also truncates the old log in one filesystem operation:
        a crash leaves either the old log or the complete new snapshot.
        """
        watermark = max(now, self._latest) if self._latest else now
        completed = {key: raw for key, raw in self._completed.items()
                     if datetime.fromisoformat(raw["ended_at"]) + OUTCOME_RETENTION > watermark}
        removed = {key: expires for key, expires in self._removed.items()
                   if datetime.fromisoformat(expires) > watermark}
        references = {key: raw for key, raw in self._references.items()
                      if raw['job_id'] in completed or self._reference_pending(key)}
        journal = self._path("outcomes.jsonl")
        if (len(completed) == len(self._completed)
                and len(removed) == len(self._removed)
                and len(references) == len(self._references)
                and (self._snapshot_only or (
                    self._journal_lines <= JOURNAL_MAX_LINES
                    and (not journal.exists() or journal.stat().st_size <=
                         JOURNAL_MAX_BYTES + self._snapshot_bytes)))):
            return
        snapshot = {"kind": "snapshot", "watermark": watermark.isoformat(),
                    "outcomes": list(completed.values()), "removed": removed,
                    "references": references}
        name = secrets.token_hex(16) + ".tmp"
        self._allocating_write(name, (json.dumps(snapshot, ensure_ascii=True, separators=(',', ':')) + '\n').encode())
        self._checkpoint("snapshot_staged")
        os.replace(self._path(name), journal)
        self._sync_directory()
        self._checkpoint("compacted")
        self._completed = completed
        self._removed = removed
        self._references = references
        self._latest = watermark
        self._journal_lines = 1
        self._snapshot_only = True
        self._snapshot_bytes = journal.stat().st_size

    def _reference_pending(self, digest):
        path = self._path(digest + '.json', check_link=False)
        return self._inspect(path) in {_Inspection.SAFE, _Inspection.UNINSPECTABLE}

    def _checkpoint(self, stage):
        """Inert failure-injection seam; production does nothing."""

    def _commit(self, now):
        self._ensure_capacity()
        self._finish_active()
        self._flush_slot()
        watermark = max(now, self._latest) if self._latest else now
        if not self._operations and self._latest is not None and watermark - self._latest <= WATERMARK_STEP:
            self._compact(now)
            return
        transaction = {"kind": "transaction", "id": secrets.token_hex(16),
                       "watermark": watermark.isoformat(),
                       "operations": self._operations, "outcomes": self._new_outcomes,
                       "removed": self._new_removed, 'references': self._new_references}
        self._append(transaction)
        self._checkpoint("journaled")
        self._apply(self._operations)
        self._checkpoint("applied")
        self._append({"kind": "close", "id": transaction["id"]})
        self._latest = watermark
        self._references.update(self._new_references)
        self._compact(now)

    @contextmanager
    def _transaction(self, now, *, read_only=False, removal=False):
        _utc(now, "now")
        with self._locked():
            fallback = False
            try:
                self._load()
                if not removal and self._active is not None:
                    try:
                        self._finish_active()
                        self._load()
                    except OSError as error:
                        if not read_only or error.errno != errno.ENOSPC:
                            raise
                        fallback = True
                now = max(now, self._latest) if self._latest else now
                self._expired = sorted(
                    (raw for raw in self._entries.values() if self._expiry(raw) <= now),
                    key=lambda raw: (self._expiry(raw), raw['output']['job']['job_id']),
                )
                try:
                    if not read_only:
                        self._ensure_capacity()
                    offset = 0
                    while offset < len(self._expired):
                        batch = self._expired[offset:offset + self._removal_batch_limit()]
                        self._removal_transaction([
                            self._removal_entry(raw, 'expired', now) for raw in batch
                        ], now)
                        for raw in batch:
                            self._entries.pop(raw['output']['job']['job_id'], None)
                        offset += len(batch)
                    if not removal:
                        self._finish_active()
                        self._flush_slot()
                        self._compact(now)
                except OSError as error:
                    if not read_only or error.errno != errno.ENOSPC:
                        raise
                    fallback = True
                    # Expiry filtering never depends on maintenance succeeding.
                    for raw in self._expired:
                        job_id = raw['output']['job']['job_id']
                        self._entries.pop(job_id, None)
                        self._completed[job_id] = self._removal_entry(raw, 'expired', now)['outcome']
                # Removal operations avoid allocating compaction altogether;
                # fallback reads still obey the outcome-retention boundary.
                self._completed = {key: raw for key, raw in self._completed.items()
                                   if datetime.fromisoformat(raw['ended_at']) + OUTCOME_RETENTION > now}
                yield now
                if removal:
                    self._removal_transaction([], now)
                elif not fallback:
                    try:
                        self._commit(now)
                    except OSError as error:
                        if not read_only or error.errno != errno.ENOSPC:
                            raise
            except OSError as error:
                if error.errno == errno.ENOSPC:
                    raise PendingOutputCapacityUnavailable() from None
                raise
            finally:
                # Keep no expired/removed answers in the long-lived instance.
                self._entries = {}
                self._expired = []

    @staticmethod
    def _job_hash(job_id):
        return hashlib.sha256(job_id.encode("utf-8")).hexdigest()

    @classmethod
    def _filename(cls, job_id):
        # An opaque id may contain separators; it is never used as a path.
        return cls._job_hash(job_id) + ".json"

    @staticmethod
    def _expiry(raw):
        return (datetime.fromisoformat(raw["extended_until"]) if raw["extended_until"]
                else datetime.fromisoformat(raw["retained_at"]) + RETENTION)

    def _collect(self, job_id, now):
        raw = self._entries.get(job_id)
        if raw is None:
            return None
        expiry = self._expiry(raw)
        return CollectResult(_output(raw["output"]), raw["receipt"],
                             datetime.fromisoformat(raw["retained_at"]), expiry,
                             expiry - now <= EXPIRY_WARNING,
                             datetime.fromisoformat(raw["extended_until"]) if raw["extended_until"] else None,
                             raw['truncation']['truncated'], raw['truncation']['original_bytes'])

    def _write(self, job_id, raw):
        target = self._path(self._filename(job_id), check_link=False)
        if self._inspect_available(target) is _Inspection.UNSAFE:
            raise ValueError("unsafe pending-output target")
        name = secrets.token_hex(16) + ".tmp"
        self._allocating_write(name, json.dumps(raw, ensure_ascii=True, separators=(',', ':')).encode())
        self._checkpoint("staged")
        self._operations.append({"kind": "write", "file": self._filename(job_id), "temp": name,
                                 'replaces_committed': target.is_file()})
        self._new_references[self._job_hash(job_id)] = raw['output']['job']
        self._entries[job_id] = raw

    def _removal_entry(self, raw, state, now, *, filename=None):
        record = {'job': raw['output']['job'], 'state': state, 'ended_at': now.isoformat()}
        if state == 'acknowledged':
            record['receipt'] = raw['receipt']
        return {'file': filename or self._filename(record['job']['job_id']), 'outcome': record}

    def _removal_batch_limit(self):
        occupied = len(self._slot['operations']) if self._slot else 0
        return REMOVAL_BATCH_SIZE - occupied or REMOVAL_BATCH_SIZE

    def _removal_transaction(self, entries, now):
        """Bounded in-place intent, fsync, then unlink. No allocating write.

        The journal transfer is deferred until a subsequent maintenance/adding
        operation, or until another batch needs the occupied slot's capacity.
        """
        if not entries and self._latest is not None and now <= self._latest:
            return 0
        if len(entries) > REMOVAL_BATCH_SIZE:
            raise PendingOutputCapacityUnavailable()
        self._ensure_capacity()
        self._ensure_references(entries, now)
        previous = self._slot
        previous_files = {op['file'] for op in previous['operations']} if previous else set()
        new_files = {entry['file'] for entry in entries}
        if len(previous_files | new_files) > REMOVAL_BATCH_SIZE:
            self._flush_slot()
            previous = None
        watermark = max(now, self._latest) if self._latest else now
        intent = {'kind': 'transaction', 'id': secrets.token_hex(16),
                  'watermark': watermark.isoformat(),
                  'operations': list(previous['operations']) if previous else [],
                  'hash_outcomes': list(previous['hash_outcomes']) if previous else [],
                  'removed': dict(previous['removed']) if previous else {}}
        for entry in entries:
            filename = entry['file']
            if filename not in {op['file'] for op in intent['operations']}:
                intent['operations'].append({'kind': 'remove', 'file': filename})
            record = entry['outcome']
            digest = Path(filename).stem
            if len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
                digest = self._job_hash(filename)
            if record is not None:
                digest = self._job_hash(record['job']['job_id'])
                intent['hash_outcomes'].append({'job_hash': digest,
                                               **{k: v for k, v in record.items() if k != 'job'}})
            intent['removed'][digest] = (watermark + OUTCOME_RETENTION + REMOVED_RETENTION).isoformat()
        try:
            self._slot_frame(self._slot_sequence + 1, intent)
        except PendingOutputCapacityUnavailable:
            # Never overwrite still-untransferred evidence if the byte bound
            # is reached before the file-count bound.
            if previous is None:
                raise
            self._flush_slot()
            return self._removal_transaction(entries, now)
        self._write_slot(intent)
        self._checkpoint('removal_intent_written')
        self._checkpoint('journaled')
        # Uncertainty here cannot overclaim how many files were removed.
        existed = {entry['file'] for entry in entries
                   if self._inspect(self._path(entry['file'], check_link=False),
                                    retry=False) is _Inspection.SAFE}
        self._apply([{'kind': 'remove', 'file': entry['file']} for entry in entries])
        self._checkpoint('removal_unlinked')
        self._checkpoint('applied')
        self._completed.update({r['job']['job_id']: r
                                for r in self._slot_transaction(intent)['outcomes']})
        self._removed.update(intent['removed'])
        self._latest = watermark
        return sum(not self._path(filename).exists() for filename in existed)

    def put(self, output: JobOutput, now: datetime) -> bool:
        _utc(now, "now")
        if now < output.ended_at:
            raise ValueError("now must not precede output.ended_at")
        with self._transaction(now):
            job_id = output.job.job_id
            if job_id in self._completed or self._job_hash(job_id) in self._removed:
                return False
            incoming = _json_value(output)
            encoded = output.output_text.encode("utf-8", errors="surrogatepass")
            fingerprint = hashlib.sha256(encoded).hexdigest()
            if job_id in self._entries:
                raw = self._entries[job_id]
                nontext = {k: v for k, v in incoming.items() if k != "output_text"}
                stored = {k: v for k, v in raw["output"].items() if k != "output_text"}
                if raw["original_sha256"] != fingerprint or stored != nontext:
                    raise ValueError("conflicting content for job_id")
                return False
            bounded = encoded[:MAX_OUTPUT_BYTES]
            while True:
                try:
                    incoming["output_text"] = bounded.decode("utf-8", errors="surrogatepass")
                    break
                except UnicodeDecodeError:
                    bounded = bounded[:-1]
            self._write(job_id, {"output": incoming, "receipt": secrets.token_urlsafe(32),
                                "retained_at": now.isoformat(), "extended_until": None,
                                "original_sha256": fingerprint,
                                "truncation": {"truncated": len(encoded) > MAX_OUTPUT_BYTES,
                                               "original_bytes": len(encoded)}})
            return True

    def collect(self, job_id: str, now: datetime) -> CollectResult | None:
        with self._transaction(now, read_only=True) as effective_now:
            return self._collect(job_id, effective_now)

    def truncation(self, job_id: str, now: datetime) -> Truncation | None:
        # Legacy standalone probe has no read-only fallback. Host collection
        # uses the loss facts in CollectResult's single locked snapshot.
        with self._transaction(now) as effective_now:
            result = self._collect(job_id, effective_now)
            return Truncation(result.output_truncated, result.original_bytes) if result else None

    def acknowledge(self, job_id: str, receipt: str, now: datetime) -> bool:
        with self._transaction(now, removal=True) as effective_now:
            raw = self._entries.get(job_id)
            if raw is None:
                outcome = self._completed.get(job_id)
                return bool(outcome and outcome["state"] == "acknowledged" and outcome["receipt"] == receipt)
            if receipt != raw["receipt"]:
                return False
            self._removal_transaction([self._removal_entry(raw, 'acknowledged', effective_now)], effective_now)
            self._entries.pop(job_id, None)
            return True

    def extend(self, job_id: str, until: datetime, now: datetime) -> CollectResult | None:
        with self._transaction(now) as effective_now:
            result = self._collect(job_id, effective_now)
            if result is None:
                return None
            _utc(until, "until")
            if until == result.expires_at:
                return result
            if until < result.expires_at or until > result.retained_at + MAX_RETENTION:
                raise ValueError("extension must not shorten retention or exceed 30 days total")
            raw = dict(self._entries[job_id], extended_until=until.isoformat())
            self._write(job_id, raw)
            return self._collect(job_id, effective_now)

    def delete(self, job_id: str, now: datetime) -> bool:
        with self._transaction(now, removal=True) as effective_now:
            if job_id not in self._entries:
                return False
            self._removal_transaction([
                self._removal_entry(self._entries[job_id], 'deleted', effective_now)
            ], effective_now)
            self._entries.pop(job_id, None)
            return True

    def delete_all(self, now: datetime) -> int:
        _utc(now, "now")
        with self._locked():
            before_replay = sum(self._inspect_available(path) is _Inspection.SAFE
                                for path in self.root.glob('*.json'))
            journal_quarantined = False
            try:
                # Do not deserialize every answer just to delete them.
                self._load(content=False)
            except StoreCorrupted:
                # Preserve damaged evidence, but let the user remove content
                # once a fresh valid slot can be allocated and checked.
                try:
                    self._read_slot()
                except StoreCorrupted:
                    slot = self._path(REMOVAL_SLOT_NAME, check_link=False)
                    if self._inspect_available(slot) is not _Inspection.SAFE:
                        raise
                    name = f"{REMOVAL_SLOT_NAME}.corrupt-{now.timestamp():.6f}-{secrets.token_hex(4)}"
                    os.replace(slot, self._path(name))
                    self._sync_directory()
                    self._read_slot()
                self._ensure_capacity()
                if not self._journal_valid:
                    journal_quarantined = True
                    journal = self._path("outcomes.jsonl")
                    if journal.exists():
                        now = max(now, datetime.fromtimestamp(
                            journal.stat().st_mtime, timezone.utc))
                        name = f"outcomes.jsonl.corrupt-{now.timestamp():.6f}-{secrets.token_hex(4)}"
                        os.replace(journal, self._path(name))
                    self._sync_directory()
                    self._completed = {}
                    self._removed = {}
                    # Archived references cannot identify outcomes on replay.
                    # Register them if possible; under ENOSPC use hashes only.
                    self._references = {}
                    self._journal_lines = 0
                    self._snapshot_only = False
                    self._snapshot_bytes = 0
                    self._active = None
                    self._journal_ids = set()
                if self._slot is not None:
                    intent = self._slot
                    self._completed.update({r['job']['job_id']: r
                                            for r in self._slot_transaction(intent)['outcomes']})
                    self._removed.update(intent['removed'])
                    when = datetime.fromisoformat(intent['watermark'])
                    self._latest = max(self._latest, when) if self._latest else when
                    self._apply(intent['operations'])
            now = max(now, self._latest) if self._latest else now
            # Changes above are durable already. Count uncertain files as
            # present rather than losing quarantine state by raising here.
            after_replay = sum(self._inspect(path) in {_Inspection.SAFE, _Inspection.UNINSPECTABLE}
                               for path in self.root.glob('*.json'))
            count = max(0, before_replay - after_replay)
            batch = []

            def remove_batch(entries):
                if journal_quarantined:
                    try:
                        self._ensure_references(entries, now)
                    except OSError as error:
                        if error.errno != errno.ENOSPC:
                            raise
                        entries = [dict(entry, outcome=None)
                                   if entry['outcome'] is not None
                                   and self._job_hash(entry['outcome']['job']['job_id']) not in self._references
                                   else entry for entry in entries]
                return self._removal_transaction(entries, now)

            active_files = {op['file'] for op in self._active['operations']
                            if op['kind'] == 'write'
                            and Path(op['file']).stem not in self._removed} if self._active else set()

            def candidates():
                # Include absent targets: their staged content must acquire a
                # durable removed hash before its only temp copy is discarded.
                for filename in active_files:
                    yield self._path(filename)
                for path in self.root.glob('*.json'):
                    if path.name not in active_files:
                        yield path

            batch_limit = self._removal_batch_limit()
            try:
                for path in candidates():
                    inspection = self._inspect_available(path)
                    exists = inspection is _Inspection.SAFE
                    if inspection is _Inspection.UNSAFE:
                        continue
                    # Only this answer is resident. A malformed answer still
                    # receives a filename-hash removal intent before unlink.
                    entry = {'file': path.name, 'outcome': None}
                    job = self._references.get(path.stem)
                    if path.name in active_files and job is not None:
                        entry['outcome'] = {'job': job, 'state': 'deleted',
                                            'ended_at': now.isoformat()}
                    try:
                        raw = json.loads(path.read_text(encoding='utf-8')) if exists else None
                        if raw is None:
                            raise ValueError('absent open-write target')
                        job = JobRef(**raw['output']['job'])
                        if (path.name == self._filename(job.job_id)
                                and job.job_id not in self._completed
                                and self._job_hash(job.job_id) not in self._removed):
                            try:
                                state = 'expired' if self._expiry(raw) <= now else 'deleted'
                            except (ValueError, KeyError, TypeError):
                                state = 'deleted'
                            entry = self._removal_entry(raw, state, now, filename=path.name)
                    except PermissionError:
                        raise PendingOutputTemporarilyUnavailable() from None
                    except (ValueError, KeyError, TypeError, AttributeError):
                        pass
                    # Drop the answer before accumulating the next reference.
                    raw = None
                    batch.append(entry)
                    if len(batch) == batch_limit:
                        count += remove_batch(batch)
                        batch = []
                        # The next batch can transfer the now-full slot only
                        # after this batch has had a chance to free space.
                        batch_limit = REMOVAL_BATCH_SIZE
                        self._checkpoint('removal_batch_finished')
                count += remove_batch(batch)
                # A removed active target skips replay when its write closes.
                # Retain temps for targets skipped by the safe-file checks.
                protected_temps = {op['temp'] for op in self._active['operations']
                                   if op['kind'] == 'write'
                                   and Path(op['file']).stem not in self._removed} if self._active else set()
                uninspectable_temp = False
                for path in self.root.glob('*.tmp'):
                    if path.name in protected_temps:
                        continue
                    inspection = self._inspect(path, retry=False)
                    if inspection is _Inspection.SAFE:
                        path.unlink()
                    elif inspection is _Inspection.UNINSPECTABLE:
                        uninspectable_temp = True
                self._sync_directory()
                if uninspectable_temp:
                    raise PendingOutputTemporarilyUnavailable()
                return count
            except OSError as error:
                if error.errno == errno.ENOSPC:
                    raise PendingOutputCapacityUnavailable() from None
                raise
            finally:
                self._entries = {}

    def pending(self, now: datetime) -> tuple[PendingEntry, ...]:
        with self._transaction(now, read_only=True) as effective_now:
            results = (self._collect(job_id, effective_now) for job_id in self._entries)
            return tuple(PendingEntry(r.output.job, r.output.job_status, r.expires_at, r.expiring_soon)
                         for r in sorted(results, key=lambda r: (r.expires_at, r.output.job.job_id)))

    def purge_expired(self, now: datetime) -> tuple[JobRef, ...]:
        with self._transaction(now, removal=True):
            return tuple(JobRef(**raw["output"]["job"]) for raw in self._expired)

    def state(self, job_id: str, now: datetime) -> Literal["pending", "acknowledged", "expired", "deleted"] | None:
        with self._transaction(now, read_only=True):
            if job_id in self._entries:
                return "pending"
            outcome = self._completed.get(job_id)
            return outcome["state"] if outcome else None

    def outcomes(self, now: datetime, *, since: datetime) -> tuple[JobOutcome, ...]:
        _utc(since, "since")
        with self._transaction(now):
            results = (JobOutcome(JobRef(**r["job"]), r["state"], datetime.fromisoformat(r["ended_at"]))
                       for r in self._completed.values())
            return tuple(sorted((r for r in results if since <= r.ended_at <= now),
                                key=lambda r: (r.ended_at, r.job.job_id)))


def default_store(*, metadata_only: bool = False) -> FileOutputStore | PendingMetadataView:
    """Select the default location; metadata views never initialize storage."""
    from orchestrator.paths import pending_output_dir

    root = pending_output_dir()
    return PendingMetadataView(root) if metadata_only else FileOutputStore(root)


@dataclass(frozen=True)
class RetainedMetadata:
    """Internal correlation and retention facts; never a collection object."""

    job: JobRef
    job_status: str
    expires_at: datetime
    expiring_soon: bool
    may_have_changed_files: bool


def pending_metadata(root, now, *, deadline, clock=time.monotonic,
                     scan_cap=256, byte_cap=8 * 1024 * 1024):
    """Read an existing store without constructor, repair, purge or commit.

    One nonblocking lock attempt. A torn/open journal, unsafe file, exhausted
    scan/byte/time budget or uncertain removal evidence is unavailable, never
    an empty result. Committed removal intent wins over leftover content.
    """
    _utc(now, 'now')
    view = FileOutputStore.__new__(FileOutputStore)
    view.root = Path(root).absolute()
    spent = 0

    def budget():
        if clock() >= deadline:
            raise PendingOutputTemporarilyUnavailable()

    def read(name, cap, *, optional=False):
        nonlocal spent
        budget()
        path = view._path(name, check_link=False)
        try:
            before = path.lstat()
        except FileNotFoundError:
            if optional:
                return None
            raise PendingOutputTemporarilyUnavailable() from None
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or getattr(before, 'st_file_attributes', 0) & 0x400
                or before.st_size > cap):
            raise PendingOutputTemporarilyUnavailable()
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_BINARY', 0))
        try:
            opened = os.fstat(fd)
            if (opened.st_ino, opened.st_dev, opened.st_size) != (
                    before.st_ino, before.st_dev, before.st_size):
                raise PendingOutputTemporarilyUnavailable()
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                data = stream.read(min(cap, byte_cap - spent) + 1)
            spent += len(data)
            budget()
            if len(data) > cap or spent > byte_cap:
                raise PendingOutputTemporarilyUnavailable()
            return data
        finally:
            os.close(fd)

    lock = None
    fd = None
    locked = False
    try:
        budget()
        info = view.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PendingOutputTemporarilyUnavailable()
        with _locks_guard:
            lock = _locks.setdefault(view.root.resolve(), threading.RLock())
        if not lock.acquire(blocking=False):
            lock = None
            raise PendingOutputTemporarilyUnavailable()
        # No O_CREAT: an absent store/lock remains absent.
        lock_path = view._path('store.lock')
        lock_info = lock_path.lstat()
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or getattr(lock_info, 'st_file_attributes', 0) & 0x400):
            raise PendingOutputTemporarilyUnavailable()
        fd = os.open(lock_path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        opened = os.fstat(fd)
        if (opened.st_ino, opened.st_dev) != (lock_info.st_ino, lock_info.st_dev):
            raise PendingOutputTemporarilyUnavailable()
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        removed, completed, references = {}, set(), {}
        active = None
        journal = read('outcomes.jsonl', 2 * JOURNAL_MAX_BYTES)
        if not journal.endswith(b'\n'):
            raise PendingOutputTemporarilyUnavailable()
        lines = journal.splitlines()
        if len(lines) > 2 * JOURNAL_MAX_LINES:
            raise PendingOutputTemporarilyUnavailable()
        for index, line in enumerate(lines):
            budget()
            record = json.loads(line)
            view._read_record(record, active)
            kind = record['kind']
            if kind == 'snapshot' and (index != 0 or active is not None):
                raise PendingOutputTemporarilyUnavailable()
            if kind in {'snapshot', 'transaction'}:
                if active is not None:
                    raise PendingOutputTemporarilyUnavailable()
                now = max(now, datetime.fromisoformat(record['watermark']))
                removed.update(record.get('removed', {}))
                completed.update(r['job']['job_id'] for r in record['outcomes'])
                references.update(record.get('references', {}))
                if kind == 'transaction':
                    active = record
            else:
                active = None
        if active is not None:
            raise PendingOutputTemporarilyUnavailable()
        view._references = references
        frames = read(REMOVAL_SLOT_NAME, REMOVAL_SLOT_BYTES, optional=True)
        if frames is not None:
            if len(frames) != REMOVAL_SLOT_BYTES:
                raise PendingOutputTemporarilyUnavailable()
            valid = []
            for bank in range(2):
                try:
                    digest, data, _ = frames[bank * _SLOT_BANK_BYTES:(bank + 1) * _SLOT_BANK_BYTES].split(b'\n', 2)
                    if hashlib.sha256(data).hexdigest().encode() != digest:
                        continue
                    frame = json.loads(data)
                    if type(frame['sequence']) is not int or frame['sequence'] < 0:
                        continue
                    intent = frame['intent']
                    if intent is not None:
                        view._read_record(view._slot_transaction(intent), None)
                        if (intent['kind'] != 'transaction'
                                or len(intent['operations']) > REMOVAL_BATCH_SIZE
                                or len(intent['hash_outcomes']) > REMOVAL_BATCH_SIZE
                                or any(op['kind'] != 'remove' for op in intent['operations'])
                                or any(r['job_hash'] not in intent['removed'] for r in intent['hash_outcomes'])):
                            continue
                    valid.append((frame['sequence'], bank, intent))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
            if not valid:
                raise PendingOutputTemporarilyUnavailable()
            intent = max(valid, key=lambda v: v[0])[2]
            if intent is not None:
                removed.update(intent['removed'])
                now = max(now, datetime.fromisoformat(intent['watermark']))
        result = []
        with os.scandir(view.root) as directory:
            for index, item in enumerate(directory):
                budget()
                if index >= scan_cap:
                    raise PendingOutputTemporarilyUnavailable()
                if not item.name.endswith('.json') or item.name[:-5] in removed:
                    continue
                raw = json.loads(read(item.name, byte_cap))
                job = JobRef(**raw['output']['job'])
                if item.name != view._filename(job.job_id):
                    raise PendingOutputTemporarilyUnavailable()
                if references.get(view._job_hash(job.job_id)) != raw['output']['job']:
                    raise PendingOutputTemporarilyUnavailable()
                retained = datetime.fromisoformat(raw['retained_at'])
                expiry = view._expiry(raw)
                _utc(retained, 'retained_at')
                _utc(expiry, 'expires_at')
                if not retained + RETENTION <= expiry <= retained + MAX_RETENTION:
                    raise PendingOutputTemporarilyUnavailable()
                if job.job_id in completed or expiry <= now:
                    continue
                status = JobStatus(raw['output']['job_status']).value
                # An explicitly observed empty list is affirmative evidence.
                # Null, absence, claims and lifecycle status prove nothing.
                changed = raw['output'].get('changed_files') != []
                result.append(RetainedMetadata(job, status, expiry,
                                               expiry - now <= EXPIRY_WARNING, changed))
        budget()
        return tuple(sorted(result, key=lambda r: (r.expires_at, r.job.job_id)))
    except Exception:
        raise PendingOutputTemporarilyUnavailable() from None
    finally:
        if fd is not None:
            try:
                if locked:
                    if os.name == 'nt':
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        if lock is not None:
            lock.release()


@dataclass(frozen=True)
class PendingMetadataView:
    """Read-only service seam; selecting it never constructs a writable store."""

    root: Path

    def host_summary(self, db_path, workspace_key, *, now=None, deadline=None,
                     clock=time.monotonic):
        return host_summary(db_path, workspace_key, now=now, deadline=deadline,
                            clock=clock, root=self.root)


def host_summary(db_path, workspace_key, *, root, now=None, deadline=None, clock=time.monotonic):
    """Service-side read-only projection; unavailable never becomes zero."""
    from orchestrator.adapters.pending_reminder import ROW_CAP, valid_workspace_key, validate_summary
    from orchestrator.service.history import pending_project_metadata

    if not valid_workspace_key(workspace_key):
        raise ValueError('pending_scope_invalid')
    deadline = deadline if deadline is not None else clock() + 0.75
    entries = pending_metadata(root, now or datetime.now(timezone.utc),
                               deadline=deadline, clock=clock)
    linked = pending_project_metadata(db_path, (e.job for e in entries), workspace_key,
                                      deadline=deadline, clock=clock)
    matching = [e for e in entries if e.job.job_id in linked]
    jobs = [{
        'job_id': e.job.job_id, **linked[e.job.job_id],
        'may_have_changed_files': e.may_have_changed_files,
        'expires_at': e.expires_at.isoformat().replace('+00:00', 'Z'),
        'expiring_soon': e.expiring_soon,
    } for e in matching[:ROW_CAP]]
    result = {'count': len(matching), 'more': len(matching) > ROW_CAP, 'jobs': jobs}
    validate_summary(result)
    if clock() >= deadline:
        raise ValueError('pending_summary_unavailable')
    return result

