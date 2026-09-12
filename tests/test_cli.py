from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import agent_memory
from agent_memory import __version__, layout
from agent_memory.cli import main
from agent_memory.home import BINDING_FILE, ENV_COMPAT_HOME, ENV_HOME

DISTRIBUTION = "agent-memory-cli"
CHECKOUT = Path(__file__).resolve().parents[1]
INSTALLED_ENV = "AGENT_MEMORY_TEST_INSTALLED"


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.delenv(ENV_HOME, raising=False)
    monkeypatch.delenv(ENV_COMPAT_HOME, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _installed_run() -> bool:
    return os.environ.get(INSTALLED_ENV) == "1"


# --- the command line -------------------------------------------------------


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"agent-memory {__version__}"


def test_no_command_shows_help_and_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage: agent-memory" in capsys.readouterr().err


def test_status_on_a_scaffolded_home(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = isolated / "home"
    layout.scaffold_home(home)
    assert main(["status", "--home", str(home)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"home: {home}"
    assert "source: flag" in out
    assert "exists: yes" in out
    assert "marker: absent" in out
    assert "gitignore: canonical" in out
    assert "org-memory: present" in out
    assert "projects: 0 with a memory dir" in out


def test_status_reports_marker_and_project_tiers(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = isolated / "home"
    layout.scaffold_home(home, project="demo")
    (home / layout.MARKER_FILE).write_text("", encoding="utf-8")
    assert main(["status", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "marker: present" in out
    assert "projects: 1 with a memory dir" in out


def test_status_on_a_missing_home_fails(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "--home", str(isolated / "nope")]) == 1
    assert "exists: no" in capsys.readouterr().out


def test_status_flags_a_non_canonical_gitignore(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = isolated / "home"
    layout.scaffold_home(home)
    (home / layout.GITIGNORE_FILE).write_text("*\n", encoding="utf-8")
    assert main(["status", "--home", str(home)]) == 0
    assert "gitignore: present, differs from canonical" in capsys.readouterr().out


def test_status_without_home_flag_uses_the_resolver(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = isolated / "env-home"
    layout.scaffold_home(home)
    monkeypatch.setenv(ENV_HOME, str(home))
    assert main(["status"]) == 0
    assert f"source: env:{ENV_HOME}" in capsys.readouterr().out


def test_status_reports_the_binding_project(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = isolated / "bound-home"
    layout.scaffold_home(home)
    (isolated / BINDING_FILE).write_text(
        '{"schema_version": 1, "project": "demo", "home": "bound-home"}', encoding="utf-8"
    )
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "project: demo" in out
    assert f"source: binding:{isolated / BINDING_FILE}" in out


def test_malformed_binding_is_a_usage_error(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (isolated / BINDING_FILE).write_text("{", encoding="utf-8")
    assert main(["status"]) == 2
    assert "agent-memory: error:" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["dangling symlink", "directory"])
def test_broken_binding_entry_is_a_usage_error_not_a_fallthrough(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    layout.scaffold_home(isolated / "outer-home")
    (isolated / BINDING_FILE).write_text('{"schema_version": 1, "home": "outer-home"}', encoding="utf-8")
    repo = isolated / "repo"
    repo.mkdir()
    if kind == "dangling symlink":
        (repo / BINDING_FILE).symlink_to(repo / "missing.json")
    else:
        (repo / BINDING_FILE).mkdir()
    monkeypatch.chdir(repo)
    assert main(["status"]) == 2
    captured = capsys.readouterr()
    assert "agent-memory: error:" in captured.err
    assert str(repo / BINDING_FILE) in captured.err
    assert "outer-home" not in captured.out


def test_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agent_memory", "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"agent-memory {__version__}"


# --- the distribution -------------------------------------------------------


def test_distribution_declares_no_runtime_dependencies() -> None:
    requires = importlib.metadata.requires(DISTRIBUTION) or []
    runtime = [entry for entry in requires if "extra ==" not in entry]
    assert runtime == []


def test_distribution_version_matches_the_package() -> None:
    assert importlib.metadata.version(DISTRIBUTION) == __version__


def test_source_tree_names_the_kernel_only_where_allowed() -> None:
    # The only permitted mentions: the compat env var line(s), the sync marker filename,
    # setup/legacy.py (which retires the kernel's hooks by exact match and so must spell them),
    # and the hidden --oacp-dir alias of the two writer verbs, debrief write and event write (the flag
    # each carried before it became a verb).
    src = CHECKOUT / "src" / "agent_memory"
    if not src.is_dir():
        pytest.skip("no source checkout beside the tests")
    legacy_table = src / "setup" / "legacy.py"
    assert legacy_table.is_file()
    token = re.compile("oacp", re.IGNORECASE)
    offending = []
    alias_lines = 0
    for path in sorted(src.rglob("*.py")):
        if path == legacy_table:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not token.search(line) or "OACP_HOME" in line or layout.MARKER_FILE in line:
                continue
            if '"--oacp-dir"' in line and path.name == "cli.py":
                alias_lines += 1
                continue
            offending.append(f"{path.relative_to(CHECKOUT)}:{number}: {line.strip()}")
    assert offending == []
    assert alias_lines == 2


def test_import_resolves_to_the_installed_wheel_when_asked() -> None:
    if not _installed_run():
        pytest.skip(f"{INSTALLED_ENV}=1 not set: source-checkout run")
    location = Path(agent_memory.__file__).resolve()
    assert "site-packages" in location.parts
    assert CHECKOUT not in location.parents


def test_console_script_runs_when_installed() -> None:
    if not _installed_run():
        pytest.skip(f"{INSTALLED_ENV}=1 not set: source-checkout run")
    exe = shutil.which("agent-memory")
    assert exe, "agent-memory console script not on PATH"
    result = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert result.stdout.strip() == f"agent-memory {__version__}"


# --- the sync verbs ---------------------------------------------------------


def _git_out(*args: str, cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_enable_push_and_pull_verbs_round_trip(isolated: Path, git_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    remote = isolated / "remote.git"
    _git_out("init", "--bare", "--quiet", str(remote), cwd=isolated)
    first = isolated / "first"
    (first / "org-memory").mkdir(parents=True)
    (first / "org-memory" / "recent.md").write_text("# recent\n", encoding="utf-8")

    assert main(["enable", "--home", str(first), "--remote", str(remote), "--agent", "claude"]) == 0
    out = capsys.readouterr().out
    assert ".gitignore: created with the managed block." in out
    assert "delivered to the remote" in out
    assert _git_out("log", "-1", "--format=%s", cwd=first).startswith("memory: claude@")

    second = isolated / "second"
    assert main(["clone", str(remote), "--home", str(second)]) == 0
    assert "Cloned the memory repository" in capsys.readouterr().out

    (first / "projects" / "demo" / "memory").mkdir(parents=True)
    (first / "projects" / "demo" / "memory" / "open_threads.md").write_text("- open\n", encoding="utf-8")
    assert main(["push", "--home", str(first), "--agent", "claude"]) == 0
    assert "committed 1 file(s)" in capsys.readouterr().out

    assert main(["pull", "--home", str(second)]) == 0
    assert capsys.readouterr().out.strip() == "memory pull: synced 1 commit(s)."
    assert (second / "projects" / "demo" / "memory" / "open_threads.md").read_text(encoding="utf-8") == "- open\n"


def test_push_and_pull_verbs_are_silent_without_the_marker(
    isolated: Path, git_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    home = isolated / "home"
    home.mkdir()
    assert main(["push", "--home", str(home)]) == 0
    assert main(["pull", "--home", str(home)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_refused_states_exit_nonzero_with_their_message_on_stderr(
    isolated: Path, git_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    outer = isolated / "outer"
    outer.mkdir()
    _git_out("init", "--quiet", cwd=outer)
    nested = outer / "home"
    nested.mkdir()
    assert main(["enable", "--home", str(nested)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agent-memory: error: root mismatch" in captured.err

    home = isolated / "home"
    assert main(["enable", "--home", str(home)]) == 0
    capsys.readouterr()
    with (home / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("!projects/*/memory/.cache/\n")
    cache = home / "projects" / "demo" / "memory" / ".cache"
    cache.mkdir(parents=True)
    (cache / "index.json").write_text("{}\n", encoding="utf-8")
    assert main(["push", "--home", str(home)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "outside the sync allowlist" in captured.err


def test_clone_verb_refuses_a_non_empty_home(isolated: Path, git_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    home = isolated / "home"
    home.mkdir()
    (home / "keep.txt").write_text("keep\n", encoding="utf-8")
    assert main(["clone", str(isolated / "missing.git"), "--home", str(home)]) == 1
    assert "agent-memory: error: refusing to clone into a non-empty home" in capsys.readouterr().err
    assert (home / "keep.txt").is_file()


def test_disable_verb(isolated: Path, git_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    home = isolated / "home"
    assert main(["enable", "--home", str(home)]) == 0
    capsys.readouterr()
    assert main(["disable", "--home", str(home)]) == 0
    assert "syncing is disabled" in capsys.readouterr().out
    assert not (home / layout.MARKER_FILE).exists()
    assert (home / ".git").is_dir()


def test_sync_verbs_resolve_the_home_like_status(
    isolated: Path, git_env: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = isolated / "env-home"
    monkeypatch.setenv(ENV_HOME, str(home))
    assert main(["enable"]) == 0
    assert (home / layout.MARKER_FILE).is_file()
    assert (home / ".gitignore").read_bytes() == layout.gitignore_text().encode("utf-8")
