# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""The startup manifest: ``agent-memory startup --runtime <r>``.

A session-start hook runs this once. It optionally pulls the home first,
then reports which memory files a session should read, in order, with each
file's readability, size and modification time as the pull left them, and
where the sync stands.
It never includes a file's content and never says a file was read: the
states are ``readable``, ``missing`` and ``unreadable``, nothing else, and
the manifest carries ``content_injected: false`` to say so. The list is
bounded by construction (the active project files, then the curated org
files, both from the layout table; ``events/``, ``debriefs/`` and
``archive/`` are excluded), and the rendered text is cut at a character
budget with a notice, so a hook can never flood a session.

Output shapes: ``--json`` is the manifest itself, ``schema_version`` first;
the default is what the runtime's hook expects on stdout: plain text for
claude, whose session start takes stdout as context, and the hook JSON
envelope for codex, whose ``additionalContext`` carries the same text.
"""

from __future__ import annotations

import datetime as dt
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import layout, sync
from .git_runner import GitRunner, run_git

SCHEMA_VERSION = 1
RUNTIME_CLAUDE = "claude"
RUNTIME_CODEX = "codex"
RUNTIMES = (RUNTIME_CLAUDE, RUNTIME_CODEX)
DEFAULT_MAX_CHARS = 8000
#: The smallest budget the command line accepts; the notice fits in a few characters at any budget.
MIN_MAX_CHARS = 1

READABLE = "readable"
MISSING = "missing"
UNREADABLE = "unreadable"
RESULT_OK = "ok"
RESULT_DEGRADED = "degraded"
PULL_NOT_REQUESTED = "not_requested"
PULL_ERROR = "error"


def build_manifest(
    home: Path,
    *,
    runtime: str,
    project: Optional[str] = None,
    pull: bool = False,
    home_source: str = "flag",
    project_source: Optional[str] = None,
    notes: Sequence[str] = (),
    runner: Optional[GitRunner] = None,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """The manifest for ``home``: the ordered files with their states, the sync's standing, warnings.

    The pull, when requested, runs before any file is inspected, so sizes,
    times and states describe the tree the session will read. ``notes`` are
    warnings the caller already knows (why no project was resolved, say).
    """
    if runtime not in RUNTIMES:
        raise ValueError(f"unknown runtime {runtime!r}; one of {', '.join(RUNTIMES)}")
    home = Path(home).expanduser().absolute()
    warnings: List[str] = list(notes)
    files: List[Dict[str, Any]] = []

    sync_info, pull_info = _sync(home, pull, runner, warnings)
    if project is not None:
        try:
            layout.validate_project_name(project)
        except ValueError as exc:
            warnings.append(f"project {project!r}: {exc}; its files are skipped")
            project = None
    if not home.is_dir():
        warnings.append(f"memory home {home} is not a directory; every file is missing")
    if project is not None:
        memory = layout.project_memory_dir(home, project)
        files.extend(_entry(home, memory / name, layout.PROJECT.name, name) for name in layout.PROJECT.files)
    else:
        warnings.append("no project resolved; pass --project or bind the repository with `agent-memory init --repo .`")
    org = layout.org_memory_dir(home)
    files.extend(_entry(home, org / name, layout.ORG.name, name) for name in layout.ORG.files)
    for entry in files:
        if entry["state"] != READABLE:
            reason = f" ({entry['error']})" if entry.get("error") else ""
            warnings.append(f"{entry['relative']}: {entry['state']}{reason}")

    excluded = [f"{layout.ORG.pattern}/{sub}/" for sub in layout.ORG.dirs]
    if project is not None:
        memory_rel = layout.project_memory_dir(home, project).relative_to(home).as_posix()
        excluded.extend(f"{memory_rel}/{sub}/" for sub in layout.PROJECT.dirs)

    moment = now or dt.datetime.now(dt.timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "runtime": runtime,
        "generated_at_utc": _iso(moment),
        "home": str(home),
        "home_source": home_source,
        "project": project,
        "project_source": project_source if project is not None else None,
        "content_injected": False,
        "files": files,
        "excluded": excluded,
        "bytes_total": sum(int(entry["bytes"]) for entry in files),
        "sync": sync_info,
        "pull": pull_info,
        "warnings": warnings,
        "result": RESULT_DEGRADED if warnings else RESULT_OK,
    }


def _entry(home: Path, path: Path, tier: str, name: str) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "tier": tier,
        "name": name,
        "path": str(path),
        "relative": path.relative_to(home).as_posix(),
        "state": MISSING,
        "bytes": 0,
        "modified_at_utc": None,
    }
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return entry
    except OSError as exc:
        entry["state"] = UNREADABLE
        entry["error"] = exc.strerror or str(exc)
        return entry
    if not stat.S_ISREG(info.st_mode):
        entry["state"] = UNREADABLE
        entry["error"] = "not a regular file"
        return entry
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError as exc:
        entry["state"] = UNREADABLE
        entry["error"] = exc.strerror or str(exc)
        return entry
    entry["state"] = READABLE
    entry["bytes"] = info.st_size
    entry["modified_at_utc"] = _iso(dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc))
    return entry


def _sync(
    home: Path, pull: bool, runner: Optional[GitRunner], warnings: List[str]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    marker = sync.is_configured(home)
    info: Dict[str, Any] = {"marker": marker, "last_commit_at_utc": None}
    pull_info: Dict[str, Any] = {"requested": pull, "status": PULL_NOT_REQUESTED, "ok": True, "lines": []}
    if pull:
        try:
            outcome = sync.pull(home, runner=runner)
            pull_info = {"requested": True, "status": outcome.status, "ok": outcome.ok, "lines": list(outcome.lines)}
        except (sync.SyncError, OSError) as exc:
            pull_info = {"requested": True, "status": PULL_ERROR, "ok": False, "lines": [f"memory pull: {exc}"]}
        if not pull_info["ok"]:
            said = pull_info["lines"][0] if pull_info["lines"] else pull_info["status"]
            warnings.append(f"memory pull did not complete; local memory may be stale: {said}")
    if marker and sync.is_git_repo(home, runner):
        result = (runner or run_git)(["log", "-1", "--format=%ct"], cwd=home, timeout=None)
        stamp = result.stdout.strip()
        if result.ok and stamp.isdigit():
            info["last_commit_at_utc"] = _iso(dt.datetime.fromtimestamp(int(stamp), dt.timezone.utc))
    return info, pull_info


# --- rendering --------------------------------------------------------------


def render_text(manifest: Dict[str, Any], *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """The manifest as the lines a session-start hook prints; cut at ``max_chars`` with a notice."""
    head = f"agent-memory startup ({manifest['runtime']}): home {manifest['home']} ({manifest['home_source']})"
    if manifest["project"] is not None:
        head += f", project {manifest['project']} ({manifest['project_source'] or 'flag'})"
    else:
        head += ", no project"
    lines = [head]
    pull = manifest["pull"]
    if pull["requested"]:
        if pull["lines"]:
            lines.extend(pull["lines"])
        elif pull["status"] == "not_configured":
            lines.append("memory pull: sync is not enabled for this home; skipped.")
    if manifest["sync"]["last_commit_at_utc"]:
        lines.append(f"memory sync: last commit {manifest['sync']['last_commit_at_utc']}.")

    number = 0
    project_files = [entry for entry in manifest["files"] if entry["tier"] == layout.PROJECT.name]
    org_files = [entry for entry in manifest["files"] if entry["tier"] == layout.ORG.name]
    if project_files:
        lines.append("Project memory, read in this order (states are readability only; no content is injected):")
        for entry in project_files:
            number += 1
            lines.append(f"  {number}. {_describe(entry)}")
    lines.append("Org memory, curated context; consult what governs the work before doing it (not read by default):")
    for entry in org_files:
        number += 1
        lines.append(f"  {number}. {_describe(entry)}")
    lines.append(f"Excluded by default: {', '.join(manifest['excluded'])}")
    if manifest["warnings"]:
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in manifest["warnings"])
    return _bound("\n".join(lines) + "\n", max_chars, manifest["runtime"])


def render_codex_hook(manifest: Dict[str, Any], *, max_chars: int = DEFAULT_MAX_CHARS) -> Dict[str, Any]:
    """The Codex ``SessionStart`` hook envelope carrying the rendered text as ``additionalContext``."""
    output: Dict[str, Any] = {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": render_text(manifest, max_chars=max_chars),
        },
    }
    if manifest["result"] != RESULT_OK:
        output["systemMessage"] = "agent-memory startup completed in degraded mode; see the warnings in its context."
    return output


def _describe(entry: Dict[str, Any]) -> str:
    if entry["state"] == READABLE:
        return f"{entry['relative']}: readable, {entry['bytes']} bytes, modified {entry['modified_at_utc']}"
    reason = f" ({entry['error']})" if entry.get("error") else ""
    return f"{entry['relative']}: {entry['state']}{reason}"


def _bound(text: str, max_chars: int, runtime: str) -> str:
    """``text`` cut to at most ``max_chars`` characters, notice included; a budget below zero counts as zero."""
    max_chars = max(0, max_chars)
    if len(text) <= max_chars:
        return text
    suffix = (
        f"\n[agent-memory: manifest text cut at {max_chars} characters; "
        f"run `agent-memory startup --runtime {runtime} --json` for the whole manifest]\n"
    )
    if len(suffix) >= max_chars:
        suffix = "[cut]\n"[:max_chars]
    return text[: max_chars - len(suffix)] + suffix


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
