"""Bounded process-local verified values, invalidated by filesystem change identity.

No verification decisions are persisted. Windows uses FILE_BASIC_INFO.ChangeTime,
not st_ctime (creation time), so restoring LastWriteTime cannot hide a write.
If reliable change metadata cannot be read, callers must do full validation.

Change times are only as fine as the filesystem clock tick, so a same-size
rewrite in the same tick keeps every metadata field. Like Git's "racily clean"
rule, a stamp whose change time is within RACY_SECONDS of now also carries a
content digest, and such stamps are never cached. Digests are compared only when
both stamps have one, so a file settling between two reads is not a "change".
"""

from __future__ import annotations

import copy
import hashlib
import os
import pickle
import stat
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _BasicInfo(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_longlong)
            for name in ("CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")
        ]
        _fields_.append(("FileAttributes", wintypes.DWORD))

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _open = _kernel.CreateFileW
    _open.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _open.restype = wintypes.HANDLE
    _info = _kernel.GetFileInformationByHandleEx
    _info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _info.restype = wintypes.BOOL
    _close = _kernel.CloseHandle
    _close.argtypes = [wintypes.HANDLE]
    _close.restype = wintypes.BOOL


RACY_SECONDS = 2
_FILETIME_EPOCH = 116444736000000000  # 1601-01-01 to 1970-01-01, in 100 ns units


def _racy(change: int) -> bool:
    """Whether a change time (FILETIME on Windows, ns elsewhere) is too recent to trust."""
    if os.name == "nt":
        now = time.time_ns() // 100 + _FILETIME_EPOCH
        return now - change < RACY_SECONDS * 10_000_000
    return time.time_ns() - change < RACY_SECONDS * 1_000_000_000


def _digest(path: Path) -> str:
    value = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            value.update(block)
    return value.hexdigest()


class Stamp(tuple):
    """Metadata tuple plus digests (keyed by relative path) of racily recent files."""

    def __new__(cls, meta, digests=None):
        value = super().__new__(cls, meta)
        value.digests = digests or {}
        return value

    def __eq__(self, other):
        if not isinstance(other, tuple):
            return NotImplemented
        if tuple(self) != tuple(other):
            return False
        theirs = getattr(other, "digests", {})
        return all(theirs[k] == v for k, v in self.digests.items() if k in theirs)

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    __hash__ = tuple.__hash__


def is_racy(stamp) -> bool:
    if isinstance(stamp, Stamp) and stamp.digests:
        return True
    return isinstance(stamp, tuple) and any(is_racy(item) for item in stamp)


def file_stamp(path: Path) -> tuple:
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
        raise OSError("Not a regular, unlinked file")
    change = value.st_ctime_ns
    if os.name == "nt":
        # Metadata access only; share read/write/delete so active consumers never
        # prevent a normal atomic publication or document lifecycle operation.
        handle = _open(str(path), 0, 7, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError("Cannot inspect file change identity")
        try:
            data = _BasicInfo()
            if (
                not _info(handle, 0, ctypes.byref(data), ctypes.sizeof(data))
                or data.FileAttributes & 0x400
                or data.ChangeTime <= 0
            ):
                raise OSError("Cannot inspect regular file change identity")
            change = data.ChangeTime
        finally:
            _close(handle)
    meta = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, change)
    return Stamp(meta, {"": _digest(path)} if _racy(change) else None)


def tree_stamp(directory: Path) -> tuple | None:
    """Detect changed bytes' metadata, rename/replacement and added/deleted files."""
    try:
        if os.name == "nt":
            from docqa.windows_identity import directory_tree_stamp

            result = directory_tree_stamp(
                directory, open_file=_open, get_info=_info, close_handle=_close
            )
            if result is None:
                return None
            return Stamp(
                result,
                {
                    name: _digest(directory / name)
                    for name, info in result
                    if not name.endswith("/") and _racy(info[3])
                },
            )
        values = []

        def visit(folder, prefix=""):
            with os.scandir(folder) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise OSError("Package links cannot be cached")
                    relative = prefix + entry.name
                    if stat.S_ISDIR(info.st_mode):
                        visit(entry.path, relative + "/")
                    elif stat.S_ISREG(info.st_mode):
                        values.append((relative, file_stamp(Path(entry.path))))
                    else:
                        raise OSError("Nonregular package entry")

        visit(directory)
        return tuple(sorted(values)) if values else None
    except (OSError, RecursionError):
        return None


def subtree_stamp(stamp, prefix: str):
    """Re-root the entries of a tree stamp under ``prefix`` (e.g. "dense/").

    Comparing this with ``tree_stamp(directory / prefix)`` re-checks only that
    subtree, for callers that already validated the whole tree once.
    """
    if stamp is None:
        return None
    meta = tuple(
        (name[len(prefix) :], info)
        for name, info in stamp
        if name.startswith(prefix) and name != prefix
    )
    digests = {
        name[len(prefix) :]: value
        for name, value in getattr(stamp, "digests", {}).items()
        if name.startswith(prefix)
    }
    return Stamp(meta, digests) if meta else None


class VerifiedCache:
    def __init__(self, *, max_entries=4, max_bytes=64 * 1024 * 1024, serialized=False):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        # Only trusted, already validated Python values enter this process-local
        # cache. These buffers are NEVER persisted or read from external input.
        # C serialization avoids walking huge sparse postings with deepcopy on
        # every phase and accounts for the actual retained buffer size.
        self.serialized = serialized
        self.entries = OrderedDict()
        self.lock = threading.RLock()
        self.flights = {}
        self.fallback = threading.RLock()

    @contextmanager
    def guard(self, key):
        with self.lock:
            if key not in self.flights and len(self.flights) < 64:
                self.flights[key] = threading.RLock()
            lock = self.flights.get(key, self.fallback)
        with lock:
            yield

    def get(self, key, stamp):
        with self.lock:
            entry = self.entries.get(key)
            if stamp is None or entry is None or entry[0] != stamp:
                self.entries.pop(key, None)
                return None
            self.entries.move_to_end(key)
            return pickle.loads(entry[1]) if self.serialized else copy.deepcopy(entry[1])

    def put(self, key, stamp, value, size):
        if stamp is None or is_racy(stamp) or (not self.serialized and size > self.max_bytes):
            return
        packed = pickle.dumps(value, protocol=5) if self.serialized else copy.deepcopy(value)
        size = len(packed) if self.serialized else size
        if size > self.max_bytes:
            return
        with self.lock:
            self.entries[key] = (stamp, packed, size)
            self.entries.move_to_end(key)
            while (
                len(self.entries) > self.max_entries
                or sum(e[2] for e in self.entries.values()) > self.max_bytes
            ):
                self.entries.popitem(last=False)

    def clear(self):
        with self.lock:
            self.entries.clear()
