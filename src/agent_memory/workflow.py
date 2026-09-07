# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""The minimal memory workflow: ``agent-memory capture`` and ``agent-memory recall``.

Two verbs and one authored text. ``capture`` appends one decision, with its
provenance, to the project's ``decision_log.md``, newest first under a UTC
date heading; it never writes through a symlink (a linked component anywhere
below the home, the file included, is refused, never followed), replaces the
file atomically with the file's own mode, and touches no other file. ``recall`` prints the content of the files the
startup manifest lists, in the manifest's order, cut at a character budget
with a notice: the bounded read the manifest describes but never performs.
``archive/``, ``events/`` and ``debriefs/`` are never loaded.

The authored text is the workflow file ``setup <runtime>`` installs beside the
hook, rendered from one shipped template with the runtime's name; every
runtime's wrapper says the same thing.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import secrets
import stat as stat_mod
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import layout, startup

SCHEMA_VERSION = 1
DECISION_FILE = "decision_log.md"
DEFAULT_AGENT_ENV = "AGENT_MEMORY_AGENT"
WORKFLOW_TEMPLATE = "templates/workflow/SKILL.md"

_HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$")
_SPACE = re.compile(r"\s+")


class WorkflowError(Exception):
    """A precondition failed; nothing was written."""


# --- the authored text ---------------------------------------------------------


def workflow_text(runtime: str) -> str:
    """The workflow file for ``runtime``, byte for byte what setup writes."""
    template = (resources.files(__package__) / WORKFLOW_TEMPLATE).read_text(encoding="utf-8")
    return template.replace("{runtime}", runtime)


# --- capture -------------------------------------------------------------------


def default_agent() -> str:
    """Who is capturing: ``$AGENT_MEMORY_AGENT`` (the hook exports it), else the user, else ``unknown``."""
    return os.environ.get(DEFAULT_AGENT_ENV) or os.environ.get("USER") or "unknown"


def default_runtime() -> str:
    """The runtime reading: ``$AGENT_MEMORY_AGENT`` when it names one, else claude."""
    agent = os.environ.get(DEFAULT_AGENT_ENV)
    return agent if agent in startup.RUNTIMES else startup.RUNTIME_CLAUDE


def format_entry(decision: str, *, why: Optional[str], agent: str, source: Optional[str], now: dt.datetime) -> str:
    """One decision as a list line: the decision, its reason, then the provenance in parentheses."""
    text = _one_line(decision)
    if not text:
        raise WorkflowError("the decision is empty")
    parts = [f"- **{text}**"]
    reason = _one_line(why or "")
    if reason:
        parts.append(f" Why: {reason}")
    provenance = [_one_line(agent) or "unknown", _iso(now)]
    reference = _one_line(source or "")
    if reference:
        provenance.append(f"source: {reference}")
    parts.append(f" ({', '.join(provenance)})")
    return "".join(parts)


def capture(
    home: Path,
    project: str,
    decision: str,
    *,
    why: Optional[str] = None,
    source: Optional[str] = None,
    agent: Optional[str] = None,
    now: Optional[dt.datetime] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Append ``decision`` to the project's decision log, newest first under today's UTC heading."""
    home = Path(home).expanduser().absolute()
    layout.validate_project_name(project)
    memory = layout.project_memory_dir(home, project)
    path = memory / DECISION_FILE
    moment = now or dt.datetime.now(dt.timezone.utc)
    agent_name = agent or default_agent()
    entry = format_entry(decision, why=why, agent=agent_name, source=source, now=moment)
    date = _iso(moment)[:10]

    link = _linked_component(home, path)
    if link is not None:
        raise WorkflowError(f"{link} is a symlink; the workflow never writes through a link")
    if not memory.is_dir():
        raise WorkflowError(
            f"project {project!r} has no memory tier at {memory}; create it with `agent-memory init --project {project}`"
        )
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise WorkflowError(f"{path} is missing; recreate it with `agent-memory init --project {project}`") from None
    except OSError as exc:
        raise WorkflowError(f"cannot inspect {path}: {exc}") from exc
    if not stat_mod.S_ISREG(info.st_mode):
        raise WorkflowError(f"{path} is not a regular file")
    try:
        before = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowError(f"cannot read {path}: {exc}") from exc

    after = insert_entry(before, date, entry)
    result = {
        "schema_version": SCHEMA_VERSION,
        "action": "capture",
        "home": str(home),
        "project": project,
        "path": str(path),
        "date": date,
        "entry": entry,
        "agent": agent_name,
        "dry_run": dry_run,
        "written": False,
    }
    if dry_run:
        return result
    _replace_text(path, after, mode=stat_mod.S_IMODE(info.st_mode))
    result["written"] = True
    return result


def insert_entry(text: str, date: str, entry: str) -> str:
    """``text`` with ``entry`` as the first item under the ``## date`` heading, creating the heading newest-first."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the trailing newline; restored below
    first = next((index for index, line in enumerate(lines) if _HEADING.match(line)), None)
    if first is not None and _HEADING.match(lines[first]).group(1) == date:
        at = first + 1
        if at < len(lines) and lines[at] == "":
            at += 1
        lines[at:at] = [entry]
    else:
        block = [f"## {date}", "", entry, ""]
        if first is None:
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend(block[:-1])
        else:
            at = first
            if at > 0 and lines[at - 1] != "":
                block.insert(0, "")
            lines[at:at] = block
    return "\n".join(lines) + "\n"


def _linked_component(home: Path, path: Path) -> Optional[Path]:
    """The first symlink below ``home`` on the way down to ``path`` (``path`` included), or ``None``.

    The home itself may be a link (a workspace marker is one); every component
    beneath it is inspected without following, so a linked ``projects/``,
    project or memory directory is refused before anything is read or staged.
    A component that does not exist ends the walk: the later checks name it.
    """
    current = home
    for part in path.relative_to(home).parts:
        current = current / part
        try:
            info = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            raise WorkflowError(f"cannot inspect {current}: {exc}") from exc
        if stat_mod.S_ISLNK(info.st_mode):
            return current
    return None


def _replace_text(path: Path, text: str, *, mode: int) -> None:
    """Write ``text`` beside ``path`` and move it into place atomically; the file keeps its mode."""
    data = text.encode("utf-8")
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temp, flags, mode)
    except OSError as exc:
        raise WorkflowError(f"cannot stage the write beside {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)  # os.open applied the umask; the original bits win
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise WorkflowError(f"cannot write {path}: {exc}") from exc


# --- recall --------------------------------------------------------------------


def recall(
    home: Path,
    *,
    project: Optional[str],
    runtime: str = startup.RUNTIME_CLAUDE,
    max_chars: int = startup.DEFAULT_MAX_CHARS,
    home_source: str = "flag",
    project_source: Optional[str] = None,
    notes: List[str] = (),
) -> Dict[str, Any]:
    """The bounded read: the manifest's readable files, in its order, with their content, cut at ``max_chars``."""
    manifest = startup.build_manifest(
        home, runtime=runtime, project=project, home_source=home_source, project_source=project_source, notes=notes
    )
    parts: List[str] = []
    files: List[Dict[str, Any]] = []
    head = f"agent-memory recall: home {manifest['home']}"
    head += f", project {manifest['project']}" if manifest["project"] is not None else ", no project"
    parts.append(head + f"; content follows in manifest order, cut at {max_chars} characters.")
    for entry in manifest["files"]:
        row = {"tier": entry["tier"], "relative": entry["relative"], "state": entry["state"], "bytes": entry["bytes"]}
        if entry["state"] == startup.READABLE:
            try:
                content = Path(entry["path"]).read_bytes().decode("utf-8", errors="replace")
            except OSError as exc:
                row["state"] = startup.UNREADABLE
                row["error"] = exc.strerror or str(exc)
                manifest["warnings"].append(f"{entry['relative']}: unreadable ({row['error']})")
            else:
                parts.append(f"\n--- {entry['relative']} ({entry['bytes']} bytes, modified {entry['modified_at_utc']}) ---")
                parts.append(content.rstrip("\n"))
        files.append(row)
    parts.append(f"\nExcluded by default: {', '.join(manifest['excluded'])}")
    if manifest["warnings"]:
        parts.append("Warnings:")
        parts.extend(f"  - {warning}" for warning in manifest["warnings"])
    text = _bound("\n".join(parts) + "\n", max_chars)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "recall",
        "home": manifest["home"],
        "project": manifest["project"],
        "content_injected": True,
        "max_chars": max_chars,
        "files": files,
        "excluded": manifest["excluded"],
        "warnings": manifest["warnings"],
        "truncated": len(text) < len("\n".join(parts)) + 1,
        "text": text,
    }


def _bound(text: str, max_chars: int) -> str:
    max_chars = max(0, max_chars)
    if len(text) <= max_chars:
        return text
    suffix = f"\n[agent-memory: recall cut at {max_chars} characters; raise --max-chars or read the remaining files directly]\n"
    if len(suffix) >= max_chars:
        suffix = "[cut]\n"[:max_chars]
    return text[: max_chars - len(suffix)] + suffix


def _one_line(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
