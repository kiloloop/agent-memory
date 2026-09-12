# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Publish one org-memory event: ``agent-memory event write``.

The mechanical event writer of the memory layout spec (the kernel's
``docs/protocol/org_memory.md`` -> "Events"), ported from the kernel's
``scripts/write_event.py``. For the same inputs and the same clock the record
is byte-identical to the script's; what the port adds is the writer contract
of :mod:`agent_memory.publication`: the record is staged in a private file,
verified through the descriptor that wrote it, published with an atomic
no-replace link and read back, so the canonical path only ever holds a
complete record. The verb appends one event and reads nothing: synthesis
never enters here.

Canonical path::

    <home>/org-memory/events/<YYYYMMDD>-<HHMMSS>-<slug>.md

Exit codes of the verb:
    0  published (or idempotent re-publish of a byte-identical record)
    1  usage / validation error
    2  publication failure (collision, read-back mismatch, hostile target)
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import layout
from .debrief import AGENT_RE, CONTROL_CHARS_RE, split_record, valid_project_segment
from .publication import WriterError, publish

ALLOWED_TYPES = ("decision", "event", "rule")

#: Lowercase alphanumerics and hyphens, 1-64 characters, alphanumeric at both
#: ends. No dots: a version segment is spelled ``0-2-3``, not ``v0.2.3``.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")

EVENTS_DIR = "events"
FRONTMATTER_DELIM = b"---\n"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: The frontmatter, in the order the kernel script emits it: the five fields
#: every event carries, then the optional provenance fields that are present.
REQUIRED_FRONTMATTER_ORDER = ("created_at_utc", "date", "agent", "project", "type")
OPTIONAL_FRONTMATTER_ORDER = ("source_ref", "related", "supersedes")

STATUS_DRY_RUN = "dry-run"

# The frontmatter is emitted as plain (unquoted) YAML scalars, which is the
# byte contract with the kernel script. A plain scalar cannot carry an
# arbitrary string: a reader hands some values back re-typed (``true`` a
# boolean, ``12`` an integer, ``2026-03-21`` a date), reads ``: `` and `` #``
# as structure, and refuses a leading indicator outright. The writer cannot
# quote without breaking the contract, so it refuses what it cannot carry.
_PLAIN_INDICATORS = "-?:,[]{}#&*!|>'\"%@`"
_RETYPED_KEYWORD_RE = re.compile(r"^(?:true|false|yes|no|on|off|y|n|null|~)$", re.IGNORECASE)
_RETYPED_NUMBER_RE = re.compile(
    r"^[-+]?(?:"
    r"[0-9_]+(?:\.[0-9_]*)?(?:[eE][-+]?[0-9]+)?"  # 12, 1.0, 1e3, 1_000
    r"|\.[0-9_]+(?:[eE][-+]?[0-9]+)?"  # .5
    r"|0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+"  # 0x1f, 0o7, 0b1
    r"|[0-9]+(?::[0-5]?[0-9])+(?:\.[0-9_]*)?"  # 1:30 (YAML 1.1 sexagesimal)
    r"|\.(?:inf|nan)"
    r")$",
    re.IGNORECASE,
)
_RETYPED_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt ].*)?$")

#: An upper bound on the provenance scalars, so a record stays readable.
MAX_SCALAR_CHARS = 200


class EventResult(NamedTuple):
    """Outcome of one event write."""

    path: Path
    status: str
    created_at_utc: str
    record: bytes


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def normalize_related(raw: str) -> List[str]:
    """Parse a ``--related`` value into a list of strings.

    Accepts a comma-separated string (``PR #43, PR #44``) or a pre-encoded
    JSON array (``["PR #43", "PR #44"]``); a value that starts with ``[`` but
    is not JSON falls back to the comma split. Items are stripped and empty
    ones dropped. Ported verbatim from the kernel script.
    """
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return [str(item).strip() for item in parsed if str(item).strip()]
        except (json.JSONDecodeError, TypeError):
            pass  # fall through to the comma split
    return [item.strip() for item in raw.split(",") if item.strip()]


def validate_slug(slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise WriterError(
            f"invalid slug {slug!r}: lowercase alphanumerics and hyphens, 1-64 characters, "
            "alphanumeric at both ends (no dots: spell v0.2.3 as 0-2-3)",
            1,
        )


def validate_type(event_type: str) -> None:
    if event_type not in ALLOWED_TYPES:
        raise WriterError(f"invalid type {event_type!r}: must be one of {', '.join(ALLOWED_TYPES)}", 1)


def validate_plain_scalar(label: str, value: str) -> None:
    """Refuse a value the plain-scalar frontmatter cannot carry verbatim."""
    if not value or value != value.strip():
        raise WriterError(f"{label} must be a non-empty string with no leading or trailing whitespace: {value!r}", 1)
    if CONTROL_CHARS_RE.search(value):
        raise WriterError(f"{label} must not contain control characters: {value!r}", 1)
    if len(value) > MAX_SCALAR_CHARS:
        raise WriterError(f"{label} is longer than {MAX_SCALAR_CHARS} characters", 1)
    if value[0] in _PLAIN_INDICATORS or ": " in value or " #" in value or value.endswith(":"):
        raise WriterError(
            f"{label} is not representable as a plain YAML scalar (leading indicator, ': ', ' #' or trailing ':'): "
            f"{value!r}",
            1,
        )
    if _RETYPED_KEYWORD_RE.match(value) or _RETYPED_NUMBER_RE.match(value) or _RETYPED_TIMESTAMP_RE.match(value):
        raise WriterError(
            f"{label} would be read back as a boolean, null, number or date rather than a string: {value!r}", 1
        )


def validate_identity(agent: str, project: str) -> None:
    if not AGENT_RE.match(agent):
        raise WriterError(f"agent {agent!r} does not match the protocol agent-name rule {AGENT_RE.pattern}", 1)
    validate_plain_scalar("agent", agent)
    if not valid_project_segment(project):
        raise WriterError(
            f"project {project!r} is not a valid workspace name (must not start with '.' or contain '/' or '\\')",
            1,
        )
    validate_plain_scalar("project", project)


def validate_related_item(item: str) -> None:
    """A related item is emitted inside double quotes with no escaping."""
    if not item or item != item.strip():
        raise WriterError(f"related item must be a non-empty trimmed string: {item!r}", 1)
    if CONTROL_CHARS_RE.search(item) or '"' in item or "\\" in item:
        raise WriterError(f"related item must not contain control characters, double quotes or backslashes: {item!r}", 1)
    if len(item) > MAX_SCALAR_CHARS:
        raise WriterError(f"related item is longer than {MAX_SCALAR_CHARS} characters", 1)


def validate_body(body: bytes) -> None:
    """The record is a Markdown file, so the body must be non-empty, valid UTF-8."""
    if not body:
        raise WriterError("refusing to publish an event with an empty body", 1)
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WriterError(f"event body is not valid UTF-8 at byte {exc.start}: {exc.reason}", 1) from exc


def body_from_file(raw: bytes) -> bytes:
    """The body a body *file* contributes: read as text, the way the kernel script reads it.

    The script opens the file in text mode, so universal newlines apply --
    ``\r\n`` and a lone ``\r`` both become ``\n`` -- and then trailing
    newlines are dropped. A Windows-authored body file therefore produces the
    same record bytes as its LF twin. Stdin and ``--body`` are not files: the
    script takes those as given (trailing ``\n`` dropped, ``\r`` kept), and so
    does the verb. A file that is not UTF-8 is refused here, before anything
    is composed.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WriterError(f"event body is not valid UTF-8 at byte {exc.start}: {exc.reason}", 1) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n").encode("utf-8")


def _as_utc(now: Optional[dt.datetime]) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


# --------------------------------------------------------------------------
# record composition
# --------------------------------------------------------------------------


def related_scalar(related: Sequence[str]) -> str:
    """``["a", "b"]``: the JSON-array shape the kernel script emits, unescaped."""
    return "[" + ", ".join(f'"{item}"' for item in related) + "]"


def frontmatter_fields(
    *,
    agent: str,
    project: str,
    event_type: str,
    created_at: dt.datetime,
    source_ref: Optional[str] = None,
    related: Optional[Sequence[str]] = None,
    supersedes: Optional[str] = None,
) -> Dict[str, str]:
    """The frontmatter mapping, in emission order, every value already a scalar string."""
    fields: Dict[str, str] = {
        "created_at_utc": created_at.strftime(TIMESTAMP_FORMAT),
        "date": created_at.strftime("%Y-%m-%d"),
        "agent": agent,
        "project": project,
        "type": event_type,
    }
    if source_ref:
        fields["source_ref"] = source_ref
    if related:
        fields["related"] = related_scalar(related)
    if supersedes:
        fields["supersedes"] = supersedes
    return fields


def compose_record(
    *,
    agent: str,
    project: str,
    event_type: str,
    created_at: dt.datetime,
    body: bytes,
    source_ref: Optional[str] = None,
    related: Optional[Sequence[str]] = None,
    supersedes: Optional[str] = None,
) -> bytes:
    """Build the full record: plain-scalar frontmatter, a blank line, the body, a newline.

    Byte-identical to the kernel script's ``build_event`` for the same inputs.
    """
    fields = frontmatter_fields(
        agent=agent,
        project=project,
        event_type=event_type,
        created_at=created_at,
        source_ref=source_ref,
        related=related,
        supersedes=supersedes,
    )
    lines = [f"{key}: {value}" for key, value in fields.items()]
    head = FRONTMATTER_DELIM + ("\n".join(lines) + "\n").encode("utf-8") + FRONTMATTER_DELIM
    record = head + b"\n" + body + b"\n"
    _assert_record_roundtrips(record, fields, body)
    return record


def _assert_record_roundtrips(record: bytes, fields: Dict[str, str], body: bytes) -> None:
    """Re-parse the composed record and assert it says what it was asked to say.

    Composition is the one step that can silently change a record's identity:
    the read-back after publication compares the file against these same
    bytes, so the writer closes the loop itself, before the store is touched.
    """
    parsed, parsed_body = parse_record(record)
    if list(parsed) != list(fields):
        raise WriterError(f"composed frontmatter does not carry exactly the composed fields in order: {list(parsed)}")
    for key, want in fields.items():
        if parsed[key] != want:
            raise WriterError(f"composed frontmatter field {key!r} did not round-trip: {parsed[key]!r} != {want!r}")
    if parsed_body != body:
        raise WriterError("composed record body did not round-trip byte-for-byte")


def parse_record(raw: bytes) -> Tuple[Dict[str, str], bytes]:
    """Split a stored event into (frontmatter mapping, body bytes) and check its shape.

    The body is what the writer was handed: the bytes between the blank line
    that follows the frontmatter and the newline the record ends with. The
    frontmatter must carry the required fields first, in order, then any
    optional fields in their order, and every value must read back as the
    string that was written -- the check the verifier runs over the staged
    bytes and over the read-back.
    """
    frontmatter, rest = split_record(raw)
    keys = list(frontmatter)
    if keys[: len(REQUIRED_FRONTMATTER_ORDER)] != list(REQUIRED_FRONTMATTER_ORDER):
        raise WriterError(f"event frontmatter does not open with the required fields in order: {keys}")
    optional = keys[len(REQUIRED_FRONTMATTER_ORDER) :]
    expected_optional = [key for key in OPTIONAL_FRONTMATTER_ORDER if key in optional]
    if optional != expected_optional or len(set(optional)) != len(optional):
        raise WriterError(f"event frontmatter carries unknown, duplicate or misordered optional fields: {optional}")

    try:
        created = dt.datetime.strptime(frontmatter["created_at_utc"], TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise WriterError(f"created_at_utc is not a UTC timestamp: {frontmatter['created_at_utc']!r}") from exc
    if frontmatter["date"] != created.strftime("%Y-%m-%d"):
        raise WriterError(f"date {frontmatter['date']!r} does not match created_at_utc {frontmatter['created_at_utc']!r}")
    try:
        validate_identity(frontmatter["agent"], frontmatter["project"])
        validate_type(frontmatter["type"])
        if "source_ref" in frontmatter:
            validate_plain_scalar("source_ref", frontmatter["source_ref"])
        if "supersedes" in frontmatter:
            validate_plain_scalar("supersedes", frontmatter["supersedes"])
        if "related" in frontmatter:
            items = _parse_related_scalar(frontmatter["related"])
            for item in items:
                validate_related_item(item)
    except WriterError as exc:
        raise WriterError(f"event frontmatter does not read back as written: {exc}") from exc

    if not rest.startswith(b"\n") or not rest.endswith(b"\n") or len(rest) < 3:
        raise WriterError("event body is not a blank line, the body and a closing newline")
    return frontmatter, rest[1:-1]


def _parse_related_scalar(value: str) -> List[str]:
    try:
        items = json.loads(value)
    except json.JSONDecodeError as exc:
        raise WriterError(f"related is not a JSON array of strings: {value!r}") from exc
    if not isinstance(items, list) or not items or not all(isinstance(item, str) for item in items):
        raise WriterError(f"related is not a non-empty JSON array of strings: {value!r}")
    if related_scalar(items) != value:
        raise WriterError(f"related does not re-serialize to the bytes written: {value!r}")
    return items


def verify_record(raw: bytes) -> None:
    """The consistency check publication runs over staged and read-back bytes."""
    parse_record(raw)


def canonical_name(created_at: dt.datetime, slug: str) -> str:
    return f"{created_at.strftime('%Y%m%d-%H%M%S')}-{slug}.md"


def canonical_path(home: Path, created_at: dt.datetime, slug: str) -> Path:
    return layout.org_memory_dir(home) / EVENTS_DIR / canonical_name(created_at, slug)


# --------------------------------------------------------------------------
# the verb
# --------------------------------------------------------------------------


def write_event(
    *,
    home: Path,
    agent: str,
    project: str,
    event_type: str,
    slug: str,
    body: bytes,
    source_ref: Optional[str] = None,
    related: Optional[Sequence[str]] = None,
    supersedes: Optional[str] = None,
    now: Optional[dt.datetime] = None,
    dry_run: bool = False,
) -> EventResult:
    """Validate, compose and publish one event.

    ``now`` names the clock the record is stamped and named with; it defaults
    to the current UTC time. With ``dry_run`` the record is validated and
    composed exactly as it would be published, and the store is not touched --
    no directories created, no files written. The status is then ``dry-run``.
    """
    validate_slug(slug)
    validate_type(event_type)
    validate_identity(agent, project)
    if source_ref:
        validate_plain_scalar("source_ref", source_ref)
    if supersedes:
        validate_plain_scalar("supersedes", supersedes)
    for item in related or ():
        validate_related_item(item)
    validate_body(body)

    created_at = _as_utc(now)
    record = compose_record(
        agent=agent,
        project=project,
        event_type=event_type,
        created_at=created_at,
        body=body,
        source_ref=source_ref,
        related=related,
        supersedes=supersedes,
    )
    target = canonical_path(home, created_at, slug)
    created_at_utc = created_at.strftime(TIMESTAMP_FORMAT)
    if dry_run:
        return EventResult(target, STATUS_DRY_RUN, created_at_utc, record)
    status = publish(target, record, verify=verify_record)
    return EventResult(target, status, created_at_utc, record)
