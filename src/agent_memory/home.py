# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Find the memory home.

Resolution order; the first hit wins:

1. an explicit path (the ``--home`` flag);
2. ``$AGENT_MEMORY_HOME``;
3. ``$OACP_HOME``, recognized so existing homes keep working, never required;
4. the nearest ``.agent-memory.json`` binding, walking up from the working
   directory: ``{"schema_version": 1, "home": "...", "project": "..."}``;
5. a workspace marker, walking up from the working directory: a symlink, or a
   file named ``workspace.json``, whose real path has the shape
   ``<home>/projects/<name>/workspace.json``; the marker names the project too;
6. ``~/agent-memory``.

Any directory entry with the binding's name is the binding, and one that
is not a readable, well-formed v1 binding -- a dangling symlink, a directory,
unreadable or malformed JSON, an unknown ``schema_version``, no home -- is an
error, never a fall-through: silently picking a different store is worse
than stopping. So is an ancestor directory the process cannot inspect: it
might hold a binding, and only a directory known to hold none keeps the
walk going. The resolver only reads paths; it never asks whether any other
tool is installed.

The project is resolved separately (:func:`find_project`): when a flag or
an environment variable chose the home, the nearest binding or marker still
names the project, provided it binds the repository to that same home. A
binding for a different home lends nothing; the mismatch is reported.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Optional, Tuple

from .layout import PROJECTS_DIR

ENV_HOME = "AGENT_MEMORY_HOME"
ENV_COMPAT_HOME = "OACP_HOME"
BINDING_FILE = ".agent-memory.json"
BINDING_SCHEMA_VERSION = 1
WORKSPACE_FILE = "workspace.json"
DEFAULT_HOME = "~/agent-memory"

SOURCE_FLAG = "flag"
SOURCE_DEFAULT = "default"


class HomeError(Exception):
    """The home could not be resolved safely."""


@dataclass(frozen=True)
class HomeResolution:
    """Where the home is and which rule chose it."""

    path: Path
    #: ``flag``, ``env:<NAME>``, ``binding:<file>``, ``marker:<file>`` or ``default``.
    source: str
    #: The project a binding or a marker named, when one of them chose the home.
    project: Optional[str] = None


def resolve_home(
    explicit: Optional[str] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[Path] = None,
) -> HomeResolution:
    """Apply the resolution order and return the first hit.

    ``env`` and ``cwd`` default to the process environment and working
    directory; tests pass their own to stay hermetic.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    if explicit is not None:
        return HomeResolution(_expand(explicit), SOURCE_FLAG)
    for name in (ENV_HOME, ENV_COMPAT_HOME):
        value = environ.get(name)
        if value:
            return HomeResolution(_expand(value), f"env:{name}")
    start = (Path.cwd() if cwd is None else Path(cwd)).expanduser().absolute()
    for finder in (find_binding, find_workspace_marker):
        found = finder(start)
        if found is not None:
            return found
    return HomeResolution(_expand(DEFAULT_HOME), SOURCE_DEFAULT)


@dataclass(frozen=True)
class ProjectResolution:
    """Which project the repository at hand belongs to, and how that was decided."""

    project: Optional[str]
    #: ``binding:<file>`` or ``marker:<file>``; ``None`` when no project was found.
    source: Optional[str]
    #: Why a binding or marker that was found did not name the project, when one was found.
    note: Optional[str] = None


def find_project(home: Path, start: Path) -> ProjectResolution:
    """The project the nearest binding or marker at or above ``start`` names for ``home``.

    The same walk :func:`resolve_home` makes, with the same fail-closed binding
    handling; the first binding or marker found decides. It names the project
    only when it binds the repository to ``home`` itself: a binding for another
    home says nothing about this one, and borrowing its project would list the
    wrong files.
    """
    start = Path(start).expanduser().absolute()
    for finder in (find_binding, find_workspace_marker):
        found = finder(start)
        if found is None:
            continue
        if not _same_path(found.path, home):
            return ProjectResolution(
                None, None, f"{found.source} binds this repository to {found.path}, not to {home}; no project taken from it"
            )
        if found.project is None:
            return ProjectResolution(None, None, f"{found.source} names no project")
        return ProjectResolution(found.project, found.source)
    return ProjectResolution(None, None)


def _same_path(first: Path, second: Path) -> bool:
    return os.path.realpath(Path(first).expanduser()) == os.path.realpath(Path(second).expanduser())


def find_binding(start: Path) -> Optional[HomeResolution]:
    """Load the nearest binding entry at or above ``start``; ``None`` when there is none.

    Any directory entry with the binding's name counts, a dangling symlink or
    a directory included: an entry that turns out unusable is an error from
    :func:`load_binding`, never a reason to keep walking up.
    """
    for directory in _ancestors(start):
        candidate = directory / BINDING_FILE
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            # An ancestor the process may not inspect could hold a binding; walking
            # past it would silently pick another store. Absence and inability are
            # different answers, and only the first keeps the walk going.
            raise HomeError(f"{candidate}: cannot inspect the binding slot: {exc.strerror or exc}") from exc
        return load_binding(candidate)
    return None


def load_binding(path: Path) -> HomeResolution:
    """Parse one binding file; anything but a well-formed v1 binding raises :class:`HomeError`."""
    if not path.is_file():
        raise HomeError(f"{path}: binding {_describe_non_file(path)}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HomeError(f"{path}: cannot read binding: {exc}") from exc
    if not isinstance(data, dict):
        raise HomeError(f"{path}: binding must be a JSON object")
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != BINDING_SCHEMA_VERSION:
        raise HomeError(
            f"{path}: unsupported binding schema_version {version!r} (this tool reads {BINDING_SCHEMA_VERSION})"
        )
    home = data.get("home")
    if not isinstance(home, str) or not home:
        raise HomeError(f"{path}: binding must name a non-empty 'home'")
    project = data.get("project")
    if project is not None and (not isinstance(project, str) or not project):
        raise HomeError(f"{path}: binding 'project' must be a non-empty string when present")
    home_path = _expand(home)
    if not home_path.is_absolute():
        home_path = path.parent / home_path
    return HomeResolution(home_path, f"binding:{path}", project=project)


def find_workspace_marker(start: Path) -> Optional[HomeResolution]:
    """Find the nearest workspace marker at or above ``start``; ``None`` when there is none.

    A marker is any symlink, or a file named ``workspace.json``, whose real
    path has the shape ``<home>/projects/<name>/workspace.json``. The shape
    is the guard: an editor's ``workspace.json`` in a repo root never sits
    two levels below a ``projects`` directory. The symlink's own name is not
    load-bearing, so a repo can call it whatever it likes.
    """
    for directory in _ancestors(start):
        for candidate in _marker_candidates(directory):
            found = _home_from_workspace_file(candidate)
            if found is not None:
                home, project = found
                return HomeResolution(home, f"marker:{candidate}", project=project)
    return None


def _marker_candidates(directory: Path) -> Iterator[Path]:
    plain = directory / WORKSPACE_FILE
    if plain.exists():
        yield plain
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name != WORKSPACE_FILE and entry.is_symlink():
            yield entry


def _home_from_workspace_file(path: Path) -> Optional[Tuple[Path, str]]:
    """``(home, project)`` when ``path`` resolves to ``<home>/projects/<project>/workspace.json``."""
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved.name != WORKSPACE_FILE or not resolved.is_file():
        return None
    projects = resolved.parent.parent
    if projects.name != PROJECTS_DIR:
        return None
    return projects.parent, resolved.parent.name


def _ancestors(start: Path) -> Iterator[Path]:
    yield start
    yield from start.parents


def _describe_non_file(path: Path) -> str:
    if path.is_dir():
        return "is a directory, not a file"
    if path.is_symlink():
        return "is a symlink whose target is missing"
    if not os.path.lexists(path):
        return "does not exist"
    return "is not a regular file"


def _expand(value: str) -> Path:
    try:
        return Path(value).expanduser()
    except RuntimeError as exc:  # ``~user`` for a user the system cannot look up
        raise HomeError(f"cannot expand {value!r}: {exc}") from exc
