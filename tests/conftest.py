# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: an isolated git environment, a home that mirrors the golden, git taken off
PATH, and a fetch that fails the way a sandbox-blocked credential helper makes it fail."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Tuple

import pytest

from agent_memory import layout
from agent_memory.git_runner import GitResult, run_git


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


@pytest.fixture
def no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Call the returned function to take git off PATH: from then on git does not run at all.
    Build the home first; the fixtures that build it need git."""
    empty = tmp_path / "no-git-bin"
    empty.mkdir()

    def shim() -> None:
        monkeypatch.setenv("PATH", str(empty))

    return shim


class FetchBlockedByCredentialHelper:
    """The real runner, except that the fetch fails the way a sandboxed `op` call does.

    The shape is a real acceptance transcript's -- the helper's own
    TLS failure, then git's fallback prompt failing for want of a terminal -- with
    the vault and item names replaced by placeholders. The predicate keys off
    "1password document" and "osstatus", so no assertion here depends on what the
    blocked secret was called, and this file ships to a public repository.

    Faking the fetch is what makes the failure reproducible. Live, it needs the
    sandbox AND a cold credential cache: the helper runs only when git has no
    cached credential for the remote, so after any unsandboxed fetch or push a
    sandboxed run reads green.
    """

    OUTPUT = (
        "Error: could not read 1Password document 'example-signing-key' from vault "
        "'example': [ERROR] failed to request.DoUnencrypted: Post \"/api/v3/auth/start\": "
        "tls: failed to verify certificate: x509: OSStatus -26276\n"
        "fatal: could not read Username for 'https://github.com': Device not configured"
    )

    def __call__(self, args: Sequence[str], *, cwd: Path, timeout: Optional[float] = None) -> GitResult:
        if args and args[0] == "fetch":
            return GitResult(128, "", self.OUTPUT)
        return run_git(args, cwd=cwd, timeout=timeout)


#: git's prompt failure with no helper in play, as each platform spells the errno.
#: macOS's "Device not configured" is git's own terminal fallback failing, not a
#: helper tell -- it rides along with every prompt failure on the platform.
PROMPT_ONLY_FAILURES = {
    "macos": "fatal: could not read Username for 'http://127.0.0.1:62548': Device not configured",
    "linux": "fatal: could not read Username for 'https://github.com': No such device or address",
}


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
