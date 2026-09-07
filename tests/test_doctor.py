# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""``agent-memory doctor``: the two memory categories, row for row against the 0.4.5 golden.

The org-memory checks are setup-level only: directory presence, canonical path
layout, staging leftovers, irregular entries. They never open a record. The
sync checks go through git alone. Neither reads memory content, and neither
changes a byte.
"""

from __future__ import annotations

import builtins
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from agent_memory import doctor, layout, sync
from agent_memory.cli import main
from agent_memory.doctor import Severity, check_memory_sync, check_org_memory, run_doctor
from agent_memory.git_runner import EXIT_TIMEOUT, GitResult, run_git

from conftest import git, synced_home, write

TESTS = Path(__file__).resolve().parent
GOLDEN_TEXT = TESTS / "golden" / "doctor_memory_0.4.5.txt"
GOLDEN_JSON = TESTS / "golden" / "doctor_memory_0.4.5.json"
CASES_DIR = TESTS / "conformance" / "org_memory" / "cases"
CASE_NAMES = sorted(p.name for p in CASES_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))
MEMORY_CATEGORIES = ("Org Memory", "Memory Sync")
DIGITS = re.compile(r"\d+")

not_root = pytest.mark.skipif(os.geteuid() == 0, reason="permission bits ignored as root")


def _rows(category: doctor.Category) -> List[Tuple[str, Severity]]:
    return [(result.name, result.severity) for result in category.results]


def _by_name(category: doctor.Category) -> Dict[str, doctor.Result]:
    return {result.name: result for result in category.results}


def _tree_digest(root: Path) -> Dict[str, str]:
    digest: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.name == ".git" or ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            digest[str(path.relative_to(root))] = f"-> {os.readlink(path)}"
        elif path.is_file():
            digest[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


# --- parity with the 0.4.5 golden -------------------------------------------


def _normalize(text: str) -> str:
    return DIGITS.sub("N", text)


def _golden_categories() -> List[dict]:
    data = json.loads(GOLDEN_JSON.read_text(encoding="utf-8"))
    return [category for category in data["categories"] if category["name"] in MEMORY_CATEGORIES]


def _golden_text_blocks() -> Dict[str, List[str]]:
    blocks: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in GOLDEN_TEXT.read_text(encoding="utf-8").splitlines():
        if line[:3] in doctor.SYMBOL.values() and not line.startswith(" "):
            current = line[4:]
            blocks[current] = [line]
        elif current and line.startswith("    "):
            blocks[current].append(line)
    return {name: block for name, block in blocks.items() if name in MEMORY_CATEGORIES}


def test_the_golden_is_the_0_4_5_memory_doctor_output() -> None:
    # The fixture carries both memory categories, every row ok, and the JSON mirrors the text.
    categories = {category["name"]: category for category in _golden_categories()}
    assert set(categories) == set(MEMORY_CATEGORIES)
    assert [row["name"] for row in categories["Org Memory"]["results"]] == ["debriefs-dir", "debriefs-layout"]
    assert [row["name"] for row in categories["Memory Sync"]["results"]] == [
        "memory-marker", "root-gitignore", "tracked-allowlist", "untracked-memory", "working-tree",
        "sync-state", "remote", "last-commit", "agents-tracked", "memory-overlays",
    ]
    assert all(row["severity"] == "ok" for category in categories.values() for row in category["results"])
    blocks = _golden_text_blocks()
    for name, category in categories.items():
        assert [line[8:] for line in blocks[name][1:]] == [row["message"] for row in category["results"]]


def test_rows_match_the_golden_on_a_home_in_the_golden_state(tmp_path: Path, git_env: None) -> None:
    # Same categories, same row names, severities and messages in the same order; only the counts differ,
    # so digits are normalized on both sides.
    home, _ = synced_home(tmp_path)
    categories = run_doctor(home)
    assert [category.name for category in categories] == list(MEMORY_CATEGORIES)
    for mine, golden in zip(categories, _golden_categories()):
        assert mine.name == golden["name"]
        assert mine.worst_severity.value == golden["worst_severity"]
        assert [(r.name, r.severity.value, _normalize(r.message)) for r in mine.results] == [
            (row["name"], row["severity"], _normalize(row["message"])) for row in golden["results"]
        ]


def test_text_report_matches_the_golden_block_for_block(tmp_path: Path, git_env: None) -> None:
    home, _ = synced_home(tmp_path)
    report = doctor.report(run_doctor(home))
    blocks = _golden_text_blocks()
    mine: Dict[str, List[str]] = {}
    for block in report.rstrip("\n").split("\n\n")[:-1]:
        lines = block.splitlines()
        mine[lines[0][4:]] = lines
    assert set(mine) == set(blocks)
    for name, lines in blocks.items():
        assert [_normalize(line) for line in mine[name]] == [_normalize(line) for line in lines]
    assert report.rstrip("\n").split("\n\n")[-1] == "No issues found."
    assert GOLDEN_TEXT.read_text(encoding="utf-8").rstrip("\n").endswith("No issues found.")


def test_json_report_matches_the_golden_shape(tmp_path: Path, git_env: None) -> None:
    home, _ = synced_home(tmp_path)
    mine = doctor.to_json(run_doctor(home))
    golden = json.loads(GOLDEN_JSON.read_text(encoding="utf-8"))
    assert mine["has_errors"] is golden["has_errors"] is False
    assert mine["memory_lint"] is None
    for category, wanted in zip(mine["categories"], _golden_categories()):
        assert set(category) == set(wanted) == {"name", "worst_severity", "results"}
        assert [set(row) for row in category["results"]] == [set(row) for row in wanted["results"]]


# --- Org Memory: the conformance cases and the unit checks -------------------


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_conformance_case(case_name: str, tmp_path: Path) -> None:
    case = CASES_DIR / case_name
    expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
    root = tmp_path / "home"
    shutil.copytree(case / "org-memory", root / "org-memory")

    cat = check_org_memory(root)

    actual = sorted((r.name, r.severity.value) for r in cat.results if r.severity in (Severity.warn, Severity.error))
    wanted = sorted((finding["name"], finding["severity"]) for finding in expected.get("findings") or [])
    assert actual == wanted, [f"{r.name}:{r.severity.value}:{r.message}" for r in cat.results]
    for finding in expected.get("findings") or []:
        needle = finding.get("message_contains")
        if needle:
            assert any(r.name == finding["name"] and needle in r.message for r in cat.results), (
                f"no {finding['name']} message containing {needle!r}"
            )


def _write_debrief(root: Path, name: str = "20260825-alice-1f3a9c2b.md") -> Path:
    path = root / "org-memory" / "debriefs" / "demo-project" / "2026" / "08" / name
    return write(path, "---\nschema_version: 1\n---\nbody\n")


def _org_rows(root: Path) -> List[Tuple[str, Severity]]:
    return _rows(check_org_memory(root))


def test_canonical_layout_passes(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    _write_debrief(root)
    assert ("debriefs-layout", Severity.ok) in _org_rows(root)


def test_uninitialized_store_is_a_skip_with_the_init_hint(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    cat = check_org_memory(root)
    assert _rows(cat) == [("org-memory-dir", Severity.skip)]
    assert cat.results[0].fix_hint == "Run: agent-memory org init"


@not_root
def test_content_is_never_opened(tmp_path: Path) -> None:
    # Setup-only contract: a record whose CONTENT is unreadable is still a clean setup.
    root = tmp_path / "home"
    root.mkdir()
    record = _write_debrief(root)
    os.chmod(record, 0o000)
    try:
        rows = _org_rows(root)
    finally:
        os.chmod(record, 0o644)
    assert ("debriefs-layout", Severity.ok) in rows
    assert not any(name == "debriefs-unreadable" for name, _ in rows)


def test_staging_artifact_reported(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    real = _write_debrief(root)
    (real.parent / ".stage.20260825-alice-1f3a9c2b.md.a1b2").write_text("partial", encoding="utf-8")
    assert ("debriefs-staging", Severity.warn) in _org_rows(root)


def test_symlinked_record_flagged(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    real = _write_debrief(root)
    (real.parent / "20260825-alice-99zz00aa.md").symlink_to(real)
    rows = _org_rows(root)
    assert ("debriefs-irregular", Severity.error) in rows
    # The regular record still passes the layout check.
    assert ("debriefs-layout", Severity.ok) in rows


def test_symlinked_directory_flagged_and_not_traversed(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    _write_debrief(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside / "2026" / "08" / "20260825-mallory-00aa11bb.md", "foreign\n")
    (root / "org-memory" / "debriefs" / "linked-project").symlink_to(outside, target_is_directory=True)
    cat = check_org_memory(root)
    assert ("debriefs-irregular", Severity.error) in _rows(cat)
    assert "1 debrief file(s)" in _by_name(cat)["debriefs-layout"].message


@not_root
def test_unreadable_directory_is_not_a_clean_empty_store(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    real = _write_debrief(root)
    blocked = real.parent.parent.parent  # demo-project/
    os.chmod(blocked, 0o000)
    try:
        cat = check_org_memory(root)
    finally:
        os.chmod(blocked, 0o755)
    assert ("debriefs-unreadable", Severity.error) in _rows(cat)
    assert not any(r.name == "debriefs-layout" and "empty store" in r.message for r in cat.results)


@not_root
def test_a_store_that_cannot_be_inspected_is_an_error_not_uninitialized(tmp_path: Path) -> None:
    # org-memory/ exists but cannot be entered: the debriefs probe is denied, which is neither
    # "not initialized" nor "missing debriefs/" and never an empty store.
    root = tmp_path / "home"
    root.mkdir()
    _write_debrief(root)
    org_memory = root / layout.ORG.pattern
    os.chmod(org_memory, 0o000)
    try:
        cat = check_org_memory(root)
    finally:
        os.chmod(org_memory, 0o755)
    assert _rows(cat) == [("debriefs-dir", Severity.error)]
    assert "could not be inspected" in cat.results[0].message
    assert "Permission denied" in cat.results[0].message


def test_directory_classification_failure_surfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An is_symlink failure on a directory entry is reported, never raised.
    root = tmp_path / "home"
    root.mkdir()
    _write_debrief(root)
    real_is_symlink = Path.is_symlink

    def flaky(self: Path) -> bool:
        if self.name == "demo-project":
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", flaky)
    cat = check_org_memory(root)
    assert ("debriefs-unreadable", Severity.error) in _rows(cat)
    assert not any(r.name == "debriefs-layout" and "empty store" in r.message for r in cat.results)


def test_record_classification_failure_surfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A stat failure while classifying a record lands in the unreadable row; other records still pass.
    root = tmp_path / "home"
    root.mkdir()
    _write_debrief(root)
    victim = _write_debrief(root, "20260825-bob-77aa88bb.md")
    real_stat = Path.stat

    def flaky(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self.name == victim.name:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    rows = _org_rows(root)
    assert ("debriefs-unreadable", Severity.error) in rows
    assert ("debriefs-layout", Severity.ok) in rows


# --- Memory Sync ------------------------------------------------------------


class ScriptedRunner:
    """Answers git calls from a table, records every call with its timeout, and never touches a repository."""

    def __init__(self, answers: Dict[Tuple[str, ...], Tuple[int, str]]) -> None:
        self.answers = answers
        self.calls: List[Tuple[Tuple[str, ...], Optional[float]]] = []

    def __call__(self, args: Sequence[str], *, cwd: Path, timeout: Optional[float] = None) -> GitResult:
        call = tuple(args)
        self.calls.append((call, timeout))
        code, out = self.answers.get(call, (0, ""))
        return GitResult(code, out if code == 0 else "", "" if code == 0 else out)

    def timed(self, *prefix: str) -> List[Optional[float]]:
        return [timeout for call, timeout in self.calls if call[: len(prefix)] == prefix]


def _scripted_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    write(home / layout.MARKER_FILE, "memory sync enabled\n")
    write(home / layout.GITIGNORE_FILE, layout.gitignore_text())
    return home


def _clean(home: Path, **overrides: Tuple[int, str]) -> Dict[Tuple[str, ...], Tuple[int, str]]:
    return {**CLEAN_ANSWERS, ("rev-parse", "--show-toplevel"): (0, str(home)), **{tuple(k.split()): v for k, v in overrides.items()}}


CLEAN_ANSWERS: Dict[Tuple[str, ...], Tuple[int, str]] = {
    ("rev-parse", "--is-inside-work-tree"): (0, "true"),
    ("ls-files",): (0, f"{layout.GITIGNORE_FILE}\n{layout.MARKER_FILE}"),
    ("ls-files", "--others", "--exclude-standard"): (0, ""),
    ("status", "--porcelain"): (0, ""),
    ("remote",): (0, "origin"),
    ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (0, "origin/main"),
    ("fetch", "--quiet"): (0, ""),
    ("rev-list", "--left-right", "--count", "HEAD...origin/main"): (0, "0 0"),
    ("rev-parse", "--verify", "HEAD"): (1, ""),
}


def test_memory_sync_not_configured(tmp_path: Path) -> None:
    cat = check_memory_sync(tmp_path)
    assert cat.name == "Memory Sync"
    assert _rows(cat) == [("memory-marker", Severity.skip)]
    assert "not configured" in cat.results[0].message
    assert cat.results[0].fix_hint == "Run: agent-memory enable [--remote URL]"


def test_memory_sync_marker_without_a_repository_stops_at_a_warning(tmp_path: Path, git_env: None) -> None:
    home = _scripted_home(tmp_path)
    cat = check_memory_sync(home)
    assert _rows(cat) == [("memory-marker", Severity.ok), ("memory-git", Severity.warn)]
    assert "not a git repository" in cat.results[1].message


def test_memory_sync_a_home_inside_another_repository_is_not_read_as_that_repository(tmp_path: Path, git_env: None) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    git("init", "--quiet", cwd=outer)
    write(outer / "unrelated.txt", "dirty outer tree\n")
    home = outer / "home"
    write(home / layout.MARKER_FILE, "memory sync enabled\n")
    write(home / layout.GITIGNORE_FILE, layout.gitignore_text())

    cat = check_memory_sync(home)

    assert _rows(cat) == [("memory-marker", Severity.ok), ("memory-git", Severity.warn)]
    assert str(outer.resolve()) in cat.results[1].message
    assert "root of its own repository" in cat.results[1].message


def test_memory_sync_warns_for_tracked_agent_state(tmp_path: Path, git_env: None) -> None:
    root = tmp_path / "home"
    root.mkdir()
    git("init", "--quiet", cwd=root)
    write(root / layout.MARKER_FILE, "marker\n")
    write(root / layout.GITIGNORE_FILE, layout.gitignore_text())
    write(root / "projects" / "demo" / "agents" / "codex" / "status.yaml", "busy\n")
    git("add", "-f", layout.MARKER_FILE, layout.GITIGNORE_FILE, "projects/demo/agents/codex/status.yaml", cwd=root)

    cat = check_memory_sync(root)
    messages = "\n".join(result.message for result in cat.results)
    assert "tracked file(s) outside memory allowlist" in messages
    assert "agents/ file(s) tracked" in messages


def test_memory_sync_warns_for_escaping_overlay(tmp_path: Path, git_env: None) -> None:
    root = tmp_path / "home"
    root.mkdir()
    git("init", "--quiet", cwd=root)
    write(root / layout.MARKER_FILE, "marker\n")
    write(root / layout.GITIGNORE_FILE, layout.gitignore_text())
    write(root / "projects" / "demo" / "memory" / ".gitignore", "!../agents/**\n")

    overlay = _by_name(check_memory_sync(root))["memory-overlays"]
    assert overlay.severity is Severity.warn
    assert "escape memory" in overlay.message


def test_memory_sync_does_not_report_agents_clean_when_ls_files_fails(tmp_path: Path) -> None:
    home = _scripted_home(tmp_path)
    runner = ScriptedRunner({**_clean(home), ("ls-files",): (1, "boom")})
    results = _by_name(check_memory_sync(home, runner=runner))
    assert results["tracked-allowlist"].severity is Severity.warn
    assert "boom" in results["tracked-allowlist"].message
    assert "agents-tracked" not in results


def test_memory_sync_fetch_carries_the_network_timeout(tmp_path: Path) -> None:
    home = _scripted_home(tmp_path)
    runner = ScriptedRunner(_clean(home))
    check_memory_sync(home, runner=runner)
    assert runner.timed("fetch", "--quiet") == [sync.NETWORK_TIMEOUT_SECONDS]
    assert [call for call, timeout in runner.calls if timeout is not None] == [("fetch", "--quiet")]


def test_memory_sync_reports_a_fetch_timeout_as_unreachable(tmp_path: Path) -> None:
    home = _scripted_home(tmp_path)
    runner = ScriptedRunner({**_clean(home), ("fetch", "--quiet"): (EXIT_TIMEOUT, "git fetch --quiet: timed out after 30s")})
    results = _by_name(check_memory_sync(home, runner=runner))
    assert results["sync-state"].severity is Severity.warn
    assert "timed out" in results["sync-state"].message
    assert (results["remote"].severity, results["remote"].message) == (Severity.warn, "remote — not reachable")


def test_memory_sync_a_failed_status_readout_is_a_warning_not_a_pass(tmp_path: Path) -> None:
    home = _scripted_home(tmp_path)
    runner = ScriptedRunner({**_clean(home), ("status", "--porcelain"): (128, "fatal: index locked")})
    results = _by_name(check_memory_sync(home, runner=runner))
    assert results["working-tree"].severity is Severity.warn
    assert "index locked" in results["working-tree"].message
    assert "sync-state" not in results and "remote" not in results


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ({("remote",): (0, "")}, ("local-only; no remote configured", Severity.ok, Severity.skip)),
        (
            {("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (128, "")},
            ("remote exists but no upstream branch is configured", Severity.warn, Severity.ok),
        ),
        ({("rev-list", "--left-right", "--count", "HEAD...origin/main"): (0, "2 1")},
         ("DIVERGED from upstream (2 ahead, 1 behind)", Severity.warn, Severity.ok)),
        ({("rev-list", "--left-right", "--count", "HEAD...origin/main"): (0, "0 3")},
         ("BEHIND upstream by 3 commit(s)", Severity.warn, Severity.ok)),
        ({("rev-list", "--left-right", "--count", "HEAD...origin/main"): (0, "4 0")},
         ("ahead by 4 unpushed commit(s)", Severity.warn, Severity.ok)),
        ({}, ("synced with upstream", Severity.ok, Severity.ok)),
    ],
    ids=["local-only", "no-upstream", "diverged", "behind", "ahead", "synced"],
)
def test_memory_sync_state_rows(
    tmp_path: Path, answers: Dict[Tuple[str, ...], Tuple[int, str]], expected: Tuple[str, Severity, Severity]
) -> None:
    home = _scripted_home(tmp_path)
    results = _by_name(check_memory_sync(home, runner=ScriptedRunner({**_clean(home), **answers})))
    text, sync_severity, remote_severity = expected
    assert results["sync-state"].message == f"sync state — {text}"
    assert results["sync-state"].severity is sync_severity
    assert results["remote"].severity is remote_severity


def test_memory_sync_last_commit_age(tmp_path: Path, git_env: None) -> None:
    home, _ = synced_home(tmp_path)
    fresh = _by_name(check_memory_sync(home))["last-commit"]
    assert (fresh.severity, fresh.message) == (Severity.ok, "last commit — fresh (0 day(s) old)")
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=doctor.STALE_MEMORY_DAYS + 1)
    stale = _by_name(check_memory_sync(home, now=later))["last-commit"]
    assert stale.severity is Severity.warn
    assert stale.message == f"last commit — stale ({doctor.STALE_MEMORY_DAYS + 1} day(s) old)"


def test_memory_sync_root_gitignore_states(tmp_path: Path, git_env: None) -> None:
    home, _ = synced_home(tmp_path)
    path = home / layout.GITIGNORE_FILE

    assert _by_name(check_memory_sync(home))["root-gitignore"].message == ".gitignore — canonical memory allowlist"

    path.write_text(layout.gitignore_text() + "*.swp\n.DS_Store\n", encoding="utf-8")
    managed = _by_name(check_memory_sync(home))["root-gitignore"]
    assert managed.severity is Severity.ok
    assert managed.message == ".gitignore — canonical memory allowlist as a managed block; 2 other line(s) kept"

    path.write_text("*\n", encoding="utf-8")
    drifted = _by_name(check_memory_sync(home))["root-gitignore"]
    assert (drifted.severity, drifted.message) == (Severity.warn, ".gitignore — drifted from canonical memory allowlist")

    path.unlink()
    missing = _by_name(check_memory_sync(home))["root-gitignore"]
    assert (missing.severity, missing.message) == (Severity.warn, ".gitignore — missing canonical memory allowlist")


def test_the_doctor_opens_only_the_allowlist_files(tmp_path: Path, git_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # Memory content is never opened: with every file open spied on, a full run touches the root
    # .gitignore and the project overlays and nothing else under the home (git reads in its own process).
    home, _ = synced_home(tmp_path)
    write(home / "projects" / "demo" / "memory" / "open_threads.md", "secret\n")
    write(home / "projects" / "demo" / "memory" / ".gitignore", "!kept/\n")
    git("add", "projects", cwd=home)
    git("commit", "--quiet", "-m", "project memory", cwd=home)
    git("push", "--quiet", cwd=home)
    opened: List[Path] = []
    real_path_open, real_io_open = Path.open, io.open

    def spy_path_open(self: Path, *args: object, **kwargs: object):
        opened.append(self)
        return real_path_open(self, *args, **kwargs)

    def spy_io_open(file: object, *args: object, **kwargs: object):
        if isinstance(file, (str, os.PathLike)):
            opened.append(Path(file))
        return real_io_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_path_open)
    monkeypatch.setattr(io, "open", spy_io_open)
    monkeypatch.setattr(builtins, "open", spy_io_open)

    categories = run_doctor(home)

    assert all(result.severity is Severity.ok for category in categories for result in category.results)
    touched = sorted({path.resolve() for path in opened if home.resolve() in path.resolve().parents})
    assert touched == [home.resolve() / layout.GITIGNORE_FILE, home.resolve() / "projects" / "demo" / "memory" / ".gitignore"]


@not_root
def test_memory_sync_an_unreadable_overlay_is_a_warning_not_a_pass(tmp_path: Path, git_env: None) -> None:
    home, _ = synced_home(tmp_path)
    overlay = write(home / "projects" / "demo" / "memory" / ".gitignore", "!kept/\n")
    os.chmod(overlay, 0o000)
    try:
        row = _by_name(check_memory_sync(home))["memory-overlays"]
    finally:
        os.chmod(overlay, 0o644)
    assert row.severity is Severity.warn
    assert "could not be inspected" in row.message
    assert "projects/demo/memory/.gitignore: Permission denied" in row.message


@not_root
@pytest.mark.parametrize("denied", ["projects", "projects/demo", "projects/demo/memory"])
def test_memory_sync_an_overlay_behind_a_denied_directory_is_never_counted_safe(
    tmp_path: Path, git_env: None, denied: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # The discovery, not only the read, can be denied: projects/, a project directory, or its
    # memory directory without search permission hides an escaping overlay from a glob. The row
    # says the check is incomplete; once access is restored the same home reports the escape.
    home, _ = synced_home(tmp_path)
    write(home / "projects" / "demo" / "memory" / ".gitignore", "!../../private\n")
    blocked = home / denied
    os.chmod(blocked, 0o000)
    try:
        row = _by_name(check_memory_sync(home))["memory-overlays"]
        exit_code = main(["doctor", "--home", str(home), "--json"])
    finally:
        os.chmod(blocked, 0o755)
    assert row.severity is Severity.warn
    assert "could not be inspected (overlay check incomplete)" in row.message
    assert "Permission denied" in row.message
    assert "safe" not in row.message
    assert exit_code == 0
    reported = next(
        r for c in json.loads(capsys.readouterr().out)["categories"] for r in c["results"] if r["name"] == "memory-overlays"
    )
    assert reported["severity"] == "warn"

    restored = _by_name(check_memory_sync(home))["memory-overlays"]
    assert restored.severity is Severity.warn
    assert restored.message == "memory .gitignore overlays can escape memory/**: projects/demo/memory/.gitignore: !../../private"


def test_memory_sync_overlay_discovery_counts_only_project_memory_overlays(tmp_path: Path, git_env: None) -> None:
    # Controls for the bounded walk: no projects/ at all, a file where a project would be, a project
    # without memory/, and a memory/ without an overlay are all "0 safe"; only the real overlay counts.
    home, _ = synced_home(tmp_path)
    assert _by_name(check_memory_sync(home))["memory-overlays"].message == "memory .gitignore overlays — 0 safe"

    write(home / "projects" / "README.md", "not a project\n")
    write(home / "projects" / "bare" / "notes.md", "no memory tier\n")
    (home / "projects" / "quiet" / "memory").mkdir(parents=True)
    assert _by_name(check_memory_sync(home))["memory-overlays"].message == "memory .gitignore overlays — 0 safe"

    write(home / "projects" / "demo" / "memory" / ".gitignore", "!kept/\n")
    assert _by_name(check_memory_sync(home))["memory-overlays"].message == "memory .gitignore overlays — 1 safe"


def test_memory_sync_an_undecodable_root_gitignore_is_a_warning_not_a_crash(tmp_path: Path, git_env: None) -> None:
    home, _ = synced_home(tmp_path)
    (home / layout.GITIGNORE_FILE).write_bytes(b"*\n\xff\n")
    cat = check_memory_sync(home)
    row = _by_name(cat)["root-gitignore"]
    assert (row.severity, row.message) == (Severity.warn, ".gitignore — could not be read: not valid UTF-8")
    assert row.fix_hint == "Re-encode the file as UTF-8"
    assert "memory-overlays" in _by_name(cat)


def test_memory_sync_an_undecodable_overlay_is_a_warning_not_a_crash(tmp_path: Path, git_env: None) -> None:
    home, _ = synced_home(tmp_path)
    overlay = home / "projects" / "demo" / "memory" / ".gitignore"
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"!kept/\n\xff\n")
    row = _by_name(check_memory_sync(home))["memory-overlays"]
    assert row.severity is Severity.warn
    assert "projects/demo/memory/.gitignore: not valid UTF-8" in row.message


@pytest.mark.parametrize(
    ("target", "row_name", "stops"),
    [(layout.MARKER_FILE, "memory-marker", True), (layout.GITIGNORE_FILE, "root-gitignore", False)],
)
def test_memory_sync_a_denied_file_probe_is_a_warning_not_absent(
    tmp_path: Path, git_env: None, monkeypatch: pytest.MonkeyPatch, target: str, row_name: str, stops: bool
) -> None:
    # A stat the doctor is denied is not "missing": the marker row cannot say "not configured" and the
    # root .gitignore row cannot say "missing allowlist" for a file that is there but unreadable.
    home, _ = synced_home(tmp_path)
    denied = home / target
    real_stat = os.stat

    def flaky(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if isinstance(path, (str, os.PathLike)) and Path(path) == denied:
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", flaky)
    cat = check_memory_sync(home)
    row = _by_name(cat)[row_name]
    assert row.severity is Severity.warn
    assert row.message == f"{target} — could not be inspected (setup check incomplete): Permission denied"
    assert (len(cat.results) == 1) is stops


# --- the doctor changes nothing -----------------------------------------------


def test_doctor_repairs_nothing(tmp_path: Path, git_env: None) -> None:
    # A home with every warning and error the doctor knows is byte-identical after the run.
    home, _ = synced_home(tmp_path)
    (home / layout.GITIGNORE_FILE).write_text("*\n", encoding="utf-8")
    write(home / "projects" / "demo" / "memory" / ".gitignore", "!../agents/**\n")
    write(home / "projects" / "demo" / "memory" / "open_threads.md", "unpublished\n")
    debriefs = home / layout.ORG.pattern / "debriefs"
    (debriefs / "demo-project" / "2026" / "09" / ".stage.20260905-alice-1f3a9c2b.md.a1b2").write_text("partial", encoding="utf-8")
    (debriefs / "demo-project" / "2026" / "09" / "20260905-alice-99zz00aa.md").symlink_to(
        debriefs / "demo-project" / "2026" / "09" / "20260905-alice-1f3a9c2b.md"
    )
    write(debriefs / "demo-project" / "20260905-alice-flat0000.md", "misplaced\n")
    before = _tree_digest(home)
    porcelain = git("status", "--porcelain", cwd=home)

    categories = run_doctor(home)

    assert _tree_digest(home) == before
    assert git("status", "--porcelain", cwd=home) == porcelain
    assert doctor.has_errors(categories)
    names = {(category.name, result.name): result.severity for category in categories for result in category.results}
    assert names[("Org Memory", "debriefs-staging")] is Severity.warn
    assert names[("Org Memory", "debriefs-irregular")] is Severity.error
    assert names[("Org Memory", "debriefs-layout")] is Severity.error
    assert names[("Memory Sync", "root-gitignore")] is Severity.warn
    assert names[("Memory Sync", "memory-overlays")] is Severity.warn


def test_an_unpublished_memory_file_is_a_warning_with_the_push_hint(tmp_path: Path, git_env: None) -> None:
    home, _ = synced_home(tmp_path)
    write(home / "projects" / "demo" / "memory" / "open_threads.md", "unpublished\n")
    row = _by_name(check_memory_sync(home))["untracked-memory"]
    assert row.severity is Severity.warn
    assert row.message == "1 untracked memory-shaped file(s): projects/demo/memory/open_threads.md"
    assert row.fix_hint == "Run: agent-memory push"


# --- the report and the command line ----------------------------------------


def test_report_prints_hints_for_every_row_that_is_not_ok(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    text = doctor.report(run_doctor(home))
    assert text == (
        "[-] Org Memory\n"
        "    [-] org-memory/ — not initialized\n"
        "        Run: agent-memory org init\n"
        "\n"
        "[-] Memory Sync\n"
        "    [-] .oacp-memory-repo — not configured; memory sync hooks are disabled\n"
        "        Run: agent-memory enable [--remote URL]\n"
        "\n"
        "No issues found.\n"
    )


def test_report_points_at_memory_lint_only_when_it_is_on_path(tmp_path: Path) -> None:
    categories = run_doctor(tmp_path)
    assert doctor.find_memory_lint(which=lambda name: None) is None
    assert doctor.find_memory_lint(which=lambda name: f"/opt/bin/{name}") == "/opt/bin/memory-lint"
    assert "memory-lint" not in doctor.report(categories)
    pointer = doctor.report(categories, memory_lint="/opt/bin/memory-lint").splitlines()[-1]
    assert pointer == "memory-lint is installed at /opt/bin/memory-lint; content checks (links, index rows, staleness) are its job."
    assert doctor.to_json(categories, memory_lint="/opt/bin/memory-lint")["memory_lint"] == "/opt/bin/memory-lint"


def test_cli_exit_codes_follow_the_error_rows(tmp_path: Path, git_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    home, _ = synced_home(tmp_path)
    assert main(["doctor", "--home", str(home)]) == 0
    assert capsys.readouterr().out.rstrip("\n").endswith("No issues found.")

    (home / layout.GITIGNORE_FILE).write_text("*\n", encoding="utf-8")  # a warning only
    assert main(["doctor", "--home", str(home)]) == 0
    assert "[!] .gitignore — drifted" in capsys.readouterr().out

    record = home / layout.ORG.pattern / "debriefs" / "demo-project" / "2026" / "09" / "20260905-alice-1f3a9c2b.md"
    (record.parent / "20260905-alice-99zz00aa.md").symlink_to(record)  # an error row
    assert main(["doctor", "--home", str(home)]) == 1
    assert capsys.readouterr().out.rstrip("\n").endswith("Doctor found issues that need attention.")


def test_cli_json_output(tmp_path: Path, git_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    home, _ = synced_home(tmp_path)
    assert main(["doctor", "--home", str(home), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["has_errors"] is False
    assert [category["name"] for category in data["categories"]] == list(MEMORY_CATEGORIES)


def test_cli_refuses_a_missing_home(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--home", str(tmp_path / "nope")]) == 1
    assert "is not a directory" in capsys.readouterr().err


def test_default_runner_is_the_engine_runner() -> None:
    assert doctor._git(Path("."), ["--version"], None) == run_git(["--version"], cwd=Path("."))
