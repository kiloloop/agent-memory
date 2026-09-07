# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Failure-atomic publication of one immutable record: stage, verify, link, read back.

The one implementation of the writer commit contract in the package; the
debrief writer uses it today and the event writer reuses it next. The
canonical path only ever holds a complete, verified record, never partial
bytes (one qualification, on platforms without a descriptor-bound link, is
stated below): the record is staged in a writer-owned private file, verified through
the descriptor that created it, then published with an atomic no-replace
primitive (``os.link``). A failure before publication leaves the canonical
namespace clean.

Staging ownership is a single invariant: **this writer only ever publishes an
inode it created itself.** The staging nonce is unpredictable, the staging
file is created with ``O_CREAT|O_EXCL|O_NOFOLLOW``, its bytes are verified
through that same descriptor, and the published name is confirmed to resolve
to that same ``(st_dev, st_ino)``. A pre-existing path is never read, adopted,
or linked. Stale staging artifacts are swept *after* the record is published,
when no writer of it can still need one.

What a record must satisfy internally is the caller's business: ``publish``
takes a ``verify`` callable that raises :class:`WriterError` when the bytes it
is handed are not a consistent record, and runs it over the staged bytes and
over the read-back.

Publication is bound to the verified descriptor where the platform can name
one: on Linux the link source is ``/proc/self/fd/<fd>``, and a staging name
swapped for a link to some other file meanwhile has orphaned the verified
inode, which the kernel then refuses to link (ENOENT): nothing foreign is ever
visible. Elsewhere (macOS, a Linux without ``/proc``) the link source is the
staging name and the contract is narrower: a swap in the window between
verification and the link makes the foreign file visible under the canonical
name from the link until the identity check that follows takes that name back
down, and a writer stopped in that interval leaves it there (the next writer
of the record reports it as a collision). What holds on such platforms is the
post-call state: the call returns with the verified record published, or
raises with the canonical path absent. That contract rests on the store
directory not being writable by other users, its default mode, so the swap
needs the owner's own uid; it is an accepted, documented platform limitation,
and the structural close is a private staging directory with the link bound
to that directory's descriptor. Either way the writer treats the swap as a
vanished stage, restages under a fresh nonce and retries; a swap that
persists exhausts the attempts and is reported, with the canonical path
absent.
"""

from __future__ import annotations

import errno
import os
import stat as stat_mod
import sys
from pathlib import Path
from typing import Callable, Optional, Tuple

# The leading dot keeps staging files outside the canonical namespace; the
# prefix is scoped to one canonical record, so every file matching it belongs
# to a writer of that exact record.
STAGE_PREFIX = ".stage."

# A staging file vanishes before publication when a concurrent writer of this
# record published and swept it (the sweep runs only after a record is
# published), or when its name was swapped under the writer (see the module
# docstring). A vanished stage is resolved against the landed record first:
# identical is idempotent, different is a collision, nothing landed is a
# restage. The vanish shows up at three points -- the fstat after the O_EXCL
# create reports 0 links, the link reports the stage missing, or the published
# name fails to resolve to the verified inode -- and all three raise
# _StageVanished. The bound covers the window where a sweep beat the publish
# into visibility, and turns a persistent swap into a reported failure.
PUBLISH_ATTEMPTS = 3

# Linux names an open descriptor's inode under /proc/self/fd, and linkat with
# AT_SYMLINK_FOLLOW publishes from that name: the link source is then the
# verified inode itself, not the staging name. Elsewhere the source is the
# name (see the module docstring).
LINK_VIA_FD = sys.platform.startswith("linux") and os.path.isdir("/proc/self/fd")

Verifier = Callable[[bytes], None]


class WriterError(Exception):
    """A validation or publication failure. Never leaves partial bytes.

    ``code`` is the process exit code the failure maps to: 1 for a usage or
    validation error, 2 for a publication failure.
    """

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


class _StageVanished(Exception):
    """Internal: the staging file disappeared before it could be published."""


def staging_path(target: Path) -> Path:
    """Writer-unique private staging name for ``target``.

    The nonce is unpredictable, which is what makes the ownership invariant
    hold: no other process can pre-create the path this writer is about to
    claim, so the ``O_EXCL`` create below always produces a fresh inode that
    this writer alone has ever written to.
    """
    nonce = f"{os.getpid():x}{os.urandom(8).hex()}"
    return target.with_name(f"{STAGE_PREFIX}{target.name}.{nonce}")


def _pread_all(fd: int, size: int) -> bytes:
    chunks = []
    offset = 0
    while offset < size:
        chunk = os.pread(fd, size - offset, offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _identity(path: Path) -> Tuple[int, int]:
    """The (device, inode) pair naming one filesystem object, without following."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise WriterError(f"cannot inspect {path}: {exc}") from exc
    return (st.st_dev, st.st_ino)


def _unlink_quietly(path: Path) -> None:
    """Best-effort removal of a staging name. Never fatal, by design.

    Every call site is cleanup: an error path that is already raising the real
    failure, the ``finally`` that releases the staging name after publication,
    or the post-publication sweep. Re-raising from any of them would replace an
    accurate outcome with an incidental one. A stage that survives the
    ``finally`` leaves the published inode with a second name, and the caller
    asserts ``st_nlink == 1`` immediately after, turning exactly that case into
    a reported failure.
    """
    try:
        os.unlink(path)
    except OSError:
        # Deliberate: see the docstring.
        pass


def read_publishable_target(target: Path) -> Optional[bytes]:
    """Return the existing canonical bytes, or None when the path is free.

    Never follows symlinks: a symlink or any non-regular file at the canonical
    path is a hard failure, not something to read through. The read is bound
    to the inode that was inspected -- a file swapped in between the inspection
    and the open is reported rather than silently accepted.
    """
    try:
        lst = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WriterError(f"cannot inspect canonical path {target}: {exc}") from exc

    if stat_mod.S_ISLNK(lst.st_mode):
        raise WriterError(
            f"canonical path {target} is a symlink; the store holds regular files only and the writer must not follow links"
        )
    if not stat_mod.S_ISREG(lst.st_mode):
        raise WriterError(f"canonical path {target} exists and is not a regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            raise WriterError(f"canonical path {target} became a symlink while it was being read") from exc
        raise WriterError(f"cannot open canonical path {target}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise WriterError(f"canonical path {target} exists and is not a regular file")
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
            raise WriterError(f"canonical path {target} was replaced while it was being read")
        return _pread_all(fd, st.st_size)
    finally:
        os.close(fd)


def _sweep_own_stages(target: Path, canonical: Optional[Tuple[int, int]] = None) -> None:
    """Remove staging artifacts for ``target`` left by writers of this record.

    Called only once the canonical record is published and verified. Two kinds
    of match are removable, and only those two: a single-link regular file
    owned by this euid (a stale or partial stage), and a regular file owned by
    this euid that shares ``canonical``, the record's own inode (an alias left
    behind when the ``finally`` unlink failed, which keeps the immutable record
    writable under a second name). Anything else -- a symlink, a directory, a
    multi-link file that is not the record, another user's file -- is left in
    place. It was never this writer's to delete.
    """
    prefix = f"{STAGE_PREFIX}{target.name}."
    euid = os.geteuid()
    try:
        entries = os.listdir(target.parent)
    except OSError:
        # The record is already published and verified; the sweep is tidying only.
        return
    for name in entries:
        if not name.startswith(prefix):
            continue
        candidate = target.parent / name
        try:
            st = os.lstat(candidate)
        except OSError:
            continue
        if not stat_mod.S_ISREG(st.st_mode) or st.st_uid != euid:
            continue
        is_record_alias = canonical is not None and (st.st_dev, st.st_ino) == canonical
        if st.st_nlink != 1 and not is_record_alias:
            continue
        _unlink_quietly(candidate)


def _link_verified(stage: Path, fd: int, target: Path) -> None:
    """Give the verified inode its canonical name, never replacing anything.

    Where the platform can name the descriptor, the link source is the
    descriptor and the staging name is irrelevant; elsewhere it is the name.
    Either way the call fails with FileExistsError when the canonical name is
    taken, which is the collision guard the contract wants.
    """
    if not LINK_VIA_FD:
        os.link(stage, target)
        return
    dir_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        # A destination dir_fd selects linkat(); follow_symlinks=True gives it
        # AT_SYMLINK_FOLLOW, which is what resolves the /proc name to the inode.
        os.link(f"/proc/self/fd/{fd}", target.name, dst_dir_fd=dir_fd, follow_symlinks=True)
    finally:
        os.close(dir_fd)


def _stage(target: Path, record: bytes, verify: Verifier) -> Tuple[Path, Tuple[int, int], int]:
    """Create, write and verify a staging file this writer owns outright.

    Returns ``(path, (st_dev, st_ino), fd)``. The identity pair is what binds
    verification to publication: the caller confirms the canonical name lands
    on this exact inode, so the bytes that were checked here are provably the
    bytes that became the record. The descriptor is returned open so the
    publication link can be bound to it; the caller closes it.
    """
    stage = staging_path(target)
    # O_RDWR, not O_WRONLY: the staged bytes are read back through this same
    # descriptor, which is what binds the verification to the published inode.
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(stage, flags, 0o644)
    except OSError as exc:
        raise WriterError(f"cannot create staging file {stage}: {exc}") from exc

    try:
        written = os.write(fd, record)
        if written != len(record):
            raise WriterError(f"short write staging {stage}: {written} of {len(record)} bytes")
        os.fsync(fd)

        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise WriterError(f"staging file {stage} is not a regular file")
        if st.st_nlink == 0:
            # Unlinked between the O_EXCL create and this fstat: a concurrent
            # writer of this record published and swept the directory. The
            # caller resolves against the landed record.
            raise _StageVanished()
        if st.st_nlink != 1:
            raise WriterError(
                f"staging file {stage} has {st.st_nlink} links; it must be the only "
                "name for its inode or publication would share it with another path"
            )

        # Verify through the descriptor that created the file, never by
        # reopening the path: the bytes checked and the inode published are
        # then provably the same object.
        staged = _pread_all(fd, st.st_size)
        if len(staged) != len(record) or staged != record:
            raise WriterError(f"staged bytes at {stage} do not match the composed record")
        try:
            verify(staged)
        except WriterError as exc:
            raise WriterError(f"staged record at {stage} is inconsistent: {exc}") from exc
        ident = (st.st_dev, st.st_ino)
    except BaseException:
        os.close(fd)
        _unlink_quietly(stage)
        raise
    return stage, ident, fd


def _collision_error(target: Path) -> WriterError:
    return WriterError(
        f"canonical path {target} already holds a different record; "
        "never replace a published record -- re-publish under a new "
        "session identifier instead"
    )


def _attempt_publish(target: Path, record: bytes, verify: Verifier) -> str:
    stage, ident, fd = _stage(target, record, verify)
    try:
        try:
            # Atomic no-replace publication; a replacing rename would be
            # forbidden here.
            _link_verified(stage, fd, target)
        except FileExistsError:
            landed = read_publishable_target(target)
            if landed == record:
                return "idempotent"
            raise _collision_error(target)
        except FileNotFoundError:
            # A concurrent writer published and swept this stage; the caller
            # resolves against the landed record.
            raise _StageVanished()
    finally:
        # The descriptor has done its work, and the staging name is always
        # released: on success the canonical path is the surviving link, on
        # failure the namespace is left clean.
        os.close(fd)
        _unlink_quietly(stage)

    # The canonical name must resolve to the inode that was verified above --
    # not to some other file that appeared at that name in the meantime.
    try:
        landed_st = os.lstat(target)
    except OSError as exc:
        raise WriterError(f"cannot inspect published record {target}: {exc}") from exc
    if (landed_st.st_dev, landed_st.st_ino) != ident:
        # The staging name was turned into a link to some other file between
        # verification and the link call (possible only where the link source
        # is the name, not the descriptor). The canonical name is the one this
        # call created -- the link would have failed had it existed -- so
        # taking it back down restores the namespace; the foreign file was
        # visible under that name from the link until here, and a writer
        # stopped in between leaves it (the name-fallback contract, module
        # docstring). The stage is then a vanished one, and the caller restages.
        _unlink_quietly(target)
        if os.path.lexists(target):
            raise WriterError(
                f"published record {target} does not resolve to the staged inode: the staging entry was "
                "replaced before publication, and the foreign name the link created could not be removed"
            )
        raise _StageVanished()
    # Link count and read-back are proved in _finalize, which every successful
    # return -- published and idempotent alike -- passes through.
    return "published"


def _finalize(target: Path, record: bytes, verify: Verifier) -> None:
    """Sweep staging artifacts, then prove the landed record stands alone.

    Every successful return from :func:`publish` passes through here, published
    and idempotent alike. A previous run can have landed the record and then
    failed to release its staging name, which leaves a second, writable name
    for the canonical inode; finding the bytes already correct says nothing
    about that. If the alias cannot be removed, this raises: a retained
    publication failure is the honest outcome.
    """
    canonical = _identity(target)
    _sweep_own_stages(target, canonical)

    st = os.lstat(target)
    if (st.st_dev, st.st_ino) != canonical:
        raise WriterError(f"published record {target} was replaced during cleanup")
    if st.st_nlink != 1:
        raise WriterError(
            f"published record {target} still has {st.st_nlink} links; the staging "
            "name could not be released and the record is reachable -- and "
            "writable -- under another path"
        )

    landed = read_publishable_target(target)
    if landed is None:
        raise WriterError(f"published record {target} disappeared before read-back")
    try:
        verify(landed)
    except WriterError as exc:
        raise WriterError(f"read-back mismatch at {target}: {exc}") from exc
    if landed != record:
        raise WriterError(f"read-back mismatch at {target}: bytes differ from the record")


def publish(target: Path, record: bytes, *, verify: Verifier) -> str:
    """Publish ``record`` at ``target``. Returns 'published' or 'idempotent'.

    Failure-atomic: on any error before publication the canonical path is left
    absent and the staging file is removed. ``verify`` is run over the staged
    bytes and over the read-back, and raises :class:`WriterError` when the
    bytes are not a consistent record.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = read_publishable_target(target)
    if existing is not None:
        if existing != record:
            raise _collision_error(target)
        # Idempotent retry after a success: nothing to write, but the previous
        # run's invariants still have to hold before this one calls it success.
        _finalize(target, record, verify)
        return "idempotent"

    for attempt in range(PUBLISH_ATTEMPTS):
        try:
            status = _attempt_publish(target, record, verify)
        except _StageVanished:
            # The sweep that removed the stage ran after a record was published:
            # ours byte-for-byte when the writers were identical.
            landed = read_publishable_target(target)
            if landed is not None:
                if landed != record:
                    raise _collision_error(target) from None
                _finalize(target, record, verify)
                return "idempotent"
            if attempt == PUBLISH_ATTEMPTS - 1:
                raise WriterError(
                    f"staging file for {target} was removed before publication on {PUBLISH_ATTEMPTS} consecutive attempts"
                ) from None
            continue
        _finalize(target, record, verify)
        return status
    raise AssertionError("unreachable")  # pragma: no cover
