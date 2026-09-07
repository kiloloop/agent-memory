# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Run git for the sync engine.

Every git invocation goes through one :class:`GitRunner`, so tests can record
calls or script answers without a repository, and the network verbs can carry
a timeout the default runner enforces. The runner never interprets git's
output; that is the engine's job.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Sequence

#: Exit codes the default runner synthesizes when git itself did not run.
EXIT_TIMEOUT = 124
EXIT_NOT_FOUND = 127


@dataclass(frozen=True)
class GitResult:
    """What one git invocation returned."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def timed_out(self) -> bool:
        return self.returncode == EXIT_TIMEOUT

    @property
    def output(self) -> str:
        """stdout and stderr together, stripped: the text for a message."""
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()


class GitRunner(Protocol):
    """Run ``git`` with ``args`` in ``cwd``; ``timeout`` is seconds or ``None``."""

    def __call__(self, args: Sequence[str], *, cwd: Path, timeout: Optional[float] = None) -> GitResult: ...


def run_git(args: Sequence[str], *, cwd: Path, timeout: Optional[float] = None) -> GitResult:
    """The default runner: a subprocess, output captured, timeouts enforced."""
    command = ["git", *args]
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return GitResult(EXIT_NOT_FOUND, "", "git: command not found")
    except subprocess.TimeoutExpired:
        return GitResult(EXIT_TIMEOUT, "", f"git {' '.join(args)}: timed out after {timeout:g}s")
    return GitResult(completed.returncode, completed.stdout, completed.stderr)
