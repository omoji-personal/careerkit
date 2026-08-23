"""Small, best-effort primitives for CareerKit's private on-disk state.

The process umask is outside CareerKit's control and commonly permits group or
world reads.  State files can contain job history, application decisions, and
cached job descriptions, so creation must be private even under a permissive
umask.  Permission tightening is best-effort for filesystems (notably some
Windows filesystems) that do not implement POSIX modes; actual I/O errors still
surface to the caller.
"""
from __future__ import annotations

import builtins
import os
import tempfile
from pathlib import Path
from typing import IO


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _harden_fd(fd: int, mode: int = PRIVATE_FILE_MODE) -> None:
    """Tighten one already-open object without following a second path lookup."""
    try:
        os.fchmod(fd, mode)
    except (AttributeError, NotImplementedError, OSError):
        # Windows and non-POSIX filesystems may not expose useful mode bits.
        pass


def _harden_directory(path: Path) -> None:
    """Tighten a directory, preferring an opened descriptor where supported."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except (NotImplementedError, OSError):
        try:
            os.chmod(path, PRIVATE_DIR_MODE)
        except (NotImplementedError, OSError):
            pass
        return
    try:
        _harden_fd(fd, PRIVATE_DIR_MODE)
    finally:
        _close_quietly(fd)


def ensure_private_directory(
    path: str | os.PathLike[str], *, harden_existing: bool = True,
) -> Path:
    """Create every missing directory in *path* as 0700 and harden the leaf.

    ``Path.mkdir(parents=True, mode=0o700)`` applies the requested mode only to
    the leaf; its recursively-created parents use the default 0777 before the
    umask.  Building the missing chain ourselves closes that first-run leak.
    Existing ancestors are left alone because they may be intentionally shared,
    while an existing state-directory leaf is tightened by default.  Callers
    writing a configured file outside ``CAREERKIT_HOME`` can pass
    ``harden_existing=False``: newly created directories remain private, but an
    already-existing external parent keeps its owner-selected mode.
    """
    target = Path(path)
    target_existed = target.exists()
    missing: list[Path] = []
    cursor = target
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent

    for directory in reversed(missing):
        try:
            directory.mkdir(mode=PRIVATE_DIR_MODE)
        except FileExistsError:
            # A concurrent first run may have created it after the existence
            # check.  The normal not-a-directory error still surfaces below.
            if not directory.is_dir():
                raise
        _harden_directory(directory)

    if not target.is_dir():
        # Match mkdir's ordinary behavior when the path exists as a file.
        target.mkdir(mode=PRIVATE_DIR_MODE)
    if harden_existing or not target_existed:
        _harden_directory(target)
    return target


def harden_private_file(path: str | os.PathLike[str]) -> None:
    """Tighten an existing file through an opened descriptor, if possible."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except (NotImplementedError, OSError):
        return
    try:
        _harden_fd(fd)
    finally:
        _close_quietly(fd)


def prepare_private_file(
    path: str | os.PathLike[str], *, harden_parent: bool = True,
) -> Path:
    """Create a non-truncated private file or tighten the existing file."""
    target = Path(path)
    ensure_private_directory(target.parent, harden_existing=harden_parent)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(target, flags, PRIVATE_FILE_MODE)
    except FileExistsError:
        harden_private_file(target)
    else:
        try:
            _harden_fd(fd)
        finally:
            _close_quietly(fd)
    return target


def _private_opener(path: str, flags: int) -> int:
    return os.open(
        path,
        flags | getattr(os, "O_CLOEXEC", 0),
        PRIVATE_FILE_MODE,
    )


def open_private_text(
    path: str | os.PathLike[str], mode: str, *, encoding: str = "utf-8",
) -> IO[str]:
    """Open text state with 0600 creation and tighten an existing file."""
    fh = builtins.open(path, mode, encoding=encoding, opener=_private_opener)
    _harden_fd(fh.fileno())
    return fh


def atomic_write_private_text(
    path: str | os.PathLike[str], text: str, *, encoding: str = "utf-8",
    harden_parent: bool = True,
) -> None:
    """Atomically replace a text file with a same-directory 0600 temporary."""
    target = Path(path)
    ensure_private_directory(target.parent, harden_existing=harden_parent)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
    )
    temporary_path = Path(temporary)
    try:
        _harden_fd(fd)
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fd = -1
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary_path, target)
        harden_private_file(target)
    finally:
        if fd >= 0:
            _close_quietly(fd)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
