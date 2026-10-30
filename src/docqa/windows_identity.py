"""Read NTFS change identities by directory, without opening every asset.

FILE_ID_EXTD_DIR_INFO includes ChangeTime and the 128-bit file ID. No TTL or
mtime-only shortcut is used. Unsupported filesystems fail closed to full hashing.
See https://learn.microsoft.com/windows/win32/api/winbase/ns-winbase-file_id_extd_dir_info
"""

import ctypes
from ctypes import wintypes
from pathlib import Path


class Entry(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in ("next", "index")]
    _fields_ += [
        (n, ctypes.c_longlong)
        for n in ("created", "accessed", "written", "changed", "size", "allocated")
    ]
    _fields_ += [(n, wintypes.DWORD) for n in ("attributes", "name_bytes", "ea", "reparse")]
    _fields_ += [("identity", ctypes.c_ubyte * 16), ("name", wintypes.WCHAR * 1)]


def directory_tree_stamp(directory, *, open_file, get_info, close_handle):
    result = []

    def visit(folder, prefix=""):
        # OPEN_REPARSE_POINT prevents following a directory replaced by a link.
        handle = open_file(str(folder), 1, 7, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError("Cannot enumerate package identity")
        subdirectories = []
        try:
            basic = ctypes.create_string_buffer(40)
            if not get_info(handle, 0, basic, 40):
                raise OSError("Cannot inspect directory")
            attrs = wintypes.DWORD.from_buffer(basic, 32).value
            if attrs & 0x400 or not attrs & 0x10:
                raise OSError("Package directory is a link or non-directory")
            buffer = ctypes.create_string_buffer(65536)
            first = True
            while True:
                if not get_info(handle, 20 if first else 19, buffer, len(buffer)):
                    if ctypes.get_last_error() == 18:  # ERROR_NO_MORE_FILES
                        break
                    raise OSError("Change identity enumeration unavailable")
                first = False
                offset = 0
                while True:
                    item = Entry.from_buffer(buffer, offset)
                    start = offset + Entry.name.offset
                    end = start + item.name_bytes
                    if item.name_bytes % 2 or end > len(buffer):
                        raise OSError("Invalid directory record")
                    name = bytes(buffer[start:end]).decode("utf-16-le")
                    if name not in {".", ".."}:
                        if not name or any(c in name for c in "\\/\0") or item.attributes & 0x400:
                            raise OSError("Package links cannot be cached")
                        relative = prefix + name
                        if item.attributes & 0x10:
                            # Include directories as well, detecting even empty additions.
                            result.append((relative + "/", bytes(item.identity)))
                            subdirectories.append((Path(folder) / name, relative + "/"))
                        else:
                            if item.changed <= 0 or not any(item.identity):
                                raise OSError("Missing file change identity")
                            result.append(
                                (
                                    relative,
                                    (
                                        bytes(item.identity),
                                        item.size,
                                        item.written,
                                        item.changed,
                                        item.attributes,
                                    ),
                                )
                            )
                    if not item.next:
                        break
                    if item.next < Entry.name.offset or offset + item.next >= len(buffer):
                        raise OSError("Invalid directory offset")
                    offset += item.next
        finally:
            close_handle(handle)
        for child, relative in subdirectories:
            visit(child, relative)

    visit(directory)
    return tuple(sorted(result)) if result else None
