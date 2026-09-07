# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory status``: the readout and the exit contract (0 clean, 1 dirty or diverged)."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from agent_memory import layout, status, sync
from agent_memory.cli import main
from agent_memory.git_runner import GitResult, run_git
from agent_memory.home import HomeResolution, resolve_home

from conftest import git, synced_home, write


@pytest.fixture
def out(capsys: pytest.CaptureFixture[str]):
    def read() -> List[str]:
        return capsys.readouterr().out.splitlines()

    return read


class RecordingRunner:
    """The real runner, recording every call and its timeout."""

    def __init__(self) -> None:
        self.calls: List[Tuple[Tuple[str, ...], Optional[float]]] = []

    def __call__(self, args: Sequence[str], *, cwd: Path, timeout: Optional[float] = None) -> GitResult:
        self.calls.append((tuple(args), timeout))
        return run_git(args, cwd=cwd, timeout=timeout)


def _resolution(home: Path) -> HomeResolution:
    return resolve_home(str(home))


# --- the readout ------------------------------------------------------------


def test_a_home_without_the_marker_reports_sync_not_configured(tmp_path: Path, out) -> None:
    home = tmp_path / "home"
    layout.scaffold_home(home)
    assert main(["status", "--home", str(home)]) == 0
    lines = out()
    assert lines[-1] == "sync: not configured"
    assert not any(line.startswith(("tree:", "fetch:")) for line in lines)


def test_a_marker_without_a_repository_is_reported_and_does_not_fail(tmp_path: Path, out) -> None:
    home = tmp_path / "home"
    layout.scaffold_home(home)
    sync.write_marker(home)
    assert main(["status", "--home", str(home)]) == 0
    assert out()[-1] == "sync: marker present, but the home is not a git repository"


def test_a_home_inside_another_repository_is_reported_without_reading_that_repository(
    tmp_path: Path, git_env: None, out
) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    git("init", "--quiet", cwd=outer)
    write(outer / "unrelated.txt", "dirty outer tree\n")
    home = outer / "home"
    layout.scaffold_home(home)
    sync.write_marker(home)
    assert main(["status", "--home", str(home), "--fetch"]) == 0
    lines = out()
    assert lines[-1] == f"sync: marker present, but the home is inside the repository at {outer.resolve()}, not one of its own"
    assert not any(line.startswith(("tree:", "fetch:")) for line in lines)


def test_a_clean_synced_home_exits_zero(tmp_path: Path, git_env: None, out) -> None:
    home, _ = synced_home(tmp_path)
    assert main(["status", "--home", str(home)]) == 0
    lines = out()
    assert "marker: present" in lines
    assert "gitignore: canonical" in lines
    assert lines[-3:] == ["sync: synced with upstream", "fetch: skipped (pass --fetch to contact the remote)", "tree: clean"]


def test_a_dirty_tree_exits_one(tmp_path: Path, git_env: None, out) -> None:
    home, _ = synced_home(tmp_path)
    write(home / layout.ORG.pattern / "recent.md", "unpublished\n")
    assert main(["status", "--home", str(home)]) == 1
    assert out()[-1] == "tree: dirty"


def test_a_diverged_home_exits_one(tmp_path: Path, git_env: None, out) -> None:
    home, remote = synced_home(tmp_path)
    other = tmp_path / "other"
    git("clone", "--quiet", str(remote), str(other), cwd=tmp_path)
    write(other / layout.ORG.pattern / "rules.md", "theirs\n")
    git("add", "org-memory/rules.md", cwd=other)
    git("commit", "--quiet", "-m", "theirs", cwd=other)
    git("push", "--quiet", cwd=other)
    write(home / layout.ORG.pattern / "decisions.md", "mine\n")
    git("add", "org-memory/decisions.md", cwd=home)
    git("commit", "--quiet", "-m", "mine", cwd=home)

    assert main(["status", "--home", str(home)]) == 0  # the stale upstream ref still reads as ahead
    assert "sync: ahead by 1 unpushed commit(s)" in out()
    assert main(["status", "--home", str(home), "--fetch"]) == 1
    lines = out()
    assert "sync: DIVERGED from upstream (1 ahead, 1 behind)" in lines
    assert "fetch: done" in lines
    assert lines[-1] == "tree: clean"


def test_ahead_and_behind_are_reported_not_failed(tmp_path: Path, git_env: None, out) -> None:
    home, remote = synced_home(tmp_path)
    write(home / layout.ORG.pattern / "decisions.md", "mine\n")
    git("add", "org-memory/decisions.md", cwd=home)
    git("commit", "--quiet", "-m", "mine", cwd=home)
    assert main(["status", "--home", str(home)]) == 0
    assert "sync: ahead by 1 unpushed commit(s)" in out()

    git("push", "--quiet", cwd=home)
    git("reset", "--quiet", "--hard", "HEAD~1", cwd=home)
    assert main(["status", "--home", str(home), "--fetch"]) == 0
    assert "sync: BEHIND upstream by 1 commit(s)" in out()


def test_local_only_home_has_no_fetch_line(tmp_path: Path, git_env: None, out) -> None:
    home = tmp_path / "home"
    assert sync.init(home).ok
    assert main(["status", "--home", str(home), "--fetch"]) == 0
    lines = out()
    assert lines[-2:] == ["sync: local-only; no remote configured", "tree: clean"]


def test_a_failed_fetch_is_reported_and_the_exit_follows_the_tree(tmp_path: Path, git_env: None, out) -> None:
    home, remote = synced_home(tmp_path)
    git("remote", "set-url", "origin", str(tmp_path / "gone.git"), cwd=home)
    assert main(["status", "--home", str(home), "--fetch"]) == 0
    assert any(line.startswith("sync: remote fetch failed: ") for line in out())


def test_fetch_is_opt_in_and_carries_the_network_timeout(tmp_path: Path, git_env: None) -> None:
    home, _ = synced_home(tmp_path)
    quiet = RecordingRunner()
    status.inspect(_resolution(home), runner=quiet)
    assert not any(call[:1] == ("fetch",) for call, _ in quiet.calls)
    assert all(timeout is None for _, timeout in quiet.calls)

    contacting = RecordingRunner()
    readout = status.inspect(_resolution(home), fetch=True, runner=contacting)
    assert [timeout for call, timeout in contacting.calls if call[:1] == ("fetch",)] == [sync.NETWORK_TIMEOUT_SECONDS]
    assert readout.fetched is True
    assert readout.exit_code == 0


def test_gitignore_states(tmp_path: Path) -> None:
    home = tmp_path / "home"
    layout.scaffold_home(home)
    assert status.gitignore_state(home) == "canonical"
    path = home / layout.GITIGNORE_FILE
    path.write_text(layout.gitignore_text() + "*.swp\n", encoding="utf-8")
    assert status.gitignore_state(home) == "canonical managed block, other lines kept"
    path.write_text("*\n", encoding="utf-8")
    assert status.gitignore_state(home) == "present, differs from canonical"
    path.write_bytes(b"\xff\xfe not text")
    assert status.gitignore_state(home) == "present, differs from canonical"
    path.unlink()
    assert status.gitignore_state(home) == "absent"


def test_status_changes_nothing(tmp_path: Path, git_env: None) -> None:
    home, _ = synced_home(tmp_path)
    write(home / layout.ORG.pattern / "recent.md", "unpublished\n")
    before: Dict[str, bytes] = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file() and ".git" not in p.parts}
    porcelain = git("status", "--porcelain", cwd=home)
    assert main(["status", "--home", str(home), "--fetch"]) == 1
    assert {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file() and ".git" not in p.parts} == before
    assert git("status", "--porcelain", cwd=home) == porcelain
