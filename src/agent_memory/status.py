# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory status``: which home resolves, and where its sync stands.

The readout is the resolution (path, the rule that chose it, the bound
project), the layout (marker, allowlist, tiers), and, when the home is a sync
repository, its :class:`~agent_memory.sync.GitState`. The remote is contacted
only with ``--fetch``; otherwise ahead/behind count against the last fetched
upstream. Exit 0 when the tree is clean and not diverged, 1 when it is dirty
or diverged (or the home does not exist). Ahead and behind are reported, not
failed: they are what ``push`` and ``pull`` are for.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import layout, sync
from .doctor import enclosing_repository, sync_state_text
from .git_runner import GitRunner
from .home import HomeResolution
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
    #: ``None`` without the marker; otherwise whether the home is a git repository.
    repository: Optional[bool] = None
    #: Set when the home is a repository only by sitting inside another one.
    enclosing: Optional[Path] = None
    git: Optional[GitState] = None
    #: Whether the remote was contacted for this readout.
    fetched: bool = False

    @property
    def clean(self) -> bool:
        """The home exists and its tree is neither dirty nor diverged."""
        if not self.exists:
            return False
        return self.git is None or not (self.git.dirty or self.git.diverged)

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.clean else EXIT_FAILED

    def lines(self) -> List[str]:
        lines = [f"home: {self.home}", f"source: {self.source}"]
        if self.project:
            lines.append(f"project: {self.project}")
        if not self.exists:
            lines.append("exists: no")
            return lines
        lines.extend(
            [
                "exists: yes",
                f"marker: {'present' if self.marker else 'absent'}",
                f"gitignore: {self.gitignore}",
                f"org-memory: {'present' if self.org_memory else 'absent'}",
                f"projects: {self.projects} with a memory dir",
            ]
        )
        if self.repository is None:
            lines.append("sync: not configured")
        elif not self.repository:
            lines.append("sync: marker present, but the home is not a git repository")
        elif self.enclosing is not None:
            lines.append(f"sync: marker present, but the home is inside the repository at {self.enclosing}, not one of its own")
        elif self.git is not None:
            lines.append(f"sync: {sync_state_text(self.git)}")
            if self.git.has_remote:
                lines.append("fetch: done" if self.fetched else "fetch: skipped (pass --fetch to contact the remote)")
            lines.append(f"tree: {'dirty' if self.git.dirty else 'clean'}")
        return lines


def inspect(resolution: HomeResolution, *, fetch: bool = False, runner: Optional[GitRunner] = None) -> Status:
    """Read the home ``resolution`` names; nothing is changed, and no network without ``fetch``."""
    home = resolution.path
    if not home.is_dir():
        return Status(home, resolution.source, resolution.project, exists=False)
    tiers = layout.allowed_memory_dirs(home)
    org = layout.org_memory_dir(home)
    marker = sync.is_configured(home)
    repository: Optional[bool] = None
    enclosing: Optional[Path] = None
    git: Optional[GitState] = None
    if marker:
        repository = sync.is_git_repo(home, runner)
        if repository:
            enclosing = enclosing_repository(home, runner)
        if repository and enclosing is None:
            git = sync.git_state(home, runner=runner, fetch=fetch)
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
        fetched=fetch and git is not None and git.has_remote,
    )


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
