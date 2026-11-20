"""The demo's own files: created exclusively, recorded, removed exactly.

``hl7bridge demo`` writes every folder and file through ``OwnedFiles``.
Each one is created exclusively, so a name that already exists is never
reused or overwritten, and is recorded in memory, in order, with its file
identity: device and inode on Linux and macOS, volume serial number and
file id on Windows. A file's identity is read from the handle that
created it.

``remove_owned`` is the only code in hl7bridge that deletes anything. It
works only on a folder this same run made with ``tempfile.mkdtemp``. It
goes through the record newest first and removes an entry only when its
name still leads to the same kind of object with the recorded identity; a
folder must also be empty, since removal is never recursive. Then it
removes the temporary folder itself, if it is empty and still has its
recorded identity. Links are never followed:

- On Windows each entry is opened with ``FILE_FLAG_OPEN_REPARSE_POINT``,
  so a junction or symlink is opened as itself. Kind and identity are read
  from that handle and the delete goes through the same handle, so what was
  checked is what is deleted.
- On Linux and macOS each folder on the way down is opened with
  ``O_NOFOLLOW`` and must have its recorded identity. The last name is
  checked with ``stat(..., dir_fd=..., follow_symlinks=False)`` and removed
  with ``unlink`` or ``rmdir`` relative to that open folder. Between that
  check and the delete, a program running as you could still rename
  another file onto that name; ``mkdtemp`` makes the folder with mode 0700,
  so no other user can.

Anything that fails a check is left in place, and the folder is kept.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .errors import DemoDirError

Identity = tuple[int, int]

REMOVED, MISSING, KEPT = "removed", "missing", "kept"

# Exclusive create: fails if the name exists, a dangling symlink included.
_NEW_FILE = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class Entry(NamedTuple):
    parts: tuple[str, ...]
    is_dir: bool
    identity: Identity

    @property
    def rel(self) -> str:
        return "/".join(self.parts)


@dataclass(slots=True)
class RemovalResult:
    removed_files: int = 0
    removed_dirs: int = 0
    root_removed: bool = False
    kept: list[str] = field(default_factory=list)


class OwnedFiles:
    """A folder the demo writes into, and the record of what it created.

    ``temporary`` is True only for a folder made by ``tempfile.mkdtemp`` in
    this run; only such a folder can be passed to ``remove_owned``."""

    def __init__(self, root: Path, *, temporary: bool) -> None:
        self.root = root
        self.temporary = temporary
        self.root_identity: Identity | None = dir_identity(root) if temporary else None
        self.entries: list[Entry] = []
        self._dirs: dict[tuple[str, ...], Identity | None] = {(): self.root_identity}

    def _parts(self, path: Path) -> tuple[str, ...]:
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            raise DemoDirError(f"{path} is not inside the demo folder {self.root}") from None
        if any(p in ("", ".", "..") for p in parts):
            raise DemoDirError(f"{path} is not a plain path inside {self.root}")
        return parts

    def dir_identity_of(self, parts: tuple[str, ...]) -> Identity | None:
        return self._dirs.get(parts)

    def makedirs(self, path: Path) -> None:
        parts = self._parts(path)
        for i in range(1, len(parts) + 1):
            prefix = parts[:i]
            if prefix in self._dirs:
                continue
            target = self.root.joinpath(*prefix)
            os.mkdir(target)  # exclusive: a folder that is already there is never adopted
            identity = dir_identity(target)
            self._dirs[prefix] = identity
            self.entries.append(Entry(prefix, True, identity))

    def write_bytes(self, path: Path, data: bytes) -> None:
        parts = self._parts(path)
        if not parts:
            raise DemoDirError(f"{path} is the demo folder itself, not a file in it")
        self.makedirs(path.parent)
        fd = os.open(path, _NEW_FILE, 0o600)
        try:
            identity = fd_identity(fd)
        except BaseException:
            os.close(fd)
            raise
        # Recorded before the write, so a failed write is still cleaned up.
        self.entries.append(Entry(parts, False, identity))
        with open(fd, "wb", closefd=True) as fh:
            fh.write(data)


def _before_step(entry: Entry | None) -> None:
    """No-op hook. Tests replace it to change the folder right before each
    removal step (``None`` is the step that removes the folder itself)."""


def remove_owned(owned: OwnedFiles) -> RemovalResult:
    """Remove what this run created in its temporary folder, and nothing
    else. See the module docstring for every check."""
    if not owned.temporary or owned.root_identity is None:
        raise DemoDirError(
            f"refusing to delete anything in {owned.root}: the demo deletes only a"
            " temporary folder it made itself in this run"
        )
    result = RemovalResult()
    for entry in reversed(owned.entries):
        _before_step(entry)
        try:
            outcome = _remove_entry(owned, entry)
        except OSError:
            outcome = KEPT
        if outcome == REMOVED:
            if entry.is_dir:
                result.removed_dirs += 1
            else:
                result.removed_files += 1
        elif outcome == KEPT:
            result.kept.append(entry.rel)
    _before_step(None)
    try:
        result.root_removed = _remove_root(owned.root, owned.root_identity)
    except OSError:
        result.root_removed = False
    return result


def _ident(st: os.stat_result) -> Identity:
    return (st.st_dev, st.st_ino)


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _FileInfo(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _CreateFileW.restype = wintypes.HANDLE
    _GetFileInformationByHandle = _kernel32.GetFileInformationByHandle
    _GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(_FileInfo))
    _GetFileInformationByHandle.restype = wintypes.BOOL
    _SetFileInformationByHandle = _kernel32.SetFileInformationByHandle
    _SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _SetFileInformationByHandle.restype = wintypes.BOOL
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL

    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    _DELETE = 0x00010000
    _FILE_READ_ATTRIBUTES = 0x0080
    _SHARE_ALL = 0x1 | 0x2 | 0x4  # read, write, delete
    _OPEN_EXISTING = 3
    _BACKUP_SEMANTICS = 0x02000000  # needed to open a folder
    _OPEN_REPARSE_POINT = 0x00200000  # open a link as itself, never its target
    _ATTR_DIRECTORY = 0x10
    _ATTR_REPARSE_POINT = 0x400
    _NOT_FOUND = (2, 3)  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
    _FILE_DISPOSITION_INFO = 4
    _FILE_DISPOSITION_INFO_EX = 21
    _DISPOSITION_DELETE_POSIX = 0x1 | 0x2  # FILE_DISPOSITION_FLAG_DELETE | POSIX_SEMANTICS

    def _open(path: Path, access: int) -> tuple[int | None, int]:
        handle = _CreateFileW(
            str(path),
            access,
            _SHARE_ALL,
            None,
            _OPEN_EXISTING,
            _BACKUP_SEMANTICS | _OPEN_REPARSE_POINT,
            None,
        )
        if handle is None or handle == _INVALID_HANDLE:
            return None, ctypes.get_last_error()
        return int(handle), 0

    def _info(handle: int) -> tuple[int, Identity] | None:
        info = _FileInfo()
        if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
            return None
        index = (info.nFileIndexHigh << 32) | info.nFileIndexLow
        return info.dwFileAttributes, (info.dwVolumeSerialNumber, index)

    def _delete(handle: int) -> bool:
        flags = wintypes.DWORD(_DISPOSITION_DELETE_POSIX)
        size = ctypes.sizeof(flags)
        if _SetFileInformationByHandle(
            handle, _FILE_DISPOSITION_INFO_EX, ctypes.byref(flags), size
        ):
            return True
        delete = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO, for older Windows and FAT
        return bool(
            _SetFileInformationByHandle(
                handle, _FILE_DISPOSITION_INFO, ctypes.byref(delete), ctypes.sizeof(delete)
            )
        )

    def fd_identity(fd: int) -> Identity:
        found = _info(msvcrt.get_osfhandle(fd))
        if found is None:
            raise OSError(ctypes.get_last_error(), "cannot read the identity of a new file")
        return found[1]

    def dir_identity(path: Path) -> Identity:
        handle, error = _open(path, _FILE_READ_ATTRIBUTES)
        if handle is None:
            raise OSError(error, f"cannot open {path}")
        try:
            found = _info(handle)
        finally:
            _CloseHandle(handle)
        if found is None:
            raise OSError(ctypes.get_last_error(), f"cannot read the identity of {path}")
        attrs, identity = found
        if not attrs & _ATTR_DIRECTORY or attrs & _ATTR_REPARSE_POINT:
            raise DemoDirError(f"{path} is not a real folder")
        return identity

    def _remove_checked(path: Path, *, is_dir: bool, identity: Identity) -> str:
        handle, error = _open(path, _DELETE | _FILE_READ_ATTRIBUTES)
        if handle is None:
            return MISSING if error in _NOT_FOUND else KEPT
        try:
            found = _info(handle)
            if found is None:
                return KEPT
            attrs, actual = found
            if attrs & _ATTR_REPARSE_POINT or bool(attrs & _ATTR_DIRECTORY) != is_dir:
                return KEPT
            if actual != identity:
                return KEPT
            # Fails for a folder that is not empty: removal is never recursive.
            return REMOVED if _delete(handle) else KEPT
        finally:
            _CloseHandle(handle)

    def _remove_entry(owned: OwnedFiles, entry: Entry) -> str:
        path = owned.root.joinpath(*entry.parts)
        return _remove_checked(path, is_dir=entry.is_dir, identity=entry.identity)

    def _remove_root(root: Path, identity: Identity) -> bool:
        return _remove_checked(root, is_dir=True, identity=identity) == REMOVED

else:
    _DIR_OPEN = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    _HAS_DIR_FD = {os.open, os.stat, os.unlink, os.rmdir} <= os.supports_dir_fd

    def fd_identity(fd: int) -> Identity:
        return _ident(os.fstat(fd))

    def dir_identity(path: Path) -> Identity:
        try:
            fd = os.open(path, _DIR_OPEN)
        except OSError:
            raise DemoDirError(f"{path} is not a real folder") from None
        try:
            return _ident(os.fstat(fd))
        finally:
            os.close(fd)

    def _check_last(st: os.stat_result, entry: Entry) -> bool:
        kind_ok = stat.S_ISDIR(st.st_mode) if entry.is_dir else stat.S_ISREG(st.st_mode)
        return kind_ok and _ident(st) == entry.identity

    def _remove_entry(owned: OwnedFiles, entry: Entry) -> str:
        if not _HAS_DIR_FD:
            return _remove_entry_by_path(owned, entry)
        fds: list[int] = []
        try:
            try:
                fds.append(os.open(owned.root, _DIR_OPEN))
            except FileNotFoundError:
                return MISSING
            if _ident(os.fstat(fds[-1])) != owned.root_identity:
                return KEPT
            for i, part in enumerate(entry.parts[:-1], start=1):
                try:
                    fds.append(os.open(part, _DIR_OPEN, dir_fd=fds[-1]))
                except FileNotFoundError:
                    return MISSING
                if _ident(os.fstat(fds[-1])) != owned.dir_identity_of(entry.parts[:i]):
                    return KEPT
            name = entry.parts[-1]
            try:
                st = os.stat(name, dir_fd=fds[-1], follow_symlinks=False)
            except FileNotFoundError:
                return MISSING
            if not _check_last(st, entry):
                return KEPT
            if entry.is_dir:
                os.rmdir(name, dir_fd=fds[-1])  # never recursive; fails if not empty
            else:
                os.unlink(name, dir_fd=fds[-1])
            return REMOVED
        finally:
            for fd in reversed(fds):
                os.close(fd)

    def _remove_entry_by_path(owned: OwnedFiles, entry: Entry) -> str:
        """For a system without dir_fd support: the same checks by path."""
        current = owned.root
        expected: list[Identity | None] = [owned.root_identity]
        expected += [owned.dir_identity_of(entry.parts[:i]) for i in range(1, len(entry.parts))]
        for i, want in enumerate(expected):
            if i:
                current = current / entry.parts[i - 1]
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                return MISSING
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or _ident(st) != want:
                return KEPT
        target = current / entry.parts[-1]
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return MISSING
        if not _check_last(st, entry):
            return KEPT
        if entry.is_dir:
            os.rmdir(target)
        else:
            os.unlink(target)
        return REMOVED

    def _remove_root(root: Path, identity: Identity) -> bool:
        if not _HAS_DIR_FD:
            st = os.lstat(root)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or _ident(st) != identity:
                return False
            os.rmdir(root)
            return True
        parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            st = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode) or _ident(st) != identity:
                return False
            os.rmdir(root.name, dir_fd=parent_fd)  # fails if anything is left inside
            return True
        finally:
            os.close(parent_fd)
