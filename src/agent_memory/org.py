# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Scaffold a memory home from the bundled templates: ``agent-memory init`` and ``org init``.

``init`` lays the home out (its directories and the canonical ``.gitignore``,
through :func:`layout.scaffold_home`), writes the org tier's files from the
templates shipped inside this package, keeps ``events/`` and ``debriefs/``
alive with a ``.gitkeep``, and, given a project, writes that project's tier
the same way. Given a repository, it records a binding there last, so a
later session started inside that repository finds this home and project.
``org init`` is the org tier alone, for a home that already exists.

Three rules, in the order they run:

1. Every template is read through the public ``importlib.resources`` API
   before anything is written. A template missing from the installed package
   is an error and the home is untouched; there is no silent fallback.
2. Nothing that exists is overwritten, so a rerun changes no byte and reports
   what it kept. A binding that would collide with one already recorded is
   refused before the first write.
3. No git, no network, no credentials: the verbs touch the filesystem only.
   Making the home a sync repository is ``enable``, a separate step.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import layout
from .home import BINDING_FILE, BINDING_SCHEMA_VERSION, HomeError, load_binding

TEMPLATES_DIR = "templates"
#: Template directory per tier; the file names are the tier's own file list.
TEMPLATE_DIRS = {layout.ORG.name: "org-memory", layout.PROJECT.name: "project-memory"}
#: Keeps an otherwise empty org-tier directory present in a git-synced home.
KEEP_FILE = ".gitkeep"


class ScaffoldError(Exception):
    """A precondition failed, or a template is missing; nothing was changed."""


@dataclass(frozen=True)
class Report:
    """What the verb created, what it found already in place, and the binding it recorded."""

    home: Path
    created: Tuple[str, ...]
    kept: Tuple[str, ...]
    project: Optional[str] = None
    binding: Optional[Path] = None
    #: ``created``, ``unchanged`` or ``""`` when no repository was given.
    binding_action: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.created) or self.binding_action == "created"

    def lines(self) -> List[str]:
        head = "Initialized memory home" if self.created else "Memory home already complete"
        lines = [f"{head}: {self.home}"]
        lines.extend(f"  + {path}" for path in self.created)
        lines.extend(f"  (exists) {path}" for path in self.kept)
        if self.project:
            lines.append(f"project: {self.project}")
        if self.binding is not None:
            verb = "recorded" if self.binding_action == "created" else "already recorded"
            lines.append(f"binding {verb}: {self.binding} -> {self.home}")
            if self.binding_action == "created":
                lines.append(f"  keep {BINDING_FILE} out of the repository's history; it holds a machine-local path")
        return lines


# --- the verbs --------------------------------------------------------------


def init(home: Path, *, project: Optional[str] = None, repo: Optional[Path] = None) -> Report:
    """Create or complete ``home``; with ``project`` its project tier too; with ``repo`` a binding in that repository."""
    home = _absolute(home)
    templates = load_templates()
    if repo is not None:
        repo = _absolute(repo)
        if project is None:
            project = repo.name
            try:
                layout.validate_project_name(project)
            except ValueError as exc:
                raise ScaffoldError(f"cannot derive a project name from {repo}: {exc}; pass --project") from exc
    if project is not None:
        try:
            layout.validate_project_name(project)
        except ValueError as exc:
            raise ScaffoldError(f"project {project!r}: {exc}") from exc
    binding: Optional[Tuple[Path, str]] = None
    if repo is not None:
        binding = _plan_binding(repo, home, project)
    created, kept = _scaffold(home, project, templates)
    action = ""
    if binding is not None:
        path, action = binding
        if action == "created":
            _write_binding(path, home, project)
    return Report(
        home,
        tuple(created),
        tuple(kept),
        project=project,
        binding=binding[0] if binding is not None else None,
        binding_action=action,
    )


def org_init(home: Path) -> Report:
    """The org tier alone, for a home that already exists."""
    home = _absolute(home)
    if not home.is_dir():
        raise ScaffoldError(f"{home} is not a directory; `agent-memory init` creates a home")
    return init(home)


# --- templates --------------------------------------------------------------


def _templates_root():
    """The bundled ``templates/`` directory as a ``Traversable``; tests point this elsewhere."""
    return resources.files(__package__) / TEMPLATES_DIR


def template_bytes(tier: layout.Tier, name: str) -> bytes:
    """The bytes of one bundled template, or :class:`ScaffoldError` when the package does not carry it."""
    relative = f"{TEMPLATES_DIR}/{TEMPLATE_DIRS[tier.name]}/{name}"
    try:
        with resources.as_file(_templates_root() / TEMPLATE_DIRS[tier.name] / name) as path:
            return path.read_bytes()
    except FileNotFoundError as exc:
        raise ScaffoldError(f"template {relative} is missing from the installed package") from exc
    except OSError as exc:
        raise ScaffoldError(f"template {relative} cannot be read: {exc.strerror or exc}") from exc


def load_templates() -> Dict[Tuple[str, str], bytes]:
    """Every tier file's template, read up front so a missing one fails before the first write."""
    return {(tier.name, name): template_bytes(tier, name) for tier in layout.TIERS for name in tier.files}


# --- the writes -------------------------------------------------------------


def _scaffold(home: Path, project: Optional[str], templates: Dict[Tuple[str, str], bytes]) -> Tuple[List[str], List[str]]:
    created: List[str] = []
    kept: List[str] = []

    def rel(path: Path) -> str:
        text = path.relative_to(home).as_posix()
        return f"{text}/" if path.is_dir() else text

    try:
        created.extend(rel(path) for path in layout.scaffold_home(home, project) if path != home)
    except OSError as exc:
        raise ScaffoldError(f"cannot lay out {home}: {exc.strerror or exc}: {exc.filename}") from exc

    def place(path: Path, data: bytes) -> None:
        try:
            fresh = layout.write_if_absent(path, data)
        except OSError as exc:
            raise ScaffoldError(f"cannot write {path}: {exc.strerror or exc}") from exc
        (created if fresh else kept).append(rel(path))

    org = layout.org_memory_dir(home)
    for name in layout.ORG.files:
        place(org / name, templates[(layout.ORG.name, name)])
    for sub in layout.ORG.dirs:
        place(org / sub / KEEP_FILE, b"")
    if project is not None:
        memory = layout.project_memory_dir(home, project)
        for name in layout.PROJECT.files:
            place(memory / name, templates[(layout.PROJECT.name, name)])
    return created, kept


def _plan_binding(repo: Path, home: Path, project: str) -> Tuple[Path, str]:
    """Where the binding goes and whether it needs writing; a collision is refused here, before any write."""
    if not repo.is_dir():
        raise ScaffoldError(f"{repo} is not a directory")
    path = repo / BINDING_FILE
    if not os.path.lexists(path):
        return path, "created"
    try:
        existing = load_binding(path)
    except HomeError as exc:
        raise ScaffoldError(f"collision: {exc}; not overwriting") from exc
    if existing.path.resolve() == home.resolve() and existing.project == project:
        return path, "unchanged"
    raise ScaffoldError(
        f"collision: {path} already binds this repository to home {existing.path} "
        f"(project {existing.project!r}); not overwriting"
    )


def _write_binding(path: Path, home: Path, project: str) -> None:
    data = {"schema_version": BINDING_SCHEMA_VERSION, "project": project, "home": str(home)}
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        raise ScaffoldError(f"cannot write {path}: {exc.strerror or exc}") from exc


def _absolute(value: Path) -> Path:
    try:
        return Path(value).expanduser().absolute()
    except RuntimeError as exc:
        raise ScaffoldError(f"cannot expand {value}: {exc}") from exc
