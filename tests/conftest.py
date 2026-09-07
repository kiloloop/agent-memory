# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: an isolated git environment, and a home that mirrors the golden."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Tuple

import pytest

from agent_memory import layout


@pytest.fixture
def git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Git with no user or system config, a fixed default branch, and a fixed identity."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "init.defaultBranch")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "main")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Memory Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "memory-test@example.invalid")


def git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    assert completed.returncode == 0, f"git {' '.join(args)} failed ({completed.returncode}): {completed.stdout}\n{completed.stderr}"
    return completed.stdout.strip()


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def synced_home(tmp_path: Path, name: str = "home") -> Tuple[Path, Path]:
    """A memory home in the golden's state: marker, canonical allowlist, one canonical debrief,
    one commit, a bare remote as upstream, clean and synced. Returns (home, remote)."""
    home = tmp_path / name
    remote = tmp_path / f"{name}-remote.git"
    home.mkdir()
    git("init", "--quiet", cwd=home)
    git("init", "--bare", "--quiet", str(remote), cwd=tmp_path)
    write(home / layout.MARKER_FILE, "memory sync enabled\n")
    write(home / layout.GITIGNORE_FILE, layout.gitignore_text())
    write(
        home / layout.ORG.pattern / "debriefs" / "demo-project" / "2026" / "09" / "20260905-alice-1f3a9c2b.md",
        "---\nschema_version: 1\n---\nbody\n",
    )
    git("add", "-f", layout.MARKER_FILE, layout.GITIGNORE_FILE, layout.ORG.pattern, cwd=home)
    git("commit", "--quiet", "-m", "seed memory", cwd=home)
    git("remote", "add", "origin", str(remote), cwd=home)
    git("push", "--quiet", "-u", "origin", "main", cwd=home)
    return home, remote
