# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Setup and health checks for a memory home: ``agent-memory doctor``.

Two categories, ported row for row from the 0.4.5 memory doctor (the golden
under ``tests/golden/``):

* **Org Memory** validates the debrief store's layout: the directory exists,
  every record sits at ``<project>/<YYYY>/<MM>/<YYYYMMDD>-<agent>-<session>.md``,
  no writer staging artifact lingers, and nothing under the store is a symlink
  or otherwise irregular. It never opens a record, and a traversal it cannot
  complete is its own error row, never a clean result.
* **Memory Sync** reads the sync marker, the root ``.gitignore``, and what git
  reports about the home: tracked paths against the allowlist, untracked
  memory-shaped files, the working tree, the upstream, the remote, the last
  commit's age, per-instance ``agents/`` state, and the project ``.gitignore``
  overlays. A git command that fails produces a warning row for its check,
  never a pass.

The doctor reads no memory content and repairs nothing: a row that is not ok
carries a hint for the human, and the home is byte-identical after a run. Every
filesystem probe distinguishes a path that is absent from one it was denied,
so nothing unreadable reads as absent, and nothing absent reads as fine. The
result frame here is local to this module.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import layout, sync
from .git_runner import GitResult, GitRunner, run_git
from .sync import GitState

#: A last commit older than this many days is reported stale.
STALE_MEMORY_DAYS = 7

#: The content checker the doctor points at when it is installed; content is its job.
MEMORY_LINT = "memory-lint"

# The agent segment is the canonical agent grammar; the session segment is
# hyphen-free, so the split on the last hyphen is deterministic.
_AGENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
DEBRIEF_FILENAME_RE = re.compile(rf"^(?P<date>\d{{8}})-(?P<agent>{_AGENT})-(?P<session>[a-z0-9]{{1,32}})\.md$")


# --- the result frame -------------------------------------------------------


class Severity(Enum):
    ok = "ok"
    warn = "warn"
    error = "error"
    skip = "skip"


SYMBOL = {
    Severity.ok: "[+]",
    Severity.warn: "[!]",
    Severity.error: "[x]",
    Severity.skip: "[-]",
}


@dataclass(frozen=True)
class Result:
    """One row: a stable name, a severity, the message, and a hint when it is not ok."""

    name: str
    severity: Severity
    message: str
    fix_hint: str = ""


@dataclass
class Category:
    """One block of rows under a heading."""

    name: str
    results: List[Result] = field(default_factory=list)

    @property
    def worst_severity(self) -> Severity:
        for severity in (Severity.error, Severity.warn, Severity.skip, Severity.ok):
            if any(result.severity is severity for result in self.results):
                return severity
        return Severity.ok

    def add(self, name: str, severity: Severity, message: str, fix_hint: str = "") -> None:
        self.results.append(Result(name, severity, message, fix_hint))


def has_errors(categories: Sequence[Category]) -> bool:
    return any(result.severity is Severity.error for category in categories for result in category.results)


# --- running ----------------------------------------------------------------


def run_doctor(
    home: Path, *, runner: Optional[GitRunner] = None, now: Optional[dt.datetime] = None
) -> List[Category]:
    """Both memory categories for ``home``, in report order."""
    return [check_org_memory(home), check_memory_sync(home, runner=runner, now=now)]


def find_memory_lint(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """Where ``memory-lint`` is on PATH, or ``None``."""
    return which(MEMORY_LINT)


def report(categories: Sequence[Category], *, memory_lint: Optional[str] = None) -> str:
    """The text report: one block per category, a verdict line, and the pointer to memory-lint when it is installed."""
    lines: List[str] = []
    for index, category in enumerate(categories):
        if index:
            lines.append("")
        lines.append(f"{SYMBOL[category.worst_severity]} {category.name}")
        for result in category.results:
            lines.append(f"    {SYMBOL[result.severity]} {result.message}")
            if result.fix_hint and result.severity is not Severity.ok:
                lines.append(f"        {result.fix_hint}")
    lines.append("")
    lines.append("Doctor found issues that need attention." if has_errors(categories) else "No issues found.")
    if memory_lint:
        lines.append(f"{MEMORY_LINT} is installed at {memory_lint}; content checks (links, index rows, staleness) are its job.")
    return "\n".join(lines) + "\n"


def to_json(categories: Sequence[Category], *, memory_lint: Optional[str] = None) -> Dict[str, Any]:
    """The report as data, in the same order as the text."""
    output: Dict[str, Any] = {
        "has_errors": has_errors(categories),
        "memory_lint": memory_lint,
        "categories": [],
    }
    for category in categories:
        rows: List[Dict[str, str]] = []
        for result in category.results:
            row = {"name": result.name, "severity": result.severity.value, "message": result.message}
            if result.fix_hint:
                row["fix_hint"] = result.fix_hint
            rows.append(row)
        output["categories"].append(
            {"name": category.name, "worst_severity": category.worst_severity.value, "results": rows}
        )
    return output


# --- Org Memory: the debrief store's layout ---------------------------------
#
# Setup-level by design: the doctor confirms the store exists, the path layout
# is canonical, and nothing irregular sits in the namespace. It never opens a
# record; content and format belong to the writer's read-back at publication
# and to git history. A failed traversal or classification produces its own
# non-ok row, never a clean result.


def check_org_memory(home: Path) -> Category:
    """The debrief store under ``org-memory/``: presence, canonical layout, staging leftovers, irregular entries."""
    cat = Category("Org Memory")
    org_memory = layout.org_memory_dir(home)
    try:
        initialized = _is_dir(org_memory)
    except OSError as exc:
        cat.add("org-memory-dir", Severity.error, f"{layout.ORG.pattern}/ — {_not_inspected(exc)}")
        return cat
    if not initialized:
        cat.add("org-memory-dir", Severity.skip, f"{layout.ORG.pattern}/ — not initialized", "Run: agent-memory org init")
        return cat

    debriefs = org_memory / "debriefs"
    try:
        present = _is_dir(debriefs)
    except OSError as exc:
        cat.add("debriefs-dir", Severity.error, f"{layout.ORG.pattern}/debriefs/ — {_not_inspected(exc)}")
        return cat
    if not present:
        cat.add(
            "debriefs-dir",
            Severity.warn,
            f"{layout.ORG.pattern}/debriefs/ — missing (pre-debrief-store layout)",
            "Run: agent-memory org init",
        )
        return cat
    cat.add("debriefs-dir", Severity.ok, f"{layout.ORG.pattern}/debriefs/ — present")

    layout_bad: List[str] = []
    staging: List[str] = []
    irregular: List[str] = []
    walk_errors: List[str] = []
    total = 0

    def _relative(path: Path) -> str:
        try:
            return path.relative_to(debriefs).as_posix() or "."
        except ValueError:
            return str(path)

    def _walk_error(exc: OSError) -> None:
        # A directory the walk cannot enter hides an unknown number of records;
        # the failure surfaces as its own row.
        location = getattr(exc, "filename", None) or str(debriefs)
        walk_errors.append(f"{_relative(Path(location))}: {exc.__class__.__name__}")

    entries: List[Path] = []
    # followlinks=False, so a symlinked directory cannot pull a foreign tree into
    # the store; the link itself is flagged below.
    for dirpath, dirnames, filenames in os.walk(debriefs, onerror=_walk_error, followlinks=False):
        current = Path(dirpath)
        kept: List[str] = []
        for name in sorted(dirnames):
            entry = current / name
            try:
                is_link = entry.is_symlink()
            except OSError as exc:
                walk_errors.append(f"{_relative(entry)}: {exc.__class__.__name__}")
                continue
            if is_link:
                irregular.append(f"{_relative(entry)}/ (symlinked directory)")
            else:
                kept.append(name)
        dirnames[:] = kept
        entries.extend(current / name for name in filenames)

    for file_path in sorted(entries):
        rel = _relative(file_path)
        if rel == ".gitkeep":
            continue
        # Writer staging artifacts (.stage.<name>.<nonce>) sit outside the
        # canonical namespace; a lingering one means an interrupted publication.
        if file_path.name.startswith(".stage."):
            staging.append(rel)
            continue
        # The namespace holds regular files reached without following links;
        # a classification failure surfaces, never raises.
        try:
            if file_path.is_symlink():
                irregular.append(f"{rel} (symlink)")
                continue
            regular = file_path.is_file()
        except OSError as exc:
            walk_errors.append(f"{rel}: {exc.__class__.__name__}")
            continue
        if not regular:
            irregular.append(f"{rel} (not a regular file)")
            continue
        total += 1
        parts = rel.split("/")
        match = DEBRIEF_FILENAME_RE.match(parts[-1]) if len(parts) == 4 else None
        date_valid = False
        if match is not None:
            try:
                dt.datetime.strptime(match.group("date"), "%Y%m%d")
                date_valid = True
            except ValueError:
                pass
        if (
            match is None
            or not date_valid
            or not _valid_project_segment(parts[0])
            or parts[1] != match.group("date")[0:4]
            or parts[2] != match.group("date")[4:6]
        ):
            layout_bad.append(rel)

    if staging:
        cat.add(
            "debriefs-staging",
            Severity.warn,
            f"{len(staging)} lingering writer staging artifact(s) (interrupted publication): {_summarize(staging)}",
            "The owning writer removes or adopts its stale staging files on retry",
        )
    if irregular:
        cat.add(
            "debriefs-irregular",
            Severity.error,
            f"{len(irregular)} non-regular entr(ies) under debriefs/ "
            f"(the store holds regular files, never symlinks): {_summarize(irregular)}",
        )
    if walk_errors:
        cat.add(
            "debriefs-unreadable",
            Severity.error,
            f"{len(walk_errors)} entr(ies) under debriefs/ could not be inspected "
            f"(setup check incomplete): {_summarize(walk_errors)}",
        )
    if total == 0:
        if not walk_errors:
            cat.add("debriefs-layout", Severity.ok, "debriefs/ — empty store, nothing to validate")
        return cat
    if layout_bad:
        cat.add(
            "debriefs-layout",
            Severity.error,
            f"{len(layout_bad)} of {total} debrief file(s) outside the canonical "
            f"<project>/<YYYY>/<MM>/<YYYYMMDD>-<agent>-<session>.md layout: {_summarize(layout_bad)}",
            "Move or rename to the canonical path; never rewrite contents",
        )
    else:
        cat.add("debriefs-layout", Severity.ok, f"{total} debrief file(s) — canonical layout")
    return cat


def _valid_project_segment(name: str) -> bool:
    try:
        layout.validate_project_name(name)
    except ValueError:
        return False
    return True


# --- Memory Sync: the marker, the allowlist, and the repository ------------


def check_memory_sync(
    home: Path, *, runner: Optional[GitRunner] = None, now: Optional[dt.datetime] = None
) -> Category:
    """The sync setup and the repository's state, through git alone; nothing is changed."""
    cat = Category("Memory Sync")
    marker = layout.MARKER_FILE

    try:
        configured = _is_file(sync.marker_path(home))
    except OSError as exc:
        cat.add("memory-marker", Severity.warn, f"{marker} — {_not_inspected(exc)}")
        return cat
    if not configured:
        cat.add(
            "memory-marker",
            Severity.skip,
            f"{marker} — not configured; memory sync hooks are disabled",
            "Run: agent-memory enable [--remote URL]",
        )
        return cat
    cat.add("memory-marker", Severity.ok, f"{marker} — present")

    if not sync.is_git_repo(home, runner):
        cat.add(
            "memory-git",
            Severity.warn,
            f"{marker} — present, but {home} is not a git repository",
            "Run `agent-memory enable`, or `agent-memory disable` to remove the marker",
        )
        return cat
    enclosing = enclosing_repository(home, runner)
    if enclosing is not None:
        # The sync verbs refuse this home; reading the enclosing repository's state as the home's would be wrong.
        cat.add(
            "memory-git",
            Severity.warn,
            f"{marker} — present, but {home} is inside the git worktree {enclosing}; "
            "a memory home must be the root of its own repository",
            "Move the home out of the enclosing repository, or `agent-memory disable` to remove the marker",
        )
        return cat

    root_gitignore = home / layout.GITIGNORE_FILE
    try:
        present = _is_file(root_gitignore)
    except OSError as exc:
        cat.add("root-gitignore", Severity.warn, f"{layout.GITIGNORE_FILE} — {_not_inspected(exc)}")
        present = None
    if present is False:
        cat.add(
            "root-gitignore",
            Severity.warn,
            f"{layout.GITIGNORE_FILE} — missing canonical memory allowlist",
            "Run: agent-memory enable",
        )
    elif present:
        try:
            content = _normalize_gitignore(root_gitignore.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            cat.add(
                "root-gitignore",
                Severity.warn,
                f"{layout.GITIGNORE_FILE} — could not be read: {_failure_text(exc)}",
                _read_hint(exc),
            )
        else:
            if content == layout.gitignore_text():
                cat.add("root-gitignore", Severity.ok, f"{layout.GITIGNORE_FILE} — canonical memory allowlist")
            elif sync.gitignore_has_managed_block(content):
                kept = len(content.splitlines()) - len(layout.gitignore_text().splitlines())
                cat.add(
                    "root-gitignore",
                    Severity.ok,
                    f"{layout.GITIGNORE_FILE} — canonical memory allowlist as a managed block; {kept} other line(s) kept",
                )
            else:
                cat.add(
                    "root-gitignore",
                    Severity.warn,
                    f"{layout.GITIGNORE_FILE} — drifted from canonical memory allowlist",
                    "Run `agent-memory enable` to restore the managed allowlist block",
                )

    tracked: Optional[List[str]] = None
    try:
        tracked = _ls_files(home, runner)
        outside = [path for path in tracked if not layout.is_allowed_memory_path(path)]
    except _GitFailed as exc:
        cat.add("tracked-allowlist", Severity.warn, f"tracked allowlist check failed: {exc}")
    else:
        if outside:
            cat.add(
                "tracked-allowlist",
                Severity.warn,
                f"{len(outside)} tracked file(s) outside memory allowlist: {_summarize(outside)}",
                "Remove runtime state from the memory repo index",
            )
        else:
            cat.add("tracked-allowlist", Severity.ok, f"tracked files — {len(tracked)} inside memory allowlist")

    try:
        untracked = [path for path in _ls_files(home, runner, "--others", "--exclude-standard") if layout.is_allowed_memory_path(path)]
    except _GitFailed as exc:
        cat.add("untracked-memory", Severity.warn, f"untracked memory check failed: {exc}")
    else:
        if untracked:
            cat.add(
                "untracked-memory",
                Severity.warn,
                f"{len(untracked)} untracked memory-shaped file(s): {_summarize(untracked)}",
                "Run: agent-memory push",
            )
        else:
            cat.add("untracked-memory", Severity.ok, "untracked memory files — none")

    state: Optional[GitState]
    try:
        state = sync.git_state(home, runner=runner, fetch=True)
    except sync.SyncError as exc:
        cat.add("working-tree", Severity.warn, f"memory git state check failed: {exc}")
        state = None

    if state is not None:
        _add_working_tree(cat, home, runner, state)

        text = f"sync state — {sync_state_text(state)}"
        if not state.has_remote:
            cat.add("sync-state", Severity.ok, text)
            cat.add("remote", Severity.skip, "remote — skipped; local-only memory repo")
        elif state.fetch_failed:
            cat.add("sync-state", Severity.warn, text, "Check network access and remote permissions")
            cat.add("remote", Severity.warn, "remote — not reachable", "Check network access and remote permissions")
        else:
            if not state.has_upstream:
                cat.add("sync-state", Severity.warn, text, "Run: git -C <home> push -u <remote> <branch>")
            elif state.diverged:
                cat.add("sync-state", Severity.warn, text, "Resolve manually; agent-memory never merges memory")
            elif state.behind:
                cat.add("sync-state", Severity.warn, text, "Run: agent-memory pull")
            elif state.ahead:
                cat.add("sync-state", Severity.warn, text, "Run: agent-memory push")
            else:
                cat.add("sync-state", Severity.ok, text)
            cat.add("remote", Severity.ok, "remote — reachable")

    if not _has_commits(home, runner):
        cat.add("last-commit", Severity.warn, "last commit — none", "Run: agent-memory push")
    else:
        age_days = _last_commit_age_days(home, runner, now=now)
        if age_days is None:
            cat.add("last-commit", Severity.warn, "last commit — timestamp unavailable")
        elif age_days > STALE_MEMORY_DAYS:
            cat.add("last-commit", Severity.warn, f"last commit — stale ({age_days} day(s) old)", "Run: agent-memory push")
        else:
            cat.add("last-commit", Severity.ok, f"last commit — fresh ({age_days} day(s) old)")

    if tracked is not None:
        agents_tracked = [
            path
            for path in tracked
            if path.startswith("agents/") or (path.startswith(f"{layout.PROJECTS_DIR}/") and "/agents/" in path)
        ]
        if agents_tracked:
            cat.add(
                "agents-tracked",
                Severity.warn,
                f"{len(agents_tracked)} agents/ file(s) tracked: {_summarize(agents_tracked)}",
                "Remove per-instance agent state from the memory repo",
            )
        else:
            cat.add("agents-tracked", Severity.ok, "agents/ tracked files — none")

    overlays, failed = _overlay_gitignores(home)
    escaping: List[str] = []
    for overlay in overlays:
        rel = _relative_to_home(home, overlay)
        try:
            patterns = _escaping_overlay_patterns(overlay)
        except (OSError, UnicodeDecodeError) as exc:
            failed.append(f"{rel}: {_failure_text(exc)}")
            continue
        escaping.extend(f"{rel}: {pattern}" for pattern in patterns)
    if failed:
        # An overlay the doctor could not find or read may still escape memory/**;
        # the row says the check is incomplete rather than counting it safe.
        cat.add(
            "memory-overlays",
            Severity.warn,
            f"{len(failed)} memory .gitignore overlay location(s) could not be inspected "
            f"(overlay check incomplete): {_summarize(failed)}",
            "Restore read access under projects/ (or re-encode the file as UTF-8) and re-run",
        )
    elif escaping:
        cat.add(
            "memory-overlays",
            Severity.warn,
            f"memory .gitignore overlays can escape memory/**: {_summarize(escaping)}",
            "Remove overlay unignore patterns containing '..'",
        )
    else:
        cat.add("memory-overlays", Severity.ok, f"memory .gitignore overlays — {len(overlays)} safe")

    return cat


def _add_working_tree(cat: Category, home: Path, runner: Optional[GitRunner], state: GitState) -> None:
    """The tree row, scoped like the counts above it.

    Memory changes inside the allowlist are what ``push`` would publish and
    read DIRTY. Changes anywhere else are named as outside the allowlist: not
    memory content, but ``pull`` still refuses a tree that carries them.
    """
    if not state.dirty:
        cat.add("working-tree", Severity.ok, "working tree — clean")
        return
    try:
        changed = sync.changed_paths(home, runner)
    except sync.SyncError as exc:
        cat.add("working-tree", Severity.warn, f"working tree — DIRTY; the change readout failed: {exc}")
        return
    memory = [path for path in changed if layout.is_allowed_memory_path(path)]
    elsewhere = [path for path in changed if not layout.is_allowed_memory_path(path)]
    if memory:
        text = f"working tree — DIRTY {len(memory)} memory change(s): {_summarize(memory)}"
        if elsewhere:
            text += f"; {len(elsewhere)} change(s) outside the memory allowlist"
        cat.add("working-tree", Severity.warn, text, "Run `agent-memory push` or resolve changes manually")
    elif elsewhere:
        cat.add(
            "working-tree",
            Severity.warn,
            f"working tree — {len(elsewhere)} change(s) outside the memory allowlist: {_summarize(elsewhere)}; "
            "no memory change is pending, but memory pull refuses a dirty tree",
            "Commit, stash or ignore them outside the memory tiers",
        )
    else:
        cat.add("working-tree", Severity.ok, "working tree — clean")


def enclosing_repository(home: Path, runner: Optional[GitRunner] = None) -> Optional[Path]:
    """The worktree root when ``home`` sits inside a repository that is not its own; ``None`` when it is the root."""
    root = sync.worktree_root(home, runner)
    if root is None or root.resolve() == home.resolve():
        return None
    return root


def sync_state_text(state: GitState) -> str:
    """One phrase for where the repository stands; ``status`` and ``doctor`` share it."""
    if not state.has_remote:
        return "local-only; no remote configured"
    if state.fetch_failed:
        return f"remote fetch failed: {state.fetch_output}"
    if not state.has_upstream:
        return "remote exists but no upstream branch is configured"
    if state.diverged:
        return f"DIVERGED from upstream ({state.ahead} ahead, {state.behind} behind)"
    if state.behind:
        return f"BEHIND upstream by {state.behind} commit(s)"
    if state.ahead:
        return f"ahead by {state.ahead} unpushed commit(s)"
    return "synced with upstream"


# --- git readout helpers the doctor alone needs -----------------------------


class _GitFailed(Exception):
    """A git readout the doctor needs did not run; the row says so."""


def _git(home: Path, args: Sequence[str], runner: Optional[GitRunner]) -> GitResult:
    return (runner or run_git)(args, cwd=home)


def _ls_files(home: Path, runner: Optional[GitRunner], *options: str) -> List[str]:
    result = _git(home, ["ls-files", *options], runner)
    if not result.ok:
        raise _GitFailed(f"{' '.join(['git', 'ls-files', *options])} failed: {result.output}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _has_commits(home: Path, runner: Optional[GitRunner]) -> bool:
    return _git(home, ["rev-parse", "--verify", "HEAD"], runner).ok


def _last_commit_age_days(home: Path, runner: Optional[GitRunner], *, now: Optional[dt.datetime]) -> Optional[int]:
    result = _git(home, ["log", "-1", "--format=%ct"], runner)
    if not result.ok:
        return None
    try:
        timestamp = int(result.stdout.strip())
    except ValueError:
        return None
    current = now or dt.datetime.now(dt.timezone.utc)
    committed = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
    return max(0, int((current - committed).total_seconds() // 86400))


def _normalize_gitignore(text: str) -> str:
    return text.replace("\r\n", "\n")


# --- filesystem probes ------------------------------------------------------
#
# pathlib's exists/is_dir/is_file/glob answer False (or skip the subtree) when
# the probe is denied, which would let an unreadable path read as absent, and
# absent as fine. These probes return the answer when there is one and raise
# when there is not; the caller's row says the check is incomplete.


def _inspect(path: Path) -> Optional[os.stat_result]:
    """``stat`` following symlinks: the result, ``None`` when the path is absent, ``OSError`` when it was denied."""
    try:
        return os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _is_dir(path: Path) -> bool:
    result = _inspect(path)
    return result is not None and stat.S_ISDIR(result.st_mode)


def _is_file(path: Path) -> bool:
    result = _inspect(path)
    return result is not None and stat.S_ISREG(result.st_mode)


def _failure_text(exc: Exception) -> str:
    if isinstance(exc, UnicodeDecodeError):
        return "not valid UTF-8"
    return getattr(exc, "strerror", None) or exc.__class__.__name__


def _not_inspected(exc: OSError) -> str:
    return f"could not be inspected (setup check incomplete): {_failure_text(exc)}"


def _read_hint(exc: Exception) -> str:
    return "Re-encode the file as UTF-8" if isinstance(exc, UnicodeDecodeError) else ""


def _relative_to_home(home: Path, path: Path) -> str:
    try:
        return path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def _overlay_gitignores(home: Path) -> Tuple[List[Path], List[str]]:
    """The project overlays ``projects/<p>/memory/.gitignore`` that exist, and the locations
    the discovery was denied, as ``<path>: <reason>``.

    A bounded walk over the tier pattern rather than ``Path.glob``: glob swallows a traversal
    it is denied, so an unreadable project or memory directory would read as "no overlay".
    Here a directory that is absent (or not a directory) is the only thing that means no
    overlay; every other failure goes back to the caller's row.
    """
    found: List[Path] = []
    failed: List[str] = []
    candidates = [home]
    for part in layout.PROJECT.parts:
        expanded: List[Path] = []
        for base in candidates:
            if part != layout.WILDCARD:
                expanded.append(base / part)
                continue
            try:
                with os.scandir(base) as entries:
                    names = sorted(entry.name for entry in entries)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                failed.append(f"{_relative_to_home(home, base)}/: {_failure_text(exc)}")
                continue
            expanded.extend(base / name for name in names)
        candidates = expanded
    for directory in candidates:
        overlay = directory / layout.GITIGNORE_FILE
        try:
            # lstat, so a dangling overlay symlink is found and then fails to read, never skipped.
            os.lstat(overlay)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            failed.append(f"{_relative_to_home(home, overlay)}: {_failure_text(exc)}")
            continue
        found.append(overlay)
    return sorted(found), failed


def _escaping_overlay_patterns(path: Path) -> List[str]:
    """Unignore patterns in a project overlay whose path climbs out of ``memory/``."""
    bad: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith("!"):
            continue
        pattern = line[1:].strip()
        parts = [part for part in pattern.replace("\\", "/").split("/") if part]
        if ".." in parts:
            bad.append(raw)
    return bad


def _summarize(paths: Sequence[str], *, limit: int = 3) -> str:
    if not paths:
        return ""
    shown = ", ".join(paths[:limit])
    if len(paths) > limit:
        shown += f", +{len(paths) - limit} more"
    return shown
