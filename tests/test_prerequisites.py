# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""The prerequisite grammar ``status`` and ``doctor`` share: three failures, three reason codes.

Every failure exits non-zero from both verbs, carries its code in the JSON, and
prints the JSON's message in the table.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List

import pytest

from agent_memory import prerequisites, sync
from agent_memory.cli import main
from agent_memory.git_runner import EXIT_NOT_FOUND, GitResult, run_git
from agent_memory.home import BINDING_FILE, ENV_COMPAT_HOME, ENV_HOME
from agent_memory.prerequisites import CREDENTIAL_HELPER_BLOCKED, GIT_MISSING, HOME_UNRESOLVED

from conftest import FetchBlockedByCredentialHelper, synced_home

VERBS = ("status", "doctor")


def test_the_grammar_is_three_reasons() -> None:
    assert prerequisites.REASONS == (HOME_UNRESOLVED, GIT_MISSING, CREDENTIAL_HELPER_BLOCKED)


def _missing_home(tmp_path: Path, no_git: Callable[[], None], monkeypatch: pytest.MonkeyPatch) -> Path:
    return tmp_path / "nope"


def _home_without_git(tmp_path: Path, no_git: Callable[[], None], monkeypatch: pytest.MonkeyPatch) -> Path:
    home, _ = synced_home(tmp_path)
    no_git()
    return home


def _home_behind_a_blocked_helper(tmp_path: Path, no_git: Callable[[], None], monkeypatch: pytest.MonkeyPatch) -> Path:
    home, _ = synced_home(tmp_path)
    monkeypatch.setattr(sync, "run_git", FetchBlockedByCredentialHelper())
    return home


#: Each reason, the home that produces it, and the arguments that reach it (the helper is judged only on a fetch).
CASES: Dict[str, tuple] = {
    HOME_UNRESOLVED: (_missing_home, []),
    GIT_MISSING: (_home_without_git, []),
    CREDENTIAL_HELPER_BLOCKED: (_home_behind_a_blocked_helper, ["--fetch"]),
}


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("reason", prerequisites.REASONS)
def test_each_failure_exits_non_zero_with_its_reason_in_the_json_and_its_message_in_the_table(
    tmp_path: Path,
    git_env: None,
    no_git: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    reason: str,
) -> None:
    make_home, extra = CASES[reason]
    home = make_home(tmp_path, no_git, monkeypatch)
    argv: List[str] = [verb, "--home", str(home), *(extra if verb == "status" else [])]

    assert main([*argv, "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    readout = data["prerequisites"]
    assert (readout["ok"], readout["reason"]) == (False, reason)
    assert data["ok" if verb == "status" else "has_errors"] is (verb != "status")

    assert main(argv) == 1
    captured = capsys.readouterr()
    table = captured.out + captured.err
    assert readout["message"] in table
    assert readout["remedy"] in table


@pytest.mark.parametrize("verb", VERBS)
def test_a_resolver_failure_is_a_usage_error_that_still_prints_the_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    monkeypatch.delenv(ENV_HOME, raising=False)
    monkeypatch.delenv(ENV_COMPAT_HOME, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / BINDING_FILE).write_text("{", encoding="utf-8")

    assert main([verb, "--json"]) == 2
    readout = json.loads(capsys.readouterr().out)["prerequisites"]
    assert readout["reason"] == HOME_UNRESOLVED
    assert readout["message"].startswith("the memory home could not be resolved: ")
    assert readout["git"] == {"present": None, "version": None}

    assert main([verb]) == 2  # the table path keeps every verb's usage error
    assert "agent-memory: error:" in capsys.readouterr().err


@pytest.mark.parametrize("verb", VERBS)
def test_git_off_path_on_a_home_without_the_marker_is_reported_not_failed(
    tmp_path: Path, no_git: Callable[[], None], capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    no_git()
    assert main([verb, "--home", str(home), "--json"]) == 0
    readout = json.loads(capsys.readouterr().out)["prerequisites"]
    assert readout["ok"] is True
    assert readout["git"] == {"present": False, "version": None}


def test_the_probe_reads_the_version_and_tells_a_missing_git_from_a_failing_one() -> None:
    def answering(code: int, out: str):
        return lambda args, *, cwd, timeout=None: GitResult(code, out)

    assert prerequisites.probe_git(answering(0, "git version 2.39.5 (Apple Git-154)\n")) == prerequisites.GitProbe(
        True, "2.39.5"
    )
    assert prerequisites.probe_git(answering(EXIT_NOT_FOUND, "")) == prerequisites.GitProbe(False)
    assert prerequisites.probe_git(answering(1, "")) == prerequisites.GitProbe(True, "")
    assert prerequisites.probe_git().present is True


@pytest.mark.parametrize("verb", VERBS)
def test_a_git_on_path_that_cannot_be_executed_is_missing_git(
    tmp_path: Path, git_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    home, _ = synced_home(tmp_path)
    shim = tmp_path / "unrunnable-bin"
    shim.mkdir()
    (shim / "git").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")  # no execute bit
    monkeypatch.setenv("PATH", str(shim))
    assert main([verb, "--home", str(home), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["prerequisites"]["reason"] == GIT_MISSING


def test_the_probe_runs_git_outside_the_home() -> None:
    # A missing or unreadable home must not read as missing git, so the probe never runs git in it.
    calls: List[Path] = []

    def recording(args, *, cwd, timeout=None):
        calls.append(cwd)
        return run_git(args, cwd=cwd, timeout=timeout)

    assert prerequisites.probe_git(recording).present is True
    assert calls == [Path(Path.cwd().anchor)]


def test_the_helper_is_judged_only_when_a_fetch_ran() -> None:
    blocked = sync.GitState(
        has_remote=True, has_upstream=True, fetch_failed=True, fetch_output=FetchBlockedByCredentialHelper.OUTPUT
    )
    git = prerequisites.GitProbe(True, "2.55.0")
    assert prerequisites.assess(git, configured=True, state=blocked, fetched=True).reason == CREDENTIAL_HELPER_BLOCKED
    unchecked = prerequisites.assess(git, configured=True, state=blocked, fetched=False)
    assert unchecked.ok
    assert unchecked.to_json()["credential_helper"] == {"checked": False, "blocked": None}
