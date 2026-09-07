# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Publish a session debrief into the debrief store: ``agent-memory debrief write``.

Implements the writer contract of the memory layout spec (the kernel's
``docs/protocol/org_memory.md`` -> "Debrief Store"). The layout and schema are
the spec's; everything here -- schema completeness, the content hash, and
failure-atomic publication through :mod:`agent_memory.publication` -- is the
writer's responsibility.

Canonical path::

    <home>/org-memory/debriefs/<project>/<YYYY>/<MM>/<YYYYMMDD>-<agent>-<session>.md

Exit codes of the verb:
    0  published (or idempotent re-publish of a byte-identical record)
    1  usage / validation error
    2  publication failure (collision, read-back mismatch, hostile target)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Dict, NamedTuple, Tuple

from . import layout
from .publication import STAGE_PREFIX, WriterError, publish, staging_path  # noqa: F401  (re-exported)

SCHEMA_VERSION = 1

# Mirrors the protocol's canonical agent-name rule; hyphens, dots,
# underscores and mixed case are all representable in the agent segment.
AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# The session identifier is the substring after the FINAL hyphen, so it must
# never contain one -- that is what keeps the three-part filename uniquely
# parseable for any valid agent name.
SESSION_RE = re.compile(r"^[a-z0-9]{1,32}$")

FRONTMATTER_DELIM = b"---\n"

REQUIRED_FRONTMATTER_ORDER = (
    "schema_version",
    "project",
    "agent",
    "runtime",
    "session",
    "started_utc",
    "ended_utc",
    "content_sha256",
    "immutable",
)

# A control character in any identity field would break out of the frontmatter
# block it is serialized into and corrupt the path segment it names, so every
# identity value is screened for them before composition.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# Runtime family names follow the same shape as agent names.
RUNTIME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

STATUS_DRY_RUN = "dry-run"


class DebriefResult(NamedTuple):
    """Outcome of one debrief write."""

    path: Path
    status: str
    content_sha256: str
    record: bytes


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def valid_project_segment(name: str) -> bool:
    """Workspace project-name rule: no leading dot, no path separators.

    Control characters are rejected on top of the protocol rule: they cannot
    appear in a usable path segment, and a newline would inject extra lines
    into the frontmatter block the name is serialized into.
    """
    return (
        bool(name)
        and not name.startswith(".")
        and "/" not in name
        and "\\" not in name
        and not CONTROL_CHARS_RE.search(name)
    )


def parse_utc(label: str, value: str) -> dt.datetime:
    """Parse an ISO 8601 UTC timestamp that ends in ``Z``."""
    if not value.endswith("Z"):
        raise WriterError(f"{label} must be ISO 8601 UTC ending in 'Z': {value!r}", 1)
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise WriterError(f"{label} is not a valid UTC timestamp: {value!r} ({exc})", 1) from exc
    return parsed.replace(tzinfo=dt.timezone.utc)


def validate_identity(project: str, agent: str, runtime: str, session: str) -> None:
    if not valid_project_segment(project):
        raise WriterError(
            f"project {project!r} is not a valid workspace name (must not start with '.' or contain '/' or '\\')",
            1,
        )
    if not AGENT_RE.match(agent):
        raise WriterError(f"agent {agent!r} does not match the protocol agent-name rule {AGENT_RE.pattern}", 1)
    if not SESSION_RE.match(session):
        raise WriterError(
            f"session {session!r} must be 1-32 lowercase letters/digits with no "
            "hyphens (the identifier is parsed as the substring after the final hyphen)",
            1,
        )
    if not RUNTIME_RE.match(runtime):
        raise WriterError(f"runtime {runtime!r} must match {RUNTIME_RE.pattern}", 1)


def validate_body(body: bytes) -> None:
    """The record is a Markdown file, so the body must be valid UTF-8.

    Checked before the store is touched: a record whose body cannot be decoded
    is unreadable to every consumer, and the post-publication read-back cannot
    catch it because it compares the file against the same bytes that composed
    it.
    """
    if not body:
        raise WriterError("refusing to publish a debrief with an empty body", 1)
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WriterError(f"debrief body is not valid UTF-8 at byte {exc.start}: {exc.reason}", 1) from exc


# --------------------------------------------------------------------------
# record composition
# --------------------------------------------------------------------------


def content_sha256(body: bytes) -> str:
    """Lowercase-hex SHA-256 over the exact body bytes -- no normalization."""
    return hashlib.sha256(body).hexdigest()


def _yaml_scalar(value: object) -> str:
    """Serialize one frontmatter value.

    ``schema_version`` is an integer and ``immutable`` a boolean; every other
    field is a string, and strings are emitted in YAML single-quoted style
    unconditionally. Conditional quoting is not safe here: identifiers the
    protocol grammar accepts -- ``true``, ``null``, ``no``, ``on``, ``y`` --
    are plain-scalar keywords a YAML reader re-types, silently changing the
    record's identity, and leading indicators such as ``*`` or ``&`` produce a
    record no parser will read at all. Single-quoted style preserves any
    control-character-free string exactly, escaping an embedded quote by
    doubling it.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if not text or text != text.strip():
        raise WriterError(f"refusing to emit an untrimmed/empty frontmatter scalar: {text!r}")
    if CONTROL_CHARS_RE.search(text):
        # Defense in depth: identity fields are screened before composition, so
        # reaching here means a caller bypassed validation.
        raise WriterError(f"refusing to emit a frontmatter scalar with control characters: {text!r}")
    return "'" + text.replace("'", "''") + "'"


def compose_record(
    *,
    project: str,
    agent: str,
    runtime: str,
    session: str,
    started_utc: str,
    ended_utc: str,
    body: bytes,
) -> bytes:
    """Build the full record: frontmatter block + verbatim body bytes."""
    fields: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "agent": agent,
        "runtime": runtime,
        "session": session,
        "started_utc": started_utc,
        "ended_utc": ended_utc,
        "content_sha256": content_sha256(body),
        "immutable": True,
    }
    lines = [f"{key}: {_yaml_scalar(fields[key])}" for key in REQUIRED_FRONTMATTER_ORDER]
    head = FRONTMATTER_DELIM + ("\n".join(lines) + "\n").encode("utf-8") + FRONTMATTER_DELIM
    record = head + body
    _assert_record_roundtrips(record, fields, body)
    return record


def _assert_record_roundtrips(record: bytes, fields: Dict[str, object], body: bytes) -> None:
    """Re-parse the composed record and assert it says what it was asked to say.

    Composition is the one step that can silently change a record's identity,
    and nothing downstream can catch it: the doctor never opens debrief files,
    and the post-publication read-back compares the stored file against these
    same composed bytes. So the writer closes the loop itself, here, before the
    store is touched.
    """
    parsed, parsed_body = split_record(record)
    if list(parsed) != list(REQUIRED_FRONTMATTER_ORDER):
        raise WriterError(
            f"composed frontmatter does not carry exactly the required fields in canonical order: {list(parsed)}"
        )
    for key, expected in fields.items():
        if isinstance(expected, bool):
            want = "true" if expected else "false"
        elif isinstance(expected, int):
            want = str(expected)
        else:
            want = str(expected)
        if parsed[key] != want:
            raise WriterError(f"composed frontmatter field {key!r} did not round-trip: {parsed[key]!r} != {want!r}")
    if parsed_body != body:
        raise WriterError("composed record body did not round-trip byte-for-byte")


def split_record(raw: bytes) -> Tuple[Dict[str, str], bytes]:
    """Split a stored record into (frontmatter mapping, body bytes).

    The body is every byte after the line that closes the frontmatter block --
    the second ``---`` line including its trailing newline -- exactly as
    stored. This is the definition the ``content_sha256`` field is computed
    over, so it must not normalize anything.
    """
    if not raw.startswith(FRONTMATTER_DELIM):
        raise WriterError("record does not begin with a '---' frontmatter delimiter")
    rest = raw[len(FRONTMATTER_DELIM) :]
    end = rest.find(b"\n" + FRONTMATTER_DELIM)
    if end == -1:
        raise WriterError("record frontmatter block is not closed by a '---' line")
    head = rest[:end]
    body = rest[end + 1 + len(FRONTMATTER_DELIM) :]

    frontmatter: Dict[str, str] = {}
    for line in head.decode("utf-8").splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            quote = value[0]
            value = value[1:-1]
            if quote == "'":
                # Undo YAML single-quoted escaping.
                value = value.replace("''", "'")
        frontmatter[key.strip()] = value
    return frontmatter, body


def verify_record(raw: bytes) -> None:
    """The consistency check publication runs over staged and read-back bytes:
    the body must hash to the ``content_sha256`` the frontmatter declares."""
    frontmatter, body = split_record(raw)
    if content_sha256(body) != frontmatter.get("content_sha256"):
        raise WriterError("body does not match content_sha256")


def canonical_name(started: dt.datetime, agent: str, session: str) -> str:
    return f"{started.strftime('%Y%m%d')}-{agent}-{session}.md"


def canonical_path(home: Path, project: str, started: dt.datetime, agent: str, session: str) -> Path:
    return (
        layout.org_memory_dir(home)
        / "debriefs"
        / project
        / started.strftime("%Y")
        / started.strftime("%m")
        / canonical_name(started, agent, session)
    )


# --------------------------------------------------------------------------
# the verb
# --------------------------------------------------------------------------


def write_debrief(
    *,
    home: Path,
    project: str,
    agent: str,
    runtime: str,
    session: str,
    started_utc: str,
    ended_utc: str,
    body: bytes,
    dry_run: bool = False,
) -> DebriefResult:
    """Validate, compose and publish one debrief.

    With ``dry_run`` the record is validated and composed exactly as it would
    be published, and the store is not touched -- no directories created, no
    files written. The status is then ``dry-run``.
    """
    validate_identity(project, agent, runtime, session)
    started = parse_utc("started_utc", started_utc)
    ended = parse_utc("ended_utc", ended_utc)
    if ended < started:
        raise WriterError(f"ended_utc ({ended_utc}) is before started_utc ({started_utc})", 1)
    validate_body(body)

    record = compose_record(
        project=project,
        agent=agent,
        runtime=runtime,
        session=session,
        started_utc=started_utc,
        ended_utc=ended_utc,
        body=body,
    )
    target = canonical_path(home, project, started, agent, session)
    digest = content_sha256(body)
    if dry_run:
        return DebriefResult(target, STATUS_DRY_RUN, digest, record)
    status = publish(target, record, verify=verify_record)
    return DebriefResult(target, status, digest, record)
