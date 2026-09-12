# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Command-line interface: ``agent-memory``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import __version__, archive, debrief, doctor, events, org, setup, startup, status, sync, workflow
from .home import BINDING_FILE, HomeError, HomeResolution, find_project, resolve_home

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
#: ``setup`` found something it will not write over; the report names it.
EXIT_CONFLICT = 3


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--home",
        metavar="PATH",
        help="memory home (default: $AGENT_MEMORY_HOME, then $OACP_HOME, then a binding or workspace marker "
        "found above the working directory, else ~/agent-memory)",
    )
    agent = argparse.ArgumentParser(add_help=False)
    agent.add_argument(
        "--agent",
        metavar="NAME",
        help=f"the agent the commit is published under (default: ${sync.ENV_AGENT}, then $AGENT_NAME, then $USER)",
    )
    parser = argparse.ArgumentParser(
        prog="agent-memory",
        description="Cross-session memory for coding agents: plain files, git-native, no server.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, handler: Callable[[argparse.Namespace], int], help_text: str, *parents: argparse.ArgumentParser) -> argparse.ArgumentParser:
        command = commands.add_parser(name, parents=[common, *parents], help=help_text, description=help_text)
        command.set_defaults(handler=handler)
        return command

    status_ = add(
        "status",
        _status,
        "Show which home resolves, which rule chose it, whether the layout is in place, and where the sync stands. "
        "Exit 1 when the tree is dirty or diverged.",
    )
    status_.add_argument(
        "--fetch", action="store_true", help="contact the remote before counting ahead/behind (no network otherwise)"
    )
    doctor_ = add(
        "doctor",
        _doctor,
        "Check the home's setup and health: the debrief store's layout and the sync repository. "
        "Reads no memory content; repairs nothing. Exit 1 on an error row.",
    )
    doctor_.add_argument("--json", dest="json_output", action="store_true", help="emit the report as JSON")
    init_ = add(
        "init",
        _init,
        "Create the memory home from the templates bundled in the package: the org tier, and with --project that "
        "project's tier; with --repo, bind that repository to the home. No git, no network.",
    )
    init_.add_argument(
        "--project", metavar="ID", help="also create this project's memory tier (derived from --repo's name when omitted)"
    )
    init_.add_argument(
        "--repo", metavar="PATH", help=f"record a {BINDING_FILE} binding in this repository, after every other check"
    )
    org_ = commands.add_parser("org", help="Org-tier commands.", description="Org-tier commands.")
    org_commands = org_.add_subparsers(dest="org_command", metavar="<command>")
    org_init = org_commands.add_parser(
        "init",
        parents=[common],
        help="Scaffold the org tier of an existing home from the bundled templates.",
        description="Scaffold the org tier of an existing home from the bundled templates.",
    )
    org_init.set_defaults(handler=_org_init)
    enable = add(
        "enable", _enable, "Make the home a sync repository: git, the managed ignore block, the marker, one commit.", agent
    )
    enable.add_argument("--remote", metavar="URL", help="git remote to sync with (added or updated as 'origin')")
    clone_ = add("clone", _clone, "Clone a memory repository into the home.")
    clone_.add_argument("url", help="git remote URL to clone")
    clone_.add_argument("--force", action="store_true", help="move a non-empty home aside before cloning")
    add("pull", _pull, "Fast-forward the home from its upstream when the tree is clean and not ahead.")
    add("push", _push, "Commit the allowlisted memory changes and push them when a remote exists.", agent)
    add("disable", _disable, "Remove the sync marker; the repository stays in place.")
    for name, handler, help_text, positional, positional_help in (
        ("archive", _archive, "Move a supplementary memory file into the project's memory/archive/.",
         "memory_file", "basename of the active memory file to archive"),
        ("restore", _restore, "Move an archived memory file back into the project's active memory.",
         "archived_file", "basename of the archived file to restore (<UTC timestamp>_<basename>)"),
    ):
        command = add(name, handler, help_text)
        command.add_argument("project", help="project name under projects/")
        command.add_argument(positional, help=positional_help)
        command.add_argument("--dry-run", action="store_true", help="perform every check and report; move nothing")
        command.add_argument("--json", dest="json_output", action="store_true", help="emit the result as JSON")
    setup_ = add(
        "setup",
        _setup,
        "Install the runtime's session-start memory hook in a repository: the script, its registration, a receipt "
        "in the home; retire the legacy hooks by exact match. Never writes over an edited file or through a symlink "
        "(exit 3 names what it kept). No push hook, ever.",
    )
    setup_.add_argument("runtime", choices=setup.RUNTIMES, help="the runtime whose hook to install")
    setup_.add_argument("--repo", metavar="PATH", help="the repository (default: the nearest .git above the working directory)")
    setup_.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    setup_.add_argument("--json", dest="json_output", action="store_true", help="emit the plan or result as JSON")
    startup_ = add(
        "startup",
        _startup,
        "Print the session-start manifest: the memory files to read, in order, with their readability, size and "
        "age, and where the sync stands. Content is never included. With --pull, fast-forward the home first.",
    )
    startup_.add_argument("--runtime", choices=startup.RUNTIMES, required=True, help="shape the output for this runtime's hook")
    startup_.add_argument("--project", metavar="ID", help="the project tier to list (default: the one the binding or marker names)")
    startup_.add_argument("--pull", action="store_true", help="pull the home before listing; a failed pull is a warning")
    startup_.add_argument("--json", dest="json_output", action="store_true", help="emit the manifest as JSON")
    startup_.add_argument(
        "--max-chars",
        type=_max_chars,
        default=startup.DEFAULT_MAX_CHARS,
        metavar="N",
        help=f"cut the rendered text, its notice included, at N characters (at least {startup.MIN_MAX_CHARS})",
    )
    capture_ = add(
        "capture",
        _capture,
        "Record one decision in the project's decision_log.md, newest first under today's UTC date, with its "
        "provenance (agent, time, source). The project comes from --project, else from the repository's binding or "
        "workspace marker.",
    )
    capture_.add_argument("decision", help="the decision, one sentence")
    capture_.add_argument("--why", metavar="TEXT", help="the reason, one sentence")
    capture_.add_argument("--source", metavar="REF", help="where it was decided: a PR, an issue, a message")
    capture_.add_argument(
        "--agent", metavar="NAME", help=f"who is capturing (default: ${workflow.DEFAULT_AGENT_ENV}, else the user)"
    )
    capture_.add_argument("--project", metavar="ID", help="the project whose log to write (default: the bound one)")
    capture_.add_argument("--dry-run", action="store_true", help="compose the entry and report; write nothing")
    capture_.add_argument("--json", dest="json_output", action="store_true", help="emit the result as JSON")
    recall_ = add(
        "recall",
        _recall,
        "Print the memory files the startup manifest lists, in its order, with their content, cut at a character "
        "budget: the bounded read at session start. archive/, events/ and debriefs/ are never loaded.",
    )
    recall_.add_argument("--project", metavar="ID", help="the project tier to read (default: the bound one)")
    recall_.add_argument(
        "--runtime",
        choices=startup.RUNTIMES,
        help=f"the runtime reading (default: ${workflow.DEFAULT_AGENT_ENV} when it names one, else {startup.RUNTIME_CLAUDE})",
    )
    recall_.add_argument(
        "--max-chars",
        type=_max_chars,
        default=startup.DEFAULT_MAX_CHARS,
        metavar="N",
        help=f"cut the text, its notice included, at N characters (at least {startup.MIN_MAX_CHARS})",
    )
    recall_.add_argument("--json", dest="json_output", action="store_true", help="emit the result as JSON")
    debrief_ = commands.add_parser("debrief", help="Debrief-store commands.", description="Debrief-store commands.")
    debrief_commands = debrief_.add_subparsers(dest="debrief_command", metavar="<command>")
    write_help = (
        "Publish one session debrief into the home's debrief store, failure-atomically: the canonical path only ever "
        "holds a complete, verified record. Exit 0 published (or an identical record was already there), 1 on a "
        "validation error, 2 on a publication failure."
    )
    write = debrief_commands.add_parser("write", parents=[common], help=write_help, description=write_help)
    write.set_defaults(handler=_debrief_write)
    write.add_argument("--project", required=True, help="workspace project name")
    write.add_argument("--agent", required=True, help="writing agent name")
    write.add_argument("--runtime", required=True, help="runtime family (claude, codex, ...)")
    write.add_argument("--session", required=True, help="short session id: 1-32 lowercase alphanumerics, no hyphens")
    write.add_argument("--started-utc", required=True, help="session start, ISO 8601 UTC (Z)")
    write.add_argument("--ended-utc", required=True, help="session end, ISO 8601 UTC (Z)")
    write.add_argument("--body-file", required=True, help="path to the debrief body in Markdown, or '-' to read stdin")
    # The name the writer carried before it became this verb; accepted, unadvertised, through v0.1.x.
    write.add_argument("--oacp-dir", dest="home", metavar="PATH", help=argparse.SUPPRESS)
    write.add_argument("--dry-run", action="store_true", help="validate and compose the record, print it, and write nothing")
    write.add_argument("--json", dest="json_output", action="store_true", help="emit a machine-readable result")
    event_ = commands.add_parser("event", help="Org-memory event commands.", description="Org-memory event commands.")
    event_commands = event_.add_subparsers(dest="event_command", metavar="<command>")
    event_help = (
        "Publish one org-memory event into the home's events store, failure-atomically: the canonical path only ever "
        "holds a complete, verified record, byte-identical to the kernel's write-event script for the same inputs. "
        "Exit 0 published (or an identical record was already there), 1 on a validation error, 2 on a publication "
        "failure."
    )
    event_write = event_commands.add_parser("write", parents=[common], help=event_help, description=event_help)
    event_write.set_defaults(handler=_event_write)
    event_write.add_argument("--agent", required=True, help="agent creating the event")
    event_write.add_argument("--project", required=True, help="originating project")
    event_write.add_argument(
        "--type", dest="event_type", required=True, choices=sorted(events.ALLOWED_TYPES), help="event type"
    )
    event_write.add_argument(
        "--slug", required=True, help="short slug for the filename: lowercase alphanumerics and hyphens, no dots"
    )
    event_body = event_write.add_mutually_exclusive_group()
    event_body.add_argument("--body", help="event body, inline")
    event_body.add_argument("--body-file", help="path to the event body in Markdown, or '-' to read stdin")
    event_write.add_argument("--source-ref", help="provenance id, e.g. the debrief stem the event was folded from")
    event_write.add_argument(
        "--related", help="cross-references: comma-separated or a JSON array (e.g. 'PR #43,issue #10')"
    )
    event_write.add_argument("--supersedes", help="path of the event this entry overrides")
    # The kernel script's home flag; accepted, unadvertised, through v0.1.x.
    event_write.add_argument("--oacp-dir", dest="home", metavar="PATH", help=argparse.SUPPRESS)
    event_write.add_argument(
        "--dry-run", action="store_true", help="validate and compose the record, print it, and write nothing"
    )
    event_write.add_argument("--json", dest="json_output", action="store_true", help="emit a machine-readable result")
    return parser


def _max_chars(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if number < startup.MIN_MAX_CHARS:
        raise argparse.ArgumentTypeError(f"must be at least {startup.MIN_MAX_CHARS}, got {number}")
    return number


def _status(args: argparse.Namespace) -> int:
    readout = status.inspect(resolve_home(args.home), fetch=args.fetch)
    print("\n".join(readout.lines()))
    return readout.exit_code


def _doctor(args: argparse.Namespace) -> int:
    home = resolve_home(args.home).path
    if not home.is_dir():
        print(f"agent-memory: error: {home} is not a directory", file=sys.stderr)
        return EXIT_FAILED
    categories = doctor.run_doctor(home)
    memory_lint = doctor.find_memory_lint()
    if args.json_output:
        print(json.dumps(doctor.to_json(categories, memory_lint=memory_lint), indent=2))
    else:
        sys.stdout.write(doctor.report(categories, memory_lint=memory_lint))
    return EXIT_FAILED if doctor.has_errors(categories) else EXIT_OK


def _init(args: argparse.Namespace) -> int:
    repo = Path(args.repo) if args.repo else None
    report = org.init(resolve_home(args.home).path, project=args.project, repo=repo)
    print("\n".join(report.lines()))
    return EXIT_OK


def _org_init(args: argparse.Namespace) -> int:
    print("\n".join(org.org_init(resolve_home(args.home).path).lines()))
    return EXIT_OK


def _enable(args: argparse.Namespace) -> int:
    return _report(sync.init(resolve_home(args.home).path, remote=args.remote, agent=args.agent))


def _clone(args: argparse.Namespace) -> int:
    return _report(sync.clone(resolve_home(args.home).path, args.url, force=args.force))


def _pull(args: argparse.Namespace) -> int:
    return _report(sync.pull(resolve_home(args.home).path))


def _push(args: argparse.Namespace) -> int:
    return _report(sync.push(resolve_home(args.home).path, agent=args.agent))


def _disable(args: argparse.Namespace) -> int:
    return _report(sync.disable(resolve_home(args.home).path))


def _archive(args: argparse.Namespace) -> int:
    result = archive.archive(resolve_home(args.home).path, args.project, args.memory_file, dry_run=args.dry_run)
    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        verb = "Would archive" if args.dry_run else "Archived"
        print(f"{verb} memory/{result['memory_file']} -> memory/{archive.ARCHIVE_DIR}/{result['archived_file']}")
    return EXIT_OK


def _restore(args: argparse.Namespace) -> int:
    result = archive.restore(resolve_home(args.home).path, args.project, args.archived_file, dry_run=args.dry_run)
    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        verb = "Would restore" if args.dry_run else "Restored"
        print(f"{verb} memory/{archive.ARCHIVE_DIR}/{result['archived_file']} -> memory/{result['restored_file']}")
    return EXIT_OK


def _setup(args: argparse.Namespace) -> int:
    spec = setup.SPECS[args.runtime]
    repo = Path(args.repo) if args.repo else setup.detect_repo(Path.cwd())
    result = setup.run_setup(spec, repo, resolve_home(args.home).path, dry_run=args.dry_run)
    if args.json_output:
        print(json.dumps(setup.to_json(result), indent=2))
    else:
        print("\n".join(setup.lines(result)))
    return EXIT_CONFLICT if result.plan.conflicts else EXIT_OK


def _resolve_project(flag: Optional[str], resolution: HomeResolution) -> Tuple[Optional[str], Optional[str], List[str]]:
    """``(project, source, notes)``: the flag, else what chose the home, else the repository's binding or marker."""
    if flag:
        return flag, "flag", []
    if resolution.project:
        return resolution.project, resolution.source, []
    # A flag or an environment variable chose the home; the repository's binding or marker still names the project.
    found = find_project(resolution.path, Path.cwd())
    return found.project, found.source, [found.note] if found.note else []


def _capture(args: argparse.Namespace) -> int:
    resolution = resolve_home(args.home)
    project, _, notes = _resolve_project(args.project, resolution)
    if project is None:
        for note in notes:
            print(f"agent-memory: {note}", file=sys.stderr)
        print(
            "agent-memory: error: no project resolved; pass --project or bind the repository with `agent-memory init --repo .`",
            file=sys.stderr,
        )
        return EXIT_USAGE
    result = workflow.capture(
        resolution.path, project, args.decision, why=args.why, source=args.source, agent=args.agent, dry_run=args.dry_run
    )
    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        verb = "would capture" if args.dry_run else "captured"
        print(f"{verb}: {result['path']} (## {result['date']})")
        print(result["entry"])
    return EXIT_OK


def _recall(args: argparse.Namespace) -> int:
    resolution = resolve_home(args.home)
    project, project_source, notes = _resolve_project(args.project, resolution)
    runtime = args.runtime or workflow.default_runtime()
    result = workflow.recall(
        resolution.path,
        project=project,
        runtime=runtime,
        max_chars=args.max_chars,
        home_source=resolution.source,
        project_source=project_source,
        notes=notes,
    )
    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        sys.stdout.write(result["text"])
    return EXIT_OK


def _startup(args: argparse.Namespace) -> int:
    resolution = resolve_home(args.home)
    project, project_source, notes = _resolve_project(args.project, resolution)
    manifest = startup.build_manifest(
        resolution.path,
        runtime=args.runtime,
        project=project,
        pull=args.pull,
        home_source=resolution.source,
        project_source=project_source,
        notes=notes,
    )
    if args.json_output:
        print(json.dumps(manifest, indent=2))
    elif args.runtime == startup.RUNTIME_CODEX:
        print(json.dumps(startup.render_codex_hook(manifest, max_chars=args.max_chars)))
    else:
        sys.stdout.write(startup.render_text(manifest, max_chars=args.max_chars))
    return EXIT_OK


def _debrief_write(args: argparse.Namespace) -> int:
    home = resolve_home(args.home).path
    try:
        if args.body_file == "-":
            body = sys.stdin.buffer.read()
        else:
            body = Path(args.body_file).expanduser().read_bytes()
    except OSError as exc:
        print(f"ERROR: cannot read body: {exc}", file=sys.stderr)
        return EXIT_FAILED

    try:
        result = debrief.write_debrief(
            home=home,
            project=args.project,
            agent=args.agent,
            runtime=args.runtime,
            session=args.session,
            started_utc=args.started_utc,
            ended_utc=args.ended_utc,
            body=body,
            dry_run=args.dry_run,
        )
    except debrief.WriterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"ERROR: publication failed: {exc}", file=sys.stderr)
        return 2

    if args.json_output:
        print(
            json.dumps(
                {
                    "path": str(result.path),
                    "status": result.status,
                    "content_sha256": result.content_sha256,
                    "schema_version": debrief.SCHEMA_VERSION,
                },
                indent=2,
            )
        )
    else:
        print(f"{result.status}: {result.path}")
        print(f"content_sha256: {result.content_sha256}")

    if args.dry_run:
        print("--- record preview (nothing was written) ---", file=sys.stderr)
        sys.stderr.flush()
        sys.stderr.buffer.write(result.record)
        sys.stderr.buffer.flush()
    return EXIT_OK


def _event_write(args: argparse.Namespace) -> int:
    home = resolve_home(args.home).path
    try:
        body = _event_body(args)
    except OSError as exc:
        print(f"ERROR: cannot read body: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except events.WriterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code

    try:
        result = events.write_event(
            home=home,
            agent=args.agent,
            project=args.project,
            event_type=args.event_type,
            slug=args.slug,
            body=body,
            source_ref=args.source_ref,
            related=events.normalize_related(args.related) if args.related else None,
            supersedes=args.supersedes,
            dry_run=args.dry_run,
        )
    except events.WriterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"ERROR: publication failed: {exc}", file=sys.stderr)
        return 2

    if args.json_output:
        print(
            json.dumps(
                {
                    "path": str(result.path),
                    "status": result.status,
                    "created_at_utc": result.created_at_utc,
                    "date": result.created_at_utc[:10],
                    "agent": args.agent,
                    "project": args.project,
                    "type": args.event_type,
                },
                indent=2,
            )
        )
    else:
        print(f"{result.status}: {result.path}")
        print(f"created_at_utc: {result.created_at_utc}")

    if args.dry_run:
        print("--- record preview (nothing was written) ---", file=sys.stderr)
        sys.stderr.flush()
        sys.stderr.buffer.write(result.record)
        sys.stderr.buffer.flush()
    return EXIT_OK


def _event_body(args: argparse.Namespace) -> bytes:
    """The body bytes: ``--body-file`` (a path, or ``-`` for stdin), ``--body``, else piped stdin.

    Trailing newlines are dropped, as the kernel script drops them; the record
    closes the body with exactly one. A body file is read as text, so its
    CRLF and CR line endings become LF (:func:`events.body_from_file`); stdin
    and the inline body are taken as given, both as the script does.
    """
    if args.body_file is not None and args.body_file != "-":
        return events.body_from_file(Path(args.body_file).expanduser().read_bytes())
    if args.body_file is not None:
        raw = sys.stdin.buffer.read()
    elif args.body is not None:
        raw = args.body.encode("utf-8")
    elif not sys.stdin.isatty():
        raw = sys.stdin.buffer.read()
    else:
        raise events.WriterError("no body provided: use --body, --body-file <path|->, or pipe the body to stdin", 1)
    return raw.rstrip(b"\n")


def _report(outcome: sync.Outcome) -> int:
    if outcome.lines:
        print("\n".join(outcome.lines), file=sys.stdout if outcome.ok else sys.stderr)
    return EXIT_OK if outcome.ok else EXIT_FAILED


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        return handler(args)
    except HomeError as exc:
        print(f"agent-memory: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (sync.SyncError, archive.ArchiveError, org.ScaffoldError, setup.SetupError, workflow.WorkflowError) as exc:
        print(f"agent-memory: error: {exc}", file=sys.stderr)
        return EXIT_FAILED
