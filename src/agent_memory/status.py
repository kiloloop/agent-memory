# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory status``: which home resolves, and where its sync stands.

The readout is the resolution (path, the rule that chose it, the bound
project), the layout (marker, allowlist, tiers), and, when the home is a sync
repository, its :class:`~agent_memory.sync.GitState`. The remote is contacted
only with ``--fetch``; otherwise ahead/behind count against the last fetched
upstream. Exit 0 when the tree is clean and not diverged, 1 when it is dirty
or diverged, or a prerequisite failed (:mod:`agent_memory.prerequisites`).
Ahead and behind are reported, not failed: they are what ``push`` and ``pull``
are for. ``--json`` prints the same readout as data, with ``ok`` true exactly
when the exit is 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import layout, prerequisites, sync
from .doctor import enclosing_repository, sync_state, sync_state_text
from .git_runner import GitRunner
from .home import HomeResolution
from .prerequisites import Prerequisites
from .sync import GitState

EXIT_OK = 0
EXIT_FAILED = 1


@dataclass(frozen=True)
class Status:
    """Everything the verb prints, plus the exit contract."""

    home: Path
    source: str
    project: Optional[str]
    exists: bool
    marker: bool = False
    gitignore: str = ""
    org_memory: bool = False
    projects: int = 0
    #: ``None`` without the marker or without git; otherwise whether the home is a git repository.
    repository: Optional[bool] = None
    #: Set when the home is a repository only by sitting inside another one.
    enclosing: Optional[Path] = None
    git: Optional[GitState] = None
    #: Whether the remote was contacted for this readout.
    fetched: bool = False
    prerequisites: Prerequisites = Prerequisites()

    @property
    def clean(self) -> bool:
        """The home exists and its tree is neither dirty nor diverged."""
        if not self.exists:
            return False
        return self.git is None or not (self.git.dirty or self.git.diverged)

    @property
    def ok(self) -> bool:
        return self.prerequisites.ok and self.clean

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.ok else EXIT_FAILED

    def lines(self) -> List[str]:
        lines = [f"home: {self.home}", f"source: {self.source}"]
        if self.project:
            lines.append(f"project: {self.project}")
        if not self.exists:
            lines.append("exists: no")
            return self._with_failure(lines)
        lines.extend(
            [
                "exists: yes",
                f"marker: {'present' if self.marker else 'absent'}",
                f"gitignore: {self.gitignore}",
                f"org-memory: {'present' if self.org_memory else 'absent'}",
                f"projects: {self.projects} with a memory dir",
            ]
        )
        if not self.marker:
            lines.append("sync: not configured")
        elif self.prerequisites.reason == prerequisites.GIT_MISSING:
            lines.append(f"sync: not read ({prerequisites.GIT_MISSING})")
        elif not self.repository:
            lines.append("sync: marker present, but the home is not a git repository")
        elif self.enclosing is not None:
            lines.append(f"sync: marker present, but the home is inside the repository at {self.enclosing}, not one of its own")
        elif self.git is not None:
            lines.append(f"sync: {sync_state_text(self.git)}")
            if self.git.has_remote:
                lines.append("fetch: done" if self.fetched else "fetch: skipped (pass --fetch to contact the remote)")
            lines.append(f"tree: {'dirty' if self.git.dirty else 'clean'}")
        return self._with_failure(lines)

    def _with_failure(self, lines: List[str]) -> List[str]:
        failure = self.prerequisites.failure
        return lines + [f"prerequisite failed: {failure.line()}"] if failure else lines

    def to_json(self) -> Dict[str, Any]:
        """The readout as data: the fields the lines show, and ``ok`` for the exit code."""
        git = self.git
        return {
            "ok": self.ok,
            "home": str(self.home),
            "source": self.source,
            "project": self.project,
            "exists": self.exists,
            "layout": {
                "marker": self.marker,
                "gitignore": self.gitignore,
                "org_memory": self.org_memory,
                "projects": self.projects,
            }
            if self.exists
            else None,
            "sync": {
                "configured": self.marker,
                "repository": self.repository,
                "enclosing": str(self.enclosing) if self.enclosing is not None else None,
                "state": sync_state(git) if git else None,
                "state_text": sync_state_text(git) if git else None,
                "remote": git.has_remote if git else None,
                "upstream": (git.upstream or None) if git else None,
                "fetched": self.fetched,
                "ahead": git.ahead if git else None,
                "behind": git.behind if git else None,
                "dirty": git.dirty if git else None,
                "diverged": git.diverged if git else None,
            }
            if self.exists
            else None,
            "prerequisites": self.prerequisites.to_json(),
        }


def inspect(resolution: HomeResolution, *, fetch: bool = False, runner: Optional[GitRunner] = None) -> Status:
    """Read the home ``resolution`` names; nothing is changed, and no network without ``fetch``."""
    home = resolution.path
    if not home.is_dir():
        return Status(
            home, resolution.source, resolution.project, exists=False, prerequisites=prerequisites.home_missing(home)
        )
    tiers = layout.allowed_memory_dirs(home)
    org = layout.org_memory_dir(home)
    marker = sync.is_configured(home)
    probe = prerequisites.probe_git(runner)
    repository: Optional[bool] = None
    enclosing: Optional[Path] = None
    git: Optional[GitState] = None
    if marker and probe.present:
        repository = sync.is_git_repo(home, runner)
        if repository:
            enclosing = enclosing_repository(home, runner)
        if repository and enclosing is None:
            git = sync.git_state(home, runner=runner, fetch=fetch)
    fetched = fetch and git is not None and git.has_remote
    return Status(
        home,
        resolution.source,
        resolution.project,
        exists=True,
        marker=marker,
        gitignore=gitignore_state(home),
        org_memory=org in tiers,
        projects=len([path for path in tiers if path != org]),
        repository=repository,
        enclosing=enclosing,
        git=git,
        fetched=fetched,
        prerequisites=prerequisites.assess(probe, configured=marker, state=git, fetched=fetched),
    )


def unresolved(error: Exception) -> Dict[str, Any]:
    """The JSON readout when the resolver itself failed, so there is no home to read."""
    return {
        "ok": False,
        "home": None,
        "source": None,
        "project": None,
        "exists": False,
        "layout": None,
        "sync": None,
        "prerequisites": prerequisites.home_error(error).to_json(),
    }


def gitignore_state(home: Path) -> str:
    """How the root ``.gitignore`` relates to the canonical allowlist, in a few words."""
    path = home / layout.GITIGNORE_FILE
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"unreadable ({exc.strerror})"
    if data == layout.gitignore_text().encode("utf-8"):
        return "canonical"
    if sync.gitignore_has_managed_block(data.decode("utf-8", errors="replace")):
        return "canonical managed block, other lines kept"
    return "present, differs from canonical"
