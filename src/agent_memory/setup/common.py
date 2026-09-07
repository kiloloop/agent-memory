# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Install a runtime's memory hook: ``agent-memory setup <runtime>``.

One planner and one applier serve every runtime; a :class:`RuntimeSpec`
carries what differs (the settings file, the script path, the hook event
and matcher, the entry's fields, and the legacy commands to retire). The
plan is computed in full, against the repository as it is, before a byte is
written, and ``--dry-run`` prints exactly that plan. Every step is
idempotent, so an interrupted apply is resumed by running setup again.

The grammar, each rule pinned by a test:

1. The hook script is written from a shipped template. A file already there
   is regenerated only while its digest matches a template this or an earlier
   version shipped; any other content is a named conflict and is kept. A
   template that lost its execute bit is made executable again, and a script
   that cannot be made runnable is never registered.
2. The registration is added once, by exact command; an entry that carries
   it is never edited, wherever it sits.
3. Legacy registrations are retired by exact command, and a legacy flag is
   removed from one simple command only, never from a compound one. Their
   scripts are removed only when every one of these holds: the settings file
   was read in full, no command anywhere in it still mentions the script,
   the digest is the template that wrote it, and no symlink lies between the
   repository and the file. Anything else is kept and named.
4. Nothing is written through a symlink: a linked script, settings file or
   hook directory is reported with its target and left to its owner.
5. A receipt in the home records what was installed, at which version and
   digest, and what was retired, so a later run and a fleet census can tell
   this tool's files from everybody else's.
6. No push hook is ever installed; a session's end is the runtime's own.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .. import __version__, layout, workflow

RECEIPT_SCHEMA_VERSION = 1
SCRIPT_MODE = 0o755
#: The verb the generated hook runs; it pulls, then prints the bounded read manifest.
MANIFEST_VERB = "startup"

WRITE_SCRIPT = "write_script"
REGENERATE_SCRIPT = "regenerate_script"
CHMOD_SCRIPT = "chmod_script"
SCRIPT_IN_PLACE = "script_in_place"
SCRIPT_CONFLICT = "script_conflict"
CREATE_SETTINGS = "create_settings"
REGISTER = "register"
REGISTERED = "registered"
REGISTRATION_HELD = "registration_held"
RETIRE_REGISTRATION = "retire_registration"
STRIP_FLAG = "strip_flag"
KEEP_FLAG = "keep_flag"
SETTINGS_CONFLICT = "settings_conflict"
REMOVE_LEGACY_FILE = "remove_legacy_file"
KEEP_LEGACY_FILE = "keep_legacy_file"
WRITE_WORKFLOW = "write_workflow"
REGENERATE_WORKFLOW = "regenerate_workflow"
WORKFLOW_IN_PLACE = "workflow_in_place"
WORKFLOW_CONFLICT = "workflow_conflict"

CHANGING_ACTIONS = frozenset(
    {
        WRITE_SCRIPT,
        REGENERATE_SCRIPT,
        CHMOD_SCRIPT,
        WRITE_WORKFLOW,
        REGENERATE_WORKFLOW,
        CREATE_SETTINGS,
        REGISTER,
        RETIRE_REGISTRATION,
        STRIP_FLAG,
        REMOVE_LEGACY_FILE,
    }
)
CONFLICT_ACTIONS = frozenset({SCRIPT_CONFLICT, SETTINGS_CONFLICT, WORKFLOW_CONFLICT})

#: How each action reads in the report: (marker, done, planned).
VERBS: Dict[str, Tuple[str, str, str]] = {
    WRITE_SCRIPT: ("+", "written", "would write"),
    REGENERATE_SCRIPT: ("~", "regenerated", "would regenerate"),
    CHMOD_SCRIPT: ("~", "made executable", "would make executable"),
    SCRIPT_IN_PLACE: ("=", "in place", "in place"),
    SCRIPT_CONFLICT: ("!", "conflict, kept", "conflict, would keep"),
    CREATE_SETTINGS: ("+", "created", "would create"),
    REGISTER: ("+", "registered", "would register"),
    REGISTERED: ("=", "already registered", "already registered"),
    REGISTRATION_HELD: ("!", "not registered", "would not register"),
    RETIRE_REGISTRATION: ("-", "retired", "would retire"),
    STRIP_FLAG: ("-", "retired flag", "would retire flag"),
    KEEP_FLAG: ("!", "flag kept", "would keep flag"),
    SETTINGS_CONFLICT: ("!", "conflict, not edited", "conflict, would not edit"),
    REMOVE_LEGACY_FILE: ("-", "removed", "would remove"),
    KEEP_LEGACY_FILE: ("!", "kept", "would keep"),
    WRITE_WORKFLOW: ("+", "written", "would write"),
    REGENERATE_WORKFLOW: ("~", "regenerated", "would regenerate"),
    WORKFLOW_IN_PLACE: ("=", "in place", "in place"),
    WORKFLOW_CONFLICT: ("!", "conflict, kept", "conflict, would keep"),
}

#: Tokens the shell reads as operators; a command carrying one is compound and is never edited.
_SHELL_OPERATOR_CHARS = frozenset("();<>|&$`")

SCRIPT_TEMPLATE = """\
#!/usr/bin/env bash
# agent-memory SessionStart hook for {name}; managed by `agent-memory setup {name}`.
# A rerun regenerates this file only while its digest matches a shipped template;
# an edited file is reported as a conflict and kept as it is.
set -u
export AGENT_MEMORY_AGENT="${{AGENT_MEMORY_AGENT:-{name}}}"
report() {{
{report_body}
}}
if ! command -v agent-memory >/dev/null 2>&1; then
  report "agent-memory: command not found; memory not pulled and no startup manifest (install agent-memory-cli)."
  exit 0
fi
agent-memory {verb} --runtime {name} --pull || report "agent-memory: {verb} exited $?; local memory may be stale."
exit 0
"""


class SetupError(Exception):
    """A precondition failed; nothing was changed."""


@dataclass(frozen=True)
class RuntimeSpec:
    """What one runtime's hook installation looks like."""

    name: str
    #: The runtime's hook settings file, repository-relative.
    settings_file: str
    #: The hook script, repository-relative; it is also the registered command.
    script_file: str
    event: str
    matcher: str
    timeout: int
    #: Shell lines of the script's ``report`` function: how a warning reaches the runtime.
    report_body: str
    #: Extra fields on the hook entry (a status message, say).
    hook_fields: Mapping[str, Any] = field(default_factory=dict)
    #: Top-level keys a settings file created from scratch starts with.
    fresh_settings: Mapping[str, Any] = field(default_factory=dict)
    #: Registrations retired by exact command, per event.
    legacy_registrations: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    #: Files removed only on a digest match, per repository-relative path.
    legacy_files: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    #: ``(argv prefix, flag)``: the flag is removed from any hook command with that prefix.
    legacy_flag: Optional[Tuple[Tuple[str, ...], str]] = None
    #: Digests of this runtime's script as earlier versions shipped it; those regenerate.
    previous_template_digests: Tuple[str, ...] = ()
    #: The workflow file, repository-relative: this runtime's thin wrapper of the shipped workflow text.
    workflow_file: str = ""
    #: Digests of this runtime's workflow file as earlier versions shipped it; those regenerate.
    previous_workflow_digests: Tuple[str, ...] = ()

    def hook(self) -> Dict[str, Any]:
        return {"type": "command", "command": self.script_file, "timeout": self.timeout, **dict(self.hook_fields)}

    def entry(self) -> Dict[str, Any]:
        return {"matcher": self.matcher, "hooks": [self.hook()]}


def script_text(spec: RuntimeSpec) -> str:
    """The hook script for ``spec``, byte for byte what setup writes."""
    return SCRIPT_TEMPLATE.format(name=spec.name, report_body=spec.report_body, verb=MANIFEST_VERB)


def template_digest(spec: RuntimeSpec) -> str:
    return _digest_bytes(script_text(spec).encode("utf-8"))


def known_template_digests(spec: RuntimeSpec) -> Tuple[str, ...]:
    """Every digest a file at the script path may carry and still count as this tool's."""
    return (template_digest(spec), *spec.previous_template_digests)


def workflow_text(spec: RuntimeSpec) -> str:
    """The workflow file for ``spec``, byte for byte what setup writes: the shipped text with the runtime's name."""
    return workflow.workflow_text(spec.name)


def workflow_digest(spec: RuntimeSpec) -> str:
    return _digest_bytes(workflow_text(spec).encode("utf-8"))


def known_workflow_digests(spec: RuntimeSpec) -> Tuple[str, ...]:
    """Every digest a file at the workflow path may carry and still count as this tool's."""
    return (workflow_digest(spec), *spec.previous_workflow_digests)


@dataclass(frozen=True)
class Step:
    """One planned action on one repository-relative path."""

    action: str
    path: str
    detail: str = ""

    @property
    def conflict(self) -> bool:
        return self.action in CONFLICT_ACTIONS

    @property
    def changes(self) -> bool:
        return self.action in CHANGING_ACTIONS


@dataclass
class Plan:
    """Everything an apply will do, computed before it does any of it."""

    spec: RuntimeSpec
    repo: Path
    home: Path
    steps: List[Step]
    #: Bytes to place at the script path; ``None`` when the script is not written.
    script_write: Optional[bytes]
    #: Whether the script write replaces an earlier template (else it creates the file).
    script_regenerate: bool
    #: Whether a template already at the script path only needs its execute bit back.
    script_chmod: bool
    #: Text to write to the settings file; ``None`` when it is not written.
    settings_write: Optional[str]
    removals: List[Path]
    receipt_path: Path
    receipt: Dict[str, Any]
    receipt_write: bool
    #: Bytes to place at the workflow path; ``None`` when it is not written.
    workflow_write: Optional[bytes] = None
    #: Whether the workflow write replaces an earlier template (else it creates the file).
    workflow_regenerate: bool = False

    @property
    def conflicts(self) -> List[Step]:
        return [step for step in self.steps if step.conflict]

    @property
    def changed(self) -> bool:
        return any(step.changes for step in self.steps) or self.receipt_write


@dataclass(frozen=True)
class Result:
    plan: Plan
    dry_run: bool


# --- planning ---------------------------------------------------------------


def plan_setup(
    spec: RuntimeSpec,
    repo: Path,
    home: Path,
    *,
    version: str = __version__,
    now: Optional[dt.datetime] = None,
) -> Plan:
    """Inspect ``repo`` and ``home`` and decide every step; nothing is written."""
    repo = _absolute(repo)
    home = _absolute(home)
    if not repo.is_dir():
        raise SetupError(f"{repo} is not a directory")
    if not home.is_dir():
        raise SetupError(f"memory home {home} does not exist; create it with `agent-memory init` first")

    steps: List[Step] = []
    script = _plan_script(spec, repo, steps)
    settings_write, data_after, settings_state = _plan_settings(spec, repo, steps, script_present=script.present)
    removals = _plan_legacy_files(spec, repo, data_after, settings_state, steps)
    flow = _plan_workflow(spec, repo, steps)

    receipt_path = home / layout.SETUP_DIR / spec.name / f"{_repo_key(repo)}.json"
    previous = _load_json(receipt_path)
    receipt = _receipt(
        spec,
        repo,
        home,
        version=version,
        steps=steps,
        script_state=script.state,
        script_digest=script.digest,
        script_mode=script.mode,
        settings_state=settings_state,
        settings_write=settings_write,
        workflow_state=flow.state,
        workflow_digest_value=flow.digest,
        previous=previous if isinstance(previous, dict) else None,
        now=now or dt.datetime.now(dt.timezone.utc),
    )
    receipt_write = not isinstance(previous, dict) or _without_stamp(previous) != _without_stamp(receipt)
    return Plan(
        spec,
        repo,
        home,
        steps,
        script.write,
        script.regenerate,
        script.chmod,
        settings_write,
        removals,
        receipt_path,
        receipt,
        receipt_write,
        workflow_write=flow.write,
        workflow_regenerate=flow.regenerate,
    )


@dataclass(frozen=True)
class _ScriptPlan:
    #: Bytes to place at the script path; ``None`` when nothing is written there.
    write: Optional[bytes]
    regenerate: bool
    #: ``written``, ``regenerated``, ``in_place`` or ``conflict``.
    state: str
    #: The digest the path will carry after the apply, when it can be known.
    digest: Optional[str]
    #: Whether a runnable file will be at the path after the apply, so registering it makes sense.
    present: bool
    #: The permission bits the path will carry after the apply; ``None`` when nothing is there.
    mode: Optional[int] = None
    #: Whether the apply only restores the execute bit of a template already in place.
    chmod: bool = False


def _plan_script(spec: RuntimeSpec, repo: Path, steps: List[Step]) -> _ScriptPlan:
    rel = spec.script_file
    script = repo / rel
    text = script_text(spec).encode("utf-8")
    current = template_digest(spec)
    known = known_template_digests(spec)

    link = _linked_component(repo, script)
    if link is not None:
        target = _realpath(link)
        content = _digest_file(script)
        mode = _mode_of(script, None)
        if content in known and _executable(mode):
            steps.append(
                Step(SCRIPT_IN_PLACE, rel, f"through the symlink {_rel(repo, link)} -> {target}; shared source left as it is")
            )
            return _ScriptPlan(None, False, "in_place", content, True, mode)
        if content is None:
            what = "is missing or unreadable there"
        elif content not in known:
            what = "differs from every shipped template"
        else:
            what = f"is a shipped template but not executable (mode {_octal(mode)}), and its mode is not changed through the link"
        steps.append(
            Step(
                SCRIPT_CONFLICT,
                rel,
                f"{_rel(repo, link)} is a symlink to {target}; the shared source {what} and is not written through",
            )
        )
        return _ScriptPlan(None, False, "conflict", content, content is not None and _executable(mode), mode)
    if os.path.lexists(script):
        if not script.is_file():
            steps.append(Step(SCRIPT_CONFLICT, rel, "exists and is not a regular file"))
            return _ScriptPlan(None, False, "conflict", None, False)
        content = _digest_file(script)
        mode = _mode_of(script, None)
        if content is None:
            steps.append(Step(SCRIPT_CONFLICT, rel, "exists and cannot be read"))
            return _ScriptPlan(None, False, "conflict", None, False, mode)
        if content == current:
            if _executable(mode):
                steps.append(Step(SCRIPT_IN_PLACE, rel, f"digest {_short(current)} is the shipped template"))
                return _ScriptPlan(None, False, "in_place", content, True, mode)
            steps.append(Step(CHMOD_SCRIPT, rel, f"digest {_short(current)} is the shipped template at mode {_octal(mode)}"))
            return _ScriptPlan(None, False, "in_place", content, True, SCRIPT_MODE, chmod=True)
        if content in known:
            steps.append(Step(REGENERATE_SCRIPT, rel, f"digest {_short(content)} is an earlier template; now {_short(current)}"))
            return _ScriptPlan(text, True, "regenerated", current, True, SCRIPT_MODE)
        steps.append(Step(SCRIPT_CONFLICT, rel, f"digest {_short(content)} matches no shipped template; edited, kept as it is"))
        return _ScriptPlan(None, False, "conflict", content, _executable(mode), mode)
    steps.append(Step(WRITE_SCRIPT, rel, f"template digest {_short(current)}"))
    return _ScriptPlan(text, False, "written", current, True, SCRIPT_MODE)


@dataclass(frozen=True)
class _FilePlan:
    #: Bytes to place at the path; ``None`` when nothing is written there.
    write: Optional[bytes]
    regenerate: bool
    #: ``written``, ``regenerated``, ``in_place``, ``conflict``, or ``absent`` when the runtime has no such file.
    state: str
    digest: Optional[str]


def _plan_workflow(spec: RuntimeSpec, repo: Path, steps: List[Step]) -> _FilePlan:
    """The workflow file follows the script's rules without the execute bit: written once, regenerated only
    while its digest is a shipped template, kept and named as a conflict when edited, never written through a link."""
    if not spec.workflow_file:
        return _FilePlan(None, False, "absent", None)
    rel = spec.workflow_file
    target = repo / rel
    text = workflow_text(spec).encode("utf-8")
    current = workflow_digest(spec)
    known = known_workflow_digests(spec)

    link = _linked_component(repo, target)
    if link is not None:
        content = _digest_file(target)
        if content in known:
            steps.append(
                Step(WORKFLOW_IN_PLACE, rel, f"through the symlink {_rel(repo, link)} -> {_realpath(link)}; shared source left as it is")
            )
            return _FilePlan(None, False, "in_place", content)
        what = "is missing or unreadable there" if content is None else "differs from every shipped template"
        steps.append(
            Step(WORKFLOW_CONFLICT, rel, f"{_rel(repo, link)} is a symlink to {_realpath(link)}; the shared source {what} and is not written through")
        )
        return _FilePlan(None, False, "conflict", content)
    if os.path.lexists(target):
        if not target.is_file():
            steps.append(Step(WORKFLOW_CONFLICT, rel, "exists and is not a regular file"))
            return _FilePlan(None, False, "conflict", None)
        content = _digest_file(target)
        if content is None:
            steps.append(Step(WORKFLOW_CONFLICT, rel, "exists and cannot be read"))
            return _FilePlan(None, False, "conflict", None)
        if content == current:
            steps.append(Step(WORKFLOW_IN_PLACE, rel, f"digest {_short(current)} is the shipped template"))
            return _FilePlan(None, False, "in_place", content)
        if content in known:
            steps.append(Step(REGENERATE_WORKFLOW, rel, f"digest {_short(content)} is an earlier template; now {_short(current)}"))
            return _FilePlan(text, True, "regenerated", current)
        steps.append(Step(WORKFLOW_CONFLICT, rel, f"digest {_short(content)} matches no shipped template; edited, kept as it is"))
        return _FilePlan(None, False, "conflict", content)
    steps.append(Step(WRITE_WORKFLOW, rel, f"template digest {_short(current)}"))
    return _FilePlan(text, False, "written", current)


def _executable(mode: Optional[int]) -> bool:
    """Whether the owner, who runs the hook, may execute a file of ``mode``."""
    return mode is not None and bool(mode & stat.S_IXUSR)


def _plan_settings(
    spec: RuntimeSpec, repo: Path, steps: List[Step], *, script_present: bool
) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    rel = spec.settings_file
    settings = repo / rel

    link = _linked_component(repo, settings)
    if link is not None:
        steps.append(
            Step(
                SETTINGS_CONFLICT,
                rel,
                f"{_rel(repo, link)} is a symlink to {_realpath(link)}; the shared source is not edited, "
                f"register {spec.script_file} there yourself",
            )
        )
        return None, _load_json(settings), "conflict"

    before: Optional[str] = None
    created = False
    if os.path.lexists(settings):
        if not settings.is_file():
            steps.append(Step(SETTINGS_CONFLICT, rel, "exists and is not a regular file"))
            return None, None, "conflict"
        try:
            before = settings.read_text(encoding="utf-8")
            data = json.loads(before)
        except (OSError, ValueError) as exc:
            steps.append(Step(SETTINGS_CONFLICT, rel, f"cannot be read as JSON: {exc}"))
            return None, None, "conflict"
        if not isinstance(data, dict) or ("hooks" in data and not isinstance(data["hooks"], dict)):
            steps.append(Step(SETTINGS_CONFLICT, rel, "expected a JSON object whose 'hooks' is an object"))
            return None, None, "conflict"
    else:
        data = dict(spec.fresh_settings)
        created = True
        steps.append(Step(CREATE_SETTINGS, rel, ""))

    hooks: Dict[str, Any] = data.setdefault("hooks", {})
    own = hooks.get(spec.event)
    if own is not None and not isinstance(own, list):
        steps.append(Step(SETTINGS_CONFLICT, rel, f"hooks.{spec.event} is not a list"))
        return None, data, "conflict"

    changed = created
    # The legacy hooks go only once their replacement can run: a repository is never left with no memory hook.
    if script_present:
        for event, commands in spec.legacy_registrations.items():
            entries = hooks.get(event)
            if not isinstance(entries, list):
                continue
            for command in commands:
                if _remove_command(entries, command):
                    steps.append(Step(RETIRE_REGISTRATION, rel, f"{event}: {command}"))
                    changed = True
            if not entries:
                del hooks[event]
        if spec.legacy_flag is not None and isinstance(own, list):
            prefix, flag = spec.legacy_flag
            for old, new, why in _strip_flag(own, prefix, flag):
                if new is None:
                    steps.append(Step(KEEP_FLAG, rel, f"{spec.event}: {flag} left in `{old}`; {why}"))
                    continue
                steps.append(Step(STRIP_FLAG, rel, f"{spec.event}: {flag} removed from `{old}`, now `{new}`"))
                changed = True

    entries = hooks.setdefault(spec.event, [])
    label = f"{spec.event} {spec.matcher!r}: {spec.script_file}"
    if _command_registered(entries, spec.script_file):
        steps.append(Step(REGISTERED, rel, label))
    elif not script_present:
        steps.append(
            Step(
                REGISTRATION_HELD,
                rel,
                f"{label}; the script is not in place, so nothing is registered to run it and the legacy hooks stay",
            )
        )
    else:
        entries.append(spec.entry())
        steps.append(Step(REGISTER, rel, label))
        changed = True
    if not entries:
        del hooks[spec.event]

    if not changed:
        return None, data, "unchanged"
    after = _dumps(data)
    if after == before:
        return None, data, "unchanged"
    return after, data, "created" if created else "updated"


def _plan_legacy_files(
    spec: RuntimeSpec, repo: Path, data_after: Optional[Dict[str, Any]], settings_state: str, steps: List[Step]
) -> List[Path]:
    """Legacy scripts to remove: only a local, unedited template that the fully read settings no longer mention.

    Every uncertainty keeps the file: a settings file that could not be read in full
    (so the registrations are unknown), a command anywhere in it that still names
    the script, however it is invoked, a symlink between the repository and the file,
    a digest that is not the template's.
    """
    removals: List[Path] = []
    inspected = settings_state != "conflict" and isinstance(data_after, dict)
    hooks = data_after.get("hooks") if inspected and isinstance(data_after, dict) else None
    for rel, digests in spec.legacy_files.items():
        path = repo / rel
        if not os.path.lexists(path):
            continue
        link = _linked_component(repo, path)
        if link is not None:
            steps.append(Step(KEEP_LEGACY_FILE, rel, f"{_rel(repo, link)} is a symlink to {_realpath(link)}; not removed"))
            continue
        if not path.is_file():
            steps.append(Step(KEEP_LEGACY_FILE, rel, "not a regular file; not removed"))
            continue
        digest = _digest_file(path)
        if digest is None or digest not in digests:
            shown = "unreadable" if digest is None else f"digest {_short(digest)}"
            steps.append(Step(KEEP_LEGACY_FILE, rel, f"{shown} is not the template that installed it; edited, kept"))
            continue
        if not inspected:
            steps.append(Step(KEEP_LEGACY_FILE, rel, f"{spec.settings_file} could not be read in full; still registered for all this tool knows"))
            continue
        mention = _mentioned_anywhere(hooks, rel)
        if mention is not None:
            what = "still registered" if mention == rel else f"still named by `{mention}`"
            steps.append(Step(KEEP_LEGACY_FILE, rel, f"{what}; not removed"))
            continue
        steps.append(Step(REMOVE_LEGACY_FILE, rel, f"digest {_short(digest)} is the legacy template"))
        removals.append(path)
    return removals


# --- the receipt ------------------------------------------------------------


def _receipt(
    spec: RuntimeSpec,
    repo: Path,
    home: Path,
    *,
    version: str,
    steps: Sequence[Step],
    script_state: str,
    script_digest: Optional[str],
    script_mode: Optional[int],
    settings_state: str,
    settings_write: Optional[str],
    workflow_state: str,
    workflow_digest_value: Optional[str],
    previous: Optional[Dict[str, Any]],
    now: dt.datetime,
) -> Dict[str, Any]:
    """The receipt as it stands after the apply: states, not actions, so a no-op rerun leaves it as it is.

    ``retired`` is a history: what earlier runs retired stays recorded, and this run's
    retirements are added once.
    """
    script = repo / spec.script_file
    settings = repo / spec.settings_file
    if settings_write is not None:
        settings_digest: Optional[str] = _digest_bytes(settings_write.encode("utf-8"))
    else:
        settings_digest = _digest_file(settings)
    if settings_state == "conflict":
        registration_state = "conflict"
    elif any(step.action == REGISTRATION_HELD for step in steps):
        registration_state = "held"
    else:
        registration_state = "registered"
    managed = [
        {
            "path": spec.script_file,
            "resolved": _realpath(script),
            "symlink": _linked_component(repo, script) is not None,
            "state": "conflict" if script_state == "conflict" else "installed",
            "digest": script_digest,
            "template_digest": template_digest(spec),
            "mode": _octal(script_mode) if script_mode is not None else None,
        },
        {
            "path": spec.settings_file,
            "resolved": _realpath(settings),
            "symlink": _linked_component(repo, settings) is not None,
            "state": registration_state,
            "digest": settings_digest,
            "registration": {"event": spec.event, "matcher": spec.matcher, "command": spec.script_file},
        },
    ]
    if spec.workflow_file:
        flow = repo / spec.workflow_file
        managed.append(
            {
                "path": spec.workflow_file,
                "resolved": _realpath(flow),
                "symlink": _linked_component(repo, flow) is not None,
                "state": "conflict" if workflow_state == "conflict" else "installed",
                "digest": workflow_digest_value,
                "template_digest": workflow_digest(spec),
            }
        )
    retired: List[Dict[str, Any]] = []
    if previous is not None and isinstance(previous.get("retired"), list):
        retired.extend(item for item in previous["retired"] if isinstance(item, dict))
    for step in steps:
        if step.action in (RETIRE_REGISTRATION, STRIP_FLAG):
            item: Dict[str, Any] = {"kind": "registration", "path": step.path, "detail": step.detail}
        elif step.action in (REMOVE_LEGACY_FILE, KEEP_LEGACY_FILE):
            item = {"kind": "file", "path": step.path, "removed": step.action == REMOVE_LEGACY_FILE, "detail": step.detail}
        else:
            continue
        if item not in retired:
            retired.append(item)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "tool": "agent-memory",
        "version": version,
        "runtime": spec.name,
        "repo": str(repo),
        "repo_resolved": _realpath(repo),
        "home": str(home),
        "written_at_utc": _iso(now),
        "managed": managed,
        "retired": retired,
        "conflicts": [{"path": step.path, "detail": step.detail} for step in steps if step.conflict],
    }


def _without_stamp(receipt: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in receipt.items() if key != "written_at_utc"}


def _repo_key(repo: Path) -> str:
    return hashlib.sha256(_realpath(repo).encode("utf-8")).hexdigest()[:16]


# --- applying ---------------------------------------------------------------


def apply_plan(plan: Plan) -> None:
    """Perform the plan's writes in a fixed order; each is safe to repeat after an interruption."""
    spec = plan.spec
    if plan.script_write is not None:
        script = plan.repo / spec.script_file
        script.parent.mkdir(parents=True, exist_ok=True)
        if plan.script_regenerate:
            _replace_file(script, plan.script_write, SCRIPT_MODE)
        else:
            _create_file(script, plan.script_write, SCRIPT_MODE)
    elif plan.script_chmod:
        _chmod_file(plan.repo / spec.script_file, SCRIPT_MODE)
    if plan.workflow_write is not None:
        flow = plan.repo / spec.workflow_file
        flow.parent.mkdir(parents=True, exist_ok=True)
        if plan.workflow_regenerate:
            _replace_file(flow, plan.workflow_write, _mode_of(flow, 0o644))
        else:
            _create_file(flow, plan.workflow_write, 0o644)
    if plan.settings_write is not None:
        settings = plan.repo / spec.settings_file
        settings.parent.mkdir(parents=True, exist_ok=True)
        _replace_file(settings, plan.settings_write.encode("utf-8"), _mode_of(settings, 0o644))
    for path in plan.removals:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    if plan.receipt_write:
        plan.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        _replace_file(plan.receipt_path, _dumps(plan.receipt).encode("utf-8"), _mode_of(plan.receipt_path, 0o644))


def run_setup(spec: RuntimeSpec, repo: Path, home: Path, *, dry_run: bool = False, version: str = __version__) -> Result:
    """Plan, and unless ``dry_run``, apply."""
    plan = plan_setup(spec, repo, home, version=version)
    if not dry_run:
        apply_plan(plan)
    return Result(plan, dry_run)


def detect_repo(start: Path) -> Path:
    """The nearest directory at or above ``start`` holding a ``.git`` entry, else ``start`` itself."""
    start = _absolute(start)
    for directory in (start, *start.parents):
        if os.path.lexists(directory / ".git"):
            return directory
    return start


# --- reporting --------------------------------------------------------------


def lines(result: Result) -> List[str]:
    plan = result.plan
    out = [f"agent-memory setup {plan.spec.name}: {plan.repo}", f"home: {plan.home}"]
    for step in plan.steps:
        marker, done, planned = VERBS[step.action]
        verb = planned if result.dry_run else done
        detail = f" ({step.detail})" if step.detail else ""
        out.append(f"  {marker} {step.path}: {verb}{detail}")
    receipt_verb = ("would write" if result.dry_run else "written") if plan.receipt_write else "unchanged"
    out.append(f"receipt: {plan.receipt_path} ({receipt_verb})")
    if plan.conflicts:
        out.append(f"conflicts: {len(plan.conflicts)}; nothing marked ! was written. Resolve them and run setup again.")
    if result.dry_run:
        out.append("dry run: nothing was written.")
    elif not plan.changed:
        out.append("nothing to do: the hook and the workflow file are installed and in place.")
    return out


def to_json(result: Result) -> Dict[str, Any]:
    plan = result.plan
    return {
        "schema_version": 1,
        "action": "setup",
        "runtime": plan.spec.name,
        "repo": str(plan.repo),
        "home": str(plan.home),
        "dry_run": result.dry_run,
        "changed": plan.changed,
        "steps": [
            {"action": step.action, "path": step.path, "detail": step.detail, "changes": step.changes, "conflict": step.conflict}
            for step in plan.steps
        ],
        "conflicts": [step.path for step in plan.conflicts],
        "receipt": {"path": str(plan.receipt_path), "written": plan.receipt_write and not result.dry_run},
    }


# --- hook-list surgery, shared by every runtime -----------------------------


def _command_registered(entries: Sequence[Any], command: str) -> bool:
    for entry in entries:
        for hook in _hooks_of(entry):
            if hook.get("command") == command:
                return True
    return False


def _mentioned_anywhere(hooks: Any, path: str) -> Optional[str]:
    """The first hook command, under any event, whose text names ``path`` or its basename; ``None`` when none does.

    A custom wrapper (``bash .claude/hooks/x.sh``) still runs the file, so any mention
    keeps it; only an exact command is ever retired.
    """
    if not isinstance(hooks, dict):
        return None
    names = (path, os.path.basename(path))
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for hook in _hooks_of(entry):
                command = hook.get("command")
                if isinstance(command, str) and any(name in command for name in names):
                    return command
    return None


def _remove_command(entries: List[Any], command: str) -> bool:
    """Drop every hook whose command is exactly ``command``; entries left empty go too. True when anything changed."""
    changed = False
    retained: List[Any] = []
    for entry in entries:
        hooks = _hooks_of(entry)
        if not hooks:
            retained.append(entry)
            continue
        kept = [hook for hook in entry["hooks"] if not (isinstance(hook, dict) and hook.get("command") == command)]
        if len(kept) == len(entry["hooks"]):
            retained.append(entry)
            continue
        changed = True
        if kept:
            updated = dict(entry)
            updated["hooks"] = kept
            retained.append(updated)
    if changed:
        entries[:] = retained
    return changed


def _strip_flag(entries: Sequence[Any], prefix: Sequence[str], flag: str) -> Iterator[Tuple[str, Optional[str], str]]:
    """Remove ``flag`` from every hook command that starts with ``prefix``; yields (before, after, reason).

    The edit is made on the command text as written, so quoting and every other
    byte survive; ``after`` is ``None``, with the reason, when the command is not
    one simple command (an operator, redirection or substitution makes it compound)
    or the flag is not a bare word in it. Such a command is left exactly as it is.
    """
    for entry in entries:
        for hook in _hooks_of(entry):
            command = hook.get("command")
            if not isinstance(command, str):
                continue
            parsed = _tokens(command)
            if parsed is None:
                continue
            argv, simple = parsed
            if argv[: len(prefix)] != list(prefix) or flag not in argv:
                continue
            if not simple:
                yield command, None, "not one simple command; edited by hand if the flag should go"
                continue
            rewritten = re.sub(rf"\s+{re.escape(flag)}(?!\S)", "", command)
            try:
                left = shlex.split(rewritten)
            except ValueError:
                left = None
            if left != [arg for arg in argv if arg != flag]:
                yield command, None, "the flag is not a bare word in it"
                continue
            hook["command"] = rewritten
            yield command, rewritten, ""


def _tokens(command: str) -> Optional[Tuple[List[str], bool]]:
    """``(tokens, simple)``: the shell's tokens of ``command``, and whether they make one simple command.

    Operators, redirections and substitutions (``&&``, ``;``, ``|``, ``>``, ``$(``,
    backticks) are read as the shell would, so a flag after a ``;`` is still seen
    and the command is still known to be compound. A newline separates commands
    too, and a lexer folds it into whitespace, so any multi-line text is compound
    by rule. ``None`` on unbalanced quotes.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
        words = shlex.split(command)
    except ValueError:
        return None
    simple = (
        tokens == words
        and "`" not in command
        and "\n" not in command
        and "\r" not in command
        and not any(token and set(token) <= _SHELL_OPERATOR_CHARS for token in tokens)
    )
    return tokens, simple


def _hooks_of(entry: Any) -> List[Dict[str, Any]]:
    if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
        return []
    return [hook for hook in entry["hooks"] if isinstance(hook, dict)]


# --- files ------------------------------------------------------------------


def _linked_component(repo: Path, path: Path) -> Optional[Path]:
    """The first symlink on the way from ``repo`` down to ``path`` (``path`` included), or ``None``."""
    current = repo
    for part in path.relative_to(repo).parts:
        current = current / part
        if current.is_symlink():
            return current
    return None


def _create_file(path: Path, data: bytes, mode: int) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError as exc:
        raise SetupError(f"{path} appeared while setup was running; run setup again") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)


def _replace_file(path: Path, data: bytes, mode: int) -> None:
    handle = tempfile.NamedTemporaryFile("wb", dir=str(path.parent), prefix=f".{path.name}.", delete=False)
    with handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temp = Path(handle.name)
    try:
        os.chmod(temp, mode)
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _chmod_file(path: Path, mode: int) -> None:
    """Set ``mode`` on the regular file at ``path`` itself, never on whatever a link there points at."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _mode_of(path: Path, default: Optional[int]) -> Optional[int]:
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return default


def _digest_file(path: Path) -> Optional[str]:
    try:
        return _digest_bytes(path.read_bytes())
    except OSError:
        return None


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _short(digest: Optional[str]) -> str:
    return (digest or "?")[:12]


def _octal(mode: Optional[int]) -> str:
    return f"{mode:04o}" if mode is not None else "unknown"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _dumps(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _realpath(path: Path) -> str:
    return os.path.realpath(path)


def _rel(repo: Path, path: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return str(path)


def _absolute(value: Path) -> Path:
    try:
        return Path(value).expanduser().absolute()
    except RuntimeError as exc:
        raise SetupError(f"cannot expand {value}: {exc}") from exc


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
