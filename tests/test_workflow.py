# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory capture`` and ``recall``, and the two-session recall proof.

The capture entry and its placement (newest first, one heading per UTC day),
what capture refuses (a missing tier, a linked component below the home, an
empty decision), the
bounded read in manifest order with its budget and exclusions, the shipped
workflow text, the CLI shapes, and the acceptance bar of the workflow issue:
two fresh sessions in a scratch repository with no git and no credentials,
the first recording a seeded decision, the second receiving the bounded
context through the installed hook and reading the decision back.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_memory import layout, org, startup, workflow
from agent_memory.cli import main
from agent_memory.home import ENV_COMPAT_HOME, ENV_HOME
from agent_memory.setup import SPECS

NOW = dt.datetime(2026, 9, 6, 7, 2, 11, tzinfo=dt.timezone.utc)
DECISION = "Use SQLite for the local cache."


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    org.init(home, project="demo")
    return home


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No home from the environment, and a cwd with no binding or marker above it."""
    monkeypatch.delenv(ENV_HOME, raising=False)
    monkeypatch.delenv(ENV_COMPAT_HOME, raising=False)
    monkeypatch.delenv(workflow.DEFAULT_AGENT_ENV, raising=False)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


def _log(home: Path, project: str = "demo") -> Path:
    return layout.project_memory_dir(home, project) / workflow.DECISION_FILE


# --- the entry -----------------------------------------------------------------


def test_entry_carries_the_decision_the_reason_and_the_provenance() -> None:
    entry = workflow.format_entry(DECISION, why="it needs no daemon", agent="claude", source="issue #8", now=NOW)
    assert entry == f"- **{DECISION}** Why: it needs no daemon (claude, 2026-09-06T07:02:11Z, source: issue #8)"


def test_entry_omits_what_was_not_given_and_collapses_whitespace() -> None:
    entry = workflow.format_entry("  Use\n  SQLite.  ", why=None, agent="codex", source=None, now=NOW)
    assert entry == "- **Use SQLite.** (codex, 2026-09-06T07:02:11Z)"
    assert workflow.format_entry("x", why="  ", agent="", source="", now=NOW) == "- **x** (unknown, 2026-09-06T07:02:11Z)"


def test_an_empty_decision_is_refused() -> None:
    with pytest.raises(workflow.WorkflowError, match="empty"):
        workflow.format_entry("  \n ", why=None, agent="claude", source=None, now=NOW)


# --- placement -----------------------------------------------------------------


def test_first_entry_opens_a_heading_after_the_template_header() -> None:
    template = "# Decision Log\n\n<!-- One dated entry per decision, newest first: what was decided, and why. -->\n"
    out = workflow.insert_entry(template, "2026-09-06", "- **a**")
    assert out == template + "\n## 2026-09-06\n\n- **a**\n"


def test_a_newer_day_goes_on_top_and_the_same_day_goes_first_under_its_heading() -> None:
    text = "# Decision Log\n\n## 2026-09-05\n\n- **old**\n"
    text = workflow.insert_entry(text, "2026-09-06", "- **a**")
    assert text == "# Decision Log\n\n## 2026-09-06\n\n- **a**\n\n## 2026-09-05\n\n- **old**\n"
    text = workflow.insert_entry(text, "2026-09-06", "- **b**")
    assert text == "# Decision Log\n\n## 2026-09-06\n\n- **b**\n- **a**\n\n## 2026-09-05\n\n- **old**\n"


def test_placement_keeps_a_file_without_a_trailing_newline_or_blank_lines_tidy() -> None:
    assert workflow.insert_entry("# Decision Log", "2026-09-06", "- **a**") == "# Decision Log\n\n## 2026-09-06\n\n- **a**\n"
    out = workflow.insert_entry("# Decision Log\n## 2026-09-05\n- **old**\n", "2026-09-06", "- **a**")
    assert out == "# Decision Log\n\n## 2026-09-06\n\n- **a**\n\n## 2026-09-05\n- **old**\n"


# --- capture -------------------------------------------------------------------


def test_capture_appends_newest_first_with_provenance_and_keeps_the_mode(home: Path) -> None:
    log = _log(home)
    log.chmod(0o600)
    first = workflow.capture(home, "demo", DECISION, why="no daemon", source="issue #8", agent="claude", now=NOW)
    later = NOW + dt.timedelta(days=1)
    second = workflow.capture(home, "demo", "Next day.", agent="codex", now=later)
    text = log.read_text(encoding="utf-8")
    assert text.index("## 2026-09-07") < text.index("## 2026-09-06")
    assert first["entry"] in text and second["entry"] in text
    assert first["written"] and first["path"] == str(log) and first["date"] == "2026-09-06" and first["agent"] == "claude"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert [p.name for p in log.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_capture_dry_run_composes_the_entry_and_writes_nothing(home: Path) -> None:
    before = _log(home).read_bytes()
    result = workflow.capture(home, "demo", DECISION, agent="claude", now=NOW, dry_run=True)
    assert result["dry_run"] and not result["written"] and result["entry"].startswith(f"- **{DECISION}**")
    assert _log(home).read_bytes() == before


def test_capture_touches_only_the_decision_log(home: Path) -> None:
    memory = layout.project_memory_dir(home, "demo")
    others = {p: p.read_bytes() for p in memory.iterdir() if p.is_file() and p.name != workflow.DECISION_FILE}
    workflow.capture(home, "demo", DECISION, agent="claude", now=NOW)
    assert {p: p.read_bytes() for p in others} == others


def test_capture_refuses_a_missing_tier_a_missing_log_and_a_symlink(home: Path, tmp_path: Path) -> None:
    with pytest.raises(workflow.WorkflowError, match="init --project other"):
        workflow.capture(home, "other", DECISION, agent="claude", now=NOW)
    log = _log(home)
    log.unlink()
    with pytest.raises(workflow.WorkflowError, match="missing"):
        workflow.capture(home, "demo", DECISION, agent="claude", now=NOW)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("# Elsewhere\n", encoding="utf-8")
    log.symlink_to(elsewhere)
    with pytest.raises(workflow.WorkflowError, match="symlink"):
        workflow.capture(home, "demo", DECISION, agent="claude", now=NOW)
    assert elsewhere.read_text(encoding="utf-8") == "# Elsewhere\n"


@pytest.mark.parametrize("component", ["projects", "projects/demo", "projects/demo/memory"])
def test_capture_refuses_a_linked_ancestor_and_leaves_its_target_untouched(home: Path, tmp_path: Path, component: str) -> None:
    linked = home / component
    outside = tmp_path / "outside"
    linked.rename(outside)
    linked.symlink_to(outside, target_is_directory=True)
    before = {p: p.read_bytes() for p in outside.rglob("*") if p.is_file()}
    with pytest.raises(workflow.WorkflowError, match="never writes through a link") as caught:
        workflow.capture(home, "demo", DECISION, agent="claude", now=NOW)
    assert str(linked) in str(caught.value)
    assert {p: p.read_bytes() for p in outside.rglob("*") if p.is_file()} == before
    assert list(outside.rglob(".*.tmp")) == []


def test_capture_accepts_a_home_that_is_itself_a_link(home: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)
    result = workflow.capture(alias, "demo", DECISION, agent="claude", now=NOW)
    assert result["written"] and result["entry"] in _log(home).read_text(encoding="utf-8")


def test_capture_keeps_the_group_and_other_bits_under_a_restrictive_umask(home: Path) -> None:
    log = _log(home)
    log.chmod(0o664)
    previous = os.umask(0o077)
    try:
        workflow.capture(home, "demo", DECISION, agent="claude", now=NOW)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(log.stat().st_mode) == 0o664


def test_capture_default_agent_comes_from_the_hook_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(workflow.DEFAULT_AGENT_ENV, "codex")
    assert workflow.default_agent() == "codex"
    monkeypatch.delenv(workflow.DEFAULT_AGENT_ENV)
    monkeypatch.setenv("USER", "alice")
    assert workflow.default_agent() == "alice"
    monkeypatch.delenv("USER", raising=False)
    assert workflow.default_agent() == "unknown"


# --- recall --------------------------------------------------------------------


def test_recall_prints_the_files_in_manifest_order_with_their_content(home: Path) -> None:
    workflow.capture(home, "demo", DECISION, agent="claude", now=NOW)
    result = workflow.recall(home, project="demo", max_chars=100_000)
    text = result["text"]
    order = [entry["relative"] for entry in result["files"]]
    manifest = startup.build_manifest(home, runtime="claude", project="demo")
    assert order == [entry["relative"] for entry in manifest["files"]]
    positions = [text.index(f"--- {relative} (") for relative in order]
    assert positions == sorted(positions)
    assert DECISION in text and "# Known Debt" in text and "# Org Memory" in text
    assert result["content_injected"] is True and not result["truncated"]
    assert result["excluded"] == manifest["excluded"]
    assert f"Excluded by default: {', '.join(manifest['excluded'])}" in text


def test_recall_never_loads_the_excluded_directories(home: Path) -> None:
    secret = "NEVER LOADED"
    for relative in ("org-memory/events/e.md", "org-memory/debriefs/p/2026/09/20260906-a-1.md", "projects/demo/memory/archive/x.md"):
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret + "\n", encoding="utf-8")
    assert secret not in workflow.recall(home, project="demo", max_chars=100_000)["text"]


@pytest.mark.parametrize("budget", [1, 5, 50, 200, 900])
def test_recall_is_cut_at_the_budget_with_a_notice(home: Path, budget: int) -> None:
    result = workflow.recall(home, project="demo", max_chars=budget)
    assert len(result["text"]) <= budget and result["truncated"]
    if budget > 150:
        assert "recall cut at" in result["text"]


def test_recall_without_a_project_reads_the_org_files_and_warns(home: Path) -> None:
    result = workflow.recall(home, project=None, max_chars=100_000)
    assert [entry["tier"] for entry in result["files"]] == ["org"] * len(layout.ORG.files)
    assert any("no project resolved" in warning for warning in result["warnings"])
    assert "no project" in result["text"].splitlines()[0]


def test_recall_names_a_missing_file_and_reads_the_rest(home: Path) -> None:
    _log(home).unlink()
    result = workflow.recall(home, project="demo", max_chars=100_000)
    states = {entry["relative"]: entry["state"] for entry in result["files"]}
    assert states["projects/demo/memory/decision_log.md"] == startup.MISSING
    assert "--- projects/demo/memory/decision_log.md" not in result["text"]
    assert "--- projects/demo/memory/known_debt.md" in result["text"]
    assert any("decision_log.md: missing" in warning for warning in result["warnings"])


# --- the shipped text ----------------------------------------------------------


@pytest.mark.parametrize("runtime", sorted(SPECS))
def test_workflow_text_is_rendered_from_the_shipped_template(runtime: str) -> None:
    text = workflow.workflow_text(runtime)
    assert text.startswith("---\nname: agent-memory\ndescription: ")
    assert f"# agent-memory workflow for {runtime}" in text
    assert f"agent-memory setup {runtime}" in text and f"--agent {runtime}" in text
    assert "{runtime}" not in text
    assert "agent-memory recall" in text and "agent-memory capture" in text
    for excluded in ("archive/", "events/", "debriefs/"):
        assert excluded in text


# --- the command line ----------------------------------------------------------


def test_cli_capture_and_recall(home: Path, isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["capture", "--home", str(home), "--project", "demo", DECISION, "--why", "no daemon", "--agent", "claude", "--json"])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["written"] and result["project"] == "demo" and result["entry"].startswith(f"- **{DECISION}**")
    assert main(["capture", "--home", str(home), "--project", "demo", "Plain output.", "--agent", "claude"]) == 0
    out = capsys.readouterr().out
    assert "captured" in out and "Plain output." in out
    assert main(["recall", "--home", str(home), "--project", "demo"]) == 0
    text = capsys.readouterr().out
    assert DECISION in text and "Plain output." in text
    assert main(["recall", "--home", str(home), "--project", "demo", "--json", "--max-chars", "300"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["content_injected"] and data["truncated"] and len(data["text"]) <= 300


def test_cli_capture_needs_a_project(home: Path, isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["capture", "--home", str(home), DECISION]) == 2
    assert "no project resolved" in capsys.readouterr().err


def test_cli_capture_refusals_exit_one_with_the_reason(home: Path, isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["capture", "--home", str(home), "--project", "other", DECISION]) == 1
    assert "init --project other" in capsys.readouterr().err
    assert main(["capture", "--home", str(home), "--project", "demo", "   "]) == 1
    assert "empty" in capsys.readouterr().err


def test_cli_takes_the_project_and_home_from_the_binding(home: Path, isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    repo = isolated / "repo"
    repo.mkdir()
    org.init(home, project="demo", repo=repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv(workflow.DEFAULT_AGENT_ENV, "codex")
    assert main(["capture", DECISION, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["project"] == "demo" and result["home"] == str(home) and result["agent"] == "codex"
    assert main(["recall", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["project"] == "demo" and DECISION in data["text"]


# --- the acceptance bar: two fresh sessions -----------------------------------------


def _tool_on_path(tmp_path: Path) -> Path:
    """A ``agent-memory`` command on PATH that runs this checkout's CLI with this interpreter."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "agent-memory"
    shim.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" -m agent_memory "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir


@pytest.mark.parametrize("runtime", sorted(SPECS))
def test_two_fresh_sessions_recall_a_seeded_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str, capsys: pytest.CaptureFixture[str]) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    monkeypatch.delenv(ENV_HOME, raising=False)
    monkeypatch.delenv(ENV_COMPAT_HOME, raising=False)
    # A scratch repository: no git, no credentials, nothing but the binding setup writes.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    home = tmp_path / "home"
    assert main(["init", "--home", str(home), "--project", "demo", "--repo", str(scratch)]) == 0
    assert main(["setup", runtime, "--home", str(home), "--repo", str(scratch)]) == 0
    capsys.readouterr()
    spec = SPECS[runtime]
    wrapper = scratch / spec.workflow_file
    assert wrapper.read_text(encoding="utf-8") == workflow.workflow_text(runtime)
    assert not (scratch / ".git").exists()

    # Session 1: records the seeded decision through the workflow, from the repository.
    monkeypatch.chdir(scratch)
    env = {**os.environ, "PATH": f"{_tool_on_path(tmp_path)}{os.pathsep}{os.environ.get('PATH', '')}"}
    env.pop(ENV_HOME, None)
    env.pop(ENV_COMPAT_HOME, None)
    env.pop(workflow.DEFAULT_AGENT_ENV, None)
    one = subprocess.run(
        ["agent-memory", "capture", "--agent", runtime, DECISION, "--why", "no daemon", "--source", "session 1"],
        cwd=str(scratch), env=env, capture_output=True, text=True, check=False,
    )
    assert one.returncode == 0, one.stderr
    assert DECISION in _log(home).read_text(encoding="utf-8")

    # Session 2: the installed hook runs at session start and hands the manifest to the runtime ...
    hook = subprocess.run([bash, spec.script_file], cwd=str(scratch), env=env, capture_output=True, text=True, check=False)
    assert hook.returncode == 0, hook.stderr
    context = hook.stdout
    if runtime == startup.RUNTIME_CODEX:
        context = json.loads(context)["hookSpecificOutput"]["additionalContext"]
    assert "project demo" in context and "content is injected" in context
    assert "projects/demo/memory/decision_log.md: readable" in context
    # ... and the bounded read the workflow file prescribes returns the decision with its provenance.
    two = subprocess.run(["agent-memory", "recall"], cwd=str(scratch), env=env, capture_output=True, text=True, check=False)
    assert two.returncode == 0, two.stderr
    assert DECISION in two.stdout and f"({runtime}, 2" in two.stdout and "source: session 1" in two.stdout
    assert two.stdout.index("--- projects/demo/memory/decision_log.md") < two.stdout.index("--- org-memory/recent.md")
