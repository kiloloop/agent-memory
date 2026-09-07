# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory startup --runtime <r>``: the bounded, ordered, honest read manifest.

The seven tier files in layout order with readability states only, the
exclusions, the optional pull with its outcome, the character budget, the
runtime shapes (plain text for claude, the hook envelope for codex), and the
project taken from a binding or a marker.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_memory import layout, org, sync
from agent_memory.cli import main
from agent_memory.home import BINDING_FILE, ENV_COMPAT_HOME, ENV_HOME
from agent_memory.startup import (
    DEFAULT_MAX_CHARS,
    MISSING,
    READABLE,
    UNREADABLE,
    build_manifest,
    render_codex_hook,
    render_text,
)

not_root = pytest.mark.skipif(os.geteuid() == 0, reason="permission bits ignored as root")

ORDER = [
    "projects/demo/memory/project_facts.md",
    "projects/demo/memory/decision_log.md",
    "projects/demo/memory/open_threads.md",
    "projects/demo/memory/known_debt.md",
    "org-memory/recent.md",
    "org-memory/decisions.md",
    "org-memory/rules.md",
]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    org.init(home, project="demo")
    return home


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.delenv(ENV_HOME, raising=False)
    monkeypatch.delenv(ENV_COMPAT_HOME, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_manifest_lists_the_tier_files_in_layout_order(home: Path) -> None:
    manifest = build_manifest(home, runtime="claude", project="demo", home_source="flag", project_source="flag")
    assert list(manifest)[0] == "schema_version" and manifest["schema_version"] == 1
    assert manifest["runtime"] == "claude" and manifest["content_injected"] is False
    assert [entry["relative"] for entry in manifest["files"]] == ORDER
    assert [entry["tier"] for entry in manifest["files"]] == ["project"] * 4 + ["org"] * 3
    for entry in manifest["files"]:
        assert entry["state"] == READABLE and entry["bytes"] > 0 and entry["modified_at_utc"].endswith("Z")
        assert entry["path"] == str(home / entry["relative"])
    assert manifest["bytes_total"] == sum(entry["bytes"] for entry in manifest["files"])
    assert manifest["excluded"] == ["org-memory/events/", "org-memory/debriefs/", "projects/demo/memory/archive/"]
    assert manifest["pull"] == {"requested": False, "status": "not_requested", "ok": True, "lines": []}
    assert manifest["sync"] == {"marker": False, "last_commit_at_utc": None}
    assert manifest["warnings"] == [] and manifest["result"] == "ok"
    assert manifest["generated_at_utc"].endswith("Z")


@not_root
def test_missing_and_unreadable_files_are_named_not_read(home: Path) -> None:
    (home / "projects" / "demo" / "memory" / "known_debt.md").unlink()
    rules = home / "org-memory" / "rules.md"
    rules.chmod(0)
    if os.access(rules, os.R_OK):
        rules.chmod(0o644)
        pytest.skip("this user reads files regardless of their mode bits")
    try:
        manifest = build_manifest(home, runtime="claude", project="demo")
    finally:
        rules.chmod(0o644)
    states = {entry["relative"]: entry["state"] for entry in manifest["files"]}
    assert states["projects/demo/memory/known_debt.md"] == MISSING
    assert states["org-memory/rules.md"] == UNREADABLE
    assert manifest["result"] == "degraded"
    assert any(warning.startswith("projects/demo/memory/known_debt.md: missing") for warning in manifest["warnings"])
    assert any(warning.startswith("org-memory/rules.md: unreadable (") for warning in manifest["warnings"])


def test_a_directory_at_a_file_slot_is_unreadable(home: Path) -> None:
    recent = home / "org-memory" / "recent.md"
    recent.unlink()
    recent.mkdir()
    manifest = build_manifest(home, runtime="claude", project="demo")
    entry = next(item for item in manifest["files"] if item["name"] == "recent.md")
    assert entry["state"] == UNREADABLE and entry["error"] == "not a regular file"


def test_no_project_lists_the_org_files_with_a_warning(home: Path) -> None:
    manifest = build_manifest(home, runtime="claude")
    assert [entry["relative"] for entry in manifest["files"]] == ORDER[4:]
    assert manifest["project"] is None and manifest["project_source"] is None
    assert manifest["excluded"] == ["org-memory/events/", "org-memory/debriefs/"]
    assert manifest["warnings"] == [
        "no project resolved; pass --project or bind the repository with `agent-memory init --repo .`"
    ]


def test_a_bad_project_name_is_a_warning_not_a_crash(home: Path) -> None:
    manifest = build_manifest(home, runtime="claude", project="../escape")
    assert manifest["project"] is None
    assert [entry["relative"] for entry in manifest["files"]] == ORDER[4:]
    assert manifest["warnings"][0].startswith("project '../escape':")


def test_a_missing_home_is_all_missing(tmp_path: Path) -> None:
    manifest = build_manifest(tmp_path / "nope", runtime="claude", project="demo")
    assert {entry["state"] for entry in manifest["files"]} == {MISSING}
    assert manifest["warnings"][0].endswith("is not a directory; every file is missing")
    assert manifest["result"] == "degraded"


def test_unknown_runtime_is_rejected(home: Path) -> None:
    with pytest.raises(ValueError, match="unknown runtime"):
        build_manifest(home, runtime="vim")


# --- the pull ---------------------------------------------------------------


def test_pull_runs_and_reports_the_sync(home: Path, tmp_path: Path, git_env: None) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    assert main(["enable", "--home", str(home), "--remote", str(remote)]) == 0
    manifest = build_manifest(home, runtime="claude", project="demo", pull=True)
    assert manifest["pull"] == {"requested": True, "status": "up_to_date", "ok": True, "lines": ["memory pull: already synced."]}
    assert manifest["sync"]["marker"] is True and manifest["sync"]["last_commit_at_utc"].endswith("Z")
    assert manifest["result"] == "ok"
    text = render_text(manifest)
    assert "memory pull: already synced." in text and "memory sync: last commit " in text


def test_pull_without_the_marker_is_silent(home: Path) -> None:
    manifest = build_manifest(home, runtime="claude", project="demo", pull=True)
    assert manifest["pull"] == {"requested": True, "status": "not_configured", "ok": True, "lines": []}
    assert manifest["warnings"] == []
    assert "memory pull: sync is not enabled for this home; skipped." in render_text(manifest)


def test_failed_pull_is_a_warning_not_a_failure(home: Path, tmp_path: Path, git_env: None) -> None:
    assert main(["enable", "--home", str(home), "--remote", str(tmp_path / "missing.git")]) == 1
    manifest = build_manifest(home, runtime="claude", project="demo", pull=True)
    assert manifest["pull"]["requested"] is True and manifest["pull"]["ok"] is False
    assert manifest["pull"]["status"] == "fetch_failed"
    assert manifest["result"] == "degraded"
    assert any("local memory may be stale" in warning for warning in manifest["warnings"])
    # The retained local files are still described.
    assert [entry["state"] for entry in manifest["files"]] == [READABLE] * 7
    assert manifest["bytes_total"] == sum((home / relative).stat().st_size for relative in ORDER)


def test_pull_runs_before_the_files_are_inspected(home: Path, tmp_path: Path, git_env: None) -> None:
    """Sizes, times and states describe the tree the pull left: an updated, an added and a removed file."""
    updated, added, removed = ORDER[0], ORDER[2], ORDER[3]
    (home / added).unlink()  # absent locally until the peer publishes it
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    assert main(["enable", "--home", str(home), "--remote", str(remote)]) == 0
    peer = tmp_path / "peer"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(peer)], check=True)
    (peer / updated).write_text("UPDATED AFTER PULL\n", encoding="utf-8")
    (peer / added).write_text("ADDED AFTER PULL\n", encoding="utf-8")
    (peer / removed).unlink()
    subprocess.run(["git", "add", "-A", "projects"], cwd=str(peer), check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "peer changes"], cwd=str(peer), check=True)
    subprocess.run(["git", "push", "--quiet"], cwd=str(peer), check=True)
    before = (home / updated).stat().st_size
    assert before != len("UPDATED AFTER PULL\n") and (home / removed).is_file() and not (home / added).exists()

    manifest = build_manifest(home, runtime="claude", project="demo", pull=True)
    assert manifest["pull"]["status"] == "synced" and manifest["pull"]["ok"] is True
    by_path = {entry["relative"]: entry for entry in manifest["files"]}
    assert by_path[updated]["state"] == READABLE and by_path[updated]["bytes"] == len("UPDATED AFTER PULL\n")
    assert by_path[added]["state"] == READABLE and by_path[added]["bytes"] == len("ADDED AFTER PULL\n")
    assert by_path[removed]["state"] == MISSING and by_path[removed]["bytes"] == 0
    assert by_path[updated]["bytes"] == (home / updated).stat().st_size
    assert manifest["bytes_total"] == sum(entry["bytes"] for entry in manifest["files"])
    assert [warning for warning in manifest["warnings"] if removed in warning] == [f"{removed}: missing"]


def test_pull_error_is_reported_not_raised(home: Path) -> None:
    (home / layout.MARKER_FILE).write_text("", encoding="utf-8")  # the marker without a repository
    manifest = build_manifest(home, runtime="claude", project="demo", pull=True)
    assert manifest["pull"]["status"] == "error" and manifest["pull"]["ok"] is False
    assert manifest["pull"]["lines"][0].startswith("memory pull: ")
    assert manifest["sync"]["marker"] is True and manifest["sync"]["last_commit_at_utc"] is None
    assert sync.is_configured(home)


# --- rendering ---------------------------------------------------------------


def test_text_lists_files_in_order_and_claims_no_read(home: Path) -> None:
    manifest = build_manifest(home, runtime="claude", project="demo", home_source="flag", project_source="flag")
    text = render_text(manifest)
    lines = text.splitlines()
    assert lines[0] == f"agent-memory startup (claude): home {home} (flag), project demo (flag)"
    numbered = [line for line in lines if line.strip()[:2] in {f"{n}." for n in range(1, 8)}]
    assert [line.split()[1].rstrip(":") for line in numbered] == ORDER
    assert all(": readable, " in line for line in numbered)
    assert "no content is injected" in text
    assert "not read by default" in text
    assert "Excluded by default: org-memory/events/, org-memory/debriefs/, projects/demo/memory/archive/" in text
    assert "Warnings:" not in text
    assert len(text) <= DEFAULT_MAX_CHARS


def test_text_is_cut_at_the_budget_with_a_notice(home: Path) -> None:
    manifest = build_manifest(home, runtime="claude", project="demo")
    text = render_text(manifest, max_chars=200)
    assert len(text) <= 200
    assert text.endswith("--json` for the whole manifest]\n")
    assert render_text(manifest, max_chars=100_000) == render_text(manifest)


@pytest.mark.parametrize("budget", [0, 1, 5, 6, 50, 100, 126, 127, 128, 150])
def test_every_budget_bounds_the_text_notice_included(home: Path, budget: int) -> None:
    manifest = build_manifest(home, runtime="claude", project="demo")
    text = render_text(manifest, max_chars=budget)
    assert len(text) <= budget
    assert text == render_text(manifest)[:budget] or text.endswith(("[cut]\n"[:budget], "for the whole manifest]\n"))
    envelope = render_codex_hook(manifest, max_chars=budget)
    assert len(envelope["hookSpecificOutput"]["additionalContext"]) <= budget
    assert render_text(manifest, max_chars=-1) == ""


def test_warnings_render_as_a_section(home: Path) -> None:
    manifest = build_manifest(home, runtime="claude")
    text = render_text(manifest)
    assert "Warnings:\n  - no project resolved;" in text


def test_codex_envelope_carries_the_text(home: Path) -> None:
    manifest = build_manifest(home, runtime="codex", project="demo")
    envelope = render_codex_hook(manifest)
    assert envelope == {
        "continue": True,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": render_text(manifest)},
    }
    degraded = render_codex_hook(build_manifest(home, runtime="codex"))
    assert "degraded" in degraded["systemMessage"]
    assert "Warnings:" in degraded["hookSpecificOutput"]["additionalContext"]


# --- the command line --------------------------------------------------------


def test_cli_startup_text_json_and_codex_shapes(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["startup", "--runtime", "claude", "--home", str(home), "--project", "demo"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"agent-memory startup (claude): home {home} (flag), project demo (flag)\n")
    assert main(["startup", "--runtime", "claude", "--home", str(home), "--project", "demo", "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["schema_version"] == 1 and manifest["project"] == "demo" and manifest["home_source"] == "flag"
    assert main(["startup", "--runtime", "codex", "--home", str(home), "--project", "demo"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["continue"] is True and envelope["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert main(["startup", "--runtime", "claude", "--home", str(home), "--project", "demo", "--max-chars", "150"]) == 0
    assert len(capsys.readouterr().out) <= 150
    for argv in (["startup"], ["startup", "--runtime", "vim"]):
        with pytest.raises(SystemExit) as exit_info:
            main(argv)
        assert exit_info.value.code == 2


@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_cli_startup_rejects_a_budget_below_the_minimum(home: Path, value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["startup", "--runtime", "claude", "--home", str(home), "--max-chars", value])
    assert exit_info.value.code == 2
    assert "--max-chars" in capsys.readouterr().err


# --- the project, when a flag or an environment variable chose the home ------


def _bind(repo: Path, home: Path, project: str = "demo") -> None:
    repo.mkdir(exist_ok=True)
    (repo / BINDING_FILE).write_text(json.dumps({"schema_version": 1, "project": project, "home": str(home)}), encoding="utf-8")


def _mark(repo: Path, home: Path) -> None:
    workspace = home / "projects" / "demo" / "workspace.json"
    workspace.write_text("{}\n", encoding="utf-8")
    repo.mkdir(exist_ok=True)
    (repo / ".oacp").symlink_to(workspace)


@pytest.mark.parametrize("chooser", ["flag", ENV_HOME, ENV_COMPAT_HOME])
@pytest.mark.parametrize("kind", ["binding", "marker"])
def test_cli_startup_keeps_the_project_when_the_home_comes_from_elsewhere(
    home: Path, isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], chooser: str, kind: str
) -> None:
    repo = isolated / "repo"
    (_bind if kind == "binding" else _mark)(repo, home)
    os.chdir(repo)
    argv = ["startup", "--runtime", "claude", "--json"]
    if chooser == "flag":
        argv += ["--home", str(home)]
    else:
        monkeypatch.setenv(chooser, str(home))
    assert main(argv) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["home_source"] == ("flag" if chooser == "flag" else f"env:{chooser}")
    assert manifest["project"] == "demo" and manifest["project_source"].startswith(f"{kind}:")
    assert [entry["tier"] for entry in manifest["files"]].count("project") == 4
    assert manifest["warnings"] == []


def test_cli_startup_borrows_no_project_from_a_binding_for_another_home(
    home: Path, isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = isolated / "other-home"
    org.init(other, project="demo")
    repo = isolated / "repo"
    _bind(repo, other)
    os.chdir(repo)
    monkeypatch.setenv(ENV_HOME, str(home))
    assert main(["startup", "--runtime", "claude", "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["home"] == str(home) and manifest["project"] is None and manifest["project_source"] is None
    assert [entry["tier"] for entry in manifest["files"]] == ["org"] * 3
    assert manifest["warnings"][0].startswith(f"binding:{repo / BINDING_FILE} binds this repository to {other}, not to {home}")
    assert "no project resolved" in manifest["warnings"][1]


def test_cli_startup_with_a_flag_home_still_fails_closed_on_a_broken_binding(
    home: Path, isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (isolated / BINDING_FILE).write_text("{broken", encoding="utf-8")
    assert main(["startup", "--runtime", "claude", "--home", str(home)]) == 2
    assert "cannot read binding" in capsys.readouterr().err


def test_cli_startup_takes_the_project_from_the_binding(home: Path, isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = isolated / "repo"
    repo.mkdir()
    (repo / BINDING_FILE).write_text(json.dumps({"schema_version": 1, "project": "demo", "home": str(home)}), encoding="utf-8")
    os.chdir(repo)
    assert main(["startup", "--runtime", "claude", "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["home"] == str(home) and manifest["project"] == "demo"
    assert manifest["home_source"].startswith("binding:") and manifest["project_source"] == manifest["home_source"]
    assert [entry["state"] for entry in manifest["files"]] == [READABLE] * 7


def test_cli_startup_takes_the_project_from_a_workspace_marker(home: Path, isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = home / "projects" / "demo" / "workspace.json"
    workspace.write_text("{}\n", encoding="utf-8")
    repo = isolated / "repo"
    repo.mkdir()
    (repo / ".oacp").symlink_to(workspace)
    os.chdir(repo)
    assert main(["startup", "--runtime", "claude", "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["home"] == str(home.resolve()) and manifest["project"] == "demo"
    assert manifest["home_source"] == f"marker:{repo / '.oacp'}" and manifest["project_source"] == manifest["home_source"]
    assert manifest["warnings"] == []


def test_cli_startup_project_flag_wins_over_the_binding(home: Path, isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (isolated / BINDING_FILE).write_text(json.dumps({"schema_version": 1, "project": "demo", "home": str(home)}), encoding="utf-8")
    assert main(["startup", "--runtime", "claude", "--project", "other", "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["project"] == "other" and manifest["project_source"] == "flag"
    assert [entry["state"] for entry in manifest["files"][:4]] == [MISSING] * 4
