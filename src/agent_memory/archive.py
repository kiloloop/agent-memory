# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Archive and restore supplementary memory files without clobbering or escaping.

A project's memory directory holds the active files the layout names plus
any number of supplementary files. ``archive`` moves one supplementary file
into ``memory/archive/`` under ``<UTC timestamp>_<basename>``; ``restore``
moves an archived file back to its original basename, into an active slot
that must be empty. The active files are never archived.

Three invariants replace check-then-rename:

* **No replace.** The move is ``os.link`` then ``os.unlink``. The link fails
  if the destination exists at the instant it is made, so a file that
  appears between any check and the move is never overwritten, and the
  moved file keeps its bytes and metadata because it is the same inode. A
  filesystem without hard links is refused rather than worked around.
* **Containment.** The project name and both basenames are validated
  lexically. The home is resolved once; every directory below it
  (``projects``, the project, ``memory``, ``memory/archive``) is opened one
  path component at a time with ``O_NOFOLLOW``, so a symlink at any level is
  refused, and the resulting directory handles address the file for every
  check and for the move itself. A directory swapped for a symlink after it
  was checked cannot redirect the link or the unlink: both run relative to
  the handle, never by re-resolving a path. The file itself may not be a
  symlink.
* **Protected identity.** An active file is protected by what it is, not by
  how it is spelled: a source whose inode is one of the active files (a case
  variant on a case-insensitive filesystem, a normalization variant, a hard
  link) is refused like the exact name.

A dry run performs every check, including the ones on the archive path, and
reports the same paths; only the directory creation and the move are
skipped. POSIX only: Windows is refused, dry run included.
"""

from __future__ import annotations

import datetime as dt
import errno
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import layout

ARCHIVE_DIR = "archive"
#: The active files of a project tier; these are never archived.
PROTECTED_FILES: Tuple[str, ...] = layout.PROJECT.files
_PLATFORM = os.name

#: errno values a filesystem without hard links (or one that forbids them) answers os.link with.
_NO_HARD_LINKS = frozenset(
    code for code in (getattr(errno, name, None) for name in ("EPERM", "EXDEV", "ENOTSUP", "EOPNOTSUPP", "EACCES")) if code
)
#: errno values open(O_NOFOLLOW) answers with when the last component is a symlink (Linux/macOS: ELOOP; BSD: EMLINK).
_IS_SYMLINK = frozenset(code for code in (getattr(errno, name, None) for name in ("ELOOP", "EMLINK")) if code)
#: Open a directory by one component, never following a symlink at that component.
_DIR_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)

_SAFE_BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ARCHIVED_BASENAME_RE = re.compile(r"^(?P<timestamp>\d{8}T\d{6}Z)_(?P<basename>[A-Za-z0-9][A-Za-z0-9._-]{0,127})$")


class ArchiveError(Exception):
    """The operation was refused; nothing was moved."""


# --- names -----------------------------------------------------------------


def validate_memory_basename(file_name: str) -> None:
    if "/" in file_name or "\\" in file_name or not _SAFE_BASENAME_RE.fullmatch(file_name):
        raise ArchiveError("memory file name must be a simple basename containing only [A-Za-z0-9._-]")


def build_archive_name(memory_file: str, now: Optional[dt.datetime] = None) -> str:
    validate_memory_basename(memory_file)
    current = now or dt.datetime.now(dt.timezone.utc)
    return f"{current.astimezone(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{memory_file}"


def original_name_from_archive(archived_file: str) -> str:
    if "/" in archived_file or "\\" in archived_file:
        raise ArchiveError("archived file name must be a simple basename")
    match = _ARCHIVED_BASENAME_RE.fullmatch(archived_file)
    if match is None:
        raise ArchiveError("archived file name must match <UTC timestamp>_<basename>")
    return match.group("basename")


# --- the verbs -------------------------------------------------------------


def archive(
    home: Path,
    project: str,
    memory_file: str,
    *,
    dry_run: bool = False,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Move ``memory/<memory_file>`` to ``memory/archive/<UTC>_<memory_file>``."""
    _require_posix()
    validate_memory_basename(memory_file)
    if memory_file in PROTECTED_FILES:
        raise ArchiveError(f"cannot archive standard active memory file: {memory_file}")
    archived_file = build_archive_name(memory_file, now=now)
    memory_dir, memory_fd = _open_memory_dir(home, project)
    archive_dir = memory_dir / ARCHIVE_DIR
    source = memory_dir / memory_file
    destination = archive_dir / archived_file
    try:
        # The layout is validated before the file is looked up, on the path both runs share:
        # a symlink or a non-directory at memory/archive is refused here; an absent one is None.
        archive_fd = _open_archive_dir(memory_fd, archive_dir, create=False)
        try:
            source_stat = _stat_regular_file(memory_file, memory_fd, source, "memory file")
            _refuse_active_identity(source_stat, memory_fd, memory_dir, memory_file)
            if archive_fd is not None:
                _require_absent(archived_file, archive_fd, destination, "archive destination")
            if not dry_run:
                if archive_fd is None:
                    archive_fd = _open_archive_dir(memory_fd, archive_dir, create=True)
                if archive_fd is None:  # unreachable: create=True never returns None
                    raise ArchiveError(f"archive directory not found: {archive_dir}")
                _move_no_replace(memory_file, memory_fd, archived_file, archive_fd, source_stat, source, destination)
        finally:
            if archive_fd is not None:
                os.close(archive_fd)
    finally:
        os.close(memory_fd)
    return {
        "project": project,
        "action": "archive",
        "memory_file": memory_file,
        "archived_file": archived_file,
        "source": str(source),
        "destination": str(destination),
        "dry_run": dry_run,
        "status": "dry-run" if dry_run else "archived",
    }


def restore(home: Path, project: str, archived_file: str, *, dry_run: bool = False) -> Dict[str, Any]:
    """Move ``memory/archive/<archived_file>`` back to ``memory/<basename>``, which must not exist."""
    _require_posix()
    restored_file = original_name_from_archive(archived_file)
    memory_dir, memory_fd = _open_memory_dir(home, project)
    archive_dir = memory_dir / ARCHIVE_DIR
    source = archive_dir / archived_file
    destination = memory_dir / restored_file
    try:
        archive_fd = _open_archive_dir(memory_fd, archive_dir, create=False)
        if archive_fd is None:
            raise ArchiveError(f"memory archive directory not found: {archive_dir}")
        try:
            source_stat = _stat_regular_file(archived_file, archive_fd, source, "archived memory file")
            _require_absent(restored_file, memory_fd, destination, "active memory destination")
            if not dry_run:
                _move_no_replace(archived_file, archive_fd, restored_file, memory_fd, source_stat, source, destination)
        finally:
            os.close(archive_fd)
    finally:
        os.close(memory_fd)
    return {
        "project": project,
        "action": "restore",
        "archived_file": archived_file,
        "restored_file": restored_file,
        "source": str(source),
        "destination": str(destination),
        "dry_run": dry_run,
        "status": "dry-run" if dry_run else "restored",
    }


# --- containment: directory handles -----------------------------------------


def _require_posix() -> None:
    if _PLATFORM != "posix":
        raise ArchiveError("archive and restore are supported on POSIX systems only")


def _open_memory_dir(home: Path, project: str) -> Tuple[Path, int]:
    """The project's memory directory as (path, handle), every directory below the home opened without following symlinks."""
    try:
        layout.validate_project_name(project)
    except ValueError as exc:
        raise ArchiveError(str(exc)) from None
    root = Path(home).expanduser().resolve()
    memory_dir = layout.project_memory_dir(root, project)
    project_dir = memory_dir.parent
    fd = _open_dir(str(root), None, root, "home", lambda: f"home not found: {root}")
    current = root
    for part in memory_dir.relative_to(root).parts:
        current = current / part
        if current == memory_dir:
            missing = f"memory directory not found: {memory_dir}"
        else:
            missing = f"project '{project}' not found at {project_dir}"
        try:
            child = _open_dir(part, fd, current, "directory", lambda: missing)
        finally:
            os.close(fd)
        fd = child
    return memory_dir, fd


def _open_archive_dir(memory_fd: int, archive_dir: Path, *, create: bool) -> Optional[int]:
    """A handle on ``memory/archive``; ``None`` when it is absent and not to be created.

    A symlink or a non-directory at that name is refused here, on the path
    both dry run and real run share, so a dry run never reports a move the
    real run could not perform.
    """
    created = False
    while True:
        try:
            return os.open(ARCHIVE_DIR, _DIR_FLAGS, dir_fd=memory_fd)
        except OSError as exc:
            if exc.errno == errno.ENOENT and not create:
                return None
            if exc.errno == errno.ENOENT and not created:
                try:
                    os.mkdir(ARCHIVE_DIR, dir_fd=memory_fd)
                except FileExistsError:
                    pass
                except OSError as mkdir_exc:
                    raise ArchiveError(
                        f"cannot create the archive directory {archive_dir}: {mkdir_exc.strerror}"
                    ) from None
                created = True
                continue
            raise _open_error(
                exc, ARCHIVE_DIR, memory_fd, archive_dir, "archive directory", lambda: f"archive directory not found: {archive_dir}"
            ) from None


def _open_dir(name: str, dir_fd: Optional[int], path: Path, what: str, missing: Callable[[], str]) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise _open_error(exc, name, dir_fd, path, what, missing) from None


def _open_error(
    exc: OSError, name: str, dir_fd: Optional[int], path: Path, what: str, missing: Callable[[], str]
) -> ArchiveError:
    # Linux answers O_NOFOLLOW on a symlink with ELOOP; macOS answers O_DIRECTORY|O_NOFOLLOW with ENOTDIR.
    # Either way the open refused it; the lstat only decides which refusal to name.
    if exc.errno in _IS_SYMLINK or (exc.errno == errno.ENOTDIR and _is_symlink(name, dir_fd)):
        return ArchiveError(f"refusing to operate through a symlink: {what} {path} is a symlink")
    if exc.errno == errno.ENOTDIR:
        return ArchiveError(f"{what} {path} is not a directory")
    if exc.errno == errno.ENOENT:
        return ArchiveError(missing())
    return ArchiveError(f"cannot open {what} {path}: {exc.strerror}")


def _is_symlink(name: str, dir_fd: Optional[int]) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _stat_regular_file(name: str, dir_fd: int, path: Path, what: str) -> os.stat_result:
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise ArchiveError(f"{what} not found: {path}") from None
    except OSError as exc:
        raise ArchiveError(f"cannot stat {what} {path}: {exc.strerror}") from None
    if stat.S_ISLNK(st.st_mode):
        raise ArchiveError(f"refusing to move a symlink: {what} {path}")
    if not stat.S_ISREG(st.st_mode):
        raise ArchiveError(f"{what} is not a regular file: {path}")
    return st


def _require_absent(name: str, dir_fd: int, path: Path, what: str) -> None:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ArchiveError(f"cannot stat {what} {path}: {exc.strerror}") from None
    raise ArchiveError(f"{what} already exists: {path}")


def _refuse_active_identity(source_stat: os.stat_result, memory_fd: int, memory_dir: Path, memory_file: str) -> None:
    """Refuse a source that *is* an active file, whatever name addresses it on this filesystem."""
    for protected in PROTECTED_FILES:
        try:
            st = os.stat(protected, dir_fd=memory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ArchiveError(f"cannot stat memory file {memory_dir / protected}: {exc.strerror}") from None
        if (st.st_dev, st.st_ino) == (source_stat.st_dev, source_stat.st_ino):
            raise ArchiveError(
                f"cannot archive standard active memory file: {memory_file} is {protected} on this filesystem"
            )


# --- the move --------------------------------------------------------------


def _move_no_replace(
    src_name: str,
    src_fd: int,
    dst_name: str,
    dst_fd: int,
    expected: os.stat_result,
    source: Path,
    destination: Path,
) -> None:
    """Link ``src_name`` (in ``src_fd``) as ``dst_name`` (in ``dst_fd``) without replacing anything, then unlink the source.

    Both syscalls run relative to the directory handles, so nothing re-resolves a
    path after validation. The new link must be a regular file with the identity
    that was checked; a symlink or a different inode planted at the source name
    meanwhile is refused and the link (ours by construction) removed. Best effort
    for a regular file: some filesystems reuse a freed inode number at once. The
    name lies inside the validated directory either way.
    """
    try:
        os.link(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd, follow_symlinks=False)
    except FileExistsError:
        raise ArchiveError(f"destination already exists: {destination}") from None
    except OSError as exc:
        if exc.errno in _NO_HARD_LINKS:
            raise ArchiveError(
                f"cannot link {source} to {destination}: {exc.strerror}; "
                "archive and restore need a filesystem with hard links"
            ) from None
        raise ArchiveError(f"cannot link {source} to {destination}: {exc.strerror}") from None
    try:
        linked = os.stat(dst_name, dir_fd=dst_fd, follow_symlinks=False)
    except OSError as exc:
        raise ArchiveError(f"cannot stat the new link {destination}: {exc.strerror}") from None
    if (linked.st_dev, linked.st_ino) != (expected.st_dev, expected.st_ino) or not stat.S_ISREG(linked.st_mode):
        try:
            os.unlink(dst_name, dir_fd=dst_fd)
        except OSError:
            pass
        raise ArchiveError(f"refusing to complete the move: {source} changed after it was checked")
    try:
        os.unlink(src_name, dir_fd=src_fd)
    except OSError as exc:
        raise ArchiveError(
            f"archived copy created at {destination} but the source could not be removed: {source}: {exc.strerror}"
        ) from None
