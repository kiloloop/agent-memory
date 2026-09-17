# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""What ``status`` and ``doctor`` need before their readout means anything.

Three prerequisite failures, each with a stable reason code. The JSON carries
the code and the message, the table prints the same message, and either verb
exits non-zero on any of them:

* ``home_unresolved``: no memory home. The resolver failed, or the path it
  chose is not a directory.
* ``git_missing``: git does not run, on a home that carries the sync marker. A
  home without the marker needs no git, so there its absence is reported, not
  failed. Without git a marked home would read as "not a git repository",
  which is a layout finding it is not.
* ``credential_helper_blocked``: a fetch failed with the signature
  :func:`agent_memory.sync.credential_helper_blocked` detects, a git
  credential helper that a sandbox stopped. The helper runs only when git has
  no cached credential for the remote, so the failure needs a cold cache as
  well as the sandbox.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from . import sync
from .git_runner import EXIT_NOT_FOUND, GitRunner, run_git
from .sync import GitState

HOME_UNRESOLVED = "home_unresolved"
GIT_MISSING = "git_missing"
CREDENTIAL_HELPER_BLOCKED = "credential_helper_blocked"
#: Every reason code, in the order the checks run.
REASONS = (HOME_UNRESOLVED, GIT_MISSING, CREDENTIAL_HELPER_BLOCKED)

GIT_MISSING_TEXT = "git could not be run from PATH"
GIT_MISSING_REMEDY = "Install git, or put it on PATH"
HELPER_BLOCKED_TEXT = (
    "the git credential helper could not run under the sandbox, so git had no credentials for the remote"
)
HELPER_BLOCKED_REMEDY = "Rerun with the sandbox off"
HOME_MISSING_REMEDY = "Pass --home or set $AGENT_MEMORY_HOME to an existing home, or create one with `agent-memory init`"
HOME_ERROR_REMEDY = "Fix the binding or path it names, or pass --home"


@dataclass(frozen=True)
class Failure:
    """One failed prerequisite: its reason code, what happened, and what to do."""

    reason: str
    message: str
    remedy: str

    def line(self) -> str:
        return f"{self.reason} — {self.message}. {self.remedy}."


@dataclass(frozen=True)
class GitProbe:
    """Whether git runs, and the version it reports (empty when it reported none)."""

    present: bool
    version: str = ""


@dataclass(frozen=True)
class Prerequisites:
    """The prerequisite readout both verbs print."""

    #: ``None`` when git was not probed, because there was no home to probe it in.
    git: Optional[GitProbe] = None
    #: Whether a fetch ran, the only time the credential helper can be judged.
    helper_checked: bool = False
    helper_blocked: bool = False
    failure: Optional[Failure] = None

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def reason(self) -> Optional[str]:
        return self.failure.reason if self.failure else None

    def to_json(self) -> Dict[str, Any]:
        failure = self.failure
        return {
            "ok": self.ok,
            "reason": failure.reason if failure else None,
            "message": failure.message if failure else None,
            "remedy": failure.remedy if failure else None,
            "git": {
                "present": self.git.present if self.git else None,
                "version": (self.git.version or None) if self.git else None,
            },
            "credential_helper": {
                "checked": self.helper_checked,
                "blocked": self.helper_blocked if self.helper_checked else None,
            },
        }


#: Where the probe runs git. ``git --version`` reads nothing from its working directory, and the
#: filesystem root is always there, so a missing or unreadable home cannot pass for a missing git.
_PROBE_CWD = Path(os.path.abspath(os.sep))


def probe_git(runner: Optional[GitRunner] = None) -> GitProbe:
    """Run ``git --version``. A git absent from PATH, or one the system will not execute, does not run."""
    try:
        result = (runner or run_git)(["--version"], cwd=_PROBE_CWD)
    except OSError:
        # The runner reports an absent git itself; this is a git it found and could not execute.
        return GitProbe(False)
    if result.returncode == EXIT_NOT_FOUND:
        return GitProbe(False)
    words = result.stdout.split()
    version = words[2] if result.ok and words[:2] == ["git", "version"] and len(words) > 2 else ""
    return GitProbe(True, version)


def helper_blocked(state: Optional[GitState]) -> bool:
    """Did this readout's fetch fail because a sandbox blocked the credential helper?"""
    return state is not None and state.fetch_failed and sync.credential_helper_blocked(state.fetch_output)


def home_missing(home: Path) -> Prerequisites:
    return Prerequisites(failure=Failure(HOME_UNRESOLVED, f"{home} is not a directory", HOME_MISSING_REMEDY))


def home_error(error: Exception) -> Prerequisites:
    return Prerequisites(
        failure=Failure(HOME_UNRESOLVED, f"the memory home could not be resolved: {error}", HOME_ERROR_REMEDY)
    )


def assess(
    git: GitProbe, *, configured: bool, state: Optional[GitState] = None, fetched: bool = False
) -> Prerequisites:
    """The readout for a home that exists. Git is required once the home carries the sync
    marker; the credential helper is judged only when ``fetched``."""
    blocked = fetched and helper_blocked(state)
    failure: Optional[Failure] = None
    if configured and not git.present:
        failure = Failure(GIT_MISSING, GIT_MISSING_TEXT, GIT_MISSING_REMEDY)
    elif blocked:
        failure = Failure(CREDENTIAL_HELPER_BLOCKED, HELPER_BLOCKED_TEXT, HELPER_BLOCKED_REMEDY)
    return Prerequisites(git, fetched, blocked, failure)
