# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""The memory-home layout, declared once.

Every other encoding of the layout is derived from the ``TIERS`` table
below: the sync allowlist written to a home's ``.gitignore``, the directories
a sync may stage, the per-path allow check, and the files and subdirectories
a fresh tier starts with. Change the table and every derivation follows;
nothing else in the package spells these names.

A home has two tiers::

    <home>/
      .gitignore                 the sync allowlist (gitignore_text)
      .oacp-memory-repo          present when the home syncs through git
      org-memory/                cross-project memory
      projects/<name>/memory/    per-project memory
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

#: Marks a home whose memory tiers sync through git. The name is a compatibility
#: contract with every existing home; keep it verbatim.
MARKER_FILE = ".oacp-memory-repo"
GITIGNORE_FILE = ".gitignore"
PROJECTS_DIR = "projects"
#: Local setup receipts, one per runtime and repository. Not a tier: the allowlist
#: never selects it, so it stays on the machine that wrote it.
SETUP_DIR = "setup"
WILDCARD = "*"

#: Top-level directories that must never sync, listed after every allow rule so
#: the deny wins even if the allowlist is widened later.
NEVER_SYNCED_DIRS: Tuple[str, ...] = ("keys",)
_NEVER_SYNCED_COMMENT = "# never sync private key material — explicit deny, wins over any future allowlist widening"

#: Lines earlier versions of the managed block carried and the current one does
#: not. :func:`agent_memory.sync.ensure_gitignore` retires them when it brings a
#: home's block up to date. Left in place they would follow the block as custom
#: rules and, git reading an ignore file last-match-wins, keep their old effect.
#: A block edit that changes or drops a line appends the old line here; never
#: remove an entry, a home somewhere may still carry it.
SUPERSEDED_GITIGNORE_LINES: Tuple[str, ...] = (
    # Unanchored through 0.1.0: re-admitted a .gitignore or a marker at any depth,
    # so an excluded tree's own ignore file read as an untracked change and blocked pull.
    f"!{GITIGNORE_FILE}",
    f"!{MARKER_FILE}",
)


@dataclass(frozen=True)
class Tier:
    """One memory tier: where it lives under the home and what a fresh one holds."""

    name: str
    #: Path pattern relative to the home; ``*`` stands for one project name.
    pattern: str
    #: Files a fresh tier directory starts with; their content is the scaffolding verbs' business.
    files: Tuple[str, ...]
    #: Subdirectories a fresh tier directory starts with.
    dirs: Tuple[str, ...]
    #: Subdirectories inside the tier that never sync.
    unsynced: Tuple[str, ...] = ()

    @property
    def parts(self) -> Tuple[str, ...]:
        return tuple(self.pattern.split("/"))


ORG = Tier(
    name="org",
    pattern="org-memory",
    files=("recent.md", "decisions.md", "rules.md"),
    dirs=("events", "debriefs"),
)
PROJECT = Tier(
    name="project",
    pattern=f"{PROJECTS_DIR}/{WILDCARD}/memory",
    files=("project_facts.md", "decision_log.md", "open_threads.md", "known_debt.md"),
    dirs=("archive",),
    unsynced=(".cache",),
)

#: The whole layout. The order is the order of the allowlist lines.
TIERS: Tuple[Tier, ...] = (ORG, PROJECT)


def gitignore_text() -> str:
    """The canonical sync allowlist for a home's ``.gitignore``, byte for byte."""
    # The ignore file and the marker are root-only: anchored, so neither entry
    # re-admits a same-named file deeper in the tree. One that sits inside a tier
    # is still ordinary tier content; anywhere else it stays ignored.
    lines = [WILDCARD, f"!{WILDCARD}/", f"!/{GITIGNORE_FILE}", f"!/{MARKER_FILE}"]
    lines.extend(f"!{tier.pattern}/**" for tier in TIERS)
    lines.extend(f"{tier.pattern}/{sub}/" for tier in TIERS for sub in tier.unsynced)
    lines.append(_NEVER_SYNCED_COMMENT)
    lines.extend(f"{name}/" for name in NEVER_SYNCED_DIRS)
    return "\n".join(lines) + "\n"


def org_memory_dir(home: Path) -> Path:
    return home.joinpath(*ORG.parts)


def project_memory_dir(home: Path, project: str) -> Path:
    validate_project_name(project)
    return home.joinpath(*(project if part == WILDCARD else part for part in PROJECT.parts))


def validate_project_name(project: str) -> None:
    if not project or project.startswith(".") or "/" in project or "\\" in project:
        raise ValueError("project name must be non-empty, contain no path separators and not start with '.'")


def allowed_memory_dirs(home: Path) -> List[Path]:
    """Existing tier directories under ``home``, in allowlist order; projects sorted by name."""
    return [path for tier in TIERS for path in _expand(home, tier.parts) if path.exists()]


def _expand(base: Path, parts: Sequence[str]) -> Iterator[Path]:
    if not parts:
        yield base
        return
    head, rest = parts[0], parts[1:]
    if head != WILDCARD:
        yield from _expand(base / head, rest)
        return
    try:
        children = sorted(base.iterdir())
    except OSError:
        return
    for child in children:
        yield from _expand(child, rest)


def is_allowed_memory_path(path: str) -> bool:
    """Whether a home-relative POSIX path is inside the sync allowlist.

    A component named in ``NEVER_SYNCED_DIRS`` denies the path at any depth,
    whatever the home's ``.gitignore`` says: the predicate, not the ignore
    file, is what the sync engine trusts.
    """
    if path in (GITIGNORE_FILE, MARKER_FILE):
        return True
    parts = path.split("/")
    if any(part in NEVER_SYNCED_DIRS for part in parts):
        return False
    for tier in TIERS:
        pattern = tier.parts
        if len(parts) <= len(pattern):
            continue
        if all(want == WILDCARD or want == have for want, have in zip(pattern, parts)):
            return parts[len(pattern)] not in tier.unsynced
    return False


def scaffold_home(home: Path, project: Optional[str] = None) -> List[Path]:
    """Lay out ``home``: its directories, the canonical ``.gitignore`` and the org
    tier, plus one project tier when ``project`` is given.

    Only missing paths are created and nothing that exists is touched, so a
    rerun on a complete home returns an empty list. The ``.gitignore`` slot
    has a tier file's guarantee: whatever occupies it, a dangling link
    included, is kept and never followed (:func:`write_if_absent`). Tier
    *files* are not written here; their content ships with the scaffolding
    verbs.

    Returns the paths created, in creation order.
    """
    created: List[Path] = []

    def mkdir(path: Path) -> None:
        if not path.is_dir():
            path.mkdir(parents=True)
            created.append(path)

    mkdir(home)
    gitignore = home / GITIGNORE_FILE
    if write_if_absent(gitignore, gitignore_text().encode("utf-8")):
        created.append(gitignore)
    mkdir(home / PROJECTS_DIR)
    _scaffold_tier(ORG, org_memory_dir(home), mkdir)
    if project is not None:
        _scaffold_tier(PROJECT, project_memory_dir(home, project), mkdir)
    return created


def write_if_absent(path: Path, data: bytes) -> bool:
    """Create ``path`` holding ``data`` when no directory entry is there; True when it was created.

    Whatever occupies the slot is kept and never followed: a regular file, a
    directory, or a symlink, dangling included. The existence probe is only
    advisory; the open is exclusive, so an entry that appears between the two
    is kept as well. Any other failure propagates as ``OSError``.
    """
    if os.path.lexists(path):
        return False
    try:
        with open(path, "xb") as handle:
            handle.write(data)
    except FileExistsError:
        return False
    return True


def _scaffold_tier(tier: Tier, root: Path, mkdir: Callable[[Path], None]) -> None:
    mkdir(root)
    for sub in tier.dirs:
        mkdir(root / sub)
