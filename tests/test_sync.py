from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from agent_memory import layout, sync
from agent_memory.git_runner import EXIT_TIMEOUT, GitResult, run_git

GOLDEN = Path(__file__).resolve().parent / "golden" / "canonical_memory_gitignore.txt"
CANONICAL = layout.gitignore_text()
MARKER = layout.MARKER_FILE
RUNTIME_FILE = "projects/demo/agents/test/status.yaml"


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "init.defaultBranch")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "main")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Memory Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "memory-test@example.invalid")
    monkeypatch.delenv(sync.ENV_AGENT, raising=False)
    monkeypatch.delenv("AGENT_NAME", raising=False)


# --- helpers ----------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed ({completed.returncode}): {completed.stdout}\n{completed.stderr}")
    return completed.stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _create_remote(tmp_path: Path) -> Tuple[Path, Path]:
    """A bare remote seeded with one memory commit on ``main``, plus the seed clone."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git("init", "--bare", "--quiet", str(remote), cwd=tmp_path)
    _git("init", "--quiet", str(seed), cwd=tmp_path)
    _write(seed / ".gitignore", CANONICAL)
    _write(seed / MARKER, "memory sync enabled\n")
    _write(seed / "org-memory" / "recent.md", "seed\n")
    _git("add", ".gitignore", MARKER, "org-memory/recent.md", cwd=seed)
    _git("commit", "--quiet", "-m", "seed memory", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "--quiet", "-u", "origin", "main", cwd=seed)
    _git("--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main", cwd=tmp_path)
    return remote, seed


def _commit_and_push(repo: Path, relative_path: str, content: str) -> None:
    _write(repo / relative_path, content)
    _git("add", relative_path, cwd=repo)
    _git("commit", "--quiet", "-m", f"update {relative_path}", cwd=repo)
    _git("push", "--quiet", cwd=repo)


def _head_paths(repo: Path) -> List[str]:
    return sorted(line for line in _git("show", "--format=", "--name-only", "HEAD", cwd=repo).splitlines() if line)


def _staged(repo: Path) -> List[str]:
    return sorted(line for line in _git("diff", "--cached", "--name-only", cwd=repo).splitlines() if line)


def _commit_count(repo: Path) -> int:
    completed = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=False
    )
    return int(completed.stdout.strip()) if completed.returncode == 0 else 0


def _remote_has(remote: Path, path: str) -> bool:
    completed = subprocess.run(
        ["git", "--git-dir", str(remote), "cat-file", "-e", f"main:{path}"], capture_output=True, check=False
    )
    return completed.returncode == 0


def _local_home(tmp_path: Path, name: str = "home") -> Path:
    home = tmp_path / name
    assert sync.init(home).ok
    return home


class RecordingRunner:
    """Answers scripted git calls without a repository and records every call with its timeout."""

    def __init__(self, answers: Optional[Dict[Tuple[str, ...], str]] = None) -> None:
        self.calls: List[Tuple[Tuple[str, ...], Optional[float]]] = []
        self.answers = answers or {}

    def __call__(self, args: Sequence[str], *, cwd: Path, timeout: Optional[float] = None) -> GitResult:
        call = tuple(args)
        self.calls.append((call, timeout))
        for prefix, stdout in self.answers.items():
            if call[: len(prefix)] == prefix:
                return GitResult(0, stdout)
        return GitResult(0, "")

    def timed(self, *prefix: str) -> List[Optional[float]]:
        return [timeout for call, timeout in self.calls if call[: len(prefix)] == prefix]


class FetchTimesOut:
    """The real runner, except that every fetch reports a timeout."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, ...]] = []

    def __call__(self, args: Sequence[str], *, cwd: Path, timeout: Optional[float] = None) -> GitResult:
        self.calls.append(tuple(args))
        if args and args[0] == "fetch":
            return GitResult(EXIT_TIMEOUT, "", "git fetch --quiet: timed out after 30s")
        return run_git(args, cwd=cwd, timeout=timeout)


# --- the network verbs carry the timeout -----------------------------------


def test_fetch_pull_push_and_clone_receive_network_timeouts(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    _write(root / MARKER, "memory sync enabled\n")
    common = {
        ("rev-parse", "--is-inside-work-tree"): "true",
        ("rev-parse", "--show-toplevel"): str(root),
        ("remote",): "origin",
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): "origin/main",
    }
    puller = RecordingRunner({**common, ("rev-list", "--left-right", "--count", "HEAD...origin/main"): "0 1"})
    pulled = sync.pull(root, runner=puller)
    pusher = RecordingRunner({**common, ("rev-list", "--left-right", "--count", "HEAD...origin/main"): "1 0"})
    pushed = sync.push(root, runner=pusher)
    cloner = RecordingRunner()
    sync.clone(tmp_path / "clone", "example.invalid/repo.git", runner=cloner)

    assert pulled.status == "synced"
    assert pushed.status == "pushed"
    assert puller.timed("fetch", "--quiet") == [sync.NETWORK_TIMEOUT_SECONDS]
    assert puller.timed("pull", "--ff-only") == [sync.NETWORK_TIMEOUT_SECONDS]
    assert pusher.timed("push") == [sync.NETWORK_TIMEOUT_SECONDS]
    assert cloner.timed("clone") == [sync.NETWORK_TIMEOUT_SECONDS]
    local_verbs = [call for call, timeout in puller.calls + pusher.calls if timeout is not None]
    assert all(call[0] in {"fetch", "pull", "push"} for call in local_verbs)


def test_fetch_timeout_on_pull_changes_nothing_and_is_its_own_status(tmp_path: Path) -> None:
    remote, seed = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    _commit_and_push(seed, "org-memory/new.md", "remote update\n")
    runner = FetchTimesOut()

    outcome = sync.pull(home, runner=runner)

    assert outcome.status == "fetch_timed_out"
    assert not outcome.ok
    assert "timed out" in outcome.lines[0]
    assert not (home / "org-memory" / "new.md").exists()
    assert not any(call[0] == "pull" for call in runner.calls)


def test_fetch_timeout_on_push_keeps_the_commit_local_and_skips_the_push(tmp_path: Path) -> None:
    remote, _ = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    _write(home / "org-memory" / "local.md", "local update\n")
    runner = FetchTimesOut()

    outcome = sync.push(home, runner=runner)

    assert outcome.status == "fetch_timed_out"
    assert not outcome.ok
    assert outcome.committed == ("org-memory/local.md",)
    assert "remains local" in outcome.lines[-1]
    assert _head_paths(home) == ["org-memory/local.md"]
    assert not any(call[0] == "push" for call in runner.calls)
    assert not _remote_has(remote, "org-memory/local.md")


# --- the selected root must be the worktree root ---------------------------


@pytest.mark.parametrize("verb", ["init", "push", "pull"])
def test_home_nested_inside_another_repository_is_refused_before_anything_is_written(
    tmp_path: Path, verb: str
) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    _git("init", "--quiet", cwd=outer)
    home = outer / "home"
    home.mkdir()
    _write(home / MARKER, "memory sync enabled\n")
    _write(home / "org-memory" / "recent.md", "nested\n")

    with pytest.raises(sync.SyncError, match="root mismatch"):
        getattr(sync, verb)(home)

    assert not (home / ".gitignore").exists()
    assert not (home / ".git").exists()
    assert _commit_count(outer) == 0
    assert _staged(outer) == []


def test_marker_without_a_repository_is_an_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home / MARKER, "memory sync enabled\n")
    with pytest.raises(sync.SyncError, match="not a git repository"):
        sync.push(home)
    with pytest.raises(sync.SyncError, match="not a git repository"):
        sync.pull(home)


# --- the managed ignore block ----------------------------------------------


def test_init_writes_the_golden_gitignore_on_a_fresh_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outcome = sync.init(home)
    assert outcome.ok and outcome.status == "local_only"
    assert outcome.receipt is not None and outcome.receipt.action == "created"
    assert (home / ".gitignore").read_bytes() == GOLDEN.read_bytes()
    assert (home / MARKER).is_file()
    assert _head_paths(home) == [".gitignore", MARKER]


def test_reinit_keeps_custom_gitignore_lines_and_returns_a_receipt(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    custom = "# unrelated existing rules\nprivate-notes/\n"
    _write(home / ".gitignore", custom)

    outcome = sync.init(home)

    assert outcome.ok
    receipt = outcome.receipt
    assert receipt is not None and receipt.action == "updated"
    assert receipt.before == custom
    assert receipt.after == CANONICAL + custom
    assert (home / ".gitignore").read_text(encoding="utf-8") == CANONICAL + custom
    assert ".gitignore before:" in outcome.lines and ".gitignore after:" in outcome.lines
    assert "    private-notes/" in outcome.lines
    assert _head_paths(home) == [".gitignore"]


@pytest.mark.parametrize(
    "existing",
    [CANONICAL, CANONICAL + "private-notes/\n", "# mine first\nprivate-notes/\n" + CANONICAL + "tail/\n"],
    ids=["exact", "custom-tail", "custom-head-and-tail"],
)
def test_init_leaves_a_gitignore_that_already_carries_the_block_untouched(tmp_path: Path, existing: str) -> None:
    home = _local_home(tmp_path)
    _write(home / ".gitignore", existing)
    before_count = _commit_count(home)

    outcome = sync.init(home)

    assert outcome.ok
    assert outcome.receipt is not None and outcome.receipt.action == "unchanged"
    assert (home / ".gitignore").read_text(encoding="utf-8") == existing
    assert _commit_count(home) == before_count + (1 if existing != CANONICAL else 0)


def test_init_upgrades_an_older_block_without_duplicating_its_lines(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    older = "\n".join(line for line in CANONICAL.splitlines() if "keys" not in line) + "\n"
    _write(home / ".gitignore", older + "private-notes/\n")

    outcome = sync.init(home)

    assert outcome.receipt is not None and outcome.receipt.action == "updated"
    assert (home / ".gitignore").read_text(encoding="utf-8") == CANONICAL + "private-notes/\n"


def test_init_keeps_the_bytes_of_an_existing_marker(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    _write(home / MARKER, "legacy marker text\n")
    assert sync.init(home).ok
    assert (home / MARKER).read_text(encoding="utf-8") == "legacy marker text\n"


# --- the publication boundary ----------------------------------------------


def test_prestaged_runtime_file_is_preserved_and_never_committed(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    _write(home / RUNTIME_FILE, "status: busy\n")
    _git("add", "-f", RUNTIME_FILE, cwd=home)
    _write(home / "org-memory" / "recent.md", "# recent\n")

    outcome = sync.push(home)

    assert outcome.ok and outcome.status == "local_only"
    assert outcome.committed == ("org-memory/recent.md",)
    assert outcome.preserved == (RUNTIME_FILE,)
    assert any("left staged and uncommitted" in line and RUNTIME_FILE in line for line in outcome.lines)
    assert _head_paths(home) == ["org-memory/recent.md"]
    assert _staged(home) == [RUNTIME_FILE]
    assert RUNTIME_FILE not in _git("ls-tree", "-r", "--name-only", "HEAD", cwd=home).splitlines()
    assert (home / RUNTIME_FILE).read_text(encoding="utf-8") == "status: busy\n"


def test_prestaged_runtime_file_survives_init_on_an_unborn_branch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _git("init", "--quiet", cwd=home)
    _write(home / RUNTIME_FILE, "status: busy\n")
    _git("add", "-f", RUNTIME_FILE, cwd=home)
    _write(home / "org-memory" / "recent.md", "# recent\n")

    outcome = sync.init(home)

    assert outcome.ok
    assert set(outcome.committed) == {".gitignore", MARKER, "org-memory/recent.md"}
    assert outcome.preserved == (RUNTIME_FILE,)
    assert _head_paths(home) == [".gitignore", MARKER, "org-memory/recent.md"]
    assert _staged(home) == [RUNTIME_FILE]


def test_staged_change_to_a_tracked_foreign_file_is_preserved(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    _write(home / RUNTIME_FILE, "status: idle\n")
    _git("add", "-f", RUNTIME_FILE, cwd=home)
    _git("commit", "--quiet", "-m", "hand-tracked runtime file", cwd=home)
    _write(home / RUNTIME_FILE, "status: busy\n")
    _git("add", RUNTIME_FILE, cwd=home)
    _write(home / "org-memory" / "recent.md", "# recent\n")

    outcome = sync.push(home)

    assert outcome.ok
    assert _head_paths(home) == ["org-memory/recent.md"]
    assert _staged(home) == [RUNTIME_FILE]
    assert _git("show", f"HEAD:{RUNTIME_FILE}", cwd=home) == "status: idle"


ALLOWED = (
    "org-memory/recent.md",
    "org-memory/debriefs/demo/2026/09/20260905-alice-1f3a9c2b.md",
    "projects/demo/memory/project_facts.md",
    "projects/demo/memory/archive/20260101T000000Z_open_threads.md",
    "projects/demo/memory/keys.md",
)
NEVER = (
    "keys/k.json",
    "keys/.trust_domain",
    "projects/demo/memory/keys/k.json",
    "projects/demo/memory/.cache/index.json",
    "projects/demo/agents/claude/inbox/msg.yaml",
    "projects/demo/status.yaml",
    "README.md",
    "state/watch/cursor",
)


def test_every_committed_path_is_inside_the_allowlist(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    for path in ALLOWED + NEVER:
        _write(home / path, f"{path}\n")

    outcome = sync.push(home)

    assert outcome.ok
    assert set(outcome.committed) == set(ALLOWED)
    assert _head_paths(home) == sorted(ALLOWED)
    assert sorted(_git("ls-files", cwd=home).splitlines()) == sorted([".gitignore", MARKER, *ALLOWED])
    assert all(layout.is_allowed_memory_path(path) for path in _head_paths(home))
    for path in NEVER:
        assert (home / path).is_file(), path


def test_a_widened_ignore_file_cannot_publish_an_unsynced_subdirectory(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    with (home / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("!projects/*/memory/.cache/\n")
    _write(home / "projects" / "demo" / "memory" / ".cache" / "index.json", "{}\n")
    _write(home / "projects" / "demo" / "memory" / "project_facts.md", "# facts\n")
    before = _commit_count(home)

    outcome = sync.push(home)

    assert outcome.status == "outside_allowlist"
    assert not outcome.ok
    assert "projects/demo/memory/.cache/index.json" in outcome.lines[0]
    assert _commit_count(home) == before
    assert _staged(home) == []
    assert (home / "projects" / "demo" / "memory" / "project_facts.md").is_file()


WIDEN_KEYS = "!keys/\n!**/keys/\n!**/keys/**\n"
NESTED_KEYS = (
    "projects/demo/memory/keys/k.json",
    "org-memory/keys/k.json",
    "projects/demo/memory/archive/keys/k.json",
    "org-memory/debriefs/keys/k.json",
)


@pytest.mark.parametrize("verb", ["push", "init"])
@pytest.mark.parametrize("denied", NESTED_KEYS)
def test_a_widened_ignore_file_cannot_publish_a_never_synced_directory(tmp_path: Path, verb: str, denied: str) -> None:
    home = _local_home(tmp_path)
    with (home / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write(WIDEN_KEYS)
    _write(home / denied, "synthetic-key-fixture\n")
    _write(home / "keys" / "root.json", "synthetic-key-fixture\n")
    _write(home / "projects" / "demo" / "memory" / "keys.md", "# about keys\n")
    _write(home / "org-memory" / "recent.md", "# recent\n")
    head = _git("rev-parse", "HEAD", cwd=home)

    outcome = getattr(sync, verb)(home)

    assert outcome.status == "outside_allowlist" and not outcome.ok
    assert denied in outcome.lines[-1] and "keys/" in outcome.lines[-1]
    assert "root.json" not in outcome.lines[-1]
    assert _git("rev-parse", "HEAD", cwd=home) == head
    assert _staged(home) == []
    for path in (denied, "keys/root.json", "projects/demo/memory/keys.md", "org-memory/recent.md"):
        assert (home / path).is_file(), path
    assert "keys" not in _git("ls-files", cwd=home)


def test_a_never_synced_path_is_refused_before_anything_reaches_the_remote(tmp_path: Path) -> None:
    remote, _ = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    with (home / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write(WIDEN_KEYS)
    _write(home / "projects" / "demo" / "memory" / "keys" / "k.json", "synthetic-key-fixture\n")
    _write(home / "org-memory" / "local.md", "local update\n")
    head = _git("rev-parse", "HEAD", cwd=home)

    outcome = sync.push(home)

    assert outcome.status == "outside_allowlist" and not outcome.ok
    assert _git("rev-parse", "HEAD", cwd=home) == head
    assert not _remote_has(remote, "projects/demo/memory/keys/k.json")
    assert not _remote_has(remote, "org-memory/local.md")
    assert _git("--git-dir", str(remote), "rev-parse", "main", cwd=tmp_path) == head


def test_an_already_staged_never_synced_path_is_refused_not_committed(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    denied = "projects/demo/memory/keys/k.json"
    _write(home / denied, "synthetic-key-fixture\n")
    _git("add", "-f", denied, cwd=home)
    _write(home / "org-memory" / "recent.md", "# recent\n")
    head = _git("rev-parse", "HEAD", cwd=home)

    outcome = sync.push(home)

    assert outcome.status == "outside_allowlist" and not outcome.ok
    assert _git("rev-parse", "HEAD", cwd=home) == head
    assert _staged(home) == [denied]
    assert "org-memory/recent.md" not in _git("ls-files", cwd=home).splitlines()


def test_keys_named_files_stay_allowed_while_keys_directories_never_are(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    _write(home / "projects" / "demo" / "memory" / "keys.md", "# about keys\n")
    _write(home / "org-memory" / "keys-rotation.md", "# rotation\n")
    outcome = sync.push(home)
    assert outcome.ok
    assert set(outcome.committed) == {"projects/demo/memory/keys.md", "org-memory/keys-rotation.md"}


# --- selected names are literal, never patterns ---------------------------


LITERAL_NAMES = ("*.md", "x?.md", "[x].md", ":x.md", "x*y.md", "a-b_c.md")


@pytest.mark.parametrize("name", LITERAL_NAMES)
def test_a_selected_name_with_pattern_characters_is_committed_as_itself(tmp_path: Path, name: str) -> None:
    home = _local_home(tmp_path)
    memory = "projects/demo/memory"
    foreign = f"{memory}/.cache/x.md"
    _write(home / foreign, "old-fixture\n")
    _git("add", "-f", foreign, cwd=home)
    _git("commit", "--quiet", "-m", "hand-tracked denied file, left clean", cwd=home)
    _write(home / memory / name, "memory-fixture\n")
    _write(home / memory / "x.md", "sibling\n")
    if name == "*.md":
        # The pattern hazard is real: read non-literally, the name reaches the hand-tracked denied file.
        assert foreign in _git("ls-files", "--", f"{memory}/{name}", cwd=home).splitlines()
    assert sync.select_paths(home) == (sorted([f"{memory}/{name}", f"{memory}/x.md"]), [])

    outcome = sync.push(home)

    assert outcome.ok
    assert set(outcome.committed) == {f"{memory}/{name}", f"{memory}/x.md"}
    assert _head_paths(home) == sorted([f"{memory}/{name}", f"{memory}/x.md"])
    assert _git("show", f"HEAD:{memory}/{name}", cwd=home) == "memory-fixture"
    assert _git("show", f"HEAD:{foreign}", cwd=home) == "old-fixture"
    assert _git("status", "--porcelain", cwd=home) == ""


def test_a_project_name_with_pattern_characters_never_matches_a_foreign_path(tmp_path: Path) -> None:
    # The kernel's project-name grammar accepts "a*"; as a git pathspec it would also match
    # projects/a/agents/memory/x.md. Every selected name must reach git literally.
    home = _local_home(tmp_path)
    foreign = "projects/a/agents/memory/x.md"
    _write(home / foreign, "old-fixture\n")
    _git("add", "-f", foreign, cwd=home)
    _git("commit", "--quiet", "-m", "seed unrelated file", cwd=home)
    _write(home / foreign, "staged-fixture\n")
    _git("add", foreign, cwd=home)
    _write(home / foreign, "unstaged-fixture\n")
    memory = "projects/a*/memory/x.md"
    _write(home / memory, "memory-fixture\n")
    assert foreign in _git("ls-files", "--", memory, cwd=home).splitlines()  # the pattern hazard is real
    assert sync.select_paths(home) == ([memory], [])
    assert sync.staged_paths(home) == [foreign]
    head = _git("rev-parse", "HEAD", cwd=home)

    outcome = sync.push(home)

    assert outcome.ok and outcome.committed == (memory,)
    assert outcome.preserved == (foreign,)
    assert _git("rev-parse", "HEAD", cwd=home) != head
    assert _head_paths(home) == [memory]
    assert _git("show", f"HEAD:{foreign}", cwd=home) == "old-fixture"
    assert _git("show", f":{foreign}", cwd=home) == "staged-fixture"
    assert (home / foreign).read_text(encoding="utf-8") == "unstaged-fixture\n"
    assert sync.staged_paths(home) == [foreign]
    assert _git("show", f"HEAD:{memory}", cwd=home) == "memory-fixture"


def test_push_is_silent_when_not_configured(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert sync.push(home) == sync.Outcome("not_configured", True)
    assert sync.pull(home) == sync.Outcome("not_configured", True)


# --- the commit identity ---------------------------------------------------


def test_commit_identity_carries_the_agent_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _local_home(tmp_path)

    def publish(content: str, **kwargs: object) -> str:
        _write(home / "org-memory" / "recent.md", content)
        assert sync.push(home, **kwargs).ok  # type: ignore[arg-type]
        return _git("log", "-1", "--format=%s", cwd=home)

    assert publish("explicit\n", agent="claude").startswith("memory: claude@")
    assert publish("env\n", env={sync.ENV_AGENT: "codex"}).startswith("memory: codex@")
    assert publish("fallback\n", env={"AGENT_NAME": "iris", "USER": "osuser"}).startswith("memory: iris@")
    assert publish("user\n", env={"USER": "osuser"}).startswith("memory: osuser@")
    assert publish("none\n", env={}).startswith(f"memory: {sync.UNKNOWN_AGENT}@")
    monkeypatch.setenv(sync.ENV_AGENT, "gemini")
    assert publish("process env\n").startswith("memory: gemini@")
    assert publish("count\n", agent="claude").endswith("(1 files)")


# --- push against a remote -------------------------------------------------


def test_push_commits_dirty_memory_and_delivers_it(tmp_path: Path) -> None:
    remote, _ = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    _write(home / "org-memory" / "local.md", "local update\n")

    outcome = sync.push(home)

    assert outcome.status == "pushed" and outcome.ok
    assert outcome.committed == ("org-memory/local.md",)
    assert "delivered" in outcome.lines[-1]
    assert _git("status", "--porcelain", cwd=home) == ""
    assert _git("--git-dir", str(remote), "show", "main:org-memory/local.md", cwd=tmp_path) == "local update"


def test_push_without_a_remote_is_reported_as_local_only(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    _write(home / "org-memory" / "local.md", "local update\n")
    outcome = sync.push(home)
    assert outcome.status == "local_only" and outcome.ok
    assert "remains local" in outcome.lines[-1]


def test_push_with_nothing_new_is_up_to_date(tmp_path: Path) -> None:
    remote, _ = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    outcome = sync.push(home)
    assert outcome.status == "up_to_date" and outcome.ok
    assert outcome.committed == ()


def test_push_refuses_a_repository_behind_its_upstream(tmp_path: Path) -> None:
    remote, seed = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    _commit_and_push(seed, "org-memory/remote.md", "remote update\n")
    _write(home / "org-memory" / "local.md", "local update\n")
    before = _commit_count(home)

    outcome = sync.push(home)

    assert outcome.status == "behind" and not outcome.ok
    assert "pull before pushing" in outcome.lines[0]
    assert _commit_count(home) == before
    assert (home / "org-memory" / "local.md").read_text(encoding="utf-8") == "local update\n"


def test_push_refuses_a_diverged_repository_without_committing(tmp_path: Path) -> None:
    remote, seed = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    _write(home / "org-memory" / "local.md", "local update\n")
    _git("add", "org-memory/local.md", cwd=home)
    _git("commit", "--quiet", "-m", "local update", cwd=home)
    _commit_and_push(seed, "org-memory/remote.md", "remote update\n")
    _write(home / "org-memory" / "uncommitted.md", "# uncommitted\n")
    before = _commit_count(home)

    outcome = sync.push(home)

    assert outcome.status == "diverged" and not outcome.ok
    assert _commit_count(home) == before
    assert (home / "org-memory" / "uncommitted.md").is_file()


def test_rejected_push_leaves_the_local_commit_in_place_and_says_so(tmp_path: Path) -> None:
    remote, _ = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'memory remote refuses pushes' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _write(home / "org-memory" / "local.md", "local update\n")

    outcome = sync.push(home)

    assert outcome.status == "push_failed" and not outcome.ok
    assert outcome.committed == ("org-memory/local.md",)
    assert "remains local" in outcome.lines[-1]
    assert _head_paths(home) == ["org-memory/local.md"]
    assert not _remote_has(remote, "org-memory/local.md")


# --- pull ------------------------------------------------------------------


def test_pull_fast_forwards_a_clean_repository_behind_its_upstream(tmp_path: Path) -> None:
    remote, seed = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    _commit_and_push(seed, "org-memory/new.md", "remote update\n")

    outcome = sync.pull(home)

    assert outcome.status == "synced" and outcome.ok
    assert outcome.lines == ("memory pull: synced 1 commit(s).",)
    assert (home / "org-memory" / "new.md").read_text(encoding="utf-8") == "remote update\n"


def test_pull_refuses_a_dirty_tree(tmp_path: Path) -> None:
    remote, seed = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    _commit_and_push(seed, "org-memory/new.md", "remote update\n")
    _write(home / "org-memory" / "recent.md", "edited locally\n")

    outcome = sync.pull(home)

    assert outcome.status == "dirty" and not outcome.ok
    assert not (home / "org-memory" / "new.md").exists()
    assert (home / "org-memory" / "recent.md").read_text(encoding="utf-8") == "edited locally\n"


def test_pull_refuses_a_diverged_repository(tmp_path: Path) -> None:
    remote, seed = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    _write(home / "org-memory" / "local.md", "local\n")
    _git("add", "org-memory/local.md", cwd=home)
    _git("commit", "--quiet", "-m", "local", cwd=home)
    _commit_and_push(seed, "org-memory/remote.md", "remote\n")

    outcome = sync.pull(home)

    assert outcome.status == "diverged" and not outcome.ok
    assert not (home / "org-memory" / "remote.md").exists()


def test_pull_with_unpushed_commits_is_not_an_error(tmp_path: Path) -> None:
    remote, _ = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    _write(home / "org-memory" / "local.md", "local\n")
    _git("add", "org-memory/local.md", cwd=home)
    _git("commit", "--quiet", "-m", "local", cwd=home)

    outcome = sync.pull(home)

    assert outcome.status == "ahead" and outcome.ok
    assert "1 unpushed commit(s)" in outcome.lines[0]


def test_pull_when_already_synced(tmp_path: Path) -> None:
    remote, _ = _create_remote(tmp_path)
    home = tmp_path / "home"
    sync.clone(home, str(remote))
    outcome = sync.pull(home)
    assert outcome.status == "up_to_date" and outcome.ok


def test_pull_without_a_remote_or_upstream_is_a_skip(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    assert sync.pull(home).status == "local_only"
    _git("remote", "add", "origin", str(tmp_path / "nowhere.git"), cwd=home)
    _git("init", "--bare", "--quiet", str(tmp_path / "nowhere.git"), cwd=tmp_path)
    outcome = sync.pull(home)
    assert outcome.status == "no_upstream" and outcome.ok


# --- clone -----------------------------------------------------------------


def test_clone_refuses_a_non_empty_home_without_force(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home / "local-only.txt", "keep me\n")
    with pytest.raises(sync.SyncError, match="non-empty home"):
        sync.clone(home, str(tmp_path / "missing.git"))
    assert (home / "local-only.txt").is_file()


def test_force_clone_preserves_the_existing_home_as_a_backup(tmp_path: Path) -> None:
    remote, _ = _create_remote(tmp_path)
    home = tmp_path / "home"
    _write(home / "local-only.txt", "keep me\n")

    outcome = sync.clone(home, str(remote), force=True)

    backups = list(tmp_path.glob("home.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "local-only.txt").read_text(encoding="utf-8") == "keep me\n"
    assert (home / "org-memory" / "recent.md").read_text(encoding="utf-8") == "seed\n"
    assert outcome.status == "cloned"
    assert any("Moved the existing home aside" in line for line in outcome.lines)


def test_force_clone_failure_restores_the_existing_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home / "local-only.txt", "keep me\n")
    with pytest.raises(sync.SyncError, match="git clone failed"):
        sync.clone(home, str(tmp_path / "missing.git"), force=True)
    assert (home / "local-only.txt").read_text(encoding="utf-8") == "keep me\n"
    assert not list(tmp_path.glob("home.backup-*"))


# --- init and the round trip ------------------------------------------------


def test_bare_remote_round_trip(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _git("init", "--bare", "--quiet", str(remote), cwd=tmp_path)
    first = tmp_path / "first"
    _write(first / "org-memory" / "recent.md", "# recent\n")

    initialized = sync.init(first, remote=str(remote), agent="claude")
    assert initialized.status == "pushed" and initialized.ok
    assert set(initialized.committed) == {".gitignore", MARKER, "org-memory/recent.md"}

    second = tmp_path / "second"
    assert sync.clone(second, str(remote)).ok
    assert (second / "org-memory" / "recent.md").read_text(encoding="utf-8") == "# recent\n"
    assert (second / ".gitignore").read_bytes() == GOLDEN.read_bytes()
    assert sync.is_configured(second)

    _write(first / "projects" / "demo" / "memory" / "decision_log.md", "- decided\n")
    pushed = sync.push(first, agent="claude")
    assert pushed.status == "pushed" and pushed.committed == ("projects/demo/memory/decision_log.md",)

    pulled = sync.pull(second)
    assert pulled.status == "synced"
    assert (second / "projects" / "demo" / "memory" / "decision_log.md").read_text(encoding="utf-8") == "- decided\n"
    assert _git("log", "-1", "--format=%s", cwd=second).startswith("memory: claude@")


def test_init_is_idempotent(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file() and ".git" not in path.parts}
    count = _commit_count(home)

    outcome = sync.init(home)

    assert outcome.ok and outcome.status == "local_only"
    assert "no memory changes to commit" in " ".join(outcome.lines)
    assert _commit_count(home) == count
    assert {path: path.read_bytes() for path in home.rglob("*") if path.is_file() and ".git" not in path.parts} == before


def test_init_adds_origin_when_another_remote_exists(tmp_path: Path) -> None:
    remote = tmp_path / "memory.git"
    other = tmp_path / "other.git"
    _git("init", "--bare", "--quiet", str(remote), cwd=tmp_path)
    _git("init", "--bare", "--quiet", str(other), cwd=tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _git("init", "--quiet", cwd=home)
    _git("remote", "add", "upstream", str(other), cwd=home)

    outcome = sync.init(home, remote=str(remote))

    assert outcome.ok and outcome.status == "pushed"
    assert _git("remote", "get-url", "origin", cwd=home) == str(remote)
    assert _remote_has(remote, MARKER)


def test_init_updates_an_existing_origin(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    _git("remote", "add", "origin", str(tmp_path / "old.git"), cwd=home)
    remote = tmp_path / "new.git"
    _git("init", "--bare", "--quiet", str(remote), cwd=tmp_path)
    outcome = sync.init(home, remote=str(remote))
    assert outcome.ok
    assert _git("remote", "get-url", "origin", cwd=home) == str(remote)


def test_disable_removes_the_marker_and_keeps_the_repository(tmp_path: Path) -> None:
    home = _local_home(tmp_path)
    outcome = sync.disable(home)
    assert outcome.status == "disabled" and outcome.ok
    assert not (home / MARKER).exists()
    assert (home / ".git").is_dir()
    assert sync.disable(home).status == "already_disabled"
    assert sync.push(home).status == "not_configured"


# --- the status parser ------------------------------------------------------


def test_status_parser_takes_both_paths_of_a_rename() -> None:
    data = "R  new.md\0old.md\0 M other.md\0?? fresh.md\0"
    assert sync._parse_status_z(data) == ["new.md", "old.md", "other.md", "fresh.md"]
    assert sync._parse_status_z("") == []
