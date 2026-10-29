"""Bounded process-local verified values, invalidated by filesystem change identity.

No verification decisions are persisted. Windows uses FILE_BASIC_INFO.ChangeTime,
not st_ctime (creation time), so restoring LastWriteTime cannot hide a write.
If reliable change metadata cannot be read, callers must do full validation.
"""

from __future__ import annotations

import copy
import os
import stat
import threading
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
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, change)


def tree_stamp(directory: Path) -> tuple | None:
    """Detect changed bytes' metadata, rename/replacement and added/deleted files."""
    try:
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


class VerifiedCache:
    def __init__(self, *, max_entries=4, max_bytes=64 * 1024 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
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
            return copy.deepcopy(entry[1])

    def put(self, key, stamp, value, size):
        if stamp is None or size > self.max_bytes:
            return
        with self.lock:
            self.entries[key] = (stamp, copy.deepcopy(value), size)
            self.entries.move_to_end(key)
            while (
                len(self.entries) > self.max_entries
                or sum(e[2] for e in self.entries.values()) > self.max_bytes
            ):
                self.entries.popitem(last=False)

    def clear(self):
        with self.lock:
            self.entries.clear()
